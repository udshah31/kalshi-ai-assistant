"""Create verified, lock-consistent state/SQLite snapshots for backup or migration."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from btc_predictor import load_state, state_transaction
from forecast_archive import SCHEMA_VERSION, archive_path

BACKUP_NAME = re.compile(r"btc15m-backup-\d{8}T\d{12}Z-[0-9a-f]{8}")


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _write(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def verify_backup(directory: str | Path) -> dict:
    """Verify checksums, model compatibility, run identity, and archive integrity."""
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("kind") != "btc15m_backup" or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported backup manifest")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"state", "archive"}:
        raise ValueError("Incomplete backup manifest")
    paths = {}
    for role, entry in files.items():
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or name in {"", ".", ".."} or Path(name).name != name:
            raise ValueError("Invalid backup filename")
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Backup evidence file missing or symlinked")
        if _digest(path) != entry.get("sha256"):
            raise ValueError("Backup checksum mismatch")
        paths[role] = path
    if paths["archive"].name != archive_path(paths["state"]).name:
        raise ValueError("Backup archive does not match the state filename")
    _, state = load_state(paths["state"])
    if state.get("state_id") != manifest.get("state_id"):
        raise ValueError("Backup state identity mismatch")
    with closing(sqlite3.connect(paths["archive"].resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Backup archive integrity check failed")
        version = connection.execute("SELECT value FROM archive_metadata WHERE key='schema_version'").fetchone()
        if version != (str(SCHEMA_VERSION),):
            raise ValueError("Unsupported backup archive schema")
    return manifest


def _prune(directory: Path, source: Path, keep: int) -> None:
    """Prune only snapshots owned by this tool for this exact source state path."""
    owned = []
    for entry in directory.iterdir():
        if entry.is_symlink() or not entry.is_dir() or not BACKUP_NAME.fullmatch(entry.name):
            continue
        try:
            manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
            if (isinstance(manifest, dict) and manifest.get("kind") == "btc15m_backup"
                    and manifest.get("schema_version") == 1 and manifest.get("source_state_file") == str(source)):
                owned.append(entry)
        except (OSError, ValueError):
            continue
    for entry in sorted(owned)[:-keep]:
        shutil.rmtree(entry)


def create_backup(state_file: str | Path, backup_dir: str | Path, keep: int = 7) -> Path:
    """Freeze a JSON/SQLite pair under the live writer lock; include WAL evidence."""
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("keep must be a positive integer")
    state_file = Path(state_file).resolve()
    archive = archive_path(state_file)
    for source in (state_file, archive):
        if not source.is_file():
            raise FileNotFoundError(f"Required evidence file missing: {source}")
    backup_dir = Path(backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Multiple manual/timer backups must not race publication or retention.
    with (backup_dir / ".backup.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        staging = Path(tempfile.mkdtemp(prefix=".btc15m-backup-", dir=backup_dir))
        try:
            with state_transaction(state_file):
                _, state = load_state(state_file)
                _write(staging / state_file.name, state_file.read_bytes())
                copied_archive = staging / archive.name
                with closing(sqlite3.connect(archive.as_uri() + "?mode=ro", uri=True)) as source:
                    with closing(sqlite3.connect(copied_archive)) as target:
                        source.backup(target)
                        target.execute("PRAGMA journal_mode=DELETE")
                os.chmod(copied_archive, 0o600)
                with copied_archive.open("rb") as handle:
                    os.fsync(handle.fileno())
                created = datetime.now(timezone.utc)
                manifest = {
                    "schema_version": 1, "kind": "btc15m_backup",
                    "created_at": created.isoformat(), "state_id": state.get("state_id"),
                    "source_state_file": str(state_file),
                    "files": {
                        "state": {"name": state_file.name, "sha256": _digest(staging / state_file.name)},
                        "archive": {"name": archive.name, "sha256": _digest(copied_archive)},
                    },
                }
                _write(staging / "manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
            verify_backup(staging)
            destination = backup_dir / ("btc15m-backup-" + created.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8])
            descriptor = os.open(staging, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.rename(staging, destination)
            descriptor = os.open(backup_dir, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _prune(backup_dir, state_file, keep)
            return destination
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def export_backups(backup_dir: str | Path, stream: BinaryIO) -> None:
    """Stream verified published snapshots while preventing retention/publication races."""
    backup_dir = Path(backup_dir).resolve()
    if not backup_dir.is_dir():
        raise FileNotFoundError(f"Backup directory missing: {backup_dir}")
    with (backup_dir / ".backup.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        directories = sorted(entry for entry in backup_dir.iterdir()
                             if entry.is_dir() and not entry.is_symlink() and BACKUP_NAME.fullmatch(entry.name))
        if not directories:
            raise ValueError("No published snapshots to export")
        snapshots = [(directory, verify_backup(directory)) for directory in directories]
        with tarfile.open(fileobj=stream, mode="w|gz") as bundle:
            for directory, manifest in snapshots:
                bundle.add(directory, arcname=directory.name, recursive=False)
                names = ["manifest.json", *(entry["name"] for entry in manifest["files"].values())]
                for name in names:
                    bundle.add(directory / name, arcname=directory.name + "/" + name, recursive=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create a consistent snapshot; retain seven by default")
    create.add_argument("--state-file", type=Path, default=Path("data/btc_15m_state.json"))
    create.add_argument("--backup-dir", type=Path, required=True)
    create.add_argument("--keep", type=int, default=7)
    verify = commands.add_parser("verify", help="Check a snapshot before migration or restore")
    verify.add_argument("directory", type=Path)
    export = commands.add_parser("export", help="Stream published snapshots as a tar.gz to stdout")
    export.add_argument("--backup-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            directory = create_backup(args.state_file, args.backup_dir, args.keep)
        elif args.command == "verify":
            directory = args.directory
            verify_backup(directory)
        else:
            export_backups(args.backup_dir, sys.stdout.buffer)
            return 0
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"backup: {exc}\n")
    print(json.dumps({"status": "ok", "backup_dir": str(directory.resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

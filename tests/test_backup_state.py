"""Verify migration snapshots preserve real state and uncheckpointed archive data."""
import fcntl
import io
import json
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import backup_state
from btc_predictor import OnlineLogisticRegression, new_state, state_transaction
from forecast_archive import record_cycle


class BackupStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state_file = self.root / "btc_15m_state.json"
        self.archive = self.root / "btc_15m_state_archive.sqlite3"
        self.backups = self.root / "backups"
        self.state = new_state(OnlineLogisticRegression(7))
        self.state["typesafe_required"] = True
        record_cycle(self.archive, self.state, now=1800000060)
        self.state_file.write_text(json.dumps(self.state, indent=2) + "\n")

    def test_snapshot_preserves_state_bytes_and_uncheckpointed_wal_rows(self):
        before = self.state_file.read_bytes()
        with closing(sqlite3.connect(self.archive)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("INSERT INTO archive_metadata VALUES ('wal_probe','durable evidence')")
            connection.commit()
            self.assertTrue(Path(str(self.archive) + "-wal").exists())
            destination = backup_state.create_backup(self.state_file, self.backups)
            manifest = backup_state.verify_backup(destination)
            copied = destination / self.archive.name
            with closing(sqlite3.connect(copied)) as snapshot:
                self.assertEqual(snapshot.execute("SELECT value FROM archive_metadata WHERE key='wal_probe'").fetchone(),
                                 ("durable evidence",))
            self.assertEqual((destination / self.state_file.name).read_bytes(), before)
            self.assertEqual(self.state_file.read_bytes(), before)
            self.assertEqual(manifest["state_id"], self.state["state_id"])
            self.assertTrue(json.loads((destination / self.state_file.name).read_text())["typesafe_required"])
            self.assertFalse(Path(str(copied) + "-wal").exists())

    def test_missing_archive_is_not_created_or_replaced_by_an_empty_snapshot(self):
        self.archive.unlink()
        with self.assertRaises(FileNotFoundError):
            backup_state.create_backup(self.state_file, self.backups)
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.backups.exists())

    def test_corrupt_archive_leaves_no_published_or_partial_backup(self):
        self.archive.write_bytes(b"not a SQLite database")
        with self.assertRaises((sqlite3.Error, ValueError)):
            backup_state.create_backup(self.state_file, self.backups)
        self.assertFalse(list(self.backups.glob("btc15m-backup-*")))
        self.assertFalse(list(self.backups.glob(".btc15m-backup-*")))

    def test_retention_preserves_other_files_and_other_state_backups(self):
        first = backup_state.create_backup(self.state_file, self.backups, keep=2)
        unrelated = self.backups / "btc15m-backup-personal"
        unrelated.mkdir()
        (unrelated / "notes.txt").write_text("keep this")
        other_state = self.root / "another_state.json"
        other_archive = self.root / "another_state_archive.sqlite3"
        other_state.write_bytes(self.state_file.read_bytes())
        with closing(sqlite3.connect(self.archive)) as source, closing(sqlite3.connect(other_archive)) as target:
            source.backup(target)
        other = backup_state.create_backup(other_state, self.backups, keep=2)
        second = backup_state.create_backup(self.state_file, self.backups, keep=2)
        third = backup_state.create_backup(self.state_file, self.backups, keep=2)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(third.exists())
        self.assertTrue(other.exists())
        self.assertEqual((unrelated / "notes.txt").read_text(), "keep this")

    def test_verification_rejects_changed_state_and_path_traversal(self):
        destination = backup_state.create_backup(self.state_file, self.backups)
        copied = destination / self.state_file.name
        before = copied.read_bytes()
        copied.write_bytes(before + b" ")
        with self.assertRaisesRegex(ValueError, "checksum"):
            backup_state.verify_backup(destination)
        copied.write_bytes(before)
        manifest_file = destination / "manifest.json"
        manifest = json.loads(manifest_file.read_text())
        manifest["files"]["state"]["name"] = "../btc_15m_state.json"
        manifest_file.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            backup_state.verify_backup(destination)

    def test_backup_waits_for_the_live_state_lock_before_reading_evidence(self):
        command = [sys.executable, str(Path(backup_state.__file__)), "create",
                   "--state-file", str(self.state_file), "--backup-dir", str(self.backups)]
        with state_transaction(self.state_file):
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            with self.assertRaises(subprocess.TimeoutExpired):
                process.communicate(timeout=.3)
            self.state["last_learning_status"] = "revision-after-lock"
            self.state_file.write_text(json.dumps(self.state))
            with closing(sqlite3.connect(self.archive)) as connection:
                connection.execute("INSERT INTO archive_metadata VALUES ('revision','after-lock')")
                connection.commit()
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, error)
        destination = Path(json.loads(output)["backup_dir"])
        copied_state = json.loads((destination / self.state_file.name).read_text())
        self.assertEqual(copied_state["last_learning_status"], "revision-after-lock")
        with closing(sqlite3.connect(destination / self.archive.name)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM archive_metadata WHERE key='revision'").fetchone(),
                             ("after-lock",))

    def test_export_contains_only_verified_published_evidence_and_can_be_restored(self):
        destination = backup_state.create_backup(self.state_file, self.backups)
        (destination / "personal-notes.txt").write_text("not snapshot evidence")
        staging = self.backups / ".btc15m-backup-incomplete"
        staging.mkdir()
        (staging / "partial.json").write_text("incomplete")
        stream = io.BytesIO()
        backup_state.export_backups(self.backups, stream)
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:gz") as bundle:
            names = set(bundle.getnames())
            self.assertEqual(names, {destination.name, destination.name + "/manifest.json",
                                     destination.name + "/btc_15m_state.json",
                                     destination.name + "/btc_15m_state_archive.sqlite3"})
            restore = self.root / "restored"
            bundle.extractall(restore, filter="data")
        backup_state.verify_backup(restore / destination.name)

    def test_export_waits_for_the_same_backup_lock_as_publication_and_retention(self):
        destination = backup_state.create_backup(self.state_file, self.backups)
        command = [sys.executable, str(Path(backup_state.__file__)), "export", "--backup-dir", str(self.backups)]
        with (self.backups / ".backup.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            with self.assertRaises(subprocess.TimeoutExpired):
                process.communicate(timeout=.3)
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, error.decode())
        with tarfile.open(fileobj=io.BytesIO(output), mode="r:gz") as bundle:
            self.assertIn(destination.name + "/manifest.json", bundle.getnames())


if __name__ == "__main__":
    unittest.main()

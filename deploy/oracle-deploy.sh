#!/usr/bin/env bash
# Replace deployed code on the Oracle Ubuntu VM without replacing live evidence.
set -Eeuo pipefail

TEST_MODE=${ORACLE_DEPLOY_TEST_MODE:-0}
if [[ "$TEST_MODE" == "1" ]]; then
    ROOT=${ORACLE_DEPLOY_ROOT:-}
    STATE_ROOT=${ORACLE_DEPLOY_STATE_ROOT:-}
    SYSTEMD_ROOT=${ORACLE_DEPLOY_SYSTEMD_ROOT:-}
    [[ -n "$ROOT" && -n "$STATE_ROOT" && -n "$SYSTEMD_ROOT" ]] || {
        printf 'oracle-deploy: test mode requires isolated path overrides\n' >&2
        exit 1
    }
    [[ "$ROOT" != "/opt/kalshi" && "$STATE_ROOT" != "/var/lib/kalshi" \
        && "$SYSTEMD_ROOT" != "/etc/systemd/system" ]] || {
        printf 'oracle-deploy: test mode cannot use production paths\n' >&2
        exit 1
    }
else
    ROOT=/opt/kalshi
    STATE_ROOT=/var/lib/kalshi
    SYSTEMD_ROOT=/etc/systemd/system
fi

STATE_FILE="$STATE_ROOT/btc_15m_state.json"
ARCHIVE_FILE="$STATE_ROOT/btc_15m_state_archive.sqlite3"
BACKUP_ROOT="$STATE_ROOT/backups"
DEPLOY_ROLLBACKS="$STATE_ROOT/deploy-rollbacks"

APP_SERVICES=(kalshi-runner.service kalshi-dashboard.service)
SCHEDULED_UNITS=(
    kalshi-backup.timer
    kalshi-backup.service
    kalshi-context-comparison.timer
    kalshi-context-comparison.service
)
ALL_UNITS=("${APP_SERVICES[@]}" "${SCHEDULED_UNITS[@]}")

usage() {
    cat <<'EOF'
Usage: sudo bash deploy/oracle-deploy.sh BUNDLE_PATH RELEASE_ID

Validate a code-only bundle, create a verified backup of the live evidence,
replace the deployed code/systemd units, restart the services, and verify the
dashboard health endpoint. A failed release restores the previous code tree.

The bundle must not contain live state, SQLite archives, or service secrets.
EOF
}

fail() {
    printf 'oracle-deploy: %s\n' "$1" >&2
    exit 1
}

if [[ "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

if [[ $# -ne 2 ]]; then
    usage >&2
    exit 2
fi

BUNDLE=$1
RELEASE_ID=$2

if [[ ! "$RELEASE_ID" =~ ^[A-Za-z0-9._-]+$ ]]; then
    fail "invalid release identifier: $RELEASE_ID"
fi
[[ -f "$BUNDLE" ]] || fail "bundle does not exist: $BUNDLE"
if [[ "$TEST_MODE" != "1" ]]; then
    [[ "$(uname -s)" == "Linux" ]] || fail "this deployment helper requires Linux"
    [[ $EUID -eq 0 ]] || fail "run as root, normally through sudo"
fi

for command in python3 tar systemctl curl install find; do
    command -v "$command" >/dev/null || fail "missing required command: $command"
done
if [[ "$TEST_MODE" != "1" ]]; then
    command -v flock >/dev/null || fail "missing required command: flock"
fi

for path in "$ROOT/backup_state.py" "$STATE_FILE" "$ARCHIVE_FILE"; do
    [[ -e "$path" ]] || fail "required deployment path is missing: $path"
done
for unit in "${ALL_UNITS[@]}"; do
    [[ -f "$SYSTEMD_ROOT/$unit" ]] || fail "required systemd unit is missing: $unit"
done

validate_bundle_manifest() {
    python3 - "$BUNDLE" <<'PY'
import sys
import tarfile
from pathlib import PurePosixPath

bundle = sys.argv[1]
forbidden_fragments = (
    "data/",
    "btc_15m_state.json",
    "btc_15m_state_archive.sqlite3",
    ".pem",
    ".key",
)
with tarfile.open(bundle, "r:gz") as archive:
    for member in archive.getmembers():
        name = member.name
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe bundle path: {name}")
        if member.issym() or member.islnk():
            raise SystemExit(f"link member is not allowed: {name}")
        if any(fragment in name for fragment in forbidden_fragments) or name.endswith(".env"):
            raise SystemExit(f"forbidden bundle member: {name}")
PY
}

STAGE=$(mktemp -d "$STATE_ROOT/.kalshi-release.XXXXXX")
ROLLBACK_ID="$(date -u +%Y%m%dT%H%M%SZ).$RELEASE_ID"
ROLLBACK_ROOT="$DEPLOY_ROLLBACKS/$ROLLBACK_ID"
ROLLBACK_CODE="$ROLLBACK_ROOT/kalshi"
ROLLBACK_UNITS="$ROLLBACK_ROOT/systemd"
LOCK_FILE="$STATE_ROOT/.kalshi-deploy.lock"
LOCK_DIR="$STATE_ROOT/.kalshi-deploy.lock.d"
SNAPSHOT=
COMMITTED=0
SERVICES_STOPPED=0
LOCK_HELD=0
POLL_SECONDS=${ORACLE_DEPLOY_POLL_SECONDS:-2}

remove_upload_if_temporary() {
    case "$BUNDLE" in
        /tmp/kalshi-*.tgz) rm -f -- "$BUNDLE" ;;
    esac
}

cleanup() {
    rm -rf -- "$STAGE"
    remove_upload_if_temporary
    if [[ "$TEST_MODE" == "1" && "$LOCK_HELD" == "1" ]]; then
        rmdir -- "$LOCK_DIR" 2>/dev/null || true
        LOCK_HELD=0
    fi
}

acquire_lock() {
    if [[ "$TEST_MODE" == "1" ]]; then
        mkdir "$LOCK_DIR" 2>/dev/null || fail "another deployment is already running"
    else
        exec 9>"$LOCK_FILE"
        flock -n 9 || fail "another deployment is already running"
    fi
    LOCK_HELD=1
}

stop_all() {
    systemctl stop "${ALL_UNITS[@]}"
}

start_all() {
    systemctl start "${APP_SERVICES[@]}" kalshi-backup.timer kalshi-context-comparison.timer
}

install_file() {
    local source=$1
    local destination=$2
    if [[ "$TEST_MODE" == "1" ]]; then
        install -m 0644 "$source" "$destination"
    else
        install -o root -g root -m 0644 "$source" "$destination"
    fi
}

validate_units() {
    local source=$1
    local unit
    for unit in "${ALL_UNITS[@]}"; do
        [[ -f "$source/deploy/systemd/$unit" ]] || fail "bundle is missing systemd unit: $unit"
    done
    for unit_path in "$source"/deploy/systemd/*; do
        [[ -f "$unit_path" ]] || fail "unexpected systemd bundle member: $unit_path"
        unit=$(basename "$unit_path")
        case "$unit" in
            kalshi-runner.service|kalshi-dashboard.service|kalshi-backup.service|kalshi-backup.timer|kalshi-context-comparison.service|kalshi-context-comparison.timer) ;;
            *) fail "unexpected systemd unit: $unit" ;;
        esac
    done
}

install_units() {
    local source=$1
    local unit
    validate_units "$source"
    for unit in "${ALL_UNITS[@]}"; do
        install_file "$source/deploy/systemd/$unit" "$SYSTEMD_ROOT/$unit"
    done
}

save_current_units() {
    local unit
    mkdir -p "$ROLLBACK_UNITS"
    for unit in "${ALL_UNITS[@]}"; do
        install_file "$SYSTEMD_ROOT/$unit" "$ROLLBACK_UNITS/$unit"
    done
}

restore_current_units() {
    local unit
    for unit in "${ALL_UNITS[@]}"; do
        [[ -f "$ROLLBACK_UNITS/$unit" ]] || return 1
        install_file "$ROLLBACK_UNITS/$unit" "$SYSTEMD_ROOT/$unit" || return 1
    done
}

wait_ready() {
    local attempt=1
    while (( attempt <= 30 )); do
        if systemctl is-active --quiet kalshi-runner.service \
            && systemctl is-active --quiet kalshi-dashboard.service \
            && curl --fail --silent --show-error --max-time 5 \
                http://127.0.0.1:8765/health >/dev/null; then
            return 0
        fi
        sleep "$POLL_SECONDS"
        attempt=$((attempt + 1))
    done
    return 1
}

rollback_on_error() {
    local status=$?
    local rollback_status=0
    local final_status=$status
    trap - EXIT
    if (( status != 0 && COMMITTED == 0 && SERVICES_STOPPED == 1 )); then
        printf 'oracle-deploy: release failed; restoring previous code and units\n' >&2
        set +e
        stop_all || rollback_status=1
        if [[ -d "$ROLLBACK_CODE" ]]; then
            rm -rf -- "$ROOT" || rollback_status=1
            mv -- "$ROLLBACK_CODE" "$ROOT" || rollback_status=1
        fi
        restore_current_units || rollback_status=1
        systemctl daemon-reload || rollback_status=1
        start_all || rollback_status=1
        wait_ready || rollback_status=1
        set -e
        if (( rollback_status != 0 )); then
            printf 'oracle-deploy: rollback failed; inspect services immediately\n' >&2
            final_status=70
        else
            printf 'oracle-deploy: rollback restored the previous release\n' >&2
        fi
    fi
    cleanup
    exit "$final_status"
}
trap rollback_on_error EXIT

acquire_lock
validate_bundle_manifest
tar -xzf "$BUNDLE" -C "$STAGE"
for path in \
    "$STAGE/btc_predictor.py" \
    "$STAGE/dashboard.py" \
    "$STAGE/live_runner.py" \
    "$STAGE/forecast_archive.py" \
    "$STAGE/validation.py" \
    "$STAGE/backup_state.py" \
    "$STAGE/deploy/oracle-deploy.sh" \
    "$STAGE/deploy/oracle-rollback.sh"; do
    [[ -f "$path" ]] || fail "bundle is missing required file: $path"
done
validate_units "$STAGE"
python3 -m compileall -q "$STAGE"

mkdir -p "$ROLLBACK_ROOT"
SERVICES_STOPPED=1
stop_all
save_current_units
SNAPSHOT=$(python3 "$ROOT/backup_state.py" create \
    --state-file "$STATE_FILE" --backup-dir "$BACKUP_ROOT" --keep 7 |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["backup_dir"])')
python3 "$ROOT/backup_state.py" verify "$SNAPSHOT" >/dev/null

mv -- "$ROOT" "$ROLLBACK_CODE"
mkdir -p "$ROOT"
tar -xzf "$BUNDLE" -C "$ROOT"
if [[ "$TEST_MODE" != "1" ]]; then
    chown -R root:root "$ROOT"
fi
install_units "$ROOT"
systemctl daemon-reload
start_all
wait_ready || fail "services or dashboard health did not become ready within 60 seconds"

COMMITTED=1
cleanup

rollback_dirs=()
while IFS= read -r rollback_dir; do
    rollback_dirs+=("$rollback_dir")
done < <(find "$DEPLOY_ROLLBACKS" -mindepth 1 -maxdepth 1 -type d -print | sort -r)
if (( ${#rollback_dirs[@]} > 3 )); then
    for old_dir in "${rollback_dirs[@]:3}"; do
        rm -rf -- "$old_dir"
    done
fi

printf 'oracle-deploy: release %s active\n' "$RELEASE_ID"
printf 'oracle-deploy: verified snapshot %s\n' "$SNAPSHOT"

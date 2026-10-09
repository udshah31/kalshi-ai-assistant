#!/usr/bin/env bash
# Restore a retained code/systemd release on the Oracle Ubuntu VM.
set -Eeuo pipefail

ROOT=/opt/kalshi
STATE_ROOT=/var/lib/kalshi
SYSTEMD_ROOT=/etc/systemd/system
STATE_FILE="$STATE_ROOT/btc_15m_state.json"
ARCHIVE_FILE="$STATE_ROOT/btc_15m_state_archive.sqlite3"
BACKUP_ROOT="$STATE_ROOT/backups"
ROLLBACK_ROOT="$STATE_ROOT/deploy-rollbacks"

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
Usage: sudo bash deploy/oracle-rollback.sh ROLLBACK_ID

Restore a retained directory from /var/lib/kalshi/deploy-rollbacks. The command
backs up current evidence, stops the application units, restores code and exact
systemd unit files, restarts services, and verifies the dashboard health check.
EOF
}

fail() {
    printf 'oracle-rollback: %s\n' "$1" >&2
    exit 1
}

if [[ "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi
[[ $# -eq 1 ]] || { usage >&2; exit 2; }
ROLLBACK_ID=$1
[[ "$ROLLBACK_ID" =~ ^[A-Za-z0-9._-]+$ ]] || fail "invalid rollback identifier"
[[ "$(uname -s)" == "Linux" ]] || fail "this rollback helper requires Linux"
[[ $EUID -eq 0 ]] || fail "run as root, normally through sudo"

for command in python3 systemctl curl install flock cp; do
    command -v "$command" >/dev/null || fail "missing required command: $command"
done
for path in "$ROOT/backup_state.py" "$STATE_FILE" "$ARCHIVE_FILE"; do
    [[ -e "$path" ]] || fail "required deployment path is missing: $path"
done

TARGET="$ROLLBACK_ROOT/$ROLLBACK_ID"
TARGET_CODE="$TARGET/kalshi"
TARGET_UNITS="$TARGET/systemd"
[[ -d "$TARGET_CODE" ]] || fail "rollback code tree does not exist: $TARGET_CODE"
for unit in "${ALL_UNITS[@]}"; do
    [[ -f "$TARGET_UNITS/$unit" ]] || fail "rollback unit is missing: $unit"
done

LOCK_FILE="$STATE_ROOT/.kalshi-deploy.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || fail "another deployment is already running"

CURRENT_ID="$(date -u +%Y%m%dT%H%M%SZ).manual-current"
CURRENT="$ROLLBACK_ROOT/$CURRENT_ID"
CURRENT_CODE="$CURRENT/kalshi"
CURRENT_UNITS="$CURRENT/systemd"
[[ ! -e "$CURRENT" ]] || fail "rollback staging path already exists: $CURRENT"
mkdir -p "$CURRENT_UNITS"
COMMITTED=0
SERVICES_STOPPED=0
CODE_MOVED=0

stop_all() {
    systemctl stop "${ALL_UNITS[@]}"
}

start_all() {
    systemctl start "${APP_SERVICES[@]}" kalshi-backup.timer kalshi-context-comparison.timer
}

save_units() {
    local destination=$1
    local unit
    for unit in "${ALL_UNITS[@]}"; do
        install -o root -g root -m 0644 "$SYSTEMD_ROOT/$unit" "$destination/$unit"
    done
}

restore_units() {
    local source=$1
    local unit
    for unit in "${ALL_UNITS[@]}"; do
        install -o root -g root -m 0644 "$source/$unit" "$SYSTEMD_ROOT/$unit" || return 1
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
        sleep 2
        attempt=$((attempt + 1))
    done
    return 1
}

rollback_on_error() {
    local status=$?
    local restore_status=0
    local final_status=$status
    trap - EXIT
    if (( status != 0 && COMMITTED == 0 && SERVICES_STOPPED == 1 )); then
        printf 'oracle-rollback: restore failed; returning to the current release\n' >&2
        set +e
        stop_all || restore_status=1
        if (( CODE_MOVED == 1 )); then
            rm -rf -- "$ROOT"
            mv -- "$CURRENT_CODE" "$ROOT" || restore_status=1
        fi
        restore_units "$CURRENT_UNITS" || restore_status=1
        systemctl daemon-reload || restore_status=1
        start_all || restore_status=1
        wait_ready || restore_status=1
        set -e
        if (( restore_status != 0 )); then
            printf 'oracle-rollback: recovery failed; inspect services immediately\n' >&2
            final_status=70
        fi
    fi
    if (( COMMITTED == 0 )); then
        rm -rf -- "$CURRENT"
    fi
    exit "$final_status"
}
trap rollback_on_error EXIT

save_units "$CURRENT_UNITS"
SERVICES_STOPPED=1
stop_all
SNAPSHOT=$(python3 "$ROOT/backup_state.py" create \
    --state-file "$STATE_FILE" --backup-dir "$BACKUP_ROOT" --keep 7 |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["backup_dir"])')
python3 "$ROOT/backup_state.py" verify "$SNAPSHOT" >/dev/null
mv -- "$ROOT" "$CURRENT_CODE"
CODE_MOVED=1
cp -a "$TARGET_CODE" "$ROOT"
restore_units "$TARGET_UNITS"
systemctl daemon-reload
start_all
wait_ready || fail "restored release did not become healthy within 60 seconds"

COMMITTED=1
printf 'oracle-rollback: release %s active\n' "$ROLLBACK_ID"
printf 'oracle-rollback: current evidence snapshot %s\n' "$SNAPSHOT"

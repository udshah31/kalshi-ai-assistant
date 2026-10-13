#!/usr/bin/env bash
# Deploy the code-only bundle into the existing ARM64 Docker/systemd runtime.
set -Eeuo pipefail

ROOT=/opt/kalshi
STATE_ROOT=/var/lib/kalshi
CONFIG_ROOT=/etc/kalshi
SYSTEMD_ROOT=/etc/systemd/system
STATE_FILE="$STATE_ROOT/btc_15m_state.json"
ARCHIVE_FILE="$STATE_ROOT/btc_15m_state_archive.sqlite3"
BACKUP_ROOT="$STATE_ROOT/backups"
ROLLBACK_ROOT="$STATE_ROOT/deploy-rollbacks"
RUNTIME_ENV="$CONFIG_ROOT/container-runtime.env"

APP_SERVICES=(kalshi-runner.service kalshi-dashboard.service)
SCHEDULED_UNITS=(kalshi-backup.timer kalshi-backup.service kalshi-context-comparison.timer kalshi-context-comparison.service)
ALL_UNITS=("${APP_SERVICES[@]}" "${SCHEDULED_UNITS[@]}")

usage() {
    cat <<'EOF'
Usage: sudo bash deploy/oracle-container-deploy.sh BUNDLE_PATH RELEASE_ID

Deploy a code-only release using the preinstalled, digest-pinned Python container.
The release binds the new /opt/kalshi code into the container and never replaces
/var/lib/kalshi evidence or /etc/kalshi/kalshi.env.
EOF
}

fail() {
    printf 'oracle-container-deploy: %s\n' "$1" >&2
    exit 1
}

if [[ "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi
[[ $# -eq 2 ]] || { usage >&2; exit 2; }
BUNDLE=$1
RELEASE_ID=$2
[[ "$RELEASE_ID" =~ ^[A-Za-z0-9._-]+$ ]] || fail "invalid release identifier"
[[ -f "$BUNDLE" ]] || fail "bundle does not exist: $BUNDLE"
[[ $EUID -eq 0 ]] || fail "run as root, normally through sudo"

for command in docker systemctl curl install find tar flock grep; do
    command -v "$command" >/dev/null || fail "missing required command: $command"
done
for path in "$ROOT" "$STATE_FILE" "$ARCHIVE_FILE" "$CONFIG_ROOT/kalshi.env" "$RUNTIME_ENV"; do
    [[ -e "$path" ]] || fail "required deployment path is missing: $path"
done
for unit in "${ALL_UNITS[@]}"; do
    [[ -f "$SYSTEMD_ROOT/$unit" ]] || fail "required systemd unit is missing: $unit"
done
id kalshi >/dev/null 2>&1 || fail "kalshi service account is missing"

# Validate the archive member names before extraction. The bundle is built by CI,
# but rejecting paths here prevents a malformed upload from escaping the stage.
validate_bundle() {
    local member
    while IFS= read -r member; do
        [[ "$member" != /* && "$member" != ../* && "$member" != *"/../"* && "$member" != *"/.." ]] \
            || fail "unsafe bundle member: $member"
        [[ "$member" != *"data/"* && "$member" != *.pem && "$member" != *.key && "$member" != *.env ]] \
            || fail "forbidden bundle member: $member"
    done < <(tar -tzf "$BUNDLE")
    local required
    for required in \
        btc_predictor.py dashboard.py live_runner.py forecast_archive.py validation.py backup_state.py \
        deploy/container-python deploy/oracle-container-deploy.sh \
        deploy/container-systemd/kalshi-dashboard.service \
        deploy/container-systemd/kalshi-runner.service \
        deploy/container-systemd/kalshi-backup.service \
        deploy/container-systemd/kalshi-context-comparison.service \
        deploy/container-systemd/kalshi-backup.timer \
        deploy/container-systemd/kalshi-context-comparison.timer; do
        tar -tzf "$BUNDLE" | grep -Fx "${required}" >/dev/null || fail "bundle is missing $required"
    done
}

validate_bundle

source "$RUNTIME_ENV"
[[ "${KALSHI_IMAGE:-}" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "runtime image is not digest-pinned"
image_arch=$(docker image inspect "$KALSHI_IMAGE" --format '{{.Architecture}}' 2>/dev/null) \
    || fail "pinned runtime image is not available locally"
[[ "$image_arch" == "arm64" ]] || fail "runtime image architecture is $image_arch, expected arm64"

STAGE=$(mktemp -d "$STATE_ROOT/.kalshi-container-release.XXXXXX")
ROLLBACK_ID="$(date -u +%Y%m%dT%H%M%SZ).$RELEASE_ID"
ROLLBACK_DIR="$ROLLBACK_ROOT/$ROLLBACK_ID"
ROLLBACK_CODE="$ROLLBACK_DIR/kalshi"
ROLLBACK_UNITS="$ROLLBACK_DIR/systemd"
ROLLBACK_RUNTIME="$ROLLBACK_DIR/container-runtime.env"
LOCK_FILE="$STATE_ROOT/.kalshi-deploy.lock"
SNAPSHOT=
STOPPED=0
MOVED=0
COMMITTED=0

cleanup() {
    rm -rf -- "$STAGE"
}

start_all() {
    systemctl start "${APP_SERVICES[@]}" kalshi-backup.timer kalshi-context-comparison.timer
}

stop_all() {
    systemctl stop "${ALL_UNITS[@]}"
}

container_python() {
    local code_root=$1
    shift
    local uid gid
    uid=$(id -u kalshi)
    gid=$(id -g kalshi)
    docker run --rm --init --network host --user "$uid:$gid" \
        --read-only --tmpfs /tmp:rw,nosuid,nodev,size=128m \
        --cap-drop ALL --security-opt no-new-privileges --pull never \
        --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPYCACHEPREFIX=/tmp/pycache \
        --env-file "$CONFIG_ROOT/kalshi.env" \
        --mount "type=bind,source=$code_root,target=/opt/kalshi,readonly" \
        --mount "type=bind,source=$STATE_ROOT,target=/var/lib/kalshi" \
        "$KALSHI_IMAGE" python3 -u "$@"
}

wait_ready() {
    local attempt=1
    while (( attempt <= 30 )); do
        if systemctl is-active --quiet kalshi-runner.service \
            && systemctl is-active --quiet kalshi-dashboard.service \
            && curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8765/health >/dev/null; then
            return 0
        fi
        sleep 2
        attempt=$((attempt + 1))
    done
    return 1
}

rollback_on_error() {
    local status=$?
    local rollback_status=0
    local final_status=$status
    trap - EXIT
    if (( status != 0 && COMMITTED == 0 && STOPPED == 1 )); then
        printf 'oracle-container-deploy: release failed; restoring previous code and units\n' >&2
        set +e
        stop_all || rollback_status=1
        if (( MOVED == 1 )); then
            rm -rf -- "$ROOT" || rollback_status=1
            mv -- "$ROLLBACK_CODE" "$ROOT" || rollback_status=1
        fi
        for unit in "${ALL_UNITS[@]}"; do
            install -o root -g root -m 0644 "$ROLLBACK_UNITS/$unit" "$SYSTEMD_ROOT/$unit" || rollback_status=1
        done
        install -o root -g root -m 0600 "$ROLLBACK_RUNTIME" "$RUNTIME_ENV" || rollback_status=1
        systemctl daemon-reload || rollback_status=1
        start_all || rollback_status=1
        wait_ready || rollback_status=1
        set -e
        if (( rollback_status != 0 )); then
            printf 'oracle-container-deploy: rollback failed; inspect services immediately\n' >&2
            final_status=70
        else
            printf 'oracle-container-deploy: rollback restored the previous release\n' >&2
        fi
    fi
    cleanup
    exit "$final_status"
}
trap rollback_on_error EXIT

exec 9>"$LOCK_FILE"
flock -n 9 || fail "another deployment is already running"
mkdir -p "$ROLLBACK_DIR" "$ROLLBACK_UNITS"
tar -xzf "$BUNDLE" -C "$STAGE"
chmod 0755 "$STAGE"
container_python "$STAGE" /opt/kalshi/backup_state.py create \
    --state-file /var/lib/kalshi/btc_15m_state.json --backup-dir /var/lib/kalshi/backups --keep 7
SNAPSHOT=$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name 'btc15m-backup-*' -print | sort | tail -n 1)
[[ -n "$SNAPSHOT" ]] || fail "verified snapshot was not published"
container_python "$STAGE" /opt/kalshi/backup_state.py verify "$SNAPSHOT" >/dev/null
container_python "$STAGE" -m compileall -q /opt/kalshi

STOPPED=1
stop_all
cp -a "$ROOT" "$ROLLBACK_CODE"
for unit in "${ALL_UNITS[@]}"; do
    install -o root -g root -m 0644 "$SYSTEMD_ROOT/$unit" "$ROLLBACK_UNITS/$unit"
done
install -o root -g root -m 0600 "$RUNTIME_ENV" "$ROLLBACK_RUNTIME"
rm -rf -- "$ROOT"
mkdir -p "$ROOT"
tar -xzf "$BUNDLE" -C "$ROOT"
chown -R root:root "$ROOT"
chmod 0755 "$ROOT/deploy/container-python" "$ROOT/deploy/oracle-container-deploy.sh"
for unit in "${ALL_UNITS[@]}"; do
    install -o root -g root -m 0644 "$ROOT/deploy/container-systemd/$unit" "$SYSTEMD_ROOT/$unit"
done
printf 'KALSHI_IMAGE=%s\n' "$KALSHI_IMAGE" > "$RUNTIME_ENV"
chown root:root "$RUNTIME_ENV"
chmod 0600 "$RUNTIME_ENV"
printf '{"release_id":"%s","image":"%s","snapshot":"%s"}\n' "$RELEASE_ID" "$KALSHI_IMAGE" "$SNAPSHOT" > "$ROOT/RELEASE.json"
chmod 0644 "$ROOT/RELEASE.json"
systemctl daemon-reload
start_all
wait_ready || fail "services or dashboard health did not become ready within 60 seconds"

COMMITTED=1
cleanup
rollback_dirs=()
while IFS= read -r rollback_dir; do
    rollback_dirs+=("$rollback_dir")
done < <(find "$ROLLBACK_ROOT" -mindepth 1 -maxdepth 1 -type d -print | sort -r)
if (( ${#rollback_dirs[@]} > 3 )); then
    for old_dir in "${rollback_dirs[@]:3}"; do
        rm -rf -- "$old_dir"
    done
fi
printf 'oracle-container-deploy: release %s active\n' "$RELEASE_ID"
printf 'oracle-container-deploy: verified snapshot %s\n' "$SNAPSHOT"

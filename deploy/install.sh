#!/usr/bin/env bash
# Install the Ubuntu/systemd deployment. Activate after importing evidence.
set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
    printf 'Usage: sudo bash deploy/install.sh\nInstalls code and systemd units; import state/archive before starting services.\n'
    exit 0
fi
if [[ $# -ne 0 ]]; then
    printf 'Unexpected arguments. Run with --help for usage.\n' >&2
    exit 2
fi
if [[ "$(uname -s)" != "Linux" ]]; then
    printf 'This installer requires a Linux VM with systemd.\n' >&2
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    printf 'Run this installer with sudo.\n' >&2
    exit 1
fi
for command in python3 systemctl useradd install; do
    command -v "$command" >/dev/null || { printf 'Missing required command: %s\n' "$command" >&2; exit 1; }
done
python3 -c 'import fcntl, hashlib, sqlite3, sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'

SOURCE="$(dirname "$(dirname "$(realpath "${BASH_SOURCE[0]}")")")"
if ! id kalshi >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/kalshi --shell /usr/sbin/nologin kalshi
fi
install -d -o root -g root -m 0755 /opt/kalshi
install -d -o kalshi -g kalshi -m 0700 /var/lib/kalshi
install -d -o root -g root -m 0700 /etc/kalshi
for file in btc_predictor.py dashboard.py live_runner.py forecast_archive.py validation.py backup_state.py; do
    install -o root -g root -m 0644 "$SOURCE/$file" "/opt/kalshi/$file"
done
for file in "$SOURCE"/deploy/systemd/*; do
    install -o root -g root -m 0644 "$file" "/etc/systemd/system/$(basename "$file")"
done
if [[ ! -e /etc/kalshi/kalshi.env ]]; then
    install -o root -g root -m 0600 "$SOURCE/deploy/kalshi.env.example" /etc/kalshi/kalshi.env
fi
systemctl daemon-reload
printf 'Installed. Import your verified snapshot into /var/lib/kalshi, configure /etc/kalshi/kalshi.env, then run:\n'
printf 'sudo systemctl enable --now kalshi-dashboard.service kalshi-runner.service kalshi-backup.timer kalshi-context-comparison.timer\n'

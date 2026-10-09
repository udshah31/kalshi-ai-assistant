#!/usr/bin/env bash
# Build a code-only upload; live data and service credentials are separate.
set -euo pipefail
ROOT="$(dirname "$(dirname "$(realpath "${BASH_SOURCE[0]}")")")"
mkdir -p "$ROOT/dist"
tar --exclude='__pycache__' --exclude='*.pyc' -czf "$ROOT/dist/kalshi-ubuntu.tgz" -C "$ROOT" \
    btc_predictor.py dashboard.py live_runner.py forecast_archive.py validation.py backup_state.py \
    README.md deploy tests
printf 'Code bundle: %s/dist/kalshi-ubuntu.tgz\n' "$ROOT"

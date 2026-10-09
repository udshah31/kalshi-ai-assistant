# Oracle Cloud: Ubuntu deployment

Run the same paper collector/dashboard on one Ubuntu VM. The scripts are
provider-neutral; this guide uses Oracle Cloud as the example provider. Code is
installed in `/opt/kalshi`; existing state, archive, telemetry, and backups live
in `/var/lib/kalshi`. Services use a dedicated `kalshi` user and start on reboot.
The dashboard listens on `127.0.0.1:8765` and is reached through SSH.

## 1. Create the VM in Oracle Cloud

Open the Oracle Cloud Console and choose **Compute → Instances → Create
instance**. Use an Ubuntu 24.04 LTS image and an Always Free-eligible shape if
available. Configure:

| Setting | Value |
| --- | --- |
| Image | **Ubuntu 24.04 LTS** |
| Shape | An Always Free-eligible AMD or Ampere shape |
| Authentication | SSH public key |
| Username | `ubuntu` |
| Incoming ports | SSH (22); the dashboard uses an SSH tunnel |
| Public IPv4 | Enabled, with the address recorded for SSH |

Check Oracle's current Always Free availability and regional limits before
creating the instance. Keep the private SSH key on your Mac and record the
instance's public IP or hostname.

## 2. Upload and install code while the Mac keeps collecting

On your Mac, from the project directory, set the real IP and downloaded key path:

```bash
VM_IP="YOUR_VM_PUBLIC_IP"
VM_USER="ubuntu"
SSH_KEY="$HOME/Downloads/YOUR_DOWNLOADED_KEY.pem"
chmod 600 "$SSH_KEY"
bash deploy/package.sh
scp -i "$SSH_KEY" dist/kalshi-ubuntu.tgz "$VM_USER@$VM_IP:kalshi-ubuntu.tgz"
ssh -i "$SSH_KEY" "$VM_USER@$VM_IP"
```

On the Ubuntu VM:

```bash
sudo apt-get update
sudo apt-get install -y python3 ca-certificates curl
mkdir -p "$HOME/kalshi-upload"
tar -xzf "$HOME/kalshi-ubuntu.tgz" -C "$HOME/kalshi-upload"
cd "$HOME/kalshi-upload"
python3 -m unittest discover -s tests -v
sudo bash deploy/install.sh
```

The installer stages files without starting the collector. Services require the
existing state and archive, so migration cannot accidentally bootstrap a new run.

## 3. Cut over with a consistent evidence snapshot

Do this after installation is ready, preferably just after a forecast cycle to
keep the cutover short. On your Mac, stop both state writers:

```bash
launchctl disable gui/$(id -u)/com.kalshi.btc15m.runner
launchctl disable gui/$(id -u)/com.kalshi.btc15m.dashboard
launchctl bootout gui/$(id -u)/com.kalshi.btc15m.runner
launchctl bootout gui/$(id -u)/com.kalshi.btc15m.dashboard
```

Disabling the labels persists across Mac logins/reboots; unloading alone does not.

From the project directory in the same Mac shell that has `VM_IP` and `SSH_KEY`:

```bash
SNAPSHOT=$(python3 backup_state.py create \
  --state-file data/btc_15m_state.json \
  --backup-dir data/migration --keep 3 | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["backup_dir"])')
python3 backup_state.py verify "$SNAPSHOT"
tar -czf dist/kalshi-evidence.tgz -C "$SNAPSHOT" \
  manifest.json btc_15m_state.json btc_15m_state_archive.sqlite3
scp -i "$SSH_KEY" dist/kalshi-evidence.tgz "$VM_USER@$VM_IP:kalshi-evidence.tgz"
```

The snapshot takes the existing cross-process state lock and uses SQLite's backup
API, including committed WAL rows. It preserves the JSON bytes, model weights,
run ID, pending ticker, official labels, paper balance, and TypeSafe requirement.
Its manifest has SHA-256 checksums and an archive integrity check.

On the **fresh** VM, import the verified snapshot:

```bash
mkdir -p "$HOME/kalshi-migration"
tar -xzf "$HOME/kalshi-evidence.tgz" -C "$HOME/kalshi-migration"
(
  set -e
  python3 /opt/kalshi/backup_state.py verify "$HOME/kalshi-migration"
  sudo test ! -e /var/lib/kalshi/btc_15m_state.json
  sudo test ! -e /var/lib/kalshi/btc_15m_state_archive.sqlite3
  sudo install -o kalshi -g kalshi -m 0600 \
    "$HOME/kalshi-migration/btc_15m_state.json" /var/lib/kalshi/btc_15m_state.json
  sudo install -o kalshi -g kalshi -m 0600 \
    "$HOME/kalshi-migration/btc_15m_state_archive.sqlite3" /var/lib/kalshi/btc_15m_state_archive.sqlite3
)
```

The grouped import aborts on failed verification or an existing destination before
either copy. Continue to activation only after it succeeds. For an existing VM
deployment, stop its services and back up its current evidence before planning a
restore.

Configure the existing TypeSafe key directly on the VM:

```bash
sudoedit /etc/kalshi/kalshi.env
```

Uncomment the `TYPESAFE_API_KEY` setting and enter the existing key privately. The
root-only environment file is read by both services. A migrated state that already
requires TypeSafe continues to withhold paper entries until that gate is satisfied.

Start the services, daily backup timer, and hourly comparison timer:

```bash
sudo systemctl enable --now kalshi-dashboard.service kalshi-runner.service kalshi-backup.timer kalshi-context-comparison.timer
sudo systemctl status kalshi-dashboard.service kalshi-runner.service --no-pager
curl --fail http://127.0.0.1:8765/health
sudo journalctl -u kalshi-runner.service -n 30 --no-pager
```

The runner waits for the next entry window if started late. Verify that the next
boundary produces a fresh exact-ticker forecast and its later official settlement.
Keep the Mac services stopped while the VM is the active collector.

### Optional Discord milestone notification

Create an incoming webhook in the destination Discord channel. Privately add its
URL to `/etc/kalshi/kalshi.env` as `DISCORD_WEBHOOK_URL`, and set
`MILESTONE_DASHBOARD_URL` to the dashboard's reachable URL (for example, its
Tailscale Serve HTTPS address). Restart `kalshi-runner.service` after saving.

The runner checks every heartbeat and posts when the existing official-validation
counter reaches **100 distinct qualifying outcomes**, even if accuracy or Brier
checks still block paper entries. Legacy proxy scores, duplicate tickers, and
late/ineligible records do not count. The message includes rolling accuracy,
Brier score, validation readiness, and the dashboard link. Enable **All Messages**
notifications for that Discord channel if you want a phone/desktop push alert.

It also posts a separate one-time notification when the evidence gate becomes
ready: at least 100 qualifying outcomes, rolling accuracy of at least 50%, and
Brier below 0.250. This does not authorize every forecast; the per-candidate
market, timing, risk, and TypeSafe checks still apply.

Discord must confirm a saved message before a delivery record is written to
`/var/lib/kalshi/btc_15m_state_milestone_alerts.json`. That private, service-owned
record prevents repeat messages for the same run after normal
service restarts; failed deliveries retry on a later heartbeat. The webhook is
never included in telemetry, delivery records, or the code bundle. When migrating
an existing deployment, preserve this delivery record alongside its model state.

## 4. Open the dashboard from your Mac

```bash
ssh -i "$SSH_KEY" -N -L 8765:127.0.0.1:8765 "$VM_USER@$VM_IP"
```

Leave that SSH session running and open <http://127.0.0.1:8765>. If another local
app uses port 8765, forward `8766:127.0.0.1:8765` and open port 8766 instead.

## 5. Backups and operations

The timer makes a verified snapshot daily around 03:10 UTC, with up to five
minutes of jitter. It keeps the latest **seven snapshots for this source state**;
unrelated historical backups and snapshots of other state files are preserved.

Run and check a first backup on the VM:

```bash
sudo systemctl start kalshi-backup.service
sudo journalctl -u kalshi-backup.service -n 10 --no-pager
sudo systemctl list-timers kalshi-backup.timer --no-pager
```

Copy snapshots off the VM periodically. From the Mac project directory:

```bash
(
  umask 077
  ssh -i "$SSH_KEY" "$VM_USER@$VM_IP" \
    'sudo python3 /opt/kalshi/backup_state.py export --backup-dir /var/lib/kalshi/backups' \
    > dist/kalshi-vm-backups.tgz.partial && \
  mv dist/kalshi-vm-backups.tgz.partial dist/kalshi-vm-backups.tgz
)
```

Export holds the same backup lock as publication/retention and includes only
verified, published snapshot evidence. The local archive is published only if SSH
completes successfully. Keep a dated copy in your chosen off-server backup
location. Before restoring any extracted snapshot, run
`python3 backup_state.py verify /path/to/snapshot` and stop both state writers.

To deliberately return collection to the Mac, first disable and stop both VM
writers so a VM reboot cannot restart them:

```bash
sudo systemctl disable --now kalshi-runner.service kalshi-dashboard.service
```

Then capture and restore the latest verified VM snapshot so the model history
stays current. Re-enable and load the Mac agents after that restore:

```bash
launchctl enable gui/$(id -u)/com.kalshi.btc15m.dashboard
launchctl enable gui/$(id -u)/com.kalshi.btc15m.runner
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.kalshi.btc15m.dashboard.plist"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.kalshi.btc15m.runner.plist"
```

When deliberately moving collection back to Oracle, disable/unload the Mac agents,
migrate the latest evidence, then re-enable the VM writer services with
`sudo systemctl enable --now kalshi-dashboard.service kalshi-runner.service`.

Inspect recent service logs or restart after a code update:

```bash
sudo journalctl -u kalshi-runner.service -u kalshi-dashboard.service --since '1 hour ago' --no-pager
sudo systemctl restart kalshi-dashboard.service kalshi-runner.service
```

Offline reports remain under `/var/lib/kalshi/btc_15m_state_validation*.json`.
The continuous runner refreshes them on startup and daily UTC.

`kalshi-context-comparison.timer` runs the separate read-only horizon comparison
hourly at minute **05 UTC**, with up to one minute of scheduling tolerance. It
catches up on a missed run after reboot and writes
`/var/lib/kalshi/btc_15m_context_comparison.json` atomically. Each run retains the
same settings: 100 training records, 25 test records, and five epochs. The report
includes insufficient coverage, missing outcomes, and exact issue-time quote
coverage when more evidence is needed. Full JSON output is saved to the report;
errors and execution status are available in the service journal.
The dashboard's **Context horizon comparison** card displays this saved report,
including all-outcome and quote-matched scores, timestamp and collection progress.
Its `/api/context-comparison` endpoint is read-only and remains available when
live price requests fail.

Run once and inspect the schedule:

```bash
sudo systemctl start kalshi-context-comparison.service
sudo systemctl show kalshi-context-comparison.service --property=Result --property=ExecMainStatus
sudo systemctl list-timers kalshi-context-comparison.timer --no-pager
sudo journalctl -u kalshi-context-comparison.service -n 10 --no-pager
```

Disable scheduled comparisons with
`sudo systemctl disable --now kalshi-context-comparison.timer`.

The archive itself is not pruned by backup retention. Disk usage can be checked with
`df -h /var/lib/kalshi` and `du -sh /var/lib/kalshi/backups`.

## 6. GitHub Actions CI/CD

### CI

The `CI` workflow runs automatically on pushes and pull requests. It executes the
unit suite, Python compilation checks, shell syntax checks, and code-bundle
inspection on Python 3.11 and 3.12. It never connects to the Oracle VM and never
needs production credentials.

### Configure the production environment

In the GitHub repository, create an environment named `production` and configure
at least one required reviewer. Add these environment secrets without committing
them to the repository:

```text
ORACLE_HOST              # public IP or hostname
ORACLE_USER              # normally ubuntu
ORACLE_SSH_PRIVATE_KEY   # complete private-key value
ORACLE_KNOWN_HOSTS       # reviewed ssh-keyscan output
```

To prepare `ORACLE_KNOWN_HOSTS`, verify the VM fingerprint through the Oracle
console or an independent channel, then save the reviewed host-key line:

```bash
ssh-keyscan -H "$VM_IP"
```

Store the output as the `ORACLE_KNOWN_HOSTS` secret. The workflow uses strict
host-key checking and never falls back to `StrictHostKeyChecking=no`.

### Release code

From the repository's **Actions → Deploy → Run workflow** page, choose the branch,
tag, or commit to deploy. GitHub runs the tests and builds the code-only bundle,
then pauses for approval in the `production` environment. After approval, it:

1. Uploads only the code bundle and deployment helper.
2. Stops the Oracle application and scheduled units.
3. Creates and verifies a lock-consistent remote evidence snapshot.
4. Installs the new code and systemd units while preserving a rollback tree.
5. Restarts the services and checks `/health`.
6. Restores the previous code automatically if service startup or health checks
   fail.

Routine releases do not copy local state or SQLite files to the VM. Initial
migration and an intentional move of collection between machines still use the
consistent snapshot procedure in section 3 and must leave only one active runner.

Inspect a release with:

```bash
sudo systemctl status kalshi-dashboard.service kalshi-runner.service --no-pager
sudo journalctl -u kalshi-runner.service -u kalshi-dashboard.service -n 40 --no-pager
curl --fail http://127.0.0.1:8765/health
```

### Manual rollback to a retained release

The automatic deploy helper restores the previous code and exact unit files when
its own restart or health check fails. If a later runtime issue requires moving
back to an older retained release, list the rollback IDs and run the bundled
rollback helper:

```bash
sudo find /var/lib/kalshi/deploy-rollbacks -mindepth 1 -maxdepth 1 -type d -print
sudo bash /opt/kalshi/deploy/oracle-rollback.sh ROLLBACK_ID
sudo systemctl status kalshi-dashboard.service kalshi-runner.service --no-pager
curl --fail http://127.0.0.1:8765/health
```

The rollback helper creates another verified evidence snapshot before changing
code, preserves the current release as a new rollback entry, restores the target
code and its saved systemd units, and fails if the services or health endpoint do
not recover.

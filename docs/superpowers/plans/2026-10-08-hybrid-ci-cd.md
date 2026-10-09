# Hybrid CI/CD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add automatic GitHub Actions CI and a manually approved, backup-safe code deployment workflow for the existing Oracle Ubuntu collector.

**Architecture:** CI runs tests and inspects the code-only bundle without contacting production. A workflow-dispatch CD job builds the same bundle, pauses at a protected `production` environment, then uploads the bundle and a remote Oracle helper. The helper stops the app services, creates a verified live-state backup, replaces only code/systemd units with a rollback copy retained, restarts services, and checks `/health`; it never imports a local state/archive snapshot.

**Tech Stack:** GitHub Actions, Ubuntu runners, Bash, OpenSSH, Python 3.11/3.12, existing `unittest`, systemd, SQLite backup utility.

**Spec:** `docs/superpowers/specs/2026-10-08-hybrid-ci-cd-design.md`

## Global Constraints

- CI must not contact the Oracle VM, modify live state, or require service credentials.
- Normal releases must not overwrite `btc_15m_state.json` or the SQLite archive.
- Production deployment is manual and protected by the GitHub `production` environment.
- Only one runner may write the live state/archive at a time.
- The remote helper must create a verified backup before replacing code.
- A failed health or service check must restore the previous code and unit files.
- Deployment remains paper-only and must not add order-placement behavior.

---

### Task 1: Add the CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`
- Test: local workflow structure and all commands invoked by the workflow

**Interfaces:**
- Consumes: `deploy/package.sh`, `tests/`, the repository Python modules.
- Produces: a passing `CI` check and a code-only `dist/kalshi-ubuntu.tgz` artifact during each CI run.

- [ ] **Step 1: Create the workflow with least-privilege permissions**

Use `push` and `pull_request` triggers, `permissions: contents: read`, and a
Python matrix containing `3.11` and `3.12`:

```yaml
name: CI

on:
  push:
  pull_request:

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: pip
      - name: Run unit tests
        run: python -m unittest discover -s tests -v
      - name: Compile Python modules
        run: python -m compileall -q *.py tests
      - name: Check shell syntax
        run: bash -n deploy/*.sh
      - name: Build code-only bundle
        run: bash deploy/package.sh
      - name: Inspect bundle contents
        run: |
          python - <<'PY'
          import tarfile

          with tarfile.open("dist/kalshi-ubuntu.tgz", "r:gz") as archive:
              names = archive.getnames()
          forbidden = ("data/", ".env", ".pem", ".key", "btc_15m_state.json", "btc_15m_state_archive.sqlite3")
          bad = [name for name in names if any(token in name for token in forbidden)]
          if bad:
              raise SystemExit(f"forbidden files in code bundle: {bad}")
          required = {"btc_predictor.py", "backup_state.py", "deploy/install.sh", "deploy/oracle-deploy.sh"}
          missing = sorted(required - set(names))
          if missing:
              raise SystemExit(f"missing required bundle files: {missing}")
          print(f"verified {len(names)} bundle entries")
          PY
```

- [ ] **Step 2: Run the exact CI commands locally**

Run from the project root:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q *.py tests
bash -n deploy/*.sh
bash deploy/package.sh
```

Expected: all tests pass, compilation and shell checks exit 0, and the bundle
contains no live state or secrets.

### Task 2: Add the rollback-safe Oracle remote helper

**Files:**
- Create: `deploy/oracle-deploy.sh`
- Create: `deploy/oracle-rollback.sh`
- Test: `bash -n deploy/oracle-deploy.sh` and its help/argument validation paths

**Interfaces:**
- Consumes: a code bundle path and a release identifier as positional arguments:
  `sudo bash deploy/oracle-deploy.sh BUNDLE_PATH RELEASE_ID`.
- Produces: a verified backup under `/var/lib/kalshi/backups`, updated code under
  `/opt/kalshi`, updated systemd units, active services, and exit status 0 only
  after the health check succeeds.

- [ ] **Step 1: Add usage and required-path validation**

Implement `set -Eeuo pipefail`, a `--help` path, root/Linux checks, required
command checks for `python3`, `tar`, `systemctl`, and `curl`, and checks for:

```text
/opt/kalshi/backup_state.py
/var/lib/kalshi/btc_15m_state.json
/var/lib/kalshi/btc_15m_state_archive.sqlite3
/etc/systemd/system/kalshi-runner.service
/etc/systemd/system/kalshi-dashboard.service
```

Reject missing bundle paths and release identifiers containing anything except
letters, digits, dot, underscore, and hyphen.

- [ ] **Step 2: Stage and validate the bundle before stopping services**

Extract the bundle into a temporary directory under `/var/lib/kalshi`, require
the expected Python files, systemd units, and deployment helpers, then run:

```bash
python3 -m compileall -q "$STAGE"
```

Production-side validation must not execute release-controlled tests as root;
the full test suite runs in CI. Exit before touching production services if
staging, path, link, unit, or compilation validation fails.

- [ ] **Step 3: Stop app and scheduled units, then create the verified backup**

Stop the runner, dashboard, backup timer/service, and context-comparison
timer/service. Run the existing lock-consistent backup tool against the remote
state path:

```bash
systemctl stop kalshi-runner.service kalshi-dashboard.service
systemctl stop kalshi-backup.timer kalshi-backup.service
systemctl stop kalshi-context-comparison.timer kalshi-context-comparison.service
python3 /opt/kalshi/backup_state.py create \
  --state-file /var/lib/kalshi/btc_15m_state.json \
  --backup-dir /var/lib/kalshi/backups --keep 7
```

Treat a backup failure as fatal and leave the old code untouched.

- [ ] **Step 4: Install the staged code with a rollback tree**

Move `/opt/kalshi` to a release-specific rollback path, create a new
`/opt/kalshi`, extract the validated bundle, install the bundled systemd units
to `/etc/systemd/system`, and run `systemctl daemon-reload`. Keep the previous
tree until the health check succeeds.

- [ ] **Step 5: Restart and verify, with rollback on any failure**

Start the runner, dashboard, backup timer, and context-comparison timer. Require
both services to be active and retry the dashboard health endpoint for up to 60
seconds. An `EXIT` trap must stop the failed release, restore the previous
`/opt/kalshi` tree and systemd units, reload systemd, restart the previous
services/timers, remove temporary files, and preserve the pre-deploy evidence
backup before returning the original non-zero status.

On success, remove the temporary bundle and retain only the newest three
rollback code trees.

- [ ] **Step 6: Add and verify manual rollback**

Implement `deploy/oracle-rollback.sh ROLLBACK_ID` with the same host deployment
lock. It must create a verified current-evidence snapshot, preserve the current
code and exact unit files as a new rollback entry, restore the selected code and
units, restart services, and require `/health` before success. If it fails, it
must attempt to restore the current release and return a distinct failure code.

Run:

```bash
bash -n deploy/oracle-rollback.sh
bash deploy/oracle-rollback.sh --help
```

Expected: syntax succeeds, help exits 0, and no production path is touched.

- [ ] **Step 7: Verify helper safety locally**

Run:

```bash
bash -n deploy/oracle-deploy.sh
bash deploy/oracle-deploy.sh --help
```

Expected: syntax succeeds, help exits 0, and no production path is touched.

### Task 3: Add the manually approved deployment workflow

**Files:**
- Create: `.github/workflows/deploy.yml`
- Test: workflow YAML structure and the local bundle/SSH command construction

**Interfaces:**
- Consumes: `workflow_dispatch` input `release_ref`, the CI-compatible bundle,
  and GitHub `production` environment secrets.
- Produces: a production code deployment with remote backup, rollback, and health
  evidence.

- [ ] **Step 1: Add workflow dispatch and protected environment**

Use `workflow_dispatch` with a required `release_ref` defaulting to `main`,
`permissions: contents: read`, a build job, and a deploy job with:

```yaml
environment:
  name: production
```

The deploy job must depend on the build job so GitHub pauses at the configured
environment approval before SSH access is used.

- [ ] **Step 2: Build and upload the release artifact**

The build job checks out `${{ inputs.release_ref }}`, runs the full test,
compile, shell, package, and bundle inspection commands from CI, then uploads
`dist/kalshi-ubuntu.tgz` with `actions/upload-artifact@v4` and
`retention-days: 1`.

- [ ] **Step 3: Configure strict SSH and upload only code**

The deploy job downloads the artifact, writes `ORACLE_SSH_PRIVATE_KEY` with
mode 0600, writes `ORACLE_KNOWN_HOSTS`, and uses:

```bash
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$RUNNER_TEMP/known_hosts"
```

Copy only the code bundle and remote helper to release-specific `/tmp` paths on
the Oracle VM. Never upload `data/`, a local state file, an archive, or the
workflow workspace.

- [ ] **Step 4: Invoke remote deployment and collect logs**

Run the helper through `sudo bash`, pass the Git commit SHA as the release ID,
and always collect the last 40 runner/dashboard journal lines. Propagate the
remote helper exit status so a failed rollback or health check fails the job.

- [ ] **Step 5: Verify workflow configuration locally**

Parse both workflow files with Ruby's standard YAML parser if available, then
confirm the files contain the expected triggers, `production` environment, four
secret names, and no plaintext secret values:

```bash
ruby -e 'require "yaml"; ARGV.each { |path| YAML.load_file(path); puts "parsed #{path}" }' \
  .github/workflows/ci.yml .github/workflows/deploy.yml
```

### Task 4: Document operation and GitHub configuration

**Files:**
- Modify: `deploy/README.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: the CI/CD workflow names, helper arguments, and secret names from
  Tasks 1–3.
- Produces: operator instructions for initial Oracle setup, routine releases,
  GitHub environment protection, rollback, and smoke checks.

- [ ] **Step 1: Add Oracle and GitHub Actions setup instructions**

Document that the scripts are provider-neutral Ubuntu/systemd deployment tools,
while the example provider is Oracle Cloud. Include the exact secrets:

```text
ORACLE_HOST
ORACLE_USER
ORACLE_SSH_PRIVATE_KEY
ORACLE_KNOWN_HOSTS
```

Explain that the `production` environment must require reviewers and that the
SSH private key is stored only as a GitHub secret.

- [ ] **Step 2: Document routine release and rollback commands**

Show how to start `Deploy` from Actions, approve the environment, inspect the
workflow logs, and manually run the remote helper only when needed. State that
the workflow backs up remote evidence but does not migrate local state.

- [ ] **Step 3: Link the root README to deployment operations**

Add a short CI/CD section after the existing Oracle deployment section, linking
to `deploy/README.md` and explaining that CI is automatic while production CD is
manual and gated.

### Task 5: Run the complete verification suite

**Files:**
- Test: all changed workflows/scripts/docs and existing `tests/`

- [ ] **Step 1: Run unit, compile, shell, and package checks**

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q *.py tests
bash -n deploy/*.sh
bash deploy/package.sh
```

- [ ] **Step 2: Inspect the bundle for evidence or secrets**

```bash
python3 - <<'PY'
import tarfile

with tarfile.open("dist/kalshi-ubuntu.tgz", "r:gz") as archive:
    names = archive.getnames()
forbidden = ("data/", ".env", ".pem", ".key", "btc_15m_state.json", "btc_15m_state_archive.sqlite3")
bad = [name for name in names if any(token in name for token in forbidden)]
assert not bad, bad
print("bundle contains no live evidence or private-key files")
PY
```

- [ ] **Step 3: Parse and inspect workflows**

```bash
ruby -e 'require "yaml"; ARGV.each { |path| YAML.load_file(path); puts "parsed #{path}" }' \
  .github/workflows/ci.yml .github/workflows/deploy.yml
```

- [ ] **Step 4: Check the working tree and report deployment prerequisites**

Because this workspace currently has no Git metadata, report that the files are
ready to commit/push once the project is connected to its GitHub repository. Do
not claim production CD was exercised locally; the Oracle workflow requires the
configured GitHub secrets and `production` reviewers.

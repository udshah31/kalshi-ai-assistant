# Hybrid CI/CD for the Kalshi Paper Collector

## Status

Design approved in chat; implementation follows after review of this spec.

## Goal

Add reproducible automation around the existing Oracle Ubuntu deployment without
automatically replacing the live model state or SQLite evidence archive.

The project will use:

- GitHub Actions CI for every push and pull request.
- A manually triggered, production-environment-gated deployment workflow for
  code-only releases to the existing Oracle VM.
- A remote deployment helper that snapshots live evidence, updates code and
  systemd units atomically enough to roll back, restarts services, and checks
  the dashboard health endpoint.
- A manual rollback helper that restores a retained code/systemd release after
  a later runtime issue.

## Non-goals

- Automatically migrating or overwriting `btc_15m_state.json` or the SQLite
  archive during a normal code release.
- Automatically enabling real trading or placing Kalshi orders.
- Deploying on every push to `main` without human approval.
- Replacing the existing systemd runtime with a container orchestrator.

## Continuous integration

Add `.github/workflows/ci.yml` triggered by `push` and `pull_request`.

The Linux CI job will:

1. Check out the repository.
2. Test the supported Python runtime(s), starting with Python 3.11 and 3.12.
3. Run `python3 -m unittest discover -s tests -v`.
4. Run Python compilation checks.
5. Run `bash -n` against the deployment shell scripts.
6. Build `dist/kalshi-ubuntu.tgz` with `deploy/package.sh`.
7. List the bundle contents and verify that live data, state files, archives,
   private keys, and environment files are absent.

CI must not contact the Oracle VM, modify live state, or require service
credentials.

## Continuous delivery

Add `.github/workflows/deploy.yml` triggered only by `workflow_dispatch`, with
an optional release reference input. The workflow will:

1. Check out the requested revision.
2. Run the same tests and bundle build as CI.
3. Upload the code bundle as a short-lived workflow artifact.
4. Wait at the GitHub `production` environment approval gate.
5. Configure SSH using repository/environment secrets:
   - `ORACLE_HOST`
   - `ORACLE_USER`
   - `ORACLE_SSH_PRIVATE_KEY`
   - `ORACLE_KNOWN_HOSTS`
6. Copy the code bundle and remote helper to the Oracle VM.
7. Invoke the remote helper over SSH.
8. Fail the workflow if the remote health check or service smoke check fails.

The production environment must be configured in GitHub with required reviewers;
the YAML workflow alone cannot enforce reviewer membership.

## Remote deployment transaction

Add `deploy/oracle-deploy.sh`, intended to run on the Oracle VM as root through
`sudo`.

Before changing code, the helper will:

1. Require Linux, root privileges, the expected state file, archive, and
   systemd service files.
2. Stop both dashboard and runner writers/readers so the release boundary is
   unambiguous.
3. Create a verified backup under `/var/lib/kalshi/backups` using the existing
   `backup_state.py` lock-consistent snapshot mechanism.
4. Stage and validate the uploaded code bundle.
5. Preserve the current `/opt/kalshi` tree and systemd units for rollback.
6. Install the new code and units, then run `systemctl daemon-reload`.
7. Start the dashboard and runner and check `http://127.0.0.1:8765/health`.
8. Check that the runner and dashboard are active through systemd.
9. Remove the temporary upload and keep a bounded number of prior code trees.

If installation, restart, or health verification fails, the helper will stop
the failed services, restore the previous code and unit files, reload systemd,
restart the previous release, and return a non-zero status. The evidence backup
created before the attempted release remains available.

The helper will never copy a local state/archive snapshot during a code-only
deployment. Initial migration and deliberate VM-to-VM collection handoff remain
manual operations described in the deployment documentation.

Add `deploy/oracle-rollback.sh` for deliberate restoration of a retained release.
It will serialize with normal deployment, snapshot current evidence, preserve the
current code and units as a new rollback entry, restore the selected code and
exact unit files, restart the services, and require a successful health check.
If restoration fails, it will attempt to return to the current release and report
a distinct failure status.

## Documentation changes

Update `deploy/README.md` to:

- Describe Oracle Cloud Ubuntu as the supported deployment example while noting
  that the scripts are provider-neutral.
- Document the GitHub secrets and `production` environment setup.
- Distinguish initial migration from routine code releases.
- Document the manual rollback and smoke-check behavior.
- State that only one runner may write the live state/archive at a time.

Update the root `README.md` with a short CI/CD section linking to the deployment
operations guide.

## Verification

Local verification will include:

- Existing full unittest suite.
- Workflow YAML parsing or equivalent structural checks.
- Shell syntax checks for all deployment scripts.
- Bundle-content inspection proving no live data or secrets are packaged.
- A dry-run remote-helper test using a temporary fake `/opt/kalshi` and state
  directory where feasible; no production service restart will be performed
  locally.
- Manual rollback argument/help validation and a transaction-harness rollback
  test using fake systemd/curl commands.

Production verification remains the deployment workflow's responsibility:

- Verified pre-deploy backup.
- Successful systemd activation.
- Dashboard `/health` response.
- Recent runner journal output.
- Explicit production approval recorded by GitHub Environment protection.

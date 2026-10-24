import subprocess
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "oracle-deploy.sh"
CONTAINER_SCRIPT = ROOT / "deploy" / "oracle-container-deploy.sh"
ROLLBACK_SCRIPT = ROOT / "deploy" / "oracle-rollback.sh"


class OracleDeployScriptTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def test_help_is_available_without_touching_production(self):
        result = self.run_script("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage:", result.stdout)
        self.assertIn("verified backup", result.stdout)

    def test_invalid_release_id_is_rejected_before_environment_checks(self):
        result = self.run_script("/tmp/missing-bundle.tgz", "bad/release")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid release identifier", result.stderr)

    def test_manual_rollback_help_is_available(self):
        result = subprocess.run(
            ["bash", str(ROLLBACK_SCRIPT), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage:", result.stdout)
        self.assertIn("deploy-rollbacks", result.stdout)

    def test_container_deploy_help_is_available(self):
        result = subprocess.run(
            ["bash", str(CONTAINER_SCRIPT), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pinned Python container", result.stdout)

    def test_container_runtime_contract_is_digest_pinned_and_state_separated(self):
        launcher = (ROOT / "deploy/container-python").read_text(encoding="utf-8")
        self.assertIn("--network host", launcher)
        self.assertIn("--pull never", launcher)
        self.assertIn("source=/opt/kalshi,target=/opt/kalshi,readonly", launcher)
        self.assertIn("source=/var/lib/kalshi,target=/var/lib/kalshi", launcher)
        self.assertIn("KALSHI_IMAGE:-", launcher)
        for name in (
            "kalshi-dashboard.service", "kalshi-runner.service",
            "kalshi-backup.service", "kalshi-context-comparison.service",
            "kalshi-late-window.service",
        ):
            unit = (ROOT / "deploy/container-systemd" / name).read_text(encoding="utf-8")
            self.assertIn("/opt/kalshi/deploy/container-python", unit)
            self.assertNotIn("/usr/bin/python3", unit)

    def test_container_deploy_opens_staged_code_for_the_service_uid(self):
        script = (ROOT / "deploy/oracle-container-deploy.sh").read_text(encoding="utf-8")
        self.assertIn('chmod 0755 "$STAGE"', script)

    def test_late_window_service_is_independent_and_bundled(self):
        unit = (ROOT / "deploy/container-systemd/kalshi-late-window.service").read_text(encoding="utf-8")
        self.assertIn("container-python late /opt/kalshi/late_window.py", unit)
        self.assertIn("--kalshi-series KXBTC15M", unit)
        self.assertIn("ExecStop=/usr/bin/docker stop --time 30 kalshi-late", unit)
        self.assertNotIn("kalshi-runner.service", unit)
        self.assertNotIn("kalshi-dashboard.service", unit)
        self.assertRegex((ROOT / "deploy/container-python").read_text(encoding="utf-8"),
                         r"case \"\$job\" in\s+backup\|comparison\|dashboard\|inspect\|late\|runner\)")
        script = CONTAINER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("APP_SERVICES=(kalshi-runner.service kalshi-dashboard.service kalshi-late-window.service)", script)
        self.assertIn("late_window.py", script)
        self.assertIn("deploy/container-systemd/kalshi-late-window.service", script)
        self.assertIn("late_window.py", (ROOT / "deploy/package.sh").read_text(encoding="utf-8"))
        # health stays runner+dashboard only
        ready = script.split("wait_ready() {")[1].split("\n}\n")[0]
        self.assertNotIn("late-window", ready)

    def test_container_rollback_removes_units_absent_from_the_previous_release(self):
        script = CONTAINER_SCRIPT.read_text(encoding="utf-8")
        # first deploy: unit is not preinstalled, not snapshotted, not stopped/started when absent
        self.assertIn("NEW_UNITS=(kalshi-late-window.service)", script)
        self.assertIn('if [[ -f "$SYSTEMD_ROOT/$unit" ]]; then\n        install', script)
        self.assertIn('if [[ -f "$ROLLBACK_UNITS/$unit" ]]; then', script)
        self.assertIn('systemctl disable "$unit"', script)
        self.assertIn('rm -f -- "$SYSTEMD_ROOT/$unit"', script)
        self.assertIn("installed_units", script.split("stop_all() {")[1].split("}")[0])
        self.assertIn("installed_units", script.split("start_all() {")[1].split("}")[0])

    def test_late_window_start_is_gated_on_enable_in_deploy_and_manual_rollback(self):
        rollback = ROLLBACK_SCRIPT.parent / "oracle-rollback.sh"
        for path in (CONTAINER_SCRIPT, rollback):
            script = path.read_text(encoding="utf-8")
            start = script.split("start_all() {")[1].split("\n}\n")[0]
            self.assertRegex(start, r"kalshi-late-window\.service.*is-enabled --quiet")
            self.assertRegex(script, r"NEW_UNITS=\(kalshi-late-window\.service\)")
            self.assertRegex(script.split("stop_all() {")[1].split("\n}\n")[0], r"installed_units")

    def test_manual_rollback_disables_and_removes_unit_missing_from_target(self):
        script = (ROOT / "deploy/oracle-rollback.sh").read_text(encoding="utf-8")
        self.assertRegex(script, r"APP_SERVICES=\([^)]*kalshi-late-window\.service")
        restore = script.split("restore_units() {")[1].split("\n}\n")[0]
        self.assertRegex(restore, r"systemctl disable --now \"\$unit\"")
        self.assertRegex(restore, r"rm -f -- \"\$SYSTEMD_ROOT/\$unit\"")
        self.assertRegex(script, r"NEW_UNITS\[\*\]")


class OracleDeployTransactionTests(unittest.TestCase):
    UNITS = (
        "kalshi-runner.service",
        "kalshi-dashboard.service",
        "kalshi-backup.service",
        "kalshi-backup.timer",
        "kalshi-context-comparison.service",
        "kalshi-context-comparison.timer",
    )

    @classmethod
    def setUpClass(cls):
        subprocess.run(["bash", "deploy/package.sh"], cwd=ROOT, check=True)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.root = base / "opt" / "kalshi"
        self.state_root = base / "var" / "lib" / "kalshi"
        self.systemd_root = base / "etc" / "systemd"
        self.bin = base / "bin"
        self.root.mkdir(parents=True)
        self.state_root.mkdir(parents=True)
        self.systemd_root.mkdir(parents=True)
        self.bin.mkdir(parents=True)

        (self.root / "backup_state.py").write_text(
            """#!/usr/bin/env python3
import json
import pathlib
import sys

if sys.argv[1] == 'create':
    target = pathlib.Path(sys.argv[sys.argv.index('--backup-dir') + 1]) / 'fake-snapshot'
    target.mkdir(parents=True, exist_ok=True)
    print(json.dumps({'backup_dir': str(target)}))
elif sys.argv[1] == 'verify':
    pass
""",
            encoding="utf-8",
        )
        (self.root / "backup_state.py").chmod(0o755)
        (self.state_root / "btc_15m_state.json").write_bytes(b"old-state")
        (self.state_root / "btc_15m_state_archive.sqlite3").write_bytes(b"old-archive")
        for unit in self.UNITS:
            (self.systemd_root / unit).write_text(f"old-{unit}\n", encoding="utf-8")

        (self.bin / "systemctl").write_text(
            """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
exit 0
""",
            encoding="utf-8",
        )
        (self.bin / "curl").write_text(
            """#!/bin/sh
count=$(cat "$FAKE_CURL_COUNTER")
count=$((count + 1))
printf '%s' "$count" > "$FAKE_CURL_COUNTER"
if [ "$count" -le "${FAKE_CURL_FAIL_COUNT:-0}" ]; then
    exit 22
fi
printf '{"ok":true}\\n'
""",
            encoding="utf-8",
        )
        for command in ("systemctl", "curl"):
            (self.bin / command).chmod(0o755)

        self.systemctl_log = base / "systemctl.log"
        self.systemctl_log.write_text("", encoding="utf-8")
        self.curl_counter = base / "curl-counter"
        self.curl_counter.write_text("0", encoding="utf-8")
        self.env = os.environ.copy()
        self.env.update(
            {
                "ORACLE_DEPLOY_TEST_MODE": "1",
                "ORACLE_DEPLOY_ROOT": str(self.root),
                "ORACLE_DEPLOY_STATE_ROOT": str(self.state_root),
                "ORACLE_DEPLOY_SYSTEMD_ROOT": str(self.systemd_root),
                "FAKE_SYSTEMCTL_LOG": str(self.systemctl_log),
                "FAKE_CURL_COUNTER": str(self.curl_counter),
                "ORACLE_DEPLOY_POLL_SECONDS": "0",
                "PATH": f"{self.bin}{os.pathsep}{self.env['PATH']}",
            }
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def run_transaction(self, release_id="test-release"):
        return subprocess.run(
            ["bash", str(SCRIPT), str(ROOT / "dist" / "kalshi-ubuntu.tgz"), release_id],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=120,
        )

    def test_success_preserves_evidence_and_retains_old_code_and_units(self):
        result = self.run_transaction()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "btc_predictor.py").is_file())
        self.assertEqual((self.state_root / "btc_15m_state.json").read_bytes(), b"old-state")
        self.assertEqual((self.state_root / "btc_15m_state_archive.sqlite3").read_bytes(), b"old-archive")
        self.assertEqual(
            (self.systemd_root / "kalshi-runner.service").read_text(encoding="utf-8"),
            (ROOT / "deploy/systemd/kalshi-runner.service").read_text(encoding="utf-8"),
        )
        rollback_dirs = list((self.state_root / "deploy-rollbacks").iterdir())
        self.assertEqual(len(rollback_dirs), 1)
        self.assertEqual(
            (rollback_dirs[0] / "kalshi" / "backup_state.py").read_text(encoding="utf-8").splitlines()[0],
            "#!/usr/bin/env python3",
        )

    def test_failed_health_check_restores_old_code_and_units(self):
        self.env["FAKE_CURL_FAIL_COUNT"] = "30"

        result = self.run_transaction("failed-release")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback", result.stderr)
        self.assertIn("old-state", (self.state_root / "btc_15m_state.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (self.root / "backup_state.py").read_text(encoding="utf-8").splitlines()[0],
            "#!/usr/bin/env python3",
        )
        for unit in self.UNITS:
            self.assertEqual(
                (self.systemd_root / unit).read_text(encoding="utf-8"),
                f"old-{unit}\n",
            )

    def test_existing_deployment_lock_blocks_a_second_release(self):
        (self.state_root / ".kalshi-deploy.lock.d").mkdir()

        result = self.run_transaction("concurrent-release")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another deployment is already running", result.stderr)


if __name__ == "__main__":
    unittest.main()

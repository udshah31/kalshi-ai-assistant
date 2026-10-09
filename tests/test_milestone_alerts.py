"""Discord delivery, exact-outcome counting, retries, and durable deduplication."""
import argparse
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import live_runner
from btc_predictor import OnlineLogisticRegression, new_state


WEBHOOK = "https://discord.com/api/webhooks/123/test-secret"
DASHBOARD = "https://kalshi-student.example.ts.net"
NOW = 1800000300


class Reply(io.BytesIO):
    status = 200


class OnePollStop(threading.Event):
    def wait(self, timeout=None):
        self.set()
        return True


class MilestoneAlertTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_file = Path(self.directory.name) / "state.json"
        self.marker = self.state_file.with_name("state_milestone_alerts.json")
        self.check = getattr(live_runner, "check_milestone_alert", None)
        self.assertTrue(callable(self.check), "The collector needs a milestone alert check")
        self.messages = []
        self.environment = patch.dict(os.environ, {
            "DISCORD_WEBHOOK_URL": WEBHOOK, "MILESTONE_DASHBOARD_URL": DASHBOARD,
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def save_state(self, count=100, state_id="run-A"):
        state = new_state(OnlineLogisticRegression(7))
        state["state_id"] = state_id
        state["paper_trades"] = [
            {"market_ticker": f"KXBTC15M-{i}", "outcome_source": "kalshi_official",
             "validation_eligible": True, "probability_up": .6,
             "outcome": "UP" if i < 40 else "DOWN", "correct": i < 40}
            for i in range(count)
        ]
        self.state_file.write_text(json.dumps(state))
        return state

    def save_ready_state(self):
        state = new_state(OnlineLogisticRegression(7))
        state["state_id"] = "ready-run"
        state["paper_trades"] = [
            {"market_ticker": f"KXBTC15M-ready-{i}", "outcome_source": "kalshi_official",
             "validation_eligible": True, "probability_up": .6,
             "outcome": "UP" if i < 60 else "DOWN", "correct": i < 60}
            for i in range(100)
        ]
        self.state_file.write_text(json.dumps(state))
        return state

    def deliver(self, request, timeout):
        if isinstance(request, str):
            return Reply(b'{"ok":true}')
        self.messages.append({"url": request.full_url,
                              "body": json.loads(request.data), "timeout": timeout})
        return Reply(b'{"id":"message-123"}')

    def test_proxy_late_and_duplicate_outcomes_cannot_complete_the_milestone(self):
        state = self.save_state(99)
        state["paper_trades"] += [dict(state["paper_trades"][0])] * 5
        state["paper_trades"] += [
            {"market_ticker": f"proxy-{i}", "probability_up": .8,
             "outcome": "UP", "correct": True} for i in range(200)
        ]
        state["paper_trades"].append({**state["paper_trades"][0],
                                      "market_ticker": "late", "validation_eligible": False})
        self.state_file.write_text(json.dumps(state))
        with patch("live_runner.urllib.request.urlopen", side_effect=self.deliver):
            result = self.check(str(self.state_file), DASHBOARD)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["samples"], 99)
        self.assertEqual(self.messages, [])
        self.assertFalse(self.marker.exists())

    def test_reaching_100_notifies_even_if_performance_is_not_ready(self):
        self.save_state()
        before = self.state_file.read_bytes()
        with patch("live_runner.urllib.request.urlopen", side_effect=self.deliver):
            first = self.check(str(self.state_file), DASHBOARD)
            second = self.check(str(self.state_file), DASHBOARD)
        self.assertEqual(first["status"], "sent")
        self.assertEqual(second["status"], "already_sent")
        self.assertEqual(len(self.messages), 1)
        message = self.messages[0]
        self.assertIn("wait=true", message["url"])
        self.assertEqual(message["body"]["allowed_mentions"], {"parse": []})
        content = message["body"]["content"]
        for detail in ("100", "40.0%", "0.280", DASHBOARD):
            self.assertIn(detail, content)
        self.assertIn("not ready", content.lower())
        self.assertNotIn("test-secret", self.marker.read_text())
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_validation_ready_notifies_once_after_accuracy_and_brier_pass(self):
        self.save_ready_state()
        with patch("live_runner.urllib.request.urlopen", side_effect=self.deliver):
            first = live_runner.check_validation_ready_alert(str(self.state_file), DASHBOARD)
            second = live_runner.check_validation_ready_alert(str(self.state_file), DASHBOARD)
        self.assertEqual(first["status"], "sent")
        self.assertEqual(second["status"], "already_sent")
        self.assertEqual(len(self.messages), 1)
        content = self.messages[0]["body"]["content"]
        for detail in ("validation gate is ready", "100", "60.0%", "0.240", DASHBOARD):
            self.assertIn(detail, content)
        self.assertNotIn("test-secret", self.marker.read_text())

    def test_validation_alert_waits_when_the_performance_gate_is_not_ready(self):
        self.save_state()
        with patch("live_runner.urllib.request.urlopen", side_effect=self.deliver):
            result = live_runner.check_validation_ready_alert(str(self.state_file), DASHBOARD)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["samples"], 100)
        self.assertEqual(self.messages, [])
        self.assertFalse(self.marker.exists())

    def test_failed_delivery_remains_retryable_and_never_logs_the_webhook(self):
        self.save_state()
        error = urllib.error.HTTPError(WEBHOOK, 500, "Unavailable", {}, None)
        with patch("live_runner.urllib.request.urlopen", side_effect=error):
            failed = self.check(str(self.state_file), DASHBOARD)
        self.assertEqual(failed["status"], "error")
        self.assertNotIn("test-secret", json.dumps(failed))
        self.assertFalse(self.marker.exists())
        with patch("live_runner.urllib.request.urlopen", side_effect=self.deliver):
            retried = self.check(str(self.state_file), DASHBOARD)
        self.assertEqual(retried["status"], "sent")

    def test_unconfirmed_delivery_does_not_consume_the_notification(self):
        self.save_state()
        with patch("live_runner.urllib.request.urlopen", return_value=Reply(b'{}')):
            result = self.check(str(self.state_file), DASHBOARD)
        self.assertEqual(result["status"], "error")
        self.assertFalse(self.marker.exists())

    def test_malformed_discord_response_cannot_crash_the_collector(self):
        self.save_state()
        with patch("live_runner.urllib.request.urlopen", return_value=Reply(b'[]')):
            try:
                result = self.check(str(self.state_file), DASHBOARD)
            except Exception:
                self.fail("A malformed Discord response must not escape into the collector loop")
        self.assertEqual(result["status"], "error")
        self.assertFalse(self.marker.exists())

    def test_missing_webhook_is_disabled_without_changing_state(self):
        self.save_state()
        before = self.state_file.read_bytes()
        with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": ""}):
            result = self.check(str(self.state_file), DASHBOARD)
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_corrupt_delivery_record_cannot_resend_a_previously_sent_alert(self):
        self.save_state()
        self.marker.write_text("{broken")
        with patch("live_runner.urllib.request.urlopen", side_effect=self.deliver):
            result = self.check(str(self.state_file), DASHBOARD)
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.messages, [])
        self.assertEqual(self.marker.read_text(), "{broken")

    def test_non_discord_webhook_cannot_receive_the_alert(self):
        self.save_state()
        with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://example.com/secret"}), \
                patch("live_runner.urllib.request.urlopen", side_effect=self.deliver):
            result = self.check(str(self.state_file), DASHBOARD)
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.messages, [])
        self.assertFalse(self.marker.exists())

    def test_runner_checks_the_milestone_between_forecast_boundaries(self):
        self.save_state()
        args = argparse.Namespace(forever=True, hours=1, poll_seconds=30,
                                  state_file=str(self.state_file),
                                  log_file=str(Path(self.directory.name) / "live.jsonl"),
                                  dashboard_url="http://127.0.0.1:8765", lookback_minutes=1440,
                                  kalshi_series="KXBTC15M")
        with patch("live_runner.time.time", return_value=NOW), \
                patch("live_runner.urllib.request.urlopen", side_effect=self.deliver), \
                redirect_stdout(io.StringIO()):
            result = live_runner.run_session(args, OnePollStop())
        rows = [json.loads(line) for line in Path(args.log_file).read_text().splitlines()]
        heartbeat = next(row for row in rows if row["type"] == "heartbeat")
        self.assertEqual(result, 0)
        self.assertFalse(heartbeat["prediction_due"])
        self.assertEqual(heartbeat["milestone_alert"]["status"], "sent")
        self.assertEqual(heartbeat["validation_alert"]["status"], "waiting")
        self.assertEqual(len(self.messages), 1)


if __name__ == "__main__":
    unittest.main()

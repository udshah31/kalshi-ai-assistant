"""Exercise the real collector -> read-only evaluator -> display summary path."""
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from btc_predictor import (
    OnlineLogisticRegression, WindowBar, archive_and_guard,
    finalize_prediction, new_state, predict_and_queue,
)
from forecast_archive import (
    archive_path, read_validation_summary, record_cycle, validation_paths,
)
from live_runner import current_prediction_cycle, initial_prediction_cycle, recommendation_needs_retry, run_validation
from validation import evaluate_archive, report_summary

START = 1800000000
NOW = START+60
BAR = START-900
FEATURES = [1,1,1,1,.2,0,.5]


def snapshot():
    return {"ticker":"KXBTC15M-test", "status":"active", "open_time":START,
            "close_time":START+900, "observed_at":NOW-1, "quote_source":"kalshi_orderbook",
            "yes_bid":.48,"yes_ask":.50,"yes_ask_size":100,
            "no_bid":.50,"no_ask":.52,"no_ask_size":100,"yes_mid":.49}


class ValidationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_file = Path(self.directory.name)/"nested"/"state.json"
        self.archive = archive_path(self.state_file)
        self.state = new_state(OnlineLogisticRegression(7))
        self.state["pending"] = {"bar_timestamp":BAR,"probability_up":.8,"features":FEATURES,
                                 "forecast_issued_at":NOW,"validation_eligible":True,
                                 "outcome_source":"kalshi_official","market_ticker":"KXBTC15M-test",
                                 "market_close_time":START+900,"market_snapshot":snapshot()}

    def test_runner_does_not_create_late_startup_forecast(self):
        cycle, elapsed = current_prediction_cycle(START + 300)
        self.assertEqual(cycle, START // 900)
        self.assertEqual(elapsed, 300)
        self.assertEqual(initial_prediction_cycle(START + 300), cycle)
        self.assertIsNone(initial_prediction_cycle(START + 60))

    def test_runner_real_offline_subprocess_exports_summary_without_state_edits(self):
        record_cycle(self.archive, self.state, now=NOW)
        self.state_file.write_text(json.dumps(self.state))
        before = self.state_file.read_bytes()
        result = run_validation(str(self.state_file))
        self.assertEqual(result["status"], "ok", result)
        full_file, summary_file = validation_paths(self.state_file)
        full, summary = json.loads(full_file.read_text()), read_validation_summary(self.state_file)
        self.assertEqual(summary["status"], "insufficient_data")
        self.assertEqual(summary["forward"]["scored_count"], 0)
        self.assertEqual(full["walk_forward"]["fold_count"], 0)
        self.assertEqual(summary["paper_account"]["status"], "no_trades")
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertFalse(summary["calibration_applied_live"])
        self.assertNotIn("folds", summary["walk_forward"])
        self.assertNotIn("predictions", summary["forward"])
        self.assertEqual(summary_file.resolve(), Path(result["summary_file"]).resolve())

    def test_pending_unavailable_or_malformed_summary_never_implies_validation(self):
        self.assertEqual(read_validation_summary(self.state_file)["status"], "pending")
        _, path = validation_paths(self.state_file)
        path.parent.mkdir(parents=True); path.write_text('["not-a-report"]')
        self.assertEqual(read_validation_summary(self.state_file)["status"], "unavailable")
        path.write_text('{incomplete')
        self.assertEqual(read_validation_summary(self.state_file)["status"], "unavailable")
        path.write_text(' '*(256*1024+1))
        self.assertEqual(read_validation_summary(self.state_file)["status"], "unavailable")

    def test_offline_subprocess_failure_has_no_fake_report(self):
        for failure in (subprocess.TimeoutExpired("validation", 120), OSError("test")):
            with patch("live_runner.subprocess.run", side_effect=failure):
                result = run_validation(str(self.state_file))
            self.assertEqual(result["status"], "error")
        self.assertFalse(validation_paths(self.state_file)[0].exists())
        self.assertFalse(self.archive.exists())

    def test_real_entry_archiving_and_exact_result_have_cost_aware_audit(self):
        model = OnlineLogisticRegression(7)
        state = new_state(model); state["last_learned_bar"] = BAR
        state["paper_trades"] = [{"market_ticker":f"old-{i}","outcome_source":"kalshi_official",
                                  "probability_up":.8,"correct":True,"outcome":"UP"} for i in range(100)]
        with patch("btc_predictor.feature_vector", return_value=FEATURES), patch.object(model, "predict_proba", return_value=.8):
            pred = predict_and_queue(model, state, [WindowBar(BAR,100,101,99,100,1)], snapshot(), now=NOW)
        pred = finalize_prediction(pred, state, {"enabled":False,"status":"disabled"}, now=NOW)
        self.assertEqual(pred["trade_signal"], "TRADE")
        archive_and_guard(self.state_file, state, [], pred, now=NOW)
        pending = state["pending"]
        state["paper_trades"].append({**pending, "outcome":"UP","settled_at":START+910,
                                      "official_result_snapshot":{"ticker":snapshot()["ticker"],"close_time":START+900,"result":"yes"}})
        state["pending"] = None
        record_cycle(self.archive, state, now=START+920)
        report = evaluate_archive(self.archive)
        account = report["paper_account"]
        self.assertEqual(account["status"], "recorded_entries_audited", account)
        self.assertEqual(account["settled_count"], 1)
        self.assertAlmostEqual(account["net_realized_pnl"], pending["demo_quantity"]-pending["demo_cost"])
        self.assertEqual(report["forward"]["scored_count"], 1)
        self.assertEqual(report["forward"]["status"], "insufficient_data")
        self.assertEqual(report["walk_forward"]["fold_count"], 0)

    def test_output_summary_matches_full_report_and_stays_compact(self):
        record_cycle(self.archive, self.state, now=NOW)
        report = evaluate_archive(self.archive)
        summary = report_summary(report)
        self.assertEqual(summary["forward"]["metrics"], report["forward"]["metrics"])
        self.assertNotIn("resolved_equity_curve", summary["paper_account"])
        self.assertNotIn("rejected_forecasts", summary["data_quality"])
        self.assertLess(len(json.dumps(summary)), 256*1024)


if __name__ == "__main__":
    unittest.main()

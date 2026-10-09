"""Saved horizon-comparison evidence is visible without running a live cycle."""
import copy
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import forecast_archive
from dashboard import DashboardApp, DashboardHandler


def metrics(count, brier, loss, accuracy):
    return {"count": count, "brier_score": brier, "log_loss": loss,
            "accuracy": accuracy, "reliability_bins": [{"count": count}]}


def report():
    models = {
        "current_15m_45m_3h": metrics(25, .2659, .7253, .32),
        "proposed_15m_30m_1h": metrics(25, .2644, .7225, .40),
        "constant_50_percent": metrics(25, .25, .6931, .40),
    }
    return {
        "schema_version": 1, "generated_at": "2026-10-08T03:41:00+00:00",
        "status": "descriptive_evaluation",
        "configuration": {"train_size": 100, "test_size": 25, "epochs": 5,
                          "current_horizons": [15, 45, 180], "proposed_horizons": [15, 30, 60]},
        "coverage": {"both_feature_sets": 129, "both_feature_sets_with_outcome": 128,
                     "walk_forward_scored": 25, "walk_forward_tested": 25,
                     "walk_forward_unscored": 0, "market_midpoint_walk_forward": 20},
        "models": models,
        "market_midpoint_baseline": metrics(20, .2224, .6361, .60),
        "market_midpoint_same_subset": {
            "current_15m_45m_3h": metrics(20, .26, .72, .35),
            "proposed_15m_30m_1h": metrics(20, .255, .71, .45),
            "constant_50_percent": metrics(20, .25, .6931, .40),
        },
        "folds": [{"status": "descriptive_evaluation", "predictions": ["private-large-detail"]}],
        "minimum_evidence_samples": 100, "additional_scored_samples_needed": 75,
    }


class DashboardContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.state = self.directory / "btc_15m_state.json"
        self.comparison = self.directory / "btc_15m_context_comparison.json"

    def write_report(self, value=None):
        self.comparison.write_text(json.dumps(report() if value is None else value), encoding="utf-8")

    def test_saved_report_exposes_distinct_full_and_quote_matched_metrics(self):
        self.write_report()
        before = self.comparison.read_bytes()
        summary = forecast_archive.read_context_comparison(self.state)
        self.assertEqual(summary["models"]["current_15m_45m_3h"]["count"], 25)
        self.assertEqual(summary["market_midpoint_same_subset"]["current_15m_45m_3h"]["count"], 20)
        self.assertEqual(summary["models"]["proposed_15m_30m_1h"]["brier_score"], .2644)
        self.assertEqual(summary["fold_count"], 1)
        self.assertNotIn("folds", summary)
        self.assertNotIn("reliability_bins", summary["models"]["current_15m_45m_3h"])
        self.assertNotIn("private-large-detail", json.dumps(summary))
        self.assertEqual(self.comparison.read_bytes(), before)
        self.assertFalse(self.state.exists())

    def test_missing_report_is_pending_and_creates_no_evidence(self):
        result = forecast_archive.read_context_comparison(self.state)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(self.comparison.exists())
        self.assertFalse(self.state.exists())

    def test_malformed_nonfinite_and_mismatched_subsets_are_unavailable(self):
        invalid = [[], {"status": "descriptive_evaluation"}]
        for changes in ("nonfinite", "mixed_quote_counts", "wrong_horizons", "missing_timestamp"):
            value = copy.deepcopy(report())
            if changes == "nonfinite":
                value["models"]["proposed_15m_30m_1h"]["brier_score"] = float("nan")
            elif changes == "mixed_quote_counts":
                value["market_midpoint_same_subset"]["current_15m_45m_3h"]["count"] = 25
            elif changes == "wrong_horizons":
                value["configuration"]["proposed_horizons"] = [15, 60, 180]
            else:
                value["generated_at"] = "unknown"
            invalid.append(value)
        for value in invalid:
            with self.subTest(value=value):
                self.write_report(value)
                summary = forecast_archive.read_context_comparison(self.state)
                self.assertEqual(summary["status"], "unavailable")
                self.assertNotIn("models", summary)
                json.dumps(summary, allow_nan=False)

    def test_missing_or_contradictory_display_counts_are_unavailable(self):
        invalid = []
        for key in ("both_feature_sets", "walk_forward_unscored", "walk_forward_tested"):
            value = report()
            value["coverage"].pop(key)
            invalid.append(value)
        for changes in ({"walk_forward_tested": 0, "both_feature_sets": 0},
                        {"walk_forward_unscored": 1}, {"both_feature_sets": 24}):
            value = report()
            value["coverage"].update(changes)
            invalid.append(value)
        for value in invalid:
            with self.subTest(coverage=value["coverage"]):
                self.write_report(value)
                self.assertEqual(forecast_archive.read_context_comparison(self.state)["status"], "unavailable")

    def test_large_multibyte_reports_are_rejected_by_byte_limit(self):
        value = report()
        value["unneeded_details"] = "é" * (4 * 1024 * 1024)
        self.comparison.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        self.assertGreater(self.comparison.stat().st_size, 8 * 1024 * 1024)
        self.assertEqual(forecast_archive.read_context_comparison(self.state)["status"], "unavailable")

    def test_context_api_loads_saved_results_without_fetching_prices_or_touching_state(self):
        self.write_report()
        self.state.write_text("existing-live-state", encoding="utf-8")
        archive = self.directory / "btc_15m_state_archive.sqlite3"
        archive.write_bytes(b"existing-archive-evidence")
        before = {path: path.read_bytes() for path in (self.state, archive, self.comparison)}

        class QuietHandler(DashboardHandler):
            app = DashboardApp(str(self.state), 1440)

            def log_message(self, *unused):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch("dashboard.fetch_candles", side_effect=AssertionError("Offline display fetched live prices")):
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/context-comparison", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    payload = json.load(response)
            self.assertEqual(payload["status"], "descriptive_evaluation")
            self.assertEqual(payload["coverage"]["both_feature_sets"], 129)
            self.assertEqual(payload["models"]["proposed_15m_30m_1h"]["brier_score"], .2644)
            self.assertEqual({path: path.read_bytes() for path in before}, before)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

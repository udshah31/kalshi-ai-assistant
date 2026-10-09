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
from urllib.error import HTTPError

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

    def get_json(self, path):
        class QuietHandler(DashboardHandler):
            app = DashboardApp(str(self.state), 1440)

            def log_message(self, *unused):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=5) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                try:
                    body = error.read()
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError:
                        payload = body.decode("utf-8")
                    return error.code, payload
                finally:
                    error.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

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

    def test_analytics_api_returns_compact_payload_without_network_calls(self):
        expected = {
            "status": "available",
            "summary": {"scored_count": 10},
            "trend": [{"count": 10}],
            "coverage": {"timely": 10},
        }
        with patch("dashboard.read_historical_analytics", return_value=expected) as reader:
            with patch("dashboard.fetch_candles", side_effect=AssertionError("analytics fetched live prices")):
                status, payload = self.get_json("/api/analytics")
        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        reader.assert_called_once_with(self.state)

    def test_forecasts_api_returns_requested_bounded_rows_without_network_calls(self):
        rows = [{"forecast_id": str(index)} for index in range(25)]

        def recent_forecasts(state_file, limit):
            self.assertEqual(state_file, self.state)
            return {"status": "available", "rows": rows[:limit]}

        with patch("dashboard.read_recent_forecasts", side_effect=recent_forecasts) as reader:
            with patch("dashboard.fetch_candles", side_effect=AssertionError("analytics fetched live prices")):
                status, payload = self.get_json("/api/analytics/forecasts?limit=25")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["rows"]), 25)
        self.assertEqual(payload["rows"][0]["forecast_id"], "0")
        reader.assert_called_once_with(self.state, 25)

    def test_invalid_forecast_limits_return_safe_json_400(self):
        for limit in ("0", "26", "not-a-number", "1.5"):
            with self.subTest(limit=limit):
                status, payload = self.get_json(f"/api/analytics/forecasts?limit={limit}")
                self.assertEqual(status, 400)
                self.assertEqual(payload, {"error": "invalid analytics limit"})

    def test_analytics_api_normalizes_sensitive_saved_report_reasons(self):
        with forecast_archive.connect_archive(forecast_archive.archive_path(self.state)):
            pass
        validation_summary = forecast_archive.validation_paths(self.state)[1]
        sensitive_text = (
            "/private/analytics/state.json", "token=synthetic-test-secret",
            "Traceback (most recent call last)",
        )
        for report_status, public_reason in (
            ("pending", "Awaiting historical analytics evidence."),
            ("unavailable", "Historical analytics are unavailable."),
        ):
            with self.subTest(status=report_status):
                validation_summary.write_text(json.dumps({
                    "status": report_status, "reason": "\n".join(sensitive_text),
                }), encoding="utf-8")
                status, payload = self.get_json("/api/analytics")
                self.assertEqual(status, 200)
                self.assertEqual(payload["status"], report_status)
                self.assertFalse(payload["validation_ready"])
                serialized = json.dumps(payload, allow_nan=False)
                for text in sensitive_text:
                    self.assertNotIn(text, serialized)
                self.assertEqual(payload["reason"], public_reason)

    def test_missing_or_malformed_analytics_reports_are_safe_and_read_only(self):
        status, payload = self.get_json("/api/analytics")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "pending")
        status, payload = self.get_json("/api/analytics/forecasts?limit=25")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "pending")
        self.assertFalse(self.state.exists())
        self.assertFalse(forecast_archive.archive_path(self.state).exists())

        with forecast_archive.connect_archive(forecast_archive.archive_path(self.state)) as connection:
            for index in range(3):
                bar_timestamp = 1800000000 + index * 900
                issued_at = bar_timestamp + 960
                connection.execute("""INSERT INTO forecasts (
                    forecast_id, state_id, bar_timestamp, market_ticker,
                    forecast_issued_at, probability_up, validation_eligible,
                    outcome_source, provenance, captured_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""", (
                    f"forecast-{index}", "test-state", bar_timestamp, f"ticker-{index}",
                    issued_at, .6, 1, "kalshi_official", "live_archive", issued_at,
                ))
        self.state.write_bytes(b"existing-live-state")
        validation_summary = forecast_archive.validation_paths(self.state)[1]
        validation_summary.write_bytes(b"{ malformed")
        paths = (self.state, forecast_archive.archive_path(self.state), validation_summary)
        before = {path: path.read_bytes() for path in paths}
        status, payload = self.get_json("/api/analytics")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "unavailable")
        self.assertFalse(payload["validation_ready"])
        self.assertNotIn("traceback", json.dumps(payload).lower())
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        status, payload = self.get_json("/api/analytics/forecasts?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "available")
        self.assertEqual([row["forecast_id"] for row in payload["rows"]],
                         ["forecast-2", "forecast-1"])
        for row in payload["rows"]:
            self.assertEqual(row["probability_up"], .6)
            self.assertEqual(row["timing_status"], "unknown")
            self.assertFalse(row["market_midpoint_available"])
            for key in ("result", "yes_mid", "settlement_available_at", "settlement_delay_seconds"):
                self.assertIsNone(row[key])
        self.assertEqual({path: path.read_bytes() for path in paths}, before)


if __name__ == "__main__":
    unittest.main()

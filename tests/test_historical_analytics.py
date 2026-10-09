import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import forecast_archive
from forecast_archive import (
    archive_path,
    connect_archive,
    read_historical_analytics,
    read_recent_forecasts,
    validation_paths,
)

START = 1800000000


class HistoricalAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_file = Path(self.directory.name) / "state.json"
        self.missing_state_file = Path(self.directory.name) / "missing" / "state.json"
        with connect_archive(archive_path(self.state_file)):
            pass

    def test_recent_forecasts_rejects_invalid_and_oversized_limits(self):
        with self.assertRaises(ValueError):
            read_recent_forecasts(self.state_file, 0)
        with self.assertRaises(ValueError):
            read_recent_forecasts(self.state_file, 26)

    def test_missing_archive_is_pending_without_creating_a_database(self):
        result = read_historical_analytics(self.missing_state_file)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(archive_path(self.missing_state_file).exists())
        self.assertFalse(self.missing_state_file.parent.exists())
        self.assertEqual(read_recent_forecasts(self.missing_state_file)["rows"], [])

    def insert_forecast(self, identity, index=0, *, ticker=None, issued_at=None,
                        probability=.8, eligible=True, source="kalshi_official",
                        result=None, snapshot_changes=None, snapshot_json=None,
                        settlement_changes=None):
        opened = START + index * 900
        issued = opened + 60 if issued_at is None else issued_at
        ticker = f"ticker-{index}" if ticker is None else ticker
        snapshot = {"ticker": ticker, "open_time": opened, "close_time": opened + 900,
                    "observed_at": issued - 1, "yes_bid": .48, "yes_ask": .50, "yes_mid": .49}
        snapshot.update(snapshot_changes or {})
        with connect_archive(archive_path(self.state_file)) as connection:
            connection.execute("INSERT INTO forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                identity, "test-state", opened - 900, ticker, issued, "[1,1,1,1,1,1,1]",
                probability, int(eligible), source,
                json.dumps(snapshot) if snapshot_json is None else snapshot_json,
                None, "live_archive", issued,
            ))
            if result is not None:
                settlement = {"forecast_id": identity, "market_ticker": ticker,
                              "market_close_time": opened + 900, "result": result,
                              "available_at": opened + 905, "outcome_source": "kalshi_official",
                              "snapshot_json": json.dumps({"ticker": ticker, "close_time": opened + 900,
                                                          "result": result}), "provenance": "live_observation"}
                settlement.update(settlement_changes or {})
                connection.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?)", tuple(settlement.values()))

    def test_recent_forecasts_deduplicate_and_preserve_unknowns(self):
        self.insert_forecast("earliest", ticker="duplicate")
        self.insert_forecast("later-settled", ticker="duplicate", issued_at=START + 80, result="yes")
        self.insert_forecast("unresolved", 1)
        self.insert_forecast("late", 2, eligible=False, issued_at=START + 2 * 900 + 121)
        self.insert_forecast("no-midpoint", 3, snapshot_changes={"yes_mid": None})
        self.insert_forecast("proxy", 4, source="coinbase_proxy", result="yes")
        result = read_recent_forecasts(self.state_file)
        self.assertEqual(result["status"], "available")
        rows = result["rows"]
        self.assertEqual([row["forecast_id"] for row in rows], ["no-midpoint", "unresolved", "earliest"])
        self.assertIsNone(rows[-1]["result"])
        self.assertIsNone(rows[1]["result"])
        self.assertIsNone(rows[0]["yes_mid"])
        self.assertFalse(rows[0]["market_midpoint_available"])
        self.assertIsNone(rows[0]["settlement_available_at"])
        self.assertIsNone(rows[0]["settlement_delay_seconds"])
        self.assertEqual(rows[-1]["timing_status"], "timely")
        self.assertEqual(rows[-1]["yes_mid"], .49)
        json.dumps(result, allow_nan=False)

    def test_equal_issue_times_choose_forecast_id_before_result_availability(self):
        self.insert_forecast("z", ticker="duplicate", result="yes")
        self.insert_forecast("a", ticker="duplicate")
        self.assertEqual(read_recent_forecasts(self.state_file, 1)["rows"][0]["forecast_id"], "a")

    def test_late_rows_are_excluded_even_with_an_incorrect_eligibility_flag(self):
        self.insert_forecast("late", issued_at=START + 121)
        self.insert_forecast("timely", ticker="ticker-0", issued_at=START + 120)
        self.assertEqual([row["forecast_id"] for row in read_recent_forecasts(self.state_file)["rows"]], ["timely"])

    def test_missing_or_malformed_snapshot_preserves_unknown_timing_and_midpoint(self):
        for index, snapshot in enumerate(("null", "{}", "{incomplete", "[]")):
            self.insert_forecast(f"unknown-{index}", index, snapshot_json=snapshot)
        rows = read_recent_forecasts(self.state_file)["rows"]
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(row["timing_status"], "unknown")
            self.assertIsNone(row["yes_mid"])
            self.assertFalse(row["market_midpoint_available"])

    def test_missing_midpoint_is_not_reconstructed_from_archived_bid_and_ask(self):
        self.insert_forecast("no-midpoint")
        with connect_archive(archive_path(self.state_file)) as connection:
            raw = connection.execute("SELECT market_snapshot_json FROM forecasts").fetchone()[0]
            snapshot = json.loads(raw)
            snapshot.pop("yes_mid")
            connection.execute("UPDATE forecasts SET market_snapshot_json=?", (json.dumps(snapshot),))
        self.assertIsNone(read_recent_forecasts(self.state_file)["rows"][0]["yes_mid"])

    def test_invalid_probability_quote_and_settlement_never_become_scores(self):
        for index, probability in enumerate((None, float("inf"), -.1, 1.1)):
            self.insert_forecast(f"invalid-{index}", index, probability=probability,
                                 snapshot_changes={"yes_mid": float("inf")}, result="yes",
                                 settlement_changes={"available_at": START + index * 900 + 899})
        rows = read_recent_forecasts(self.state_file)["rows"]
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertIsNone(row["probability_up"])
            self.assertIsNone(row["yes_mid"])
            self.assertIsNone(row["result"])
            self.assertIsNone(row["settlement_delay_seconds"])
        json.dumps(rows, allow_nan=False)

    def test_valid_official_settlement_uses_observation_delay_not_issue_delay(self):
        self.insert_forecast("settled", result="no")
        row = read_recent_forecasts(self.state_file)["rows"][0]
        self.assertEqual(row["result"], "no")
        self.assertEqual(row["settlement_available_at"], START + 905)
        self.assertEqual(row["settlement_delay_seconds"], 5)

    def test_mismatched_official_settlement_remains_unresolved(self):
        self.insert_forecast("mismatch", result="yes",
                             settlement_changes={
                                 "snapshot_json": json.dumps({"ticker": "other", "result": "yes"})})
        row = read_recent_forecasts(self.state_file)["rows"][0]
        self.assertIsNone(row["result"])
        self.assertIsNone(row["settlement_available_at"])
        self.assertIsNone(row["settlement_delay_seconds"])

    def test_both_queries_reject_noninteger_and_out_of_bounds_limits(self):
        for query in (read_recent_forecasts, read_historical_analytics):
            for limit in (0, -1, 26, True, False, 1.0, "25", None):
                with self.subTest(query=query.__name__, limit=limit), self.assertRaisesRegex(ValueError, "integer from 1 to 25"):
                    query(self.missing_state_file, limit)

    def test_missing_tables_and_corrupt_archives_are_unavailable_without_migration(self):
        for contents in (b"", b"not SQLite"):
            path = archive_path(self.missing_state_file)
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(contents)
            for query in (read_recent_forecasts, read_historical_analytics):
                result = query(self.missing_state_file)
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn(str(path), json.dumps(result))
            self.assertEqual(path.read_bytes(), contents)

    def test_queries_do_not_write_state_archive_reports_or_journal_mode(self):
        self.insert_forecast("settled", result="yes")
        self.state_file.write_text("unchanged-live-state")
        before = {path: path.read_bytes() for path in Path(self.directory.name).iterdir() if path.is_file()}
        with patch("forecast_archive.connect_archive", side_effect=AssertionError("writer called")):
            self.assertEqual(read_recent_forecasts(self.state_file)["status"], "available")
            read_historical_analytics(self.state_file)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        with closing(forecast_archive._connect_archive_readonly(self.state_file)) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM forecasts")

    @staticmethod
    def validation_summary(*, generated_at=None, scored_count=35, eligible_count=35):
        generated_at = generated_at or datetime.now(timezone.utc).isoformat()
        metrics = {
            "count": scored_count, "accuracy": .6, "brier_score": .2,
            "accuracy_wilson_95": {"lower": .4, "upper": .75},
        }
        constant = {"count": scored_count, "brier_score": .25}
        return {
            "schema_version": 1, "generated_at": generated_at,
            "status": "insufficient_data", "calibration_applied_live": False,
            "limitations": ["descriptive"],
            "data_quality": {
                "status": "insufficient_data",
                "collection_quality": {
                    "timely_forecast_rows": 30, "excluded_late_forecasts": 5,
                    "excluded_missing_ticker": 2, "missing_feature_vectors": 3,
                    "missing_fresh_market_snapshots": 4, "interval_gap_count": 6,
                    "settlement_delay_seconds": {"median": 5.0, "p95": 8.0, "max": 10.0},
                },
            },
            "forward": {
                "eligible_forecasts": eligible_count, "scored_count": scored_count,
                "unscored_count": eligible_count - scored_count,
                "minimum_evidence_samples": 100,
                "additional_scored_samples_needed": max(0, 100 - scored_count),
                "metrics": metrics,
                "baselines": {"constant_50_percent": constant},
            },
            "walk_forward": {
                "status": "insufficient_data", "scored_count": scored_count,
                "test_count": scored_count, "minimum_evidence_samples": 100,
                "additional_scored_samples_needed": max(0, 100 - scored_count),
            },
        }

    def write_validation_summary(self, value):
        _, path = validation_paths(self.state_file)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_historical_analytics_has_fixed_stride_metrics_and_projection(self):
        for index in range(35):
            self.insert_forecast(f"forecast-{index}", index, probability=.8,
                                 result="yes" if index % 2 == 0 else "no")
        self.write_validation_summary(self.validation_summary())
        result = read_historical_analytics(self.state_file)
        self.assertEqual(result["status"], "available")
        self.assertEqual([point["issued_at"] for point in result["trend"]],
                         [START + index * 900 + 60 for index in (24, 29, 34)])
        self.assertEqual([point["count"] for point in result["trend"]], [25, 25, 25])
        self.assertEqual([point["accuracy"] for point in result["trend"]], [.52, .48, .52])
        for actual, expected in zip((point["brier_score"] for point in result["trend"]),
                                    (.328, .352, .328)):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(result["summary"]["eligible_count"], 35)
        self.assertEqual(result["summary"]["minimum_count"], 100)
        self.assertEqual(result["summary"]["constant_50_brier"], .25)
        self.assertEqual(result["walk_forward"]["scored_count"], 35)
        self.assertEqual(result["walk_forward"]["test_target"], 100)
        self.assertEqual(result["coverage"], {
            "timely": 30, "late": 5, "missing_ticker": 2,
            "missing_features": 3, "missing_fresh_quotes": 4,
            "interval_gaps": 6,
            "settlement_delay_seconds": {"median": 5.0, "p95": 8.0, "max": 10.0},
        })
        self.assertEqual(result["note"], "Descriptive archived evidence; not profitability proof.")
        self.assertEqual(result["freshness"]["status"], "fresh")
        json.dumps(result, allow_nan=False)
        self.assertTrue(all(isinstance(point["issued_at"], float) for point in result["trend"]))

    def test_trend_omits_metrics_when_window_has_fewer_than_ten_known_labels(self):
        for index in range(25):
            self.insert_forecast(f"forecast-{index}", index,
                                 result="yes" if index < 9 else None)
        self.write_validation_summary(self.validation_summary(scored_count=9, eligible_count=25))
        point = read_historical_analytics(self.state_file)["trend"][0]
        self.assertEqual(point["count"], 9)
        self.assertNotIn("accuracy", point)
        self.assertNotIn("brier_score", point)

    def test_missing_validation_report_is_pending_without_fabricated_metrics(self):
        self.insert_forecast("forecast-0", result="yes")
        result = read_historical_analytics(self.state_file)
        self.assertEqual(result["status"], "pending")
        self.assertIsNone(result["summary"]["accuracy"])
        self.assertIsNone(result["coverage"]["timely"])
        self.assertFalse(result["validation_ready"])

    def test_stale_validation_report_is_diagnostic_only_and_explicitly_marked(self):
        self.insert_forecast("forecast-0", result="yes")
        now = datetime.now(timezone.utc).timestamp()
        old = datetime.fromtimestamp(now - forecast_archive.STALE_REPORT_SECONDS - 1, timezone.utc).isoformat()
        self.write_validation_summary(self.validation_summary(generated_at=old, scored_count=1, eligible_count=1))
        with patch("forecast_archive.time.time", return_value=now):
            result = read_historical_analytics(self.state_file)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["freshness"]["status"], "stale")
        self.assertEqual(result["report_freshness"], "stale")
        self.assertGreater(result["report_age_seconds"], forecast_archive.STALE_REPORT_SECONDS)
        self.assertFalse(result["validation_ready"])
        self.assertEqual(result["summary"]["eligible_count"], 1)

    def test_malformed_or_contradictory_validation_report_is_unavailable(self):
        self.insert_forecast("forecast-0", result="yes")
        invalid = self.validation_summary()
        invalid["generated_at"] = "not-a-timestamp"
        self.write_validation_summary(invalid)
        result = read_historical_analytics(self.state_file)
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["validation_ready"])
        self.assertIsNone(result["summary"]["accuracy"])
        invalid["generated_at"] = datetime.now(timezone.utc).isoformat()
        invalid["forward"]["unscored_count"] = 99
        self.write_validation_summary(invalid)
        self.assertEqual(read_historical_analytics(self.state_file)["status"], "unavailable")

    def test_future_validation_report_is_unavailable(self):
        future = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.write_validation_summary(self.validation_summary(generated_at=future))
        self.assertEqual(read_historical_analytics(self.state_file)["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()

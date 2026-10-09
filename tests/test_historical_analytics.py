import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
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
        self.cache_patch = patch("forecast_archive._ARCHIVE_CACHE", OrderedDict())
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
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

    def test_snapshot_conflicts_are_excluded_before_ticker_deduplication_and_scoring(self):
        for index in range(25):
            opened = START + index * 900
            conflict = (
                {"ticker": "other"}, {"ticker": None},
                {"open_time": opened + 1}, {"open_time": None},
                {"close_time": opened + 901},
            )[index % 5]
            self.insert_forecast(f"invalid-{index}", index, probability=.2,
                                 result="yes", snapshot_changes=conflict)
            self.insert_forecast(f"valid-{index}", index, issued_at=opened + 80,
                                 result="yes")
        self.write_validation_summary(self.validation_summary(scored_count=25, eligible_count=25))
        rows = read_recent_forecasts(self.state_file)["rows"]
        self.assertEqual([row["forecast_id"] for row in rows],
                         [f"valid-{index}" for index in reversed(range(25))])
        point = read_historical_analytics(self.state_file)["trend"][0]
        self.assertEqual(point["count"], 25)
        self.assertEqual(point["accuracy"], 1)
        self.assertAlmostEqual(point["brier_score"], .04)

    def test_snapshot_conflicts_without_a_valid_retry_cannot_form_a_trend(self):
        for index in range(25):
            self.insert_forecast(f"invalid-{index}", index, result="yes",
                                 snapshot_changes={"close_time": "invalid"})
        self.assertEqual(read_recent_forecasts(self.state_file)["rows"], [])
        self.assertEqual(read_historical_analytics(self.state_file)["trend"], [])

    def test_missing_optional_snapshot_identity_fields_remain_canonical(self):
        for index, missing in enumerate(("ticker", "open_time", "close_time")):
            opened = START + index * 900
            snapshot = {"ticker": f"ticker-{index}", "open_time": opened,
                        "close_time": opened + 900}
            snapshot.pop(missing)
            self.insert_forecast(f"earliest-{index}", index, result="yes",
                                 snapshot_json=json.dumps(snapshot))
            self.insert_forecast(f"later-{index}", index, issued_at=opened + 80, result="no")
        rows = read_recent_forecasts(self.state_file)["rows"]
        self.assertEqual([row["forecast_id"] for row in rows],
                         ["earliest-2", "earliest-1", "earliest-0"])
        for row in rows:
            self.assertEqual(row["timing_status"], "unknown")
            self.assertIsNone(row["yes_mid"])
            self.assertEqual(row["result"], "yes")

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

    def test_invalid_probabilities_are_excluded_from_canonical_population(self):
        for index, probability in enumerate((None, float("inf"), -.1, 1.1)):
            self.insert_forecast(f"invalid-{index}", index, probability=probability,
                                 snapshot_changes={"yes_mid": float("inf")}, result="yes",
                                 settlement_changes={"available_at": START + index * 900 + 899})
        rows = read_recent_forecasts(self.state_file)["rows"]
        self.assertEqual(rows, [])
        json.dumps(rows, allow_nan=False)

    def test_probability_and_forecast_identity_validation_precedes_dedup_like_evaluator(self):
        from validation import evaluate_archive

        for index, probability in enumerate((None, float("inf"), -.1, 1.1)):
            self.insert_forecast(f"invalid-{index}", index, probability=probability, result="no")
            self.insert_forecast(f"valid-{index}", index, probability=.8,
                                 issued_at=START + index * 900 + 80, result="yes")
        for index, identity in enumerate((None, "", b"invalid-id"), start=4):
            self.insert_forecast(identity, index, result="no")
            self.insert_forecast(f"valid-{index}", index, probability=.8,
                                 issued_at=START + index * 900 + 80, result="yes")
        self.insert_forecast("first-unresolved", 7, snapshot_json="{}")
        self.insert_forecast("later-settled", 7, issued_at=START + 7 * 900 + 80, result="yes")
        rows = read_recent_forecasts(self.state_file)["rows"]
        self.assertEqual([row["forecast_id"] for row in rows],
                         ["first-unresolved", *[f"valid-{i}" for i in reversed(range(7))]])
        self.assertIsNone(rows[0]["result"])
        evaluation = evaluate_archive(archive_path(self.state_file))
        self.assertEqual(evaluation["forward"]["eligible_forecasts"], len(rows))
        self.assertEqual({p["forecast_id"] for p in evaluation["forward"]["predictions"]},
                         {row["forecast_id"] for row in rows if row["result"] is not None})
        self.assertEqual(evaluation["forward"]["metrics"]["accuracy"], 1)

    def test_invalid_quote_and_settlement_remain_unknown_for_valid_forecast(self):
        self.insert_forecast("invalid-evidence", snapshot_changes={"yes_mid": float("inf")},
                             result="yes", settlement_changes={"available_at": START + 899})
        rows = read_recent_forecasts(self.state_file)["rows"]
        self.assertEqual(len(rows), 1)
        for row in rows:
            self.assertEqual(row["probability_up"], .8)
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

    def test_archive_connection_failure_returns_safe_unavailable_payloads(self):
        error = f"private connection failure: {archive_path(self.state_file)}"
        with patch("forecast_archive.sqlite3.connect", side_effect=sqlite3.OperationalError(error)):
            for query in (read_recent_forecasts, read_historical_analytics):
                with self.subTest(query=query.__name__):
                    result = query(self.state_file)
                    self.assertEqual(result["status"], "unavailable")
                    self.assertNotIn(error, json.dumps(result))
                    self.assertNotIn(str(self.state_file.parent), json.dumps(result))
                    if query is read_recent_forecasts:
                        self.assertEqual(result["rows"], [])
                    else:
                        self.assertEqual(result["trend"], [])
                        self.assertIsNone(result["summary"]["accuracy"])
                        self.assertFalse(result["validation_ready"])

    def test_invalid_limits_still_raise_when_archive_connection_fails(self):
        with patch("forecast_archive.sqlite3.connect", side_effect=sqlite3.OperationalError("unavailable")):
            for query in (read_recent_forecasts, read_historical_analytics):
                for limit in (0, 26, True):
                    with self.subTest(query=query.__name__, limit=limit), self.assertRaises(ValueError):
                        query(self.state_file, limit)

    def test_queries_make_no_application_writes_to_evidence(self):
        self.insert_forecast("settled", result="yes")
        self.state_file.write_text("unchanged-live-state")
        # Native SQLite WAL/SHM coordination is permitted; evidence is not writable.
        before = {path: path.read_bytes() for path in (self.state_file, archive_path(self.state_file))}
        with patch("forecast_archive.connect_archive", side_effect=AssertionError("writer called")):
            self.assertEqual(read_recent_forecasts(self.state_file)["status"], "available")
            read_historical_analytics(self.state_file)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        with closing(forecast_archive._connect_archive_readonly(self.state_file)) as connection:
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM forecasts")
            connection.execute("PRAGMA query_only=OFF")
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM forecasts")

    def insert_history(self, count, *, duplicate_ticker=False):
        with connect_archive(archive_path(self.state_file)) as connection:
            for index in range(count):
                opened = START + index * 900
                identity = f"bulk-{index:06d}"
                ticker = "duplicate" if duplicate_ticker else identity
                connection.execute("INSERT INTO forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    identity, "test-state", opened - 900, ticker, opened + 60,
                    None, .8, 1, "kalshi_official", None, None, "live_archive", opened + 60))
                connection.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?)", (
                    identity, ticker, opened + 900, "yes" if index % 2 == 0 else "no",
                    opened + 905, "kalshi_official", None, "live_observation"))

    def test_large_history_streams_bounded_tail_with_global_windows_and_limit_parity(self):
        self.insert_history(5007)
        self.insert_forecast("late-retry", 5008, ticker="bulk-000000", result="no")
        self.write_validation_summary(self.validation_summary(scored_count=5007, eligible_count=5007))
        real_connect = sqlite3.connect

        class StreamingCursor(sqlite3.Cursor):
            def fetchall(self):
                raise AssertionError("history must be streamed, not fetched all at once")

        class StreamingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                return self.cursor(factory=StreamingCursor).execute(sql, parameters)

        def connect(*args, **kwargs):
            return real_connect(*args, **kwargs, factory=StreamingConnection)

        with patch("forecast_archive.sqlite3.connect", side_effect=connect), patch(
                "forecast_archive._rolling_trend", wraps=forecast_archive._rolling_trend) as rolling:
            full = read_historical_analytics(self.state_file)
            self.assertEqual(full["status"], "available")
            self.assertEqual([p["issued_at"] for p in full["trend"]],
                             [START + i * 900 + 60 for i in range(4884, 5005, 5)])
            self.assertEqual([p["accuracy"] for p in full["trend"]], [.52, .48] * 12 + [.52])
            self.assertLessEqual(len(rolling.call_args.args[0]), 149)
            for limit in (1, 2, 25):
                self.assertEqual(read_historical_analytics(self.state_file, limit)["trend"], full["trend"][-limit:])
                rows = read_recent_forecasts(self.state_file, limit)["rows"]
                self.assertEqual([r["forecast_id"] for r in rows],
                                 [f"bulk-{i:06d}" for i in range(5006, 5006 - limit, -1)])

    def test_resource_exhaustion_never_returns_partial_rows_or_metrics(self):
        self.insert_history(40)
        self.write_validation_summary(self.validation_summary(scored_count=40, eligible_count=40))
        for name, value in (("ARCHIVE_MAX_ROWS", 30), ("ARCHIVE_MAX_TICKERS", 30),
                            ("ARCHIVE_MAX_BYTES", 100), ("ARCHIVE_SCAN_SECONDS", 0),
                            ("ARCHIVE_MAX_VM_STEPS", 1)):
            with self.subTest(budget=name), patch.object(forecast_archive, name, value):
                forecast_archive._ARCHIVE_CACHE.clear()
                for query in (read_recent_forecasts, read_historical_analytics):
                    result = query(self.state_file, 1)
                    self.assertEqual(result["status"], "unavailable")
                    self.assertEqual(result.get("rows", result.get("trend")), [])
                    if "summary" in result:
                        self.assertTrue(all(v is None for v in result["summary"].values()))
                        self.assertFalse(result["validation_ready"])
                    self.assertNotIn(str(self.state_file.parent), json.dumps(result, allow_nan=False))

    def test_duplicate_rows_still_exhaust_scan_budget(self):
        self.insert_history(40, duplicate_ticker=True)
        with patch("forecast_archive.ARCHIVE_MAX_ROWS", 30):
            self.assertEqual(read_recent_forecasts(self.state_file, 1)["status"], "unavailable")

    def test_sql_sort_work_is_interrupted_before_materialization(self):
        self.insert_history(1000)
        with connect_archive(archive_path(self.state_file)) as connection:
            connection.execute("DROP INDEX forecasts_time")
        with patch("forecast_archive.ARCHIVE_MAX_VM_STEPS", 1000), patch(
                "forecast_archive._snapshot_conflicts", wraps=forecast_archive._snapshot_conflicts) as decoded:
            self.assertEqual(read_recent_forecasts(self.state_file, 1)["status"], "unavailable")
            self.assertEqual(decoded.call_count, 0)

    def test_oversized_sql_record_fails_closed(self):
        self.insert_forecast("oversized", snapshot_json=json.dumps({"extra": "x" * (70 * 1024)}))
        self.assertEqual(read_recent_forecasts(self.state_file)["status"], "unavailable")

    def test_repeated_and_concurrent_endpoints_share_one_scan(self):
        self.insert_history(35)
        self.write_validation_summary(self.validation_summary())
        from dashboard import DashboardApp
        app = DashboardApp(str(self.state_file), 1440)
        barrier = threading.Barrier(8)

        def query(index):
            barrier.wait(timeout=3)
            return app.analytics() if index % 2 else app.recent_forecasts(1)

        with patch("forecast_archive._canonical_archive_rows", wraps=forecast_archive._canonical_archive_rows) as scan:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(query, range(8)))
            self.assertTrue(all(r["status"] == "available" for r in results))
            # Caller mutation must not poison another response or endpoint.
            rows = app.recent_forecasts(25)["rows"]
            rows[0]["probability_up"] = 0
            self.assertEqual(app.recent_forecasts(1)["rows"][0]["probability_up"], .8)
            app.analytics()["trend"].clear()
            self.assertEqual(len(app.analytics()["trend"]), 3)
            self.assertEqual(scan.call_count, 1)

    def test_failed_scan_is_reused_until_cache_expiry(self):
        self.insert_history(40)
        with patch("forecast_archive.ARCHIVE_MAX_ROWS", 30), patch(
                "forecast_archive._canonical_archive_rows", wraps=forecast_archive._canonical_archive_rows) as scan:
            for query in (read_recent_forecasts, read_historical_analytics, read_recent_forecasts):
                self.assertEqual(query(self.state_file)["status"], "unavailable")
            self.assertEqual(scan.call_count, 1)

    def test_cache_expires_and_does_not_cache_report_projection(self):
        self.insert_forecast("original")
        self.write_validation_summary(self.validation_summary(scored_count=0, eligible_count=1))
        with patch("forecast_archive._canonical_archive_rows", wraps=forecast_archive._canonical_archive_rows) as scan:
            self.assertEqual(read_historical_analytics(self.state_file)["status"], "available")
            self.write_validation_summary({"status": "unavailable"})
            self.assertEqual(read_historical_analytics(self.state_file)["status"], "unavailable")
            self.assertEqual(scan.call_count, 1)
            clock = time.monotonic()
            with patch("forecast_archive.time.monotonic", return_value=clock + 11):
                self.assertEqual(read_recent_forecasts(self.state_file)["status"], "available")
            self.assertEqual(scan.call_count, 2)

    def test_cache_is_capped_across_source_paths(self):
        self.insert_forecast("original")
        with patch("forecast_archive._canonical_archive_rows", wraps=forecast_archive._canonical_archive_rows) as scan:
            read_recent_forecasts(self.state_file)
            for index in range(4):
                other = Path(self.directory.name) / f"other-{index}.json"
                with connect_archive(archive_path(other)):
                    pass
                self.assertEqual(read_recent_forecasts(other)["status"], "available")
            read_recent_forecasts(self.state_file)
            self.assertEqual(scan.call_count, 6)

    def test_wal_commits_invalidate_cache_and_remain_visible_without_checkpoint(self):
        self.insert_forecast("original")
        path = archive_path(self.state_file)
        with connect_archive(path) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            before = path.read_bytes()
            self.assertEqual(read_recent_forecasts(self.state_file)["rows"][0]["probability_up"], .8)
            writer.execute("UPDATE forecasts SET probability_up=.6")
            writer.commit()
            self.assertGreater(Path(str(path) + "-wal").stat().st_size, 0)
            self.assertEqual(path.read_bytes(), before)  # committed change lives only in WAL
            self.assertEqual(read_recent_forecasts(self.state_file)["rows"][0]["probability_up"], .6)
            # Checkpoint/reset also changes the evidence identity.
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            writer.execute("UPDATE forecasts SET probability_up=.4")
            writer.commit()
            self.assertEqual(read_recent_forecasts(self.state_file)["rows"][0]["probability_up"], .4)

    def test_same_size_wal_reuse_invalidates_cached_evidence(self):
        self.insert_forecast("original")
        path = archive_path(self.state_file)
        with connect_archive(path) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE forecasts SET probability_up=.6")
            writer.commit()
            self.assertEqual(read_recent_forecasts(self.state_file)["rows"][0]["probability_up"], .6)
            wal = Path(str(path) + "-wal")
            before_size = wal.stat().st_size
            writer.execute("PRAGMA wal_checkpoint(RESTART)")
            writer.execute("UPDATE forecasts SET probability_up=.4")
            writer.commit()
            self.assertEqual(wal.stat().st_size, before_size)
            self.assertEqual(read_recent_forecasts(self.state_file)["rows"][0]["probability_up"], .4)

    def test_busy_database_has_finite_wait_and_safe_failure(self):
        self.insert_forecast("original")
        with closing(forecast_archive._connect_archive_readonly(self.state_file)) as reader:
            timeout = reader.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 250)
        # DELETE mode is only for this synthetic fixture to obtain an exclusive lock.
        with closing(sqlite3.connect(archive_path(self.state_file))) as writer:
            writer.execute("PRAGMA journal_mode=DELETE")
            writer.execute("BEGIN EXCLUSIVE")
            started = time.monotonic()
            self.assertEqual(read_recent_forecasts(self.state_file)["status"], "unavailable")
            self.assertLess(time.monotonic() - started, 1)

    def test_scan_contention_is_bounded_and_independent_of_live_prediction_lock(self):
        from dashboard import DashboardApp
        self.insert_history(35)
        self.write_validation_summary(self.validation_summary())
        app = DashboardApp(str(self.state_file), 1440)
        started, release = threading.Event(), threading.Event()
        original_scan = forecast_archive._canonical_archive_rows

        def delayed_scan(connection):
            started.set()
            self.assertTrue(release.wait(3))
            return original_scan(connection)

        with ThreadPoolExecutor(max_workers=1) as pool, patch(
                "forecast_archive._canonical_archive_rows", side_effect=delayed_scan) as scan:
            future = pool.submit(app.analytics)
            try:
                self.assertTrue(started.wait(3))
                self.assertTrue(app._lock.acquire(timeout=.1))
                app._lock.release()
                before = time.monotonic()
                self.assertEqual(app.recent_forecasts(1)["status"], "unavailable")
                self.assertLess(time.monotonic() - before, .75)
                self.assertEqual(scan.call_count, 1)
                # Cached live status still works while analytics is in flight.
                app._cache = (time.time(), {"ok": True, "synthetic": True})
                self.assertEqual(app.status(), {"ok": True, "synthetic": True})
            finally:
                release.set()
            self.assertEqual(future.result(timeout=3)["status"], "available")
        # A live status lock held by another thread cannot block either reader.
        with app._lock, ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(app.analytics).result(timeout=1)["status"], "available")
            self.assertEqual(pool.submit(app.recent_forecasts, 1).result(timeout=1)["status"], "available")

    def test_source_replacement_and_deletion_invalidate_cache(self):
        self.insert_forecast("original")
        self.assertEqual(read_recent_forecasts(self.state_file)["rows"][0]["forecast_id"], "original")
        replacement = Path(self.directory.name) / "replacement.sqlite3"
        with connect_archive(replacement):
            pass
        replacement.replace(archive_path(self.state_file))
        self.assertEqual(read_recent_forecasts(self.state_file)["rows"], [])
        archive_path(self.state_file).unlink()
        self.assertEqual(read_recent_forecasts(self.state_file)["status"], "pending")

    def test_changed_source_during_scan_is_discarded_before_reuse(self):
        self.insert_forecast("original")
        scanned, release = threading.Event(), threading.Event()
        original_scan = forecast_archive._canonical_archive_rows

        def delayed_scan(connection):
            result = original_scan(connection)
            scanned.set()
            self.assertTrue(release.wait(3))
            return result

        with ThreadPoolExecutor(max_workers=1) as pool, patch(
                "forecast_archive._canonical_archive_rows", side_effect=delayed_scan):
            future = pool.submit(read_recent_forecasts, self.state_file)
            try:
                self.assertTrue(scanned.wait(3))
                self.insert_forecast("new", 1)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=3)["status"], "unavailable")
        self.assertEqual([r["forecast_id"] for r in read_recent_forecasts(self.state_file)["rows"]],
                         ["new", "original"])

    @staticmethod
    def validation_summary(*, generated_at=None, scored_count=35, eligible_count=35,
                           walk_scored_count=None):
        generated_at = generated_at or datetime.now(timezone.utc).isoformat()
        walk_scored_count = scored_count if walk_scored_count is None else walk_scored_count
        status = "insufficient_data" if scored_count < 100 else "descriptive_evaluation"
        metrics = {
            "count": scored_count, "accuracy": .6 if scored_count else None,
            "brier_score": .2 if scored_count else None,
            "accuracy_wilson_95": {"lower": .4, "upper": .75} if scored_count else None,
        }
        constant = {"count": scored_count, "brier_score": .25 if scored_count else None}
        return {
            "schema_version": 1, "generated_at": generated_at,
            "status": status, "calibration_applied_live": False,
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
                "status": "insufficient_data" if walk_scored_count < 100 else "descriptive_evaluation",
                "scored_count": walk_scored_count,
                "test_count": walk_scored_count, "minimum_evidence_samples": 100,
                "additional_scored_samples_needed": max(0, 100 - walk_scored_count),
            },
        }

    def write_validation_summary(self, value):
        _, path = validation_paths(self.state_file)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_historical_analytics_has_fixed_stride_metrics_and_projection(self):
        for index in range(35):
            self.insert_forecast(f"forecast-{index}", index, probability=.8,
                                 result="yes" if index % 2 == 0 else "no")
        self.write_validation_summary(self.validation_summary(
            scored_count=30, eligible_count=35, walk_scored_count=12))
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
        self.assertEqual(result["summary"].get("scored_count"), 30)
        self.assertEqual(result["summary"]["minimum_count"], 100)
        self.assertEqual(result["summary"]["constant_50_brier"], .25)
        self.assertEqual(result["walk_forward"]["scored_count"], 12)
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

    @unittest.skipUnless(shutil.which("node"), "Node.js required for executable dashboard renderer tests")
    def test_dashboard_renderer_and_polling_against_archive_payloads(self):
        from dashboard import HTML

        for index in range(35):
            self.insert_forecast(f"forecast-{index}", index, probability=.8,
                                 result="yes" if index % 2 == 0 else "no")
        self.write_validation_summary(self.validation_summary(
            scored_count=30, eligible_count=35, walk_scored_count=12))
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("test_dashboard_analytics.js"))],
            input=json.dumps({"html": HTML, "analytics": read_historical_analytics(self.state_file),
                              "forecasts": read_recent_forecasts(self.state_file)}),
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("renderer and polling checks passed", result.stdout)

    def test_historical_limit_keeps_newest_points_after_full_window_calculation(self):
        # 157 rows produce 27 complete windows, with two rows after the last stride.
        for index in range(157):
            self.insert_forecast(f"forecast-{index}", index,
                                 result="yes" if index % 2 == 0 else "no")
        self.write_validation_summary(self.validation_summary(scored_count=157, eligible_count=157))
        trend = read_historical_analytics(self.state_file)["trend"]
        self.assertEqual(len(trend), 25)
        self.assertEqual([point["issued_at"] for point in trend],
                         [START + index * 900 + 60 for index in range(34, 155, 5)])
        for limit, indices, accuracies in ((1, (154,), (.52,)), (2, (149, 154), (.48, .52))):
            with self.subTest(limit=limit):
                result = read_historical_analytics(self.state_file, limit=limit)
                self.assertEqual(result["status"], "available")
                self.assertEqual([point["issued_at"] for point in result["trend"]],
                                 [START + index * 900 + 60 for index in indices])
                self.assertEqual([point["count"] for point in result["trend"]], [25] * limit)
                self.assertEqual([point["accuracy"] for point in result["trend"]], list(accuracies))
                self.assertAlmostEqual(result["trend"][-1]["brier_score"], .328)

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
        self.assertIn("scored_count", result["summary"])
        self.assertIsNone(result["summary"]["scored_count"])
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
        self.assertEqual(result["summary"].get("scored_count"), 1)

    def test_malformed_or_contradictory_validation_report_is_unavailable(self):
        self.insert_forecast("forecast-0", result="yes")
        invalid = self.validation_summary()
        invalid["generated_at"] = "not-a-timestamp"
        self.write_validation_summary(invalid)
        result = read_historical_analytics(self.state_file)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("scored_count", result["summary"])
        self.assertIsNone(result["summary"]["scored_count"])
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

    def test_impossible_constant_50_brier_is_unavailable(self):
        invalid = self.validation_summary()
        invalid["forward"]["baselines"]["constant_50_percent"]["brier_score"] = .9
        self.write_validation_summary(invalid)
        result = read_historical_analytics(self.state_file)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["summary"]["constant_50_brier"])

    def test_walk_forward_counts_cannot_exceed_forward_population(self):
        for eligible, walk_scored, walk_tested in ((35, 35, 36), (35, 36, 36), (40, 41, 41)):
            with self.subTest(eligible=eligible, scored=walk_scored, tested=walk_tested):
                invalid = self.validation_summary(eligible_count=eligible)
                invalid["walk_forward"].update({
                    "scored_count": walk_scored, "test_count": walk_tested,
                    "additional_scored_samples_needed": 100 - walk_scored,
                })
                self.write_validation_summary(invalid)
                result = read_historical_analytics(self.state_file)
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["walk_forward"]["scored_count"])

    def test_walk_forward_status_must_match_minimum_scored_target(self):
        for scored, status in ((99, "descriptive_evaluation"), (100, "insufficient_data")):
            with self.subTest(scored=scored, status=status):
                invalid = self.validation_summary(scored_count=scored, eligible_count=125)
                invalid["walk_forward"]["status"] = status
                self.write_validation_summary(invalid)
                result = read_historical_analytics(self.state_file)
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["walk_forward"]["status"])

    def test_report_invariants_allow_empty_and_target_boundary_populations(self):
        for scored, status in ((0, "insufficient_data"), (99, "insufficient_data"),
                               (100, "descriptive_evaluation")):
            with self.subTest(scored=scored):
                report = self.validation_summary(scored_count=scored, eligible_count=125)
                report["walk_forward"]["test_count"] = 125
                self.write_validation_summary(report)
                result = read_historical_analytics(self.state_file)
                self.assertEqual(result["status"], "available")
                self.assertEqual(result["summary"].get("scored_count"), scored)
                self.assertEqual(result["walk_forward"]["status"], status)
                self.assertEqual(result["walk_forward"]["scored_count"], scored)
                self.assertEqual(result["summary"]["constant_50_brier"], .25 if scored else None)


if __name__ == "__main__":
    unittest.main()

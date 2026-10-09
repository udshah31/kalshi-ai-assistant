"""Official forecasts freeze only with timely, aligned, fresh quote evidence."""
import copy
import io
import json
import math
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from btc_predictor import (
    OnlineLogisticRegression, WindowBar, archive_and_guard, command_run,
    finalize_prediction, load_state, new_state, predict_and_queue, save_state,
)
from dashboard import DashboardApp
from forecast_archive import archive_path, record_cycle
from live_runner import recommendation_needs_retry, run_session
from validation import evaluate_archive

START = 1800000000
BAR = START-900


def market(now):
    return {"ticker": "KXBTC15M-timing", "status": "active",
            "open_time": START, "close_time": START+900, "observed_at": now-1,
            "quote_source": "kalshi_orderbook", "yes_bid": .48, "yes_ask": .50,
            "yes_mid": .49, "no_bid": .50, "no_ask": .52,
            "yes_ask_size": 100, "no_ask_size": 100}


class ForecastTimingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/"state.json"
        self.archive = archive_path(self.path)
        self.model = OnlineLogisticRegression(7)
        self.model.bias = math.log(4)
        self.state = new_state(self.model)
        self.state["last_learned_bar"] = BAR
        self.state["paper_trades"] = [
            {"market_ticker": f"history-{i}", "outcome_source": "kalshi_official",
             "probability_up": .8, "outcome": "UP", "correct": True} for i in range(100)
        ]
        self.bars = [WindowBar(BAR-(21-i)*900, 100+i, 101+i, 99+i, 100+i, 100)
                     for i in range(22)]

    def predict(self, now, quotes):
        return predict_and_queue(self.model, self.state, self.bars, quotes, now=now)

    def rows(self, table):
        with closing(sqlite3.connect(self.archive)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]

    def test_missing_market_stays_a_preview_without_a_frozen_forecast(self):
        result = self.predict(START+10, None)
        result = finalize_prediction(result, self.state, {"enabled": False}, now=START+10)
        archive_and_guard(self.path, self.state, self.bars, result, now=START+10)
        self.assertIsNone(self.state["pending"])
        self.assertEqual(result["trade_signal"], "WAIT")
        self.assertEqual(result["forecast_status"], "awaiting_market_evidence")
        self.assertIsNone(result["forecast_issued_at"])
        self.assertEqual(self.rows("forecasts"), [])
        self.assertEqual(len(self.rows("decisions")), 1)
        self.assertEqual(self.model.updates, 0)

    def test_timely_retry_freezes_probability_and_quotes_at_the_actual_issue_time(self):
        preview = self.predict(START+10, None)
        archive_and_guard(self.path, self.state, self.bars, preview, now=START+10)
        self.model.bias = math.log(7/3)
        issued = self.predict(START+41, market(START+41))
        record_cycle(self.archive, self.state, prediction=issued, now=START+41)
        pending = self.state["pending"]
        self.assertEqual(pending["forecast_issued_at"], START+41)
        self.assertAlmostEqual(pending["probability_up"], .7)
        self.assertLessEqual(pending["market_snapshot"]["observed_at"], pending["forecast_issued_at"])
        self.assertEqual(issued["forecast_status"], "issued")
        self.assertEqual(len(self.rows("forecasts")), 1)
        self.state["paper_trades"].append({
            **pending, "outcome": "UP", "correct": True, "settled_at": START+905,
            "official_result_snapshot": {"ticker": market(START+41)["ticker"],
                                         "close_time": START+900, "result": "yes"},
        })
        self.state["pending"] = None
        record_cycle(self.archive, self.state, now=START+905)
        report = evaluate_archive(self.archive)
        self.assertEqual(report["forward"]["scored_count"], 1)
        self.assertEqual(report["forward"]["baselines"]["market_midpoint"]["eligible_count"], 1)

    def test_unusable_quotes_cannot_create_an_official_forecast(self):
        for changes in (
            {"quote_source": "market_summary_only"}, {"observed_at": START+9-61},
            {"observed_at": START+11}, {"yes_bid": None}, {"yes_bid": .7},
            {"yes_mid": .9}, {"open_time": START-900, "close_time": START},
        ):
            with self.subTest(changes=changes):
                self.state["pending"] = None
                quotes = market(START+10); quotes.update(changes)
                result = self.predict(START+10, quotes)
                self.assertIsNone(self.state["pending"])
                self.assertEqual(result["trade_signal"], "WAIT")
                self.assertEqual(result["forecast_status"], "awaiting_market_evidence")
                self.assertTrue(recommendation_needs_retry(result, START+10))

    def test_unknown_depth_blocks_trading_but_valid_quotes_can_be_archived(self):
        quotes = market(START+10); quotes["yes_ask_size"] = None
        result = self.predict(START+10, quotes)
        self.assertIsNotNone(self.state["pending"])
        self.assertEqual(result["forecast_status"], "issued")
        self.assertEqual(result["trade_signal"], "WAIT")
        self.assertFalse(result["signal_checks"]["liquidity"])

    def test_expired_entry_window_is_recorded_once_without_training_or_forecast(self):
        self.predict(START+10, None)
        for now in (START+121, START+150):
            result = self.predict(now, market(now))
            archive_and_guard(self.path, self.state, self.bars, result, now=now)
        self.assertIsNone(self.state["pending"])
        self.assertEqual(result["forecast_status"], "missed_window")
        self.assertEqual(result["trade_signal"], "WAIT")
        self.assertFalse(recommendation_needs_retry(result, START+150))
        self.assertEqual(len(self.state["skipped_predictions"]), 1)
        self.assertEqual(self.rows("forecasts"), [])
        self.assertEqual(len(self.rows("skipped_forecasts")), 1)
        self.assertEqual(self.model.updates, 0)

    def test_entry_deadline_is_inclusive_and_next_window_can_resume(self):
        issued = self.predict(START+120, market(START+120))
        self.assertEqual(issued["forecast_status"], "issued")
        self.assertEqual(self.state["pending"]["forecast_issued_at"], START+120)
        self.state["pending"] = None
        self.predict(START+121, None)
        self.bars = [WindowBar(b.timestamp+900, b.open, b.high, b.low, b.close, b.volume)
                     for b in self.bars]
        quotes = market(START+910)
        quotes.update({"ticker": "KXBTC15M-next", "open_time": START+900, "close_time": START+1800})
        resumed = self.predict(START+910, quotes)
        self.assertEqual(resumed["forecast_status"], "issued")
        self.assertEqual(self.state["pending"]["market_ticker"], "KXBTC15M-next")

    def test_existing_issued_forecast_is_not_retimed_or_repriced(self):
        self.predict(START+10, market(START+10))
        original = copy.deepcopy(self.state["pending"])
        self.model.bias = math.log(7/3)
        quotes = market(START+70); quotes.update({"yes_bid": .50, "yes_ask": .52, "yes_mid": .51})
        self.predict(START+70, quotes)
        self.assertEqual(self.state["pending"], original)

    def test_previous_unsettled_forecast_is_preserved(self):
        self.state["pending"] = {"bar_timestamp": BAR-900, "market_ticker": "older-ticker",
                                 "probability_up": .8, "forecast_issued_at": START-850,
                                 "outcome_source": "kalshi_official"}
        original = copy.deepcopy(self.state["pending"])
        result = self.predict(START+10, market(START+10))
        self.assertEqual(self.state["pending"], original)
        self.assertEqual(result["trade_signal"], "WAIT")
        self.assertTrue(recommendation_needs_retry(result, START+10))

    def test_cli_defers_then_persists_the_first_evidence_ready_forecast(self):
        save_state(self.path, self.model, self.state)
        now = [START+10]
        args = SimpleNamespace(state_file=str(self.path), kalshi_series="KXBTC15M", epochs=5)
        with ExitStack() as stack:
            stack.enter_context(patch("btc_predictor.time.time", side_effect=lambda: now[0]))
            stack.enter_context(patch("btc_predictor._load_bars", return_value=self.bars))
            stack.enter_context(patch("btc_predictor.fetch_kalshi_market", side_effect=[None, market(START+41)]))
            for expected, instant in (("awaiting_market_evidence", START+10), ("issued", START+41)):
                now[0] = instant; output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(command_run(args), 0)
                result = json.loads(output.getvalue())
                _, saved = load_state(self.path)
                self.assertEqual(result["prediction"]["forecast_status"], expected)
                if expected == "awaiting_market_evidence":
                    self.assertIsNone(saved["pending"])
                else:
                    self.assertEqual(saved["pending"]["forecast_issued_at"], START+41)

    def test_dashboard_defers_then_persists_the_first_evidence_ready_forecast(self):
        save_state(self.path, self.model, self.state)
        now = [START+10]
        with ExitStack() as stack:
            stack.enter_context(patch("btc_predictor.time.time", side_effect=lambda: now[0]))
            stack.enter_context(patch("dashboard.fetch_candles", return_value=[]))
            stack.enter_context(patch("dashboard.aggregate_candles", return_value=self.bars))
            stack.enter_context(patch("dashboard.fetch_kalshi_market", side_effect=[None, market(START+41)]))
            stack.enter_context(patch("dashboard.fetch_previous_kalshi_market", return_value=None))
            stack.enter_context(patch("dashboard.fetch_spot_price", return_value=None))
            app = DashboardApp(str(self.path), 1440, cache_seconds=0)
            first = app.status(); _, saved = load_state(self.path)
            self.assertTrue(first["ok"], first.get("error"))
            self.assertIsNone(saved["pending"])
            self.assertEqual(first["prediction"]["forecast_status"], "awaiting_market_evidence")
            now[0] = START+41
            second = app.status(); _, saved = load_state(self.path)
            self.assertTrue(second["ok"], second.get("error"))
            self.assertEqual(saved["pending"]["forecast_issued_at"], START+41)
        self.assertEqual(len(self.rows("forecasts")), 1)

    def receipt_timing_inputs(self):
        return [[self.bars[0].timestamp + minute * 60,
                 99 + minute // 15, 101 + minute // 15,
                 100 + minute // 15, 100 + minute // 15, 100 / 15]
                for minute in range(22 * 15)]

    def test_cli_archives_price_receipt_time_before_market_and_review_latency(self):
        save_state(self.path, self.model, self.state)
        now = [START+5]

        def candles(**unused):
            now[0] = START+10
            return self.receipt_timing_inputs()

        def quotes(*unused):
            now[0] = START+20
            return market(now[0])

        def review(*unused):
            now[0] = START+20.003
            return {"enabled": False, "status": "disabled"}

        args = SimpleNamespace(state_file=str(self.path), kalshi_series="KXBTC15M",
                               epochs=5, product_id="BTC-USD", lookback_minutes=1440)
        with patch("btc_predictor.time.time", side_effect=lambda: now[0]), \
             patch("btc_predictor.fetch_candles", side_effect=candles), \
             patch("btc_predictor.fetch_kalshi_market", side_effect=quotes), \
             patch("btc_predictor.typesafe_strategy_review", side_effect=review), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(command_run(args), 0)
        forecast = self.rows("forecasts")[0]
        current = next(row for row in self.rows("bars") if row["timestamp"] == BAR)
        self.assertEqual(current["observed_at"], START+10)
        self.assertEqual(forecast["forecast_issued_at"], START+20)
        self.assertEqual(forecast["captured_at"], START+20.003)
        self.assertLess(current["observed_at"], forecast["forecast_issued_at"])
        self.assertTrue(all(row["observed_at"] == START+10 for row in self.rows("candles")))

    def test_dashboard_archives_price_receipt_time_before_context_latency(self):
        save_state(self.path, self.model, self.state)
        now = [START+5]

        def candles(**unused):
            now[0] = START+10
            return self.receipt_timing_inputs()

        def quotes(*unused):
            now[0] = START+20
            return market(now[0])

        with patch("btc_predictor.time.time", side_effect=lambda: now[0]), \
             patch("dashboard.fetch_candles", side_effect=candles), \
             patch("dashboard.fetch_kalshi_market", side_effect=quotes), \
             patch("dashboard.fetch_previous_kalshi_market", return_value=None), \
             patch("dashboard.fetch_spot_price", return_value=None):
            result = DashboardApp(str(self.path), 1440).status()
        self.assertTrue(result["ok"], result.get("error"))
        forecast = self.rows("forecasts")[0]
        current = next(row for row in self.rows("bars") if row["timestamp"] == BAR)
        self.assertEqual(current["observed_at"], START+10)
        self.assertEqual(forecast["forecast_issued_at"], START+20)
        self.assertLess(current["observed_at"], forecast["forecast_issued_at"])

    def test_runner_retries_preview_and_logs_only_the_evidence_ready_forecast_as_issued(self):
        save_state(self.path, self.model, self.state)
        now = [START+10]
        log = Path(self.directory.name)/"live.jsonl"
        command_args = SimpleNamespace(state_file=str(self.path), kalshi_series="KXBTC15M", epochs=5)
        args = SimpleNamespace(forever=True, hours=1, poll_seconds=30, log_file=str(log),
                               dashboard_url="http://127.0.0.1:8765", state_file=str(self.path),
                               lookback_minutes=1440, kalshi_series="KXBTC15M")

        class TwoPollStop(threading.Event):
            def wait(inner, timeout=None):
                if now[0] == START+10:
                    now[0] = START+41
                else:
                    inner.set()
                return inner.is_set()

        def execute(*unused):
            output = io.StringIO()
            with redirect_stdout(output):
                code = command_run(command_args)
            return {"exit_code": code, "result": json.loads(output.getvalue()), "stderr": ""}

        with ExitStack() as stack:
            stack.enter_context(patch("btc_predictor.time.time", side_effect=lambda: now[0]))
            stack.enter_context(patch("btc_predictor._load_bars", return_value=self.bars))
            stack.enter_context(patch("btc_predictor.fetch_kalshi_market", side_effect=[None, market(START+41)]))
            stack.enter_context(patch("live_runner.run_prediction", side_effect=execute))
            stack.enter_context(patch("live_runner.check_dashboard_health", return_value=(True, '{"ok":true}')))
            stack.enter_context(patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "", "TYPESAFE_API_KEY": ""}))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(run_session(args, TwoPollStop()), 0)
        predictions = [json.loads(line) for line in log.read_text().splitlines()
                       if json.loads(line).get("type") == "prediction"]
        self.assertEqual(len(predictions), 2)
        self.assertFalse(predictions[0]["forecast_issued"])
        self.assertFalse(predictions[0]["prediction_ok"])
        self.assertTrue(predictions[0]["recommendation_retry_requested"])
        self.assertTrue(predictions[1]["forecast_issued"])
        self.assertTrue(predictions[1]["prediction_ok"])
        self.assertTrue(predictions[1]["prediction_fresh"])
        self.assertFalse(predictions[1]["recommendation_retry_requested"])


if __name__ == "__main__":
    unittest.main()

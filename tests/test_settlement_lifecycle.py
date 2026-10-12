"""Finalized labels, durable retry queues, and independent forecast collection."""
import copy
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import btc_predictor as predictor
import forecast_archive
import validation
from dashboard import DashboardApp
from live_runner import recommendation_needs_retry


START = 1800000000
BAR = START - 900
CLOSE = START + 900
TICKER = "KXBTC15M-lifecycle"


def market(start=START, ticker=TICKER):
    return {"ticker": ticker, "status": "active", "open_time": start,
            "close_time": start + 900, "observed_at": start + 9,
            "quote_source": "kalshi_orderbook", "yes_bid": .48, "yes_ask": .50,
            "yes_mid": .49, "yes_ask_size": 100,
            "no_bid": .50, "no_ask": .52, "no_ask_size": 100}


def pending(*, committed=False, start=START, ticker=TICKER):
    return {"bar_timestamp": start - 900, "close": 100., "features": [.1] * 7,
            "probability_up": .7, "demo_side": "UP",
            "trade_signal": "TRADE" if committed else "WAIT",
            "demo_quantity": 10 if committed else 0, "demo_entry_price": .5,
            "demo_cost": 5.3 if committed else 0., "cost_allowance_per_contract": .03,
            "trade_committed_at": start + 10 if committed else None,
            "market_ticker": ticker, "market_close_time": start + 900,
            "forecast_issued_at": start + 10, "validation_eligible": True,
            "outcome_source": "kalshi_official", "market_snapshot": market(start, ticker)}


def result(*, status="finalized", label="yes", start=START, ticker=TICKER):
    return {"ticker": ticker, "open_time": start, "close_time": start + 900,
            "status": status, "result": label, "expiration_value": "101.00",
            "settlement_time": start + 905 if status == "finalized" else None}


class SettlementLifecycleTests(unittest.TestCase):
    def fixture(self, **kwargs):
        model = predictor.OnlineLogisticRegression(7)
        state = predictor.new_state(model)
        state["pending"] = pending(**kwargs)
        return model, state

    def test_archived_ticker_result_uses_the_historical_read_endpoint(self):
        payload = {"market": {"ticker": TICKER, "close_time": CLOSE,
                              "status": "finalized", "result": "yes", "settlement_ts": CLOSE + 5}}
        unavailable = urllib.error.HTTPError("https://example.invalid/market", 404, "archived", {}, None)
        self.addCleanup(unavailable.close)
        with patch.object(predictor.urllib.request, "urlopen", side_effect=[
                unavailable, io.BytesIO(json.dumps(payload).encode())]) as fetch:
            response = predictor.fetch_kalshi_market_by_ticker(TICKER)
        self.assertTrue(predictor.is_finalized_result(response, TICKER, CLOSE, CLOSE + 10))
        self.assertIn("/historical/markets/" + TICKER, fetch.call_args_list[1].args[0])

    def test_transient_api_errors_do_not_probe_the_historical_endpoint(self):
        failure = urllib.error.HTTPError("https://example.invalid/market", 503, "unavailable", {}, None)
        self.addCleanup(failure.close)
        with patch.object(predictor.urllib.request, "urlopen", side_effect=failure) as fetch:
            with self.assertRaises(urllib.error.HTTPError):
                predictor.fetch_kalshi_market_by_ticker(TICKER)
        self.assertEqual(fetch.call_count, 1)

    def test_provisional_disputed_and_amended_results_do_not_teach_or_pay(self):
        for status in ("determined", "disputed", "amended", "closed", None):
            with self.subTest(status=status):
                model, state = self.fixture(committed=True)
                with patch.object(predictor, "fetch_kalshi_market_by_ticker",
                                  return_value=result(status=status)):
                    learned = predictor.learn_from_pending(model, state, [], now=CLOSE + 10)
                self.assertIsNone(learned)
                self.assertEqual(model.updates, 0)
                self.assertEqual(state["demo_account"]["balance"], 100)
                self.assertEqual(state["paper_trades"], [])
                self.assertEqual(state["settlement_queue"][TICKER]["demo_cost"], 5.3)

    def test_finalized_result_needs_a_plausible_settlement_timestamp(self):
        for timestamp in (None, CLOSE - 1, CLOSE + 11, "not-a-timestamp"):
            with self.subTest(timestamp=timestamp):
                model, state = self.fixture()
                response = {**result(), "settlement_time": timestamp}
                with patch.object(predictor, "fetch_kalshi_market_by_ticker", return_value=response):
                    self.assertIsNone(predictor.learn_from_pending(model, state, [], now=CLOSE + 10))
                self.assertEqual(model.updates, 0)

    def test_provisional_yes_then_final_no_updates_and_pays_exactly_once(self):
        model, state = self.fixture(committed=True)
        with patch.object(predictor, "fetch_kalshi_market_by_ticker", return_value=result(status="determined")):
            predictor.learn_from_pending(model, state, [], now=CLOSE + 10)
        with patch.object(predictor, "fetch_kalshi_market_by_ticker", return_value=result(label="no")) as fetch:
            learned = predictor.learn_from_pending(model, state, [], now=CLOSE + 60)
            duplicate = predictor.learn_from_pending(model, state, [], now=CLOSE + 120)
        self.assertEqual(learned["label"], 0)
        self.assertIsNone(duplicate)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(model.updates, 1)
        self.assertEqual(state["demo_account"]["trades"], 1)
        self.assertAlmostEqual(state["demo_account"]["balance"], 94.7)
        self.assertFalse(state["settlement_queue"])
        self.assertEqual(learned["paper_trade"]["exit_timestamp"], CLOSE)
        self.assertEqual(learned["demo_trade"]["exit_timestamp"], CLOSE)

    def test_outage_longer_than_an_hour_keeps_original_evidence_and_recovers(self):
        model, state = self.fixture(committed=True)
        original = copy.deepcopy(state["pending"])
        with patch.object(predictor, "fetch_kalshi_market_by_ticker", side_effect=OSError("offline")):
            self.assertIsNone(predictor.learn_from_pending(model, state, [], now=CLOSE + 3601))
        queued = state["settlement_queue"][TICKER]
        for key in ("probability_up", "features", "forecast_issued_at", "demo_quantity", "demo_cost"):
            self.assertEqual(queued[key], original[key])
        self.assertFalse(state["skipped_predictions"])
        with patch.object(predictor, "fetch_kalshi_market_by_ticker", return_value=result()):
            learned = predictor.learn_from_pending(model, state, [], now=CLOSE + 7200)
        self.assertEqual(learned["label"], 1)
        self.assertEqual(model.updates, 1)
        self.assertAlmostEqual(state["demo_account"]["balance"], 104.7)
        self.assertFalse(state["settlement_queue"])

    def test_legacy_unresolved_entries_reenter_polling_without_double_settlement(self):
        model, state = self.fixture(committed=True)
        record = state.pop("pending")
        state["pending"] = None
        state["unresolved_demo_trades"] = [copy.deepcopy(record), copy.deepcopy(record)]
        with patch.object(predictor, "fetch_kalshi_market_by_ticker", return_value=result()) as fetch:
            learned = predictor.learn_from_pending(model, state, [], now=CLOSE + 7200)
            predictor.learn_from_pending(model, state, [], now=CLOSE + 7300)
        self.assertEqual(learned["label"], 1)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(model.updates, 1)
        self.assertEqual(state["demo_account"]["trades"], 1)
        self.assertFalse(state["unresolved_demo_trades"])

    def test_waiting_results_do_not_prevent_next_forecast_even_with_a_position(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                model, state = self.fixture(committed=committed)
                original = copy.deepcopy(state["pending"])
                new_bar = START
                bars = [predictor.WindowBar(new_bar-(21-i)*900, 100+i, 101+i, 99+i, 100+i, 100)
                        for i in range(22)]
                prediction = predictor.predict_and_queue(
                    model, state, bars, market(CLOSE, "NEXT"), now=CLOSE + 10)
                self.assertEqual(prediction["forecast_status"], "issued")
                self.assertEqual(state["pending"]["market_ticker"], "NEXT")
                self.assertEqual(state["settlement_queue"][TICKER], original)
                self.assertEqual(prediction["signal_checks"]["pending_slot"], not committed)
                if committed:
                    self.assertEqual(prediction["trade_signal"], "WAIT")

    def test_late_old_result_does_not_move_learning_freshness_backwards(self):
        model, state = self.fixture()
        state["last_learned_bar"] = START + 9000
        with patch.object(predictor, "fetch_kalshi_market_by_ticker", return_value=result()):
            predictor.learn_from_pending(model, state, [], now=CLOSE + 10000)
        self.assertEqual(state["last_learned_bar"], START + 9000)

    def test_retries_are_bounded_and_do_not_starve_other_tickers(self):
        model, state = self.fixture()
        state["pending"] = None
        state["settlement_queue"] = {f"old-{i}": pending(start=START-i*900, ticker=f"old-{i}")
                                     for i in range(7)}
        with patch.object(predictor, "fetch_kalshi_market_by_ticker", side_effect=OSError("offline")) as fetch:
            predictor.learn_from_pending(model, state, [], now=CLOSE + 7200)
            first = [call.args[0] for call in fetch.call_args_list]
            self.assertGreater(len(first), 0)
            self.assertLessEqual(len(first), 3)
            fetch.reset_mock()
            predictor.learn_from_pending(model, state, [], now=CLOSE + 7201)
            second = [call.args[0] for call in fetch.call_args_list]
        self.assertTrue(second)
        self.assertFalse(set(first) & set(second))
        self.assertEqual(len(state["settlement_queue"]), 7)

    def test_runner_retries_settlements_after_a_new_forecast_was_issued(self):
        prediction = {"forecast_status": "issued", "settlement_queue_size": 1,
                      "signal_checks": {"pending_slot": True}}
        self.assertTrue(recommendation_needs_retry(prediction, START + 400))

    def test_legacy_provisional_learning_is_reconciled_without_retraining_twice(self):
        model, state = self.fixture(committed=True)
        original = state["pending"]
        original.update({"official_result_snapshot": result(status="determined"),
                         "settled_at": CLOSE + 1, "official_expiration_value": 101})
        model.update(original["features"], 1)  # simulate an old-version premature update
        state["paper_trades"] = [predictor.settle_demo_trade(original, 1, None)]
        predictor.settle_demo_account(state, original, 1, None)
        state["pending"] = None
        with patch.object(predictor, "fetch_kalshi_market_by_ticker", return_value=result(label="no")):
            learned = predictor.learn_from_pending(model, state, [], now=CLOSE + 120)
        self.assertIsNotNone(learned)
        self.assertEqual(model.updates, 1)
        self.assertEqual(len(state["paper_trades"]), 1)
        self.assertEqual(state["paper_trades"][0]["outcome"], "DOWN")
        self.assertEqual(state["demo_account"]["trades"], 1)
        self.assertEqual(state["demo_account"]["wins"], 0)
        self.assertEqual(state["demo_account"]["losses"], 1)
        self.assertAlmostEqual(state["demo_account"]["balance"], 94.7)
        self.assertTrue(state["revised_learning_labels"])

    def test_same_label_reconciliation_does_not_change_weights_or_account_totals(self):
        model, state = self.fixture(committed=True)
        original = state["pending"]
        original.update({"official_result_snapshot": result(status="determined"),
                         "settled_at": CLOSE + 1, "official_expiration_value": 101})
        model.update(original["features"], 1)
        state["paper_trades"] = [predictor.settle_demo_trade(original, 1, None)]
        predictor.settle_demo_account(state, original, 1, None)
        state["pending"] = None
        before_model = model.to_dict()
        before_totals = {key: state["demo_account"][key] for key in
                         ("balance", "realized_pnl", "total_traded", "trades", "wins", "losses")}
        with patch.object(predictor, "fetch_kalshi_market_by_ticker", return_value=result()):
            predictor.learn_from_pending(model, state, [], now=CLOSE + 60)
        self.assertEqual(model.to_dict(), before_model)
        self.assertEqual({key: state["demo_account"][key] for key in before_totals}, before_totals)
        self.assertFalse(state["revised_learning_labels"])
        self.assertEqual(predictor.official_validation(state)["samples"], 1)

    def test_legacy_proxy_entry_without_a_label_is_preserved_as_unresolved(self):
        model, state = self.fixture(committed=True)
        state["pending"].update({"outcome_source": "coinbase_proxy", "market_ticker": None})
        predictor.learn_from_pending(model, state, [], now=CLOSE + 7200)
        self.assertEqual(len(state["unresolved_demo_trades"]), 1)
        self.assertEqual(state["unresolved_demo_trades"][0]["demo_cost"], 5.3)
        skipped = len(state["skipped_predictions"])
        predictor.learn_from_pending(model, state, [], now=CLOSE + 7300)
        self.assertEqual(len(state["skipped_predictions"]), skipped)
        self.assertEqual(model.updates, 0)


class SettlementArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_path = Path(self.directory.name) / "state.json"
        self.path = forecast_archive.archive_path(self.state_path)
        self.model = predictor.OnlineLogisticRegression(7)
        self.state = predictor.new_state(self.model)
        self.state["pending"] = pending()

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    def test_queue_and_each_result_stage_are_archived_before_final_scoring(self):
        forecast_archive.record_cycle(self.path, self.state, now=START + 10)
        original_forecast = self.rows("forecasts")[0]
        with patch.object(predictor, "fetch_kalshi_market_by_ticker", return_value=result(status="determined")):
            predictor.learn_from_pending(self.model, self.state, [], now=CLOSE + 10)
        forecast_archive.record_cycle(self.path, self.state, now=CLOSE + 10)
        self.assertEqual(self.rows("settlements"), [])
        self.assertEqual(len(self.rows("settlement_observations")), 1)
        self.assertEqual(validation.evaluate_archive(self.path)["forward"]["scored_count"], 0)
        with patch.object(predictor, "fetch_kalshi_market_by_ticker", return_value=result(label="no")):
            predictor.learn_from_pending(self.model, self.state, [], now=CLOSE + 60)
        forecast_archive.record_cycle(self.path, self.state, now=CLOSE + 60)
        self.assertEqual(self.rows("forecasts")[0], original_forecast)
        self.assertEqual(len(self.rows("settlement_observations")), 2)
        self.assertEqual(self.rows("settlements")[0]["result"], "no")
        self.assertEqual(self.rows("settlements")[0]["available_at"], CLOSE + 60)
        report = validation.evaluate_archive(self.path)
        self.assertEqual(report["forward"]["scored_count"], 1)
        self.assertEqual(report["forward"]["predictions"][0]["label"], 0)

    def test_legacy_provisional_row_is_preserved_as_observation_then_finalized(self):
        forecast_archive.record_cycle(self.path, self.state, now=START + 10)
        identity = self.rows("forecasts")[0]["forecast_id"]
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?)", (
                identity, TICKER, CLOSE, "yes", CLOSE + 1, "kalshi_official",
                json.dumps(result(status="determined")), "state_import"))
        report = validation.evaluate_archive(self.path)
        self.assertEqual(report["forward"]["scored_count"], 0)
        with patch.object(predictor, "fetch_kalshi_market_by_ticker", return_value=result(label="no")):
            predictor.learn_from_pending(self.model, self.state, [], now=CLOSE + 60)
        forecast_archive.record_cycle(self.path, self.state, now=CLOSE + 60)
        observed = self.rows("settlement_observations")
        self.assertEqual({row["result"] for row in observed}, {"yes", "no"})
        self.assertEqual(self.rows("settlements")[0]["result"], "no")
        self.assertEqual(self.rows("settlements")[0]["available_at"], CLOSE + 60)

    def test_coinbase_outage_does_not_prevent_official_settlement_recovery(self):
        predictor.save_state(self.state_path, self.model, self.state)
        args = SimpleNamespace(state_file=str(self.state_path), kalshi_series="KXBTC15M", epochs=5)
        with patch.object(predictor, "time") as clock, \
                patch.object(predictor, "_load_bars", side_effect=RuntimeError("Coinbase unavailable")), \
                patch.object(predictor, "fetch_kalshi_market_by_ticker", return_value=result()), \
                redirect_stdout(io.StringIO()):
            clock.time.return_value = CLOSE + 60
            predictor.command_run(args)
        model, state = predictor.load_state(self.state_path)
        self.assertEqual(model.updates, 1)
        self.assertEqual(state["paper_trades"][0]["outcome"], "UP")
        self.assertEqual(len(self.rows("settlements")), 1)

    def test_feature_gap_does_not_discard_a_settlement_already_observed(self):
        predictor.save_state(self.state_path, self.model, self.state)
        args = SimpleNamespace(state_file=str(self.state_path), kalshi_series="KXBTC15M", epochs=5)
        with patch.object(predictor, "time") as clock, \
                patch.object(predictor, "_load_bars", return_value=[predictor.WindowBar(START, 100, 101, 99, 100, 1)]), \
                patch.object(predictor, "fetch_kalshi_market", return_value=market(CLOSE, "NEXT")), \
                patch.object(predictor, "fetch_kalshi_market_by_ticker", return_value=result()), \
                redirect_stdout(io.StringIO()):
            clock.time.return_value = CLOSE + 60
            predictor.command_run(args)
        model, state = predictor.load_state(self.state_path)
        self.assertEqual(model.updates, 1)
        self.assertEqual(state["paper_trades"][0]["outcome"], "UP")
        self.assertEqual(len(self.rows("settlements")), 1)

    def test_dashboard_feature_gap_preserves_settlement_without_fetching_it_again(self):
        self.state["pending"] = pending(committed=True)
        predictor.save_state(self.state_path, self.model, self.state)
        bars = [predictor.WindowBar(START-(22-i)*900, 100, 101, 99, 100, 1)
                for i in range(23) if i != 10]
        with patch("btc_predictor.time.time", return_value=CLOSE + 60), \
                patch("dashboard.fetch_candles", return_value=[]), \
                patch("dashboard.aggregate_candles", return_value=bars), \
                patch("dashboard.fetch_kalshi_market", return_value=market(CLOSE, "NEXT")), \
                patch("dashboard.fetch_previous_kalshi_market", return_value=None), \
                patch("dashboard.fetch_spot_price", return_value=None), \
                patch.object(predictor, "fetch_kalshi_market_by_ticker", side_effect=[
                    result(), OSError("result endpoint unavailable on retry")]) as fetch:
            payload = DashboardApp(str(self.state_path), 1440).status()
        model, state = predictor.load_state(self.state_path)
        self.assertEqual(model.updates, 1)
        self.assertEqual(state["demo_account"]["trades"], 1)
        self.assertAlmostEqual(state["demo_account"]["balance"], 104.7)
        self.assertEqual(len(self.rows("settlements")), 1)
        self.assertEqual(self.rows("settlements")[0]["available_at"], CLOSE + 60)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["learned_previous"]["outcome"], "UP")
        fetch.assert_called_once_with(TICKER)

    def test_replay_after_restart_and_rolling_history_pruning_cannot_pay_twice(self):
        self.state["pending"] = pending(committed=True)
        original = copy.deepcopy(self.state["pending"])
        with patch.object(predictor, "fetch_kalshi_market_by_ticker", return_value=result()):
            predictor.learn_from_pending(self.model, self.state, [], now=CLOSE + 60)
        forecast_archive.record_cycle(self.path, self.state, now=CLOSE + 60)
        self.state["paper_trades"] = []  # simulate eventual rolling-ledger pruning
        predictor.save_state(self.state_path, self.model, self.state)
        model, state = predictor.load_state(self.state_path)
        state["settlement_queue"][TICKER] = original
        with patch.object(predictor, "fetch_kalshi_market_by_ticker") as fetch:
            self.assertIsNone(predictor.learn_from_pending(model, state, [], now=CLOSE + 120))
        fetch.assert_not_called()
        self.assertEqual(model.updates, 1)
        self.assertEqual(state["demo_account"]["trades"], 1)
        self.assertAlmostEqual(state["demo_account"]["balance"], 104.7)
        self.assertFalse(state["settlement_queue"])

    def test_schema_one_upgrade_preserves_provisional_evidence_and_forecast(self):
        forecast_archive.record_cycle(self.path, self.state, now=START + 10)
        original = self.rows("forecasts")[0]
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE settlement_observations")
            db.execute("UPDATE archive_metadata SET value='1' WHERE key='schema_version'")
            db.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?)", (
                original["forecast_id"], TICKER, CLOSE, "yes", CLOSE + 1, "kalshi_official",
                json.dumps(result(status="determined")), "live_observation"))
        forecast_archive.record_cycle(self.path, self.state, now=CLOSE + 30)
        self.assertEqual(self.rows("forecasts")[0], original)
        self.assertEqual(self.rows("settlements")[0]["available_at"], CLOSE + 1)
        self.assertEqual(self.rows("settlement_observations")[0]["available_at"], CLOSE + 1)
        self.assertEqual(validation.evaluate_archive(self.path)["forward"]["scored_count"], 0)


if __name__ == "__main__":
    unittest.main()

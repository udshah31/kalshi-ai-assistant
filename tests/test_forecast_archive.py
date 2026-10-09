import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from btc_predictor import (
    OnlineLogisticRegression, WindowBar, new_state, predict_and_queue,
    finalize_prediction, archive_and_guard, learning_analysis,
)
from forecast_archive import archive_path, record_cycle

START = 1800000000
NOW = START + 60
BAR = START - 900


def pending():
    market = {"ticker": "KXBTC15M-test", "status": "active", "open_time": START,
              "close_time": START+900, "observed_at": NOW-1, "quote_source": "kalshi_orderbook",
              "yes_bid": .48, "yes_ask": .50, "yes_ask_size": 100,
              "no_bid": .50, "no_ask": .52, "no_ask_size": 100, "yes_mid": .49}
    return {"bar_timestamp": BAR, "close": 100, "features": [1,1,1,1,.2,0,.5],
            "probability_up": .8, "forecast_issued_at": NOW, "validation_eligible": True,
            "outcome_source": "kalshi_official", "market_ticker": market["ticker"],
            "market_close_time": START+900, "market_snapshot": market,
            "demo_side": "UP", "trade_signal": "WAIT", "demo_quantity": 0, "demo_cost": 0,
            "demo_entry_price": .5, "cost_allowance_per_contract": .03}


def settled(record):
    return {**record, "outcome": "UP", "correct": True, "settled_at": START+905,
            "official_result_snapshot": {"ticker": record["market_ticker"], "close_time": START+900, "result": "yes"}}


class ForecastArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = archive_path(Path(self.directory.name)/"state.json")
        self.state = new_state(OnlineLogisticRegression(7))
        self.state["pending"] = pending()

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(r) for r in connection.execute(f"SELECT * FROM {table}")]

    def test_durable_forecasts_prices_and_outcomes_survive_bounded_state(self):
        candles = [[BAR+i*60, 99, 101, 100, 100, 1] for i in range(15)]
        bar = WindowBar(BAR, 100, 101, 99, 100, 15)
        initial = record_cycle(self.path, self.state, bars=[bar], raw_candles=candles, now=NOW)
        self.assertEqual(initial["counts"]["candles"], 15)
        self.assertEqual(initial["counts"]["forecasts"], 1)
        self.state["paper_trades"] = [settled(self.state["pending"])]
        self.state["pending"] = None
        record_cycle(self.path, self.state, now=START+910)
        self.state["paper_trades"] = []
        record_cycle(self.path, self.state, now=START+920)
        self.assertEqual(len(self.rows("settlements")), 1)
        self.assertEqual(len(self.rows("forecasts")), 1)
        self.assertEqual(json.loads(self.rows("forecasts")[0]["features_json"]), [1,1,1,1,.2,0,.5])
        self.assertEqual(self.rows("settlements")[0]["available_at"], START+905)

    def test_duplicate_cycles_cannot_rewrite_original_probability_or_label(self):
        record_cycle(self.path, self.state, now=NOW)
        self.state["pending"]["probability_up"] = .2
        self.state["pending"]["features"] = [-1]*7
        self.state["pending"]["forecast_issued_at"] += 30
        record_cycle(self.path, self.state, now=NOW+30)
        stored = self.rows("forecasts")[0]
        self.assertEqual(stored["probability_up"], .8)
        self.assertEqual(stored["forecast_issued_at"], NOW)
        self.state["paper_trades"] = [settled(self.state["pending"])]
        self.state["pending"] = None
        record_cycle(self.path, self.state, now=START+910)
        self.state["paper_trades"][0].update({"outcome": "DOWN", "settled_at": START+930})
        self.state["paper_trades"][0]["official_result_snapshot"]["result"] = "no"
        record_cycle(self.path, self.state, now=START+940)
        self.assertEqual(self.rows("settlements")[0]["result"], "yes")
        self.assertEqual(self.rows("settlements")[0]["available_at"], START+905)

    def test_legacy_import_does_not_reconstruct_features_or_official_labels(self):
        self.state["pending"] = None
        self.state["paper_trades"] = [{"bar_timestamp": BAR, "probability_up": .8, "outcome": "UP", "pnl": .5}]
        record_cycle(self.path, self.state, now=NOW)
        self.assertEqual(len(self.rows("legacy_scores")), 1)
        self.assertEqual(self.rows("settlements"), [])
        score = settled(pending()); score.pop("features")
        self.state["paper_trades"].append(score)
        record_cycle(self.path, self.state, now=START+910)
        self.assertIsNone(self.rows("forecasts")[0]["features_json"])
        self.assertEqual(self.rows("forecasts")[0]["provenance"], "state_import")

    def test_bad_future_partial_and_mismatched_records_are_not_invented(self):
        bar = WindowBar(START, 100, 101, 99, 100, 1)  # unfinished at NOW
        candles = [[START+120, 99, 101, 100, 100, 1], [BAR, 99, 101, 100, float("nan"), 1]]
        record_cycle(self.path, self.state, bars=[bar], raw_candles=candles, now=NOW)
        self.assertEqual(self.rows("candles"), [])
        self.assertEqual(self.rows("bars"), [])
        score = settled(pending()); score["official_result_snapshot"]["ticker"] = "wrong"
        self.state["paper_trades"] = [score]
        record_cycle(self.path, self.state, now=START+910)
        self.assertEqual(self.rows("settlements"), [])
        score["official_result_snapshot"]["ticker"] = pending()["market_ticker"]
        score["settled_at"] = START+1000
        record_cycle(self.path, self.state, now=START+910)
        self.assertEqual(self.rows("settlements"), [])

    def test_price_receipt_time_does_not_rewrite_history_or_complete_future_bars(self):
        original = WindowBar(BAR, 100, 101, 99, 100, 15)
        record_cycle(self.path, self.state, bars=[original], now=NOW)
        changed = WindowBar(BAR, 200, 201, 199, 200, 30)
        unfinished = WindowBar(START, 100, 101, 99, 100, 15)
        record_cycle(self.path, self.state, bars=[changed, unfinished],
                     prices_observed_at=START+890, now=START+910)
        prices = self.rows("bars")
        self.assertEqual(len(prices), 1)
        self.assertEqual(prices[0]["close"], 100)
        self.assertEqual(prices[0]["observed_at"], NOW)
        self.assertEqual(self.rows("forecasts")[0]["forecast_issued_at"], NOW)

    def test_invalid_price_receipt_times_cannot_create_archive_evidence(self):
        for observed in (NOW+1, -1, float("nan"), True):
            with self.subTest(observed=observed), self.assertRaises(ValueError):
                record_cycle(self.path, self.state, prices_observed_at=observed, now=NOW)
        self.assertFalse(self.path.exists())

    def test_state_reset_separates_runs_without_deleting_previous_evidence(self):
        record_cycle(self.path, self.state, now=NOW)
        newer = new_state(OnlineLogisticRegression(7)); newer["pending"] = pending()
        record_cycle(self.path, newer, now=NOW)
        self.assertNotEqual(self.state["state_id"], newer["state_id"])
        self.assertEqual(len(self.rows("forecasts")), 2)

    def test_public_snapshots_do_not_store_secret_fields(self):
        secret = "unit-test-secret-not-a-real-key"
        self.state["pending"]["market_snapshot"]["api_key"] = secret
        self.state["pending"]["typesafe_review"] = {"enabled": True, "status": "ok", "api_key": secret}
        prediction = {"bar_timestamp": BAR, "kalshi": self.state["pending"]["market_snapshot"], "trade_signal": "WAIT",
                      "typesafe_review": {"status": "error", "reason": secret, "Authorization": secret}}
        record_cycle(self.path, self.state, prediction=prediction, now=NOW)
        self.assertNotIn(secret, json.dumps(self.rows("forecasts")+self.rows("decisions")))

    def test_committed_paper_entry_is_immutable_and_distinct_from_matches(self):
        record = self.state["pending"]
        record.update({"trade_committed_at": NOW, "trade_signal": "TRADE", "demo_quantity": 18, "demo_cost": 9.54,
                       "entry_market_snapshot": record["market_snapshot"], "entry_signal_checks": {"market": True},
                       "typesafe_review": {"enabled": False, "status": "disabled"}})
        record_cycle(self.path, self.state, now=NOW)
        record["demo_cost"] = 1
        record_cycle(self.path, self.state, now=NOW+1)
        self.assertAlmostEqual(self.rows("paper_entries")[0]["cost"], 9.54)
        self.assertEqual(self.rows("settlements"), [])

    def candidate(self):
        model = OnlineLogisticRegression(7)
        self.state = new_state(model)
        self.state["last_learned_bar"] = BAR
        self.state["paper_trades"] = [{"market_ticker": f"historical-{i}", "probability_up": .8,
                                      "outcome_source": "kalshi_official", "correct": True, "outcome": "UP"}
                                     for i in range(100)]
        with patch("btc_predictor.feature_vector", return_value=pending()["features"]), patch.object(model, "predict_proba", return_value=.8):
            prediction = predict_and_queue(model, self.state, [WindowBar(BAR,100,101,99,100,1)], pending()["market_snapshot"], now=NOW)
        result = finalize_prediction(prediction, self.state, {"enabled": False, "status": "disabled"}, now=NOW)
        self.assertEqual(result["trade_signal"], "TRADE")
        return result

    def test_archive_failure_vetoes_new_entry_without_losing_learning_state(self):
        prediction = self.candidate()
        self.assertTrue(prediction["paper_entry_created"])
        with patch("forecast_archive.record_cycle", side_effect=sqlite3.OperationalError("fake-private-diagnostic")):
            final = archive_and_guard(Path(self.directory.name)/"state.json", self.state, [], prediction, now=NOW)
        self.assertEqual(final["trade_signal"], "WAIT")
        self.assertEqual(final["analysis"]["suggestion"], "WAIT")
        self.assertEqual(self.state["pending"]["demo_cost"], 0)
        self.assertNotIn("trade_committed_at", self.state["pending"])
        self.assertNotIn("fake-private-diagnostic", json.dumps(final))
        self.assertEqual(len(self.state["paper_trades"]), 100)
        self.assertEqual(self.state["archive_status"]["status"], "error")
        archive_and_guard(Path(self.directory.name)/"state.json", self.state, [], now=NOW)
        self.assertEqual(self.state["archive_status"]["status"], "ok")

    def test_archive_failure_does_not_retroactively_cancel_existing_entry(self):
        prediction = self.candidate()
        prediction["paper_entry_created"] = False
        original_cost = self.state["pending"]["demo_cost"]
        with patch("forecast_archive.record_cycle", side_effect=OSError("unavailable")):
            final = archive_and_guard(Path(self.directory.name)/"state.json", self.state, [], prediction, now=NOW+10)
        self.assertEqual(final["trade_signal"], "WAIT")
        self.assertTrue(final["open_paper_trade"])
        self.assertEqual(self.state["pending"]["demo_cost"], original_cost)
        self.assertIn("trade_committed_at", self.state["pending"])

    def test_original_model_snapshot_is_not_mutated_by_future_updates(self):
        model = OnlineLogisticRegression(7)
        snapshot = model.to_dict()
        model.update([1]*7, 1)
        self.assertEqual(snapshot["weights"], [0]*7)
        self.assertNotEqual(snapshot["weights"], model.weights)

    def test_early_quote_discovery_can_bind_but_never_reprice_forecast(self):
        original = pending()
        self.state["pending"].update({"market_ticker": None, "market_snapshot": None, "outcome_source": "unmatched_research", "validation_eligible": False})
        record_cycle(self.path, self.state, now=NOW)
        self.state["pending"] = original
        record_cycle(self.path, self.state, now=NOW+5)
        self.assertEqual(self.rows("forecasts")[0]["market_ticker"], original["market_ticker"])
        self.assertTrue(self.rows("forecasts")[0]["validation_eligible"])
        self.assertEqual(self.rows("forecasts")[0]["probability_up"], .8)


if __name__ == "__main__":
    unittest.main()

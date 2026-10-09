import json
import tempfile
import unittest
from pathlib import Path

from btc_predictor import (
    FEATURE_NAMES,
    OnlineLogisticRegression,
    WindowBar,
    aggregate_candles,
    apply_typesafe_gate,
    build_typesafe_request,
    demo_account_summary,
    evaluate_trade_signal,
    feature_vector,
    learn_from_pending,
    learning_analysis,
    learning_freshness,
    load_state,
    new_state,
    normalize_kalshi_market,
    paper_trade_summary,
    save_state,
    train_model,
)


class PredictorTests(unittest.TestCase):
    def test_aggregate_candles_requires_complete_windows(self):
        candles = []
        for minute in range(30):
            timestamp = minute * 60
            price = 100.0 + minute
            candles.append([timestamp, price - 0.5, price + 0.5, price, price, 2.0])

        bars = aggregate_candles(candles)

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].timestamp, 0)
        self.assertEqual(bars[0].open, 100.0)
        self.assertEqual(bars[0].close, 114.0)
        self.assertEqual(bars[0].volume, 30.0)

        incomplete = [row for row in candles if row[0] != 15 * 60]
        self.assertEqual(len(aggregate_candles(incomplete)), 1)

    def test_feature_vector_is_bounded(self):
        bars = [
            WindowBar(
                timestamp=index * 900,
                open=100.0 + index,
                high=101.0 + index,
                low=99.0 + index,
                close=100.0 + index,
                volume=100.0,
            )
            for index in range(25)
        ]

        features = feature_vector(bars, 24)

        self.assertIsNotNone(features)
        self.assertEqual(len(features), len(FEATURE_NAMES))
        self.assertTrue(all(-30.0 <= value <= 30.0 for value in features))

    def test_online_model_learns_simple_directional_signal(self):
        model = OnlineLogisticRegression(n_features=1, learning_rate=0.2)
        for _ in range(30):
            model.update([1.0], 1)
            model.update([-1.0], 0)

        self.assertGreater(model.predict_proba([1.0]), 0.8)
        self.assertLess(model.predict_proba([-1.0]), 0.2)

    def test_state_round_trip_and_pending_learning(self):
        model = OnlineLogisticRegression(n_features=len(FEATURE_NAMES))
        state = new_state(model)
        state["pending"] = {
            "bar_timestamp": 900,
            "close": 100.0,
            "features": [0.1] * len(FEATURE_NAMES),
            "probability_up": 0.5,
        }
        bars = [
            WindowBar(0, 99.0, 100.0, 98.0, 99.0, 100.0),
            WindowBar(900, 100.0, 101.0, 99.0, 100.0, 100.0),
            WindowBar(1800, 101.0, 102.0, 100.0, 101.0, 100.0),
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_state(path, model, state)
            loaded_model, loaded_state = load_state(path)
            learned = learn_from_pending(loaded_model, loaded_state, bars)
            save_state(path, loaded_model, loaded_state)
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(learned["outcome"], "UP")
        self.assertTrue(learned["paper_trade"]["correct"])
        self.assertIsNone(loaded_state["pending"])
        self.assertEqual(loaded_model.updates, 1)
        self.assertEqual(payload["model"]["updates"], 1)
        self.assertEqual(payload["paper_trades"][0]["pnl"], 0.5)

    def test_stale_pending_prediction_is_recovered_without_fake_outcome(self):
        model = OnlineLogisticRegression(n_features=len(FEATURE_NAMES))
        state = new_state(model)
        state["pending"] = {
            "bar_timestamp": 900,
            "close": 100.0,
            "features": [0.1] * len(FEATURE_NAMES),
            "probability_up": 0.5,
        }
        bars = [
            WindowBar(2700, 103.0, 104.0, 102.0, 103.0, 100.0),
            WindowBar(3600, 104.0, 105.0, 103.0, 104.0, 100.0),
        ]

        recovered = learn_from_pending(model, state, bars)

        self.assertEqual(recovered["status"], "skipped_stale")
        self.assertIsNone(state["pending"])
        self.assertEqual(len(state["skipped_predictions"]), 1)
        self.assertEqual(model.updates, 0)
        self.assertEqual(learning_freshness(state, 3600, now=4500)["status"], "RECOVERED / WAITING FOR FRESH RESULT")

    def test_learning_analysis_blocks_while_recovery_waits_for_fresh_result(self):
        state = new_state(OnlineLogisticRegression(n_features=len(FEATURE_NAMES)))
        state["last_learning_status"] = "recovered_skip"
        state["last_learned_bar"] = 900
        state["pending"] = {"bar_timestamp": 3600}

        analysis = learning_analysis(
            state,
            {"bar_timestamp": 3600, "trade_signal": "WAIT", "trade_reason": "test"},
            now=4500,
        )

        self.assertEqual(analysis["risk_gate"], "BLOCK")
        self.assertIn("recovered / waiting", analysis["reason"])
        self.assertEqual(analysis["learning_freshness"]["status"], "RECOVERED / WAITING FOR FRESH RESULT")

    def test_trade_signal_requires_confidence_and_market_edge(self):
        market = {
            "ticker": "KXBTC15M-test", "status": "active", "open_time": 2700,
            "close_time": 3600, "observed_at": 2730, "quote_source": "kalshi_orderbook",
            "yes_bid": 0.50,
            "yes_ask": 0.52,
            "yes_bid_size": 100.0,
            "yes_ask_size": 100.0,
            "no_bid": 0.48,
            "no_ask": 0.50,
            "no_bid_size": 100.0,
            "no_ask_size": 100.0,
        }
        trend_features = [1.0, 1.0, 1.0, 1.0, 0.5, 0.0, 1.0]
        trade = evaluate_trade_signal(0.68, market, trend_features, bar_timestamp=1800, now=2730)
        wait = evaluate_trade_signal(0.55, market, trend_features, bar_timestamp=1800, now=2730)

        self.assertEqual(trade["signal"], "TRADE")
        self.assertAlmostEqual(trade["gross_edge"], 0.16)
        self.assertAlmostEqual(trade["edge"], 0.13)
        self.assertEqual(wait["signal"], "WAIT")

    def test_typesafe_review_is_optional_and_can_veto_only_trade(self):
        prediction = {
            "trade_signal": "TRADE",
            "direction": "UP",
            "probability_up": 0.70,
            "model_edge": 0.10,
            "demo_quantity": 10,
            "demo_cost": 5.0,
        }
        reviewed = apply_typesafe_gate(
            prediction,
            {"status": "ok", "choice": "review", "confidence": 0.80},
        )
        self.assertEqual(reviewed["trade_signal"], "WAIT")
        self.assertEqual(reviewed["demo_quantity"], 0)

        request = build_typesafe_request(prediction, new_state(OnlineLogisticRegression(len(FEATURE_NAMES))))
        self.assertEqual(request["model"], "jev-latest")
        self.assertEqual(request["questions"]["strategy_review"]["type"], "choice")
        self.assertIn("approve_trade", request["questions"]["strategy_review"]["criteria"])

    def test_learning_analysis_blocks_after_weak_recent_results(self):
        state = {
            "paper_trades": [
                {"correct": False, "outcome": "DOWN", "probability_up": 0.8, "pnl": -0.5}
                for _ in range(5)
            ],
            "demo_account": {
                "starting_balance": 100.0,
                "balance": 97.5,
                "budget_per_trade": 10.0,
                "realized_pnl": -2.5,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "demo_trades": [],
            },
        }

        analysis = learning_analysis(
            state,
            {
                "trade_signal": "TRADE",
                "direction": "UP",
                "probability_up": 0.8,
                "model_edge": 0.2,
                "demo_cost": 10.0,
            },
        )

        self.assertEqual(analysis["risk_gate"], "BLOCK")
        self.assertEqual(analysis["suggestion"], "WAIT")
        self.assertLess(analysis["rolling_accuracy"], 0.5)

    def test_demo_account_settles_only_trade_signals(self):
        model = OnlineLogisticRegression(n_features=len(FEATURE_NAMES))
        state = new_state(model)
        state["pending"] = {
            "bar_timestamp": 900,
            "close": 100.0,
            "features": [0.1] * len(FEATURE_NAMES),
            "probability_up": 0.68,
            "demo_side": "UP",
            "trade_signal": "TRADE",
            "demo_entry_price": 0.50,
            "demo_quantity": 20,
            "demo_cost": 10.0,
        }
        bars = [
            WindowBar(900, 100.0, 101.0, 99.0, 100.0, 100.0),
            WindowBar(1800, 101.0, 102.0, 100.0, 101.0, 100.0),
        ]

        learned = learn_from_pending(model, state, bars)
        account = demo_account_summary(state)

        self.assertTrue(learned["demo_trade"]["correct"])
        self.assertEqual(account["trades"], 1)
        self.assertEqual(account["wins"], 1)
        self.assertAlmostEqual(account["total_traded"], 10.0)
        self.assertAlmostEqual(account["balance"], 110.0)

    def test_paper_trade_summary_reports_accuracy_and_pnl(self):
        summary = paper_trade_summary(
            {
                "paper_trades": [
                    {"correct": True, "entry_price": 0.40, "pnl": 0.60},
                    {"correct": False, "entry_price": 0.60, "pnl": -0.60},
                ]
            }
        )

        self.assertEqual(summary["settled_trades"], 2)
        self.assertEqual(summary["wins"], 1)
        self.assertAlmostEqual(summary["accuracy"], 0.5)
        self.assertAlmostEqual(summary["pnl"], 0.0)

    def test_normalize_kalshi_market_calculates_midpoints(self):
        market = normalize_kalshi_market(
            {
                "ticker": "KXBTC15M-test",
                "event_ticker": "KXBTC15M-test-event",
                "title": "BTC price up in next 15 mins?",
                "yes_sub_title": "Target Price: $86,513.45",
                "close_time": "2026-10-02T05:00:00Z",
                "settlement_ts": "2026-10-02T05:00:04Z",
                "result": "yes",
                "yes_bid_dollars": "0.70",
                "yes_ask_dollars": "0.74",
                "no_bid_dollars": "0.26",
                "no_ask_dollars": "0.30",
            }
        )

        self.assertEqual(market["ticker"], "KXBTC15M-test")
        self.assertEqual(market["result"], "yes")
        self.assertEqual(market["settlement_time"], "2026-10-02T05:00:04Z")
        self.assertAlmostEqual(market["yes_mid"], 0.72)
        self.assertAlmostEqual(market["no_mid"], 0.28)

    def test_train_model_returns_metrics(self):
        samples = [([1.0] + [0.0] * 6, 1, 0), ([-1.0] + [0.0] * 6, 0, 900)] * 5

        model, metrics = train_model(samples, epochs=2)

        self.assertEqual(model.n_features, len(FEATURE_NAMES))
        self.assertEqual(metrics["samples"], 10.0)
        self.assertGreaterEqual(metrics["accuracy"], 0.8)


if __name__ == "__main__":
    unittest.main()

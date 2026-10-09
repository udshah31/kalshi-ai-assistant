"""Regression coverage for evidence-driven, paper-only recommendations."""
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from btc_predictor import (
    FEATURE_NAMES, INTERVAL_SECONDS, MIN_LIVE_SAMPLES, OnlineLogisticRegression,
    WindowBar, apply_typesafe_gate, evaluate_trade_signal, finalize_prediction,
    learn_from_pending, normalize_kalshi_market, orderbook_quotes,
    official_validation, predict_and_queue, new_state, review_fingerprint,
    learning_freshness, feature_vector, aggregate_candles, settle_demo_account,
    load_state, save_state, command_run, learning_analysis,
)


NOW = 1800000060  # 60 seconds after a 15-minute boundary
START = NOW - NOW % INTERVAL_SECONDS
BAR = START - INTERVAL_SECONDS


def market():
    return {
        "ticker": "KXBTC15M-example", "status": "active",
        "open_time": START, "close_time": START + INTERVAL_SECONDS,
        "observed_at": NOW, "quote_source": "kalshi_orderbook",
        "yes_bid": .48, "yes_ask": .50, "no_bid": .50, "no_ask": .52,
        "yes_ask_size": 100., "no_ask_size": 100.,
        "yes_bid_size": 100., "no_bid_size": 100.,
        "yes_mid": .49, "no_mid": .51,
    }


FEATURES = [1., 1., 1., 1., .2, .1, .5]


class RecommendationTests(unittest.TestCase):
    def test_missing_liquidity_is_unknown_not_zero(self):
        normalized = normalize_kalshi_market({"ticker": "example"})
        self.assertIsNone(normalized["no_ask_size"])
        m = market(); m["yes_ask_size"] = None
        signal = evaluate_trade_signal(.8, m, FEATURES, bar_timestamp=BAR, now=NOW)
        self.assertIsNone(signal["liquidity"])
        self.assertEqual(signal["signal"], "WAIT")
        self.assertIn("unknown", signal["reason"].lower())

    def test_orderbook_infers_asks_and_sizes_from_opposite_bids(self):
        quotes = orderbook_quotes({"orderbook_fp": {
            "yes_dollars": [["0.4800", "80.50"]],
            "no_dollars": [["0.5000", "100.25"]],
        }})
        self.assertAlmostEqual(quotes["no_ask"], .52)
        self.assertAlmostEqual(quotes["no_ask_size"], 80.5)
        self.assertAlmostEqual(quotes["yes_ask_size"], 100.25)
        self.assertEqual(orderbook_quotes({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})["yes_ask_size"], 0)
        with self.assertRaises(ValueError):
            orderbook_quotes({})

    def test_misaligned_stale_expired_and_late_setups_are_blocked(self):
        for fields, check in [
            ({"close_time": START + 1800}, "market_alignment"),
            ({"observed_at": NOW - 120}, "quote_freshness"),
            ({"close_time": NOW - 1}, "market_open"),
        ]:
            m = market(); m.update(fields)
            signal = evaluate_trade_signal(.8, m, FEATURES, bar_timestamp=BAR, now=NOW)
            self.assertEqual(signal["signal"], "WAIT")
            self.assertFalse(signal["checks"][check])
        signal = evaluate_trade_signal(.8, market(), FEATURES, bar_timestamp=BAR, now=START+400)
        self.assertFalse(signal["checks"]["entry_timing"])
        signal = evaluate_trade_signal(.8, market(), FEATURES, bar_timestamp=BAR-900, now=NOW)
        self.assertFalse(signal["checks"]["candle_freshness"])

    def test_ask_edge_includes_explicit_cost_allowance(self):
        signal = evaluate_trade_signal(.8, market(), FEATURES, bar_timestamp=BAR, now=NOW)
        self.assertEqual(signal["signal"], "TRADE")
        self.assertAlmostEqual(signal["gross_edge"], .3)
        self.assertLess(signal["edge"], signal["gross_edge"])
        self.assertGreater(signal["cost_allowance"], 0)
        self.assertTrue(signal["blockers"] == [])

    def test_proxy_history_does_not_count_as_official_validation(self):
        state = {"paper_trades": [{"correct": True, "outcome": "UP", "probability_up": .8}]*200}
        validation = official_validation(state)
        self.assertEqual(validation["samples"], 0)
        self.assertFalse(validation["ready"])
        self.assertEqual(validation["minimum_samples"], MIN_LIVE_SAMPLES)

    def test_new_forecast_uses_official_result_even_if_coinbase_disagrees(self):
        model = OnlineLogisticRegression(len(FEATURE_NAMES)); state = new_state(model)
        state["pending"] = {
            "bar_timestamp": BAR, "close": 100., "features": FEATURES,
            "probability_up": .8, "demo_side": "UP", "trade_signal": "WAIT",
            "market_ticker": "KXBTC15M-example", "market_close_time": START+900,
            "outcome_source": "kalshi_official",
        }
        bars = [WindowBar(START, 100., 110., 99., 110., 100.)]
        settled = {"ticker": "KXBTC15M-example", "close_time": START+900,
                   "result": "no", "expiration_value": "99.00"}
        with patch("btc_predictor.fetch_kalshi_market_by_ticker", return_value=settled):
            result = learn_from_pending(model, state, bars, now=START+910)
        self.assertEqual(result["outcome"], "DOWN")
        self.assertEqual(state["paper_trades"][-1]["outcome_source"], "kalshi_official")
        self.assertEqual(model.updates, 1)
        self.assertIsNone(learn_from_pending(model, state, bars, now=START+920))
        self.assertEqual(official_validation(state)["samples"], 1)

    def test_unsettled_official_market_is_not_filled_from_coinbase(self):
        model = OnlineLogisticRegression(len(FEATURE_NAMES)); state = new_state(model)
        state["pending"] = {"bar_timestamp": BAR, "close": 100., "features": FEATURES,
                            "probability_up": .8, "market_ticker": "KXBTC15M-example",
                            "market_close_time": START+900, "outcome_source": "kalshi_official"}
        with patch("btc_predictor.fetch_kalshi_market_by_ticker", return_value={"result": ""}):
            self.assertIsNone(learn_from_pending(model, state, [WindowBar(START, 100, 110, 99, 110, 100)], now=START+910))
        self.assertIsNotNone(state["pending"])
        self.assertEqual(model.updates, 0)
        self.assertEqual(len(state["paper_trades"]), 0)

    def test_enabled_typesafe_failure_blocks_candidate(self):
        for review in ({"enabled": True, "status": "error"},
                       {"enabled": True, "status": "ok", "choice": "approve_trade", "confidence": .9}):
            pred = {"trade_signal": "TRADE", "demo_quantity": 10, "demo_cost": 5}
            result = apply_typesafe_gate(pred, review, now=NOW)
            self.assertEqual(result["trade_signal"], "WAIT")
            self.assertEqual(result["demo_cost"], 0)

    def strong_candidate(self):
        model = OnlineLogisticRegression(len(FEATURE_NAMES)); state = new_state(model)
        state["paper_trades"] = [{"market_ticker": f"KXBTC15M-history-{i}", "outcome_source": "kalshi_official",
                                  "correct": True, "probability_up": .8, "outcome": "UP"}
                                 for i in range(MIN_LIVE_SAMPLES)]
        state["last_learned_bar"] = BAR
        with patch("btc_predictor.feature_vector", return_value=FEATURES), patch.object(model, "predict_proba", return_value=.8):
            prediction = predict_and_queue(model, state, [WindowBar(BAR, 100, 101, 99, 100, 100)], market(), now=NOW)
        self.assertEqual(prediction["trade_signal"], "TRADE")
        self.assertEqual(state["pending"]["trade_signal"], "WAIT")
        return model, state, prediction

    def valid_review(self, prediction, choice="approve_trade"):
        return {"enabled": True, "status": "ok", "choice": choice, "confidence": .9,
                "reviewed_at": NOW, "expires_at": NOW + 60, "fingerprint": review_fingerprint(prediction)}

    def test_valid_approval_commits_budget_with_costs_and_freezes_entry(self):
        model, state, prediction = self.strong_candidate()
        result = finalize_prediction(prediction, state, self.valid_review(prediction), now=NOW)
        self.assertEqual(result["trade_signal"], "TRADE")
        pending = state["pending"]
        self.assertGreater(pending["demo_quantity"], 0)
        self.assertLessEqual(pending["demo_cost"], 10)
        self.assertAlmostEqual(pending["demo_cost"], pending["demo_quantity"]*(pending["demo_entry_price"]+.03))
        original_cost = pending["demo_cost"]
        with patch("btc_predictor.feature_vector", return_value=FEATURES):
            late = predict_and_queue(model, state, [WindowBar(BAR, 100, 101, 99, 100, 100)], market(), now=START+300)
        finalize_prediction(late, state, {"enabled": False, "status": "disabled"}, now=START+300)
        self.assertEqual(late["trade_signal"], "WAIT")
        self.assertEqual(state["pending"]["demo_cost"], original_cost)
        self.assertTrue(late["open_paper_trade"])

    def test_exact_typesafe_veto_prevents_saved_trade_and_account_pnl(self):
        _, state, prediction = self.strong_candidate()
        result = finalize_prediction(prediction, state, self.valid_review(prediction, "wait"), now=NOW)
        self.assertEqual(result["trade_signal"], "WAIT")
        self.assertEqual(result["analysis"]["suggestion"], "WAIT")
        self.assertEqual(state["pending"]["trade_signal"], "WAIT")
        self.assertEqual(state["pending"]["demo_quantity"], 0)
        self.assertIsNone(settle_demo_account(state, state["pending"], 1, None))
        self.assertEqual(state["demo_account"]["balance"], 100)

    def test_type_safe_stale_cross_market_and_failures_all_withhold(self):
        for mode in ("expired", "different_price", "different_ticker", "error", "low_confidence"):
            _, state, prediction = self.strong_candidate()
            review = self.valid_review(prediction)
            if mode == "expired": review["expires_at"] = NOW-1
            if mode == "different_price": prediction["market_entry_price"] = .51
            if mode == "different_ticker": prediction["kalshi"]["ticker"] = "other"
            if mode == "error": review["status"] = "error"
            if mode == "low_confidence": review["confidence"] = .4
            final = finalize_prediction(prediction, state, review, now=NOW)
            self.assertEqual(final["trade_signal"], "WAIT", mode)
            self.assertEqual(state["pending"]["trade_signal"], "WAIT", mode)

    def test_review_delay_cannot_authorize_expired_market_evidence(self):
        _, state, prediction = self.strong_candidate()
        final = finalize_prediction(prediction, state, self.valid_review(prediction), now=NOW+61)
        self.assertEqual(final["trade_signal"], "WAIT")
        self.assertEqual(state["pending"]["demo_cost"], 0)

    def test_official_labels_do_not_require_coinbase_target_bar(self):
        model, state, prediction = self.strong_candidate()
        finalize_prediction(prediction, state, {"enabled": False, "status": "disabled"}, now=NOW)
        settled = {"ticker": market()["ticker"], "close_time": START+900, "result": "yes", "expiration_value": "105"}
        with patch("btc_predictor.fetch_kalshi_market_by_ticker", return_value=settled):
            result = learn_from_pending(model, state, [], now=START+920)
        self.assertEqual(result["outcome"], "UP")
        self.assertEqual(result["paper_trade"]["exit_close"], 105)
        self.assertIsNotNone(result["demo_trade"])
        updates, balance = model.updates, state["demo_account"]["balance"]
        self.assertIsNone(learn_from_pending(model, state, [], now=START+930))
        self.assertEqual(model.updates, updates)
        self.assertEqual(state["demo_account"]["balance"], balance)

    def test_missing_or_wrong_official_result_recovers_without_fake_label(self):
        for result in ({"result": "yes", "ticker": "wrong", "close_time": START+900},
                       {"result": "yes", "ticker": market()["ticker"], "close_time": START+1800}):
            model, state, _ = self.strong_candidate()
            with patch("btc_predictor.fetch_kalshi_market_by_ticker", return_value=result):
                learned = learn_from_pending(model, state, [], now=START+900+3601)
            self.assertEqual(learned["status"], "skipped_stale")
            self.assertIsNone(state["pending"])
            self.assertEqual(model.updates, 0)
            self.assertEqual(state["demo_account"]["balance"], 100)

    def test_unresolved_committed_entry_is_preserved_and_blocks_new_trades(self):
        model, state, prediction = self.strong_candidate()
        finalize_prediction(prediction, state, {"enabled": False, "status": "disabled"}, now=NOW)
        with patch("btc_predictor.fetch_kalshi_market_by_ticker", return_value={"result": ""}):
            learned = learn_from_pending(model, state, [], now=START+900+3601)
        self.assertEqual(learned["status"], "skipped_stale")
        self.assertEqual(len(state["unresolved_demo_trades"]), 1)
        self.assertGreater(state["unresolved_demo_trades"][0]["demo_cost"], 0)
        self.assertEqual(state["demo_account"]["trades"], 0)
        self.assertEqual(state["demo_account"]["realized_pnl"], 0)
        self.assertTrue(any("unresolved committed" in b for b in learning_analysis(state, now=START+4501)["blockers"]))

    def test_wall_clock_freshness_detects_no_progress(self):
        state = {"pending": {"bar_timestamp": BAR-900*8}, "last_learned_bar": BAR-900*9}
        with patch("btc_predictor.time.time", return_value=NOW):
            self.assertEqual(learning_freshness(state)["status"], "STALE / RECOVERY NEEDED")
        state["pending"]["bar_timestamp"] = BAR
        state["last_learned_bar"] = BAR
        with patch("btc_predictor.time.time", return_value=NOW):
            self.assertEqual(learning_freshness(state)["status"], "FRESH")

    def test_gap_or_nan_candles_cannot_supply_features(self):
        bars = [WindowBar(i*900, 100, 101, 99, 100, 100) for i in range(25)]
        bars[23] = WindowBar(23*900+900, 100, 101, 99, 100, 100)
        self.assertIsNone(feature_vector(bars, 24))
        candles = [[i*60, 99, 101, 100, 100, 1] for i in range(15)]
        candles[7][4] = float("nan")
        self.assertEqual(aggregate_candles(candles), [])

    def test_late_and_duplicate_samples_do_not_manufacture_validation(self):
        trade = {"market_ticker": "ticker", "outcome_source": "kalshi_official", "correct": True, "probability_up": .8, "outcome": "UP"}
        self.assertEqual(official_validation({"paper_trades": [trade, trade]})["samples"], 1)
        self.assertEqual(official_validation({"paper_trades": [{**trade, "validation_eligible": False}]})["samples"], 0)

    def test_dashboard_applies_veto_and_state_readback_has_no_fill(self):
        from dashboard import DashboardApp
        model, state, candidate = self.strong_candidate()
        state["typesafe_required"] = True
        state["typesafe_review"] = self.valid_review(candidate, "wait")
        bars = [WindowBar(BAR, 100, 101, 99, 100, 100)] * 22
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory)/"state.json"
            save_state(path, model, state)
            stack.enter_context(patch("dashboard.time.time", return_value=NOW))
            stack.enter_context(patch("dashboard.fetch_candles", return_value=[]))
            stack.enter_context(patch("dashboard.aggregate_candles", return_value=bars))
            stack.enter_context(patch("dashboard.fetch_kalshi_market", return_value=market()))
            stack.enter_context(patch("dashboard.fetch_previous_kalshi_market", return_value=None))
            stack.enter_context(patch("dashboard.fetch_spot_price", return_value=None))
            stack.enter_context(patch("btc_predictor.feature_vector", return_value=FEATURES))
            payload = DashboardApp(str(path), 1440).status()
            _, saved = load_state(path)
        self.assertTrue(payload["ok"], payload.get("error"))
        self.assertEqual(payload["prediction"]["trade_signal"], "WAIT")
        self.assertEqual(payload["prediction"]["analysis"]["suggestion"], "WAIT")
        self.assertEqual(saved["pending"]["trade_signal"], "WAIT")
        self.assertEqual(saved["pending"]["demo_cost"], 0)
        self.assertEqual(saved["demo_account"]["trades"], 0)

    def test_cli_final_action_matches_persisted_action(self):
        model, state, candidate = self.strong_candidate()
        state["typesafe_required"] = True
        bars = [WindowBar(BAR, 100, 101, 99, 100, 100)]
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory)/"state.json"; save_state(path, model, state)
            stack.enter_context(patch("btc_predictor.time.time", return_value=NOW))
            stack.enter_context(patch("btc_predictor._load_bars", return_value=bars))
            stack.enter_context(patch("btc_predictor.feature_vector", return_value=FEATURES))
            stack.enter_context(patch("btc_predictor.fetch_kalshi_market", return_value=market()))
            stack.enter_context(patch("btc_predictor.typesafe_strategy_review", return_value=self.valid_review(candidate, "review")))
            out = io.StringIO()
            with redirect_stdout(out):
                code = command_run(SimpleNamespace(state_file=str(path), kalshi_series="KXBTC15M", epochs=5))
            result = json.loads(out.getvalue()); _, saved = load_state(path)
        self.assertEqual(code, 0)
        self.assertEqual(result["prediction"]["trade_signal"], "WAIT")
        self.assertEqual(result["prediction"]["trade_signal"], saved["pending"]["trade_signal"])
        self.assertEqual(saved["pending"]["demo_quantity"], 0)

    def test_retry_lagging_evidence_and_settlements_but_not_late_entries(self):
        from live_runner import recommendation_needs_retry
        self.assertTrue(recommendation_needs_retry({"signal_checks": {"orderbook": False}}, NOW))
        self.assertFalse(recommendation_needs_retry({"signal_checks": {"orderbook": False}}, START+300))
        self.assertTrue(recommendation_needs_retry({"forecast_status": "awaiting_market_evidence"}, START+300))
        self.assertTrue(recommendation_needs_retry({"signal_checks": {"pending_slot": False}}, START+300))
        self.assertTrue(recommendation_needs_retry({"signal_checks": {"typesafe": False}}, NOW))
        self.assertFalse(recommendation_needs_retry({"signal_checks": {"risk_validation": False, "confidence": False}}, NOW))

    def test_statistical_gate_requires_sample_count_and_beating_baseline(self):
        _, state, _ = self.strong_candidate()
        state["paper_trades"].pop()
        self.assertFalse(official_validation(state)["ready"])
        _, state, _ = self.strong_candidate()
        for trade in state["paper_trades"][:40]:
            trade.update({"outcome": "DOWN", "correct": False})
        validation = official_validation(state)
        self.assertGreater(validation["accuracy"], .5)
        self.assertGreater(validation["brier"], .25)
        self.assertFalse(validation["ready"])
        self.assertTrue(any("baseline" in b for b in validation["blockers"]))

    def test_threshold_boundaries_do_not_lower_safety_limits(self):
        m = market(); m.update({"yes_bid": .53, "yes_ask": .54, "yes_ask_size": 25.})
        signal = evaluate_trade_signal(.65, m, FEATURES, bar_timestamp=BAR, now=NOW)
        self.assertEqual(signal["signal"], "TRADE")
        self.assertAlmostEqual(signal["edge"], .08)
        m["yes_ask"] = .541
        self.assertFalse(evaluate_trade_signal(.65, m, FEATURES, bar_timestamp=BAR, now=NOW)["checks"]["edge"])
        m["yes_ask"] = .54; m["yes_ask_size"] = 24.99
        self.assertFalse(evaluate_trade_signal(.65, m, FEATURES, bar_timestamp=BAR, now=NOW)["checks"]["liquidity"])

    def test_finalization_persists_veto_and_updates_analysis(self):
        state = new_state(OnlineLogisticRegression(len(FEATURE_NAMES)))
        state["pending"] = {"bar_timestamp": BAR, "trade_signal": "WAIT", "demo_cost": 0}
        pred = {"bar_timestamp": BAR, "trade_signal": "TRADE", "direction": "UP",
                "probability_up": .8, "demo_quantity": 10, "demo_cost": 5,
                "signal_checks": {}, "blockers": []}
        review = {"enabled": True, "status": "error"}
        final = finalize_prediction(pred, state, review, now=NOW)
        self.assertEqual(final["trade_signal"], "WAIT")
        self.assertEqual(state["pending"]["trade_signal"], "WAIT")
        self.assertEqual(final["analysis"]["suggestion"], "WAIT")
        self.assertEqual(final["recommendation"]["action"], "WAIT")


if __name__ == "__main__":
    unittest.main()

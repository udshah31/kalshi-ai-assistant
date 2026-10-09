import hashlib
import json
import math
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import validation
from btc_predictor import FEATURE_NAMES


SCHEMA = """
CREATE TABLE forecasts (
 forecast_id TEXT PRIMARY KEY, state_id TEXT, bar_timestamp INTEGER,
 market_ticker TEXT, forecast_issued_at REAL, features_json TEXT,
 probability_up REAL, validation_eligible INTEGER, outcome_source TEXT,
 market_snapshot_json TEXT, model_json TEXT, provenance TEXT, captured_at REAL
);
CREATE TABLE settlements (
 forecast_id TEXT PRIMARY KEY REFERENCES forecasts(forecast_id),
 market_ticker TEXT, market_close_time REAL, result TEXT, available_at REAL,
 outcome_source TEXT, snapshot_json TEXT, provenance TEXT
);
CREATE TABLE paper_entries (
 forecast_id TEXT PRIMARY KEY REFERENCES forecasts(forecast_id),
 entered_at REAL, side TEXT, quantity INTEGER, entry_price REAL, cost REAL,
 cost_allowance REAL, market_snapshot_json TEXT, checks_json TEXT, review_json TEXT
);
CREATE TABLE candles (
 product_id TEXT, timestamp INTEGER, open REAL, high REAL, low REAL,
 close REAL, volume REAL, observed_at REAL
);
CREATE TABLE bars (
 product_id TEXT, timestamp INTEGER, open REAL, high REAL, low REAL,
 close REAL, volume REAL, observed_at REAL
);
"""


def market(index, **changes):
    bar = 180000 + index * 900
    result = {
        "ticker": f"BTC-{index}", "status": "active", "open_time": bar + 900,
        "close_time": bar + 1800, "observed_at": bar + 930,
        "quote_source": "kalshi_orderbook", "yes_bid": .50, "yes_ask": .52,
        "yes_bid_size": 100, "yes_ask_size": 100,
        "no_bid": .48, "no_ask": .50, "no_bid_size": 100, "no_ask_size": 100,
    }
    result.update(changes)
    return result


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.archive = self.directory / "archive # research.sqlite"
        with closing(sqlite3.connect(self.archive)) as db, db:
            db.executescript(SCHEMA)

    def insert(self, table, values):
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with closing(sqlite3.connect(self.archive)) as db, db:
            db.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values()))

    def update(self, table, row_id, **values):
        with closing(sqlite3.connect(self.archive)) as db, db:
            db.execute(f"UPDATE {table} SET " + ", ".join(f"{key} = ?" for key in values) + " WHERE forecast_id = ?", (*values.values(), row_id))

    def add_forecast(self, index, label=1, *, settled=True, forecast_changes=None, settlement_changes=None):
        bar = 180000 + index * 900
        values = {
            "forecast_id": f"f{index}", "state_id": "state-A", "bar_timestamp": bar,
            "market_ticker": f"BTC-{index}", "forecast_issued_at": bar + 930,
            "features_json": json.dumps([(-1 if index % 2 else 1) * .5] * len(FEATURE_NAMES)),
            "probability_up": .75 if label else .25, "validation_eligible": 1,
            "outcome_source": "kalshi_official", "market_snapshot_json": json.dumps(market(index)),
            "model_json": json.dumps({"weights": [999999] * len(FEATURE_NAMES)}),
            "provenance": "fixture", "captured_at": bar + 931,
        }
        values.update(forecast_changes or {})
        self.insert("forecasts", values)
        if settled:
            result = {
                "forecast_id": values["forecast_id"], "market_ticker": values["market_ticker"],
                "market_close_time": bar + 1800, "result": "yes" if label else "no",
                "available_at": bar + 1810, "outcome_source": "kalshi_official",
                "snapshot_json": json.dumps({"ticker": values["market_ticker"], "open_time": bar + 900, "close_time": bar + 1800, "result": "yes" if label else "no"}),
                "provenance": "first local observation",
            }
            result.update(settlement_changes or {})
            self.insert("settlements", result)
        return values

    def series(self, count):
        for index in range(count):
            self.add_forecast(index, label=int(index % 3 != 1))

    def add_entry(self, index, *, side="UP", quantity=10, allowance=.03, changes=None):
        snapshot = market(index)
        price = snapshot["yes_ask" if side == "UP" else "no_ask"]
        entry = {
            "forecast_id": f"f{index}", "entered_at": snapshot["observed_at"] + 1,
            "side": side, "quantity": quantity, "entry_price": price,
            "cost": quantity * (price + allowance), "cost_allowance": allowance,
            "market_snapshot_json": json.dumps(snapshot),
            "checks_json": json.dumps(dict.fromkeys(validation.REQUIRED_ENTRY_CHECKS, True)),
            "review_json": json.dumps({"enabled": False, "status": "disabled"}),
        }
        entry.update(changes or {})
        self.insert("paper_entries", entry)
        return entry

    def evaluate(self, **kwargs):
        report = validation.evaluate_archive(self.archive, **kwargs)
        json.dumps(report, allow_nan=False)  # every result must be strict JSON-safe
        return report

    def test_missing_database_never_created(self):
        path = self.directory / "missing" / "does-not-exist.sqlite"
        with self.assertRaisesRegex(FileNotFoundError, "does not exist.*no file was created"):
            validation.evaluate_archive(path)
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())

    def test_readonly_archive_and_live_state_bytes_unchanged(self):
        self.series(12)
        state = self.directory / "live-state.json"
        state.write_text('{"model": "do not use or modify", "gate": false}', encoding="utf-8")
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.directory.iterdir()}
        real_connect = sqlite3.connect
        calls = []

        def readonly_connect(database, **kwargs):
            calls.append((database, kwargs))
            return real_connect(database, **kwargs)

        with mock.patch("validation.sqlite3.connect", side_effect=readonly_connect):
            self.evaluate(train_size=3, calibration_size=2, test_size=2)
        self.assertEqual(len(calls), 1)
        self.assertIn("mode=ro", calls[0][0])
        self.assertTrue(calls[0][1]["uri"])
        after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.directory.iterdir()}
        self.assertEqual(before, after)

    def test_empty_archive_has_no_fake_scores_or_returns(self):
        report = self.evaluate()
        self.assertEqual(report["data_quality"]["counts"]["forecasts"], 0)
        self.assertIn("no archived forecasts", report["data_quality"]["missing_evidence"])
        self.assertEqual(report["forward"]["status"], "insufficient_data")
        self.assertEqual(report["walk_forward"]["status"], "insufficient_data")
        self.assertIsNone(report["forward"]["metrics"]["accuracy"])
        self.assertIsNone(report["walk_forward"]["calibrated_metrics"]["brier_score"])
        account = report["paper_account"]
        self.assertEqual(account["status"], "no_trades")
        self.assertEqual(account["net_realized_pnl"], 0)
        self.assertIsNone(account["winrate"])
        self.assertIsNone(account["realized_return"])
        self.assertIsNone(account["max_realized_drawdown"])

    def test_missing_tables_report_missing_evidence(self):
        empty = self.directory / "no-schema.sqlite"
        sqlite3.connect(empty).close()
        report = validation.evaluate_archive(empty)
        self.assertEqual(report["forward"]["status"], "insufficient_data")
        self.assertIn("missing table: forecasts", report["data_quality"]["missing_evidence"])

    def test_tiny_sample_is_descriptive_and_insufficient(self):
        self.add_forecast(0)
        report = self.evaluate()
        self.assertEqual(report["forward"]["status"], "insufficient_data")
        self.assertEqual(report["forward"]["predictive_evidence"], "insufficient_data")
        self.assertEqual(report["forward"]["scored_count"], 1)
        self.assertEqual(report["walk_forward"]["fold_count"], 0)
        self.assertEqual(report["paper_account"]["status"], "no_trades")

    def test_forward_metrics_are_calculated_from_actual_predictions(self):
        self.add_forecast(0, label=1, forecast_changes={"probability_up": .5})
        self.add_forecast(1, label=0, forecast_changes={"probability_up": .5})
        metrics = self.evaluate()["forward"]["metrics"]
        self.assertEqual(metrics["accuracy"], .5)
        self.assertEqual(metrics["brier_score"], .25)
        self.assertAlmostEqual(metrics["log_loss"], math.log(2))
        self.assertLess(metrics["accuracy_wilson_95"]["lower"], .5)
        self.assertGreater(metrics["accuracy_wilson_95"]["upper"], .5)
        self.assertEqual(metrics["reliability_bins"][5]["count"], 2)
        self.assertEqual(metrics["reliability_bins"][5]["observed_up_rate"], .5)

    def test_missing_or_malformed_features_allow_forward_not_retraining(self):
        for index, features in enumerate((None, "invalid JSON", "[1]", "[true, 1, 1, 1, 1, 1, 1]", "[NaN, 1, 1, 1, 1, 1, 1]")):
            self.add_forecast(index, forecast_changes={"features_json": features})
        report = self.evaluate(train_size=1, calibration_size=1, test_size=1)
        self.assertEqual(report["forward"]["scored_count"], 5)
        self.assertEqual(report["walk_forward"]["eligible_feature_records"], 0)
        self.assertEqual(report["walk_forward"]["missing_feature_records"], 5)
        self.assertEqual(report["data_quality"]["missing_evidence_counts"]["features_missing_or_invalid"], 5)

    def test_late_proxy_and_ineligible_forecasts_excluded(self):
        self.add_forecast(0)
        self.add_forecast(1, forecast_changes={"forecast_issued_at": market(1)["open_time"] + 121})
        self.add_forecast(2, forecast_changes={"forecast_issued_at": market(2)["open_time"] - 1})
        self.add_forecast(3, forecast_changes={"outcome_source": "coinbase_proxy"})
        self.add_forecast(4, settlement_changes={"outcome_source": "coinbase_proxy"})
        self.add_forecast(5, forecast_changes={"validation_eligible": 0})
        self.add_forecast(6, forecast_changes={"probability_up": float("inf")})
        self.add_forecast(7, forecast_changes={"probability_up": 1.01})
        report = self.evaluate()
        self.assertEqual(report["forward"]["scored_count"], 1)
        excluded = report["data_quality"]["excluded_counts"]
        self.assertEqual(excluded["forecast_outside_first_120_seconds"], 2)
        self.assertEqual(excluded["nonofficial_forecast_source"], 1)
        self.assertEqual(excluded["invalid_probability"], 2)
        self.assertEqual(report["data_quality"]["missing_evidence_counts"]["nonofficial_settlement_source"], 1)

    def test_malformed_labels_identity_interval_and_availability_are_not_scored(self):
        self.add_forecast(0)
        self.add_forecast(1, settlement_changes={"result": None})
        self.add_forecast(2, settlement_changes={"result": "UP"})
        self.add_forecast(3, settlement_changes={"market_ticker": "OTHER"})
        self.add_forecast(4, settlement_changes={"market_close_time": market(4)["close_time"] + 900})
        self.add_forecast(5, settlement_changes={"available_at": market(5)["close_time"] - 1})
        self.add_forecast(6, settlement_changes={"available_at": None})
        self.add_forecast(7, settlement_changes={"snapshot_json": json.dumps({"ticker": "OTHER"})})
        self.add_forecast(8, settlement_changes={"snapshot_json": json.dumps({"result": "no"})})
        report = self.evaluate()
        self.assertEqual(report["forward"]["scored_count"], 1)
        missing = report["data_quality"]["missing_evidence_counts"]
        self.assertEqual(missing["missing_or_invalid_official_label"], 2)
        self.assertEqual(missing["invalid_settlement_availability"], 2)
        self.assertEqual(missing["settlement_snapshot_label_mismatch"], 3)

    def test_forecast_snapshot_mismatch_is_not_exact_contract(self):
        self.add_forecast(0, forecast_changes={"market_snapshot_json": json.dumps(market(0, ticker="wrong"))})
        self.add_forecast(1, forecast_changes={"market_snapshot_json": json.dumps(market(1, open_time=1))})
        self.assertEqual(self.evaluate()["forward"]["scored_count"], 0)

    def test_ticker_deduplicated_across_resets_without_best_outcome_selection(self):
        first = self.add_forecast(0, forecast_changes={"probability_up": .1})
        self.add_forecast(0, forecast_changes={"forecast_id": "reset-f0", "state_id": "state-B", "forecast_issued_at": first["forecast_issued_at"] + 5})
        report = self.evaluate()
        self.assertEqual(report["forward"]["scored_count"], 1)
        self.assertEqual(report["forward"]["predictions"][0]["forecast_id"], "f0")
        self.assertEqual(report["forward"]["metrics"]["accuracy"], 0)
        self.assertEqual(report["data_quality"]["excluded_counts"]["duplicate_market_ticker"], 1)

    def test_unresolved_earliest_ticker_is_not_replaced_by_resolved_reset(self):
        first = self.add_forecast(0, settled=False)
        self.add_forecast(0, forecast_changes={"forecast_id": "reset-f0", "state_id": "state-B", "forecast_issued_at": first["forecast_issued_at"] + 5})
        report = self.evaluate()
        self.assertEqual(report["forward"]["scored_count"], 0)
        self.assertEqual(report["forward"]["unscored_count"], 1)

    def test_midpoint_baseline_requires_real_fresh_aligned_quotes(self):
        self.add_forecast(0)
        self.add_forecast(1, forecast_changes={"market_snapshot_json": None})
        self.add_forecast(2, forecast_changes={"market_snapshot_json": "{"})
        self.add_forecast(3, forecast_changes={"market_snapshot_json": json.dumps(market(3, yes_ask=None, yes_mid=.51))})
        self.add_forecast(4, forecast_changes={"market_snapshot_json": json.dumps(market(4, observed_at=market(4)["observed_at"] - 61))})
        self.add_forecast(5, forecast_changes={"market_snapshot_json": json.dumps(market(5, observed_at=market(5)["observed_at"] + 1))})
        self.add_forecast(6, forecast_changes={"market_snapshot_json": json.dumps(market(6, yes_bid=.6, yes_ask=.5))})
        report = self.evaluate()
        baseline = report["forward"]["baselines"]["market_midpoint"]
        self.assertEqual(report["forward"]["scored_count"], 7)
        self.assertEqual(baseline["eligible_count"], 1)
        self.assertEqual(baseline["missing_count"], 6)
        self.assertAlmostEqual(baseline["metrics"]["brier_score"], (.51 - 1) ** 2)

    def test_walk_forward_rolling_boundaries_and_frozen_model(self):
        self.series(16)
        report = self.evaluate(train_size=3, calibration_size=2, test_size=2, epochs=3)
        walk = report["walk_forward"]
        self.assertEqual(walk["fold_count"], 5)
        self.assertEqual(walk["test_count"], 10)
        self.assertEqual(walk["status"], "insufficient_data")
        self.assertEqual(walk["unused_tail_count"], 1)
        first = walk["folds"][0]
        self.assertEqual(first["train"]["forecast_ids"], ["f0", "f1", "f2"])
        self.assertEqual(first["calibration"]["forecast_ids"], ["f3", "f4"])
        self.assertEqual(first["test"]["forecast_ids"], ["f5", "f6"])
        self.assertAlmostEqual(first["training_prior_probability_up"], 2 / 3)
        for fold in walk["folds"]:
            self.assertLess(fold["train"]["latest_label_available_at"], fold["block_start"])
            self.assertLess(fold["calibration"]["latest_label_available_at"], fold["block_start"])
            self.assertFalse(set(fold["test"]["forecast_ids"]) & set(fold["train"]["forecast_ids"]))
            self.assertFalse(set(fold["test"]["forecast_ids"]) & set(fold["calibration"]["forecast_ids"]))
            self.assertEqual(fold["calibration_fit"]["count"], 2)
            self.assertFalse(fold["calibration_fit"]["applied_live"])

    def test_future_labels_do_not_change_earlier_model_or_calibration(self):
        self.series(16)
        before = self.evaluate(train_size=3, calibration_size=2, test_size=2)["walk_forward"]
        for index in range(9, 16):
            self.update("settlements", f"f{index}", result="no", snapshot_json=json.dumps({"ticker": f"BTC-{index}", "result": "no"}))
        after = self.evaluate(train_size=3, calibration_size=2, test_size=2)["walk_forward"]
        for index in (0, 1):
            self.assertEqual(before["folds"][index], after["folds"][index])
        # The next fold's held-out outcomes changed, but neither fit did.
        self.assertEqual(before["folds"][2]["calibration_fit"], after["folds"][2]["calibration_fit"])
        for old, new in zip(before["folds"][2]["predictions"], after["folds"][2]["predictions"]):
            self.assertEqual(old["raw_probability_up"], new["raw_probability_up"])
            self.assertEqual(old["calibrated_probability_up"], new["calibrated_probability_up"])

    def test_calibration_uses_heldout_labels_not_test_outcomes_or_training_scores(self):
        self.series(9)
        before = self.evaluate(train_size=3, calibration_size=2, test_size=2)["walk_forward"]["folds"][0]
        for index in (5, 6):
            self.update("settlements", f"f{index}", result="no", snapshot_json=json.dumps({"result": "no"}))
        test_changed = self.evaluate(train_size=3, calibration_size=2, test_size=2)["walk_forward"]["folds"][0]
        self.assertEqual(before["calibration_fit"], test_changed["calibration_fit"])
        for old, new in zip(before["predictions"], test_changed["predictions"]):
            self.assertEqual(old["raw_probability_up"], new["raw_probability_up"])
            self.assertEqual(old["calibrated_probability_up"], new["calibrated_probability_up"])
        for index in (3, 4):
            self.update("settlements", f"f{index}", result="no", snapshot_json=json.dumps({"result": "no"}))
        calibration_changed = self.evaluate(train_size=3, calibration_size=2, test_size=2)["walk_forward"]["folds"][0]
        self.assertNotEqual(before["calibration_fit"], calibration_changed["calibration_fit"])
        for old, new in zip(before["predictions"], calibration_changed["predictions"]):
            self.assertEqual(old["raw_probability_up"], new["raw_probability_up"])
            self.assertNotEqual(old["calibrated_probability_up"], new["calibrated_probability_up"])

    def test_delayed_settlement_not_used_just_because_market_closed(self):
        self.series(14)
        self.update("settlements", "f2", available_at=market(10)["observed_at"])
        walk = self.evaluate(train_size=3, calibration_size=2, test_size=2)["walk_forward"]
        first = walk["folds"][0]
        self.assertEqual(first["test"]["forecast_ids"], ["f6", "f7"])
        self.assertEqual(first["train"]["forecast_ids"], ["f0", "f1", "f3"])
        self.assertNotIn("f2", first["calibration"]["forecast_ids"])
        self.assertGreater(walk["delayed_labels_excluded_at_block_start"], 0)

    def test_availability_equal_to_block_start_is_excluded(self):
        self.series(10)
        self.update("settlements", "f4", available_at=market(5)["observed_at"])
        first = self.evaluate(train_size=3, calibration_size=2, test_size=2)["walk_forward"]["folds"][0]
        self.assertEqual(first["test"]["forecast_ids"], ["f6", "f7"])

    def test_unknown_test_outcomes_do_not_change_fold_assignment_or_predictions(self):
        self.series(11)
        before = self.evaluate(train_size=3, calibration_size=2, test_size=2)["walk_forward"]["folds"][0]
        with closing(sqlite3.connect(self.archive)) as db, db:
            db.execute("DELETE FROM settlements WHERE forecast_id IN ('f5', 'f6')")
        after = self.evaluate(train_size=3, calibration_size=2, test_size=2)["walk_forward"]["folds"][0]
        self.assertEqual(before["test"]["forecast_ids"], after["test"]["forecast_ids"])
        self.assertEqual(after["status"], "incomplete_outcomes")
        self.assertEqual(after["scored_count"], 0)
        for old, new in zip(before["predictions"], after["predictions"]):
            self.assertEqual(old["raw_probability_up"], new["raw_probability_up"])
            self.assertEqual(old["calibrated_probability_up"], new["calibrated_probability_up"])
            self.assertIsNone(new["label"])

    def test_archived_latest_live_model_is_never_used(self):
        self.series(10)
        before = self.evaluate(train_size=3, calibration_size=2, test_size=2)["walk_forward"]
        for index in range(10):
            self.update("forecasts", f"f{index}", model_json=json.dumps({"bias": -99999, "updates": 99999, "weights": [-99999] * 7}))
        after = self.evaluate(train_size=3, calibration_size=2, test_size=2)["walk_forward"]
        self.assertEqual(before, after)

    def test_recorded_fee_slippage_costs_unresolved_cash_and_realized_drawdown(self):
        self.add_forecast(0, label=1)
        self.add_forecast(1, label=1, forecast_changes={"probability_up": .25})
        self.add_forecast(2, settled=False)
        self.add_entry(0, quantity=10, allowance=.06)
        self.add_entry(1, side="DOWN", quantity=5, allowance=.05)
        self.add_entry(2, quantity=3)
        account = self.evaluate(train_size=1, calibration_size=1, test_size=1)["paper_account"]
        self.assertEqual(account["settled_count"], 2)
        self.assertEqual(account["unresolved_count"], 1)
        self.assertEqual(account["invalid_entry_count"], 0)
        self.assertAlmostEqual(account["net_realized_pnl"], 1.45)
        self.assertAlmostEqual(account["cash_cost"], 10.2)
        self.assertAlmostEqual(account["unresolved_cash_cost"], 1.65)
        self.assertAlmostEqual(account["cash_balance_excluding_invalid_entries"], 99.8)
        self.assertEqual(account["winrate"], .5)
        self.assertAlmostEqual(account["realized_return"], .0145)
        self.assertAlmostEqual(account["max_realized_drawdown"], 2.75)
        self.assertAlmostEqual(account["resolved_equity_curve"][1]["equity"], 104.2)
        self.assertAlmostEqual(account["resolved_equity_curve"][2]["equity"], 101.45)
        self.assertNotIn("sharpe", account)

    def test_missing_prices_cost_allowance_quote_depth_and_checks_fail_account_audit(self):
        for index in range(9):
            self.add_forecast(index)
        self.add_entry(0, changes={"entry_price": None})
        self.add_entry(1, changes={"cost_allowance": None})
        self.add_entry(2, changes={"cost": 5.2})  # omits recorded allowance
        self.add_entry(3, changes={"market_snapshot_json": json.dumps(market(3, yes_ask_size=1))})
        self.add_entry(4, changes={"market_snapshot_json": json.dumps(market(4, observed_at=market(4)["observed_at"] - 61))})
        self.add_entry(5, changes={"market_snapshot_json": json.dumps(market(5, yes_ask=None))})
        self.add_entry(6, changes={"checks_json": json.dumps({"probability": True})})
        self.add_entry(7, changes={"checks_json": json.dumps({**dict.fromkeys(validation.REQUIRED_ENTRY_CHECKS, True), "budget": False})})
        self.add_entry(8, changes={"entered_at": market(8)["close_time"] + 1})
        account = self.evaluate()["paper_account"]
        self.assertEqual(account["invalid_entry_count"], 9)
        self.assertEqual(account["settled_count"], 0)
        self.assertEqual(account["net_realized_pnl"], 0)
        self.assertIsNone(account["winrate"])
        reasons = {reason for entry in account["invalid_entries"] for reason in entry["reasons"]}
        self.assertIn("recorded_cost_omits_allowance_or_fill_cost", reasons)
        self.assertIn("entry_depth_missing_or_insufficient", reasons)
        self.assertIn("required_deterministic_checks_missing_or_failed", reasons)

    def test_recorded_review_must_be_approved_fresh_and_bound_when_enabled(self):
        for index in range(6):
            self.add_forecast(index)
            entered = market(index)["observed_at"] + 1
            review = {
                "enabled": True, "status": "ok", "choice": "approve_trade", "confidence": .9,
                "reviewed_at": entered - 1, "expires_at": entered + 30,
                "fingerprint": "original-recorded-fingerprint", "applies_to_current_evidence": True,
            }
            if index == 1:
                review["expires_at"] = entered - 1
            elif index == 2:
                review["choice"] = "wait"
            elif index == 3:
                review["applies_to_current_evidence"] = False
            elif index == 4:
                review["current_fingerprint"] = "different-evidence"
            elif index == 5:
                review.pop("fingerprint")
            checks = {**dict.fromkeys(validation.REQUIRED_ENTRY_CHECKS, True), "typesafe": True}
            self.add_entry(index, changes={"checks_json": json.dumps(checks), "review_json": json.dumps(review)})
        account = self.evaluate(train_size=1, calibration_size=1, test_size=1)["paper_account"]
        self.assertEqual(account["settled_count"], 1)
        self.assertEqual(account["invalid_entry_count"], 5)
        self.assertEqual(account["settled_entries"][0]["forecast_id"], "f0")
        self.assertIn("not_independently_reconstructable", account["settled_entries"][0]["warnings"][0])

    def test_nonofficial_or_mismatched_settlement_leaves_valid_entry_unresolved(self):
        self.add_forecast(0, settlement_changes={"outcome_source": "coinbase_proxy"})
        self.add_forecast(1, settlement_changes={"market_ticker": "other"})
        self.add_entry(0)
        self.add_entry(1)
        account = self.evaluate()["paper_account"]
        self.assertEqual(account["settled_count"], 0)
        self.assertEqual(account["unresolved_count"], 2)
        self.assertAlmostEqual(account["cash_cost"], 11)
        self.assertEqual(account["payouts"], 0)

    def test_budget_cannot_use_future_payout_or_hide_unresolved_commitments(self):
        self.add_forecast(0, settlement_changes={"available_at": market(4)["observed_at"]})
        self.add_forecast(1, settled=False)
        self.add_forecast(2)
        self.add_entry(0, quantity=100)  # $55 committed, payout not yet observed
        self.add_entry(1, quantity=90)   # $49.50 cannot fit remaining $45
        self.add_entry(2, quantity=80)   # $44 still fundable, not a hypothetical trade
        account = self.evaluate()["paper_account"]
        self.assertEqual(account["settled_count"], 2)
        self.assertEqual(account["invalid_entry_count"], 1)
        self.assertEqual(account["invalid_entries"][0]["forecast_id"], "f1")
        self.assertIn("recorded_entry_exceeds_available_paper_cash", account["invalid_entries"][0]["reasons"])
        self.assertAlmostEqual(account["cash_cost"], 99)

    def test_independent_state_resets_do_not_combine_cash_or_return(self):
        self.add_forecast(0, forecast_changes={"state_id":"run-A"})
        self.add_forecast(1, label=0, forecast_changes={"state_id":"run-B"})
        self.add_entry(0)
        self.add_entry(1)
        account = self.evaluate()["paper_account"]
        self.assertEqual(account["status"], "multiple_independent_runs")
        self.assertEqual(account["run_count"], 2)
        self.assertIsNone(account["net_realized_pnl"])
        self.assertIsNone(account["realized_return"])
        self.assertEqual(account["runs"]["run-A"]["starting_equity"], 100)
        self.assertEqual(account["runs"]["run-B"]["starting_equity"], 100)
        self.assertGreater(account["runs"]["run-A"]["net_realized_pnl"], 0)
        self.assertLess(account["runs"]["run-B"]["net_realized_pnl"], 0)

    def test_extreme_features_and_malformed_ids_remain_json_safe(self):
        self.series(8)
        for index in range(8):
            self.update("forecasts", f"f{index}", features_json=json.dumps([(-1 if index % 2 else 1) * 1e308] + [1e308] * 6))
        report = self.evaluate(train_size=3, calibration_size=2, test_size=2)
        self.assertEqual(report["walk_forward"]["successful_fold_count"], 0)
        self.assertEqual(report["walk_forward"]["folds"][0]["status"], "numerical_failure")
        self.update("forecasts", "f0", forecast_id=sqlite3.Binary(b"invalid-text-id"))
        report = self.evaluate()
        self.assertIsNone(report["data_quality"]["rejected_forecasts"][0]["forecast_id"])

    def test_collection_quality_reports_late_rate_and_settlement_delay_without_changing_scores(self):
        self.add_forecast(0, settled=True)
        self.add_forecast(1, forecast_changes={"forecast_issued_at": 180000+900+200}, settled=True)
        report = self.evaluate()
        quality = report["data_quality"]["collection_quality"]
        self.assertEqual(quality["official_forecast_rows"], 2)
        self.assertEqual(quality["timely_forecast_rows"], 1)
        self.assertEqual(quality["excluded_late_forecasts"], 1)
        self.assertEqual(quality["settlement_observations"], 2)
        self.assertEqual(quality["settlement_delay_seconds"]["median"], 10)
        self.assertEqual(report["forward"]["scored_count"], 1)
        self.assertEqual(quality["status"], "collecting_timely_evidence")

    def test_tiny_report_identifies_missing_evidence_counts(self):
        self.add_forecast(0)
        report = self.evaluate()
        self.assertEqual(report["forward"]["additional_scored_samples_needed"], 99)
        self.assertEqual(report["walk_forward"]["additional_scored_samples_needed"], 100)
        self.assertTrue(any("more prospective evidence needed" in item for item in report["data_quality"]["missing_evidence"]))

    def test_positive_integer_options_required(self):
        for name in ("train_size", "calibration_size", "test_size", "epochs"):
            for value in (0, -1, True, 1.5):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, "positive integer"):
                    self.evaluate(**{name: value})

    def test_cli_outputs_atomic_json_and_preserves_archive(self):
        self.series(10)
        output = self.directory / "report.json"
        output.write_text("old report", encoding="utf-8")
        before = self.archive.read_bytes()
        result = subprocess.run([sys.executable, str(Path(validation.__file__)), "--archive-file", str(self.archive), "--output-json", str(output), "--train-size", "3", "--calibration-size", "2", "--test-size", "2", "--epochs", "2"], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), json.loads(output.read_text(encoding="utf-8")))
        self.assertEqual(self.archive.read_bytes(), before)
        self.assertFalse(list(self.directory.glob("*.tmp")))

    def test_summary_json_is_distinct_and_preserves_archive(self):
        self.series(10)
        output = self.directory/"reports"/"full.json"
        summary = self.directory/"reports"/"summary.json"
        before = self.archive.read_bytes()
        command = [sys.executable,str(Path(validation.__file__)),"--archive-file",str(self.archive),
                   "--output-json",str(output),"--summary-json",str(summary)]
        result = subprocess.run(command,text=True,capture_output=True,check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text()), json.loads(result.stdout))
        small = json.loads(summary.read_text())
        self.assertNotIn("folds", small["walk_forward"])
        self.assertFalse(small["calibration_applied_live"])
        self.assertEqual(self.archive.read_bytes(),before)
        result = subprocess.run([*command[:-1],str(output)],text=True,capture_output=True,check=False)
        self.assertEqual(result.returncode,2)
        self.assertIn("different output paths",result.stderr)

    def test_cli_missing_database_error_and_output_cannot_overwrite_database(self):
        missing = self.directory / "missing.sqlite"
        result = subprocess.run([sys.executable, str(Path(validation.__file__)), "--archive-file", str(missing)], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not exist", result.stderr)
        self.assertFalse(missing.exists())
        before = self.archive.read_bytes()
        result = subprocess.run([sys.executable, str(Path(validation.__file__)), "--archive-file", str(self.archive), "--output-json", str(self.archive)], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("must not replace", result.stderr)
        self.assertEqual(self.archive.read_bytes(), before)

    def test_context_feature_vector_uses_only_completed_point_in_time_bars(self):
        bars = [
            {"product_id": "BTC-USD", "timestamp": 180000 + index * 900, "open": 100 + index,
             "high": 101 + index, "low": 99 + index, "close": 100 + index,
             "volume": 100, "observed_at": 180000 + index * 900 + 900}
            for index in range(25)
        ]
        frozen = [.11, .22, .33, .44, .55, .66, .77]
        features = validation.context_feature_vector(bars, 180000 + 24 * 900,
                                                      180000 + 24 * 900 + 930,
                                                      (1, 2, 4), frozen_features=frozen)
        self.assertEqual(len(features), len(FEATURE_NAMES))
        self.assertEqual(features[0], .11)
        self.assertAlmostEqual(features[1], (124 / 122 - 1) * 100, places=8)
        self.assertAlmostEqual(features[2], (124 / 120 - 1) * 100, places=8)
        self.assertEqual(features[3:], [.44, .55, .66, .77])
        self.assertEqual(frozen, [.11, .22, .33, .44, .55, .66, .77])
        self.assertIsNone(validation.context_feature_vector(
            bars, 180000 + 24 * 900, 180000 + 24 * 900 + 899, (1, 2, 4),
            frozen_features=frozen))

    def context_series(self, count, *, unsettled=()):
        for index in range(count):
            self.add_forecast(index, label=int(index % 3 != 1), settled=index not in unsettled)
        with closing(sqlite3.connect(self.archive)) as db, db:
            for index in range(-24, count):
                timestamp = 180000 + index * 900
                db.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)",
                           ("BTC-USD", timestamp, 100 + index, 101 + index,
                            99 + index, 100 + index, 100, timestamp + 900))

    def test_context_comparison_uses_five_bars_and_only_changes_two_features(self):
        self.context_series(15)
        with closing(sqlite3.connect(self.archive)) as db, db:
            db.execute("DELETE FROM bars WHERE timestamp < ?", (180000 - 4 * 900,))
        report = validation.compare_context_models(self.archive, train_size=8, test_size=5, epochs=2)
        self.assertEqual(report["coverage"]["both_feature_sets"], 15)
        self.assertEqual(report["folds"][0]["test_forecast_ids"], ["f8", "f9", "f10", "f11", "f12"])

    def test_context_missing_outcome_does_not_change_test_membership_or_predictions(self):
        self.context_series(18)
        before = validation.compare_context_models(self.archive, train_size=8, test_size=5, epochs=2)
        with closing(sqlite3.connect(self.archive)) as db, db:
            db.execute("DELETE FROM settlements WHERE forecast_id = ?", ("f10",))
        after = validation.compare_context_models(self.archive, train_size=8, test_size=5, epochs=2)
        expected = ["f8", "f9", "f10", "f11", "f12"]
        self.assertEqual(before["folds"][0]["test_forecast_ids"], expected)
        self.assertEqual(after["folds"][0]["test_forecast_ids"], expected)
        self.assertEqual(after["folds"][0]["scored_count"], 4)
        for original, missing in zip(before["folds"][0]["predictions"], after["folds"][0]["predictions"]):
            self.assertEqual(original["current_probability_up"], missing["current_probability_up"])
            self.assertEqual(original["proposed_probability_up"], missing["proposed_probability_up"])
        self.assertIsNone(after["folds"][0]["predictions"][2]["label"])
        self.assertEqual(after["models"]["current_15m_45m_3h"]["count"], 9)
        self.assertEqual(after["models"]["proposed_15m_30m_1h"]["count"], 9)
        self.assertEqual(after["models"]["constant_50_percent"]["count"], 9)

    def test_context_delayed_training_label_advances_one_record_to_first_valid_fold(self):
        self.context_series(14)
        self.update("settlements", "f7", available_at=market(8)["observed_at"] + 1)
        report = validation.compare_context_models(self.archive, train_size=8, test_size=5, epochs=2)
        self.assertEqual(len(report["folds"]), 1)
        first = report["folds"][0]
        self.assertEqual(first["test_start_index"], 9)
        self.assertEqual(first["test_forecast_ids"], ["f9", "f10", "f11", "f12", "f13"])
        self.assertLess(first["train"]["latest_label_available_at"], first["block_start"])
        self.assertEqual(report["coverage"]["delayed_labels_excluded_at_block_start"], 1)

    def test_context_earliest_ticker_issuance_wins_even_without_a_settlement(self):
        self.context_series(10, unsettled=(6,))
        self.add_forecast(6, forecast_changes={"forecast_id": "f6-retry", "forecast_issued_at": market(6)["observed_at"] + 1})
        report = validation.compare_context_models(self.archive, train_size=3, test_size=5, epochs=2)
        self.assertEqual(report["folds"][0]["test_forecast_ids"], ["f3", "f4", "f5", "f6", "f7"])
        self.assertIsNone(report["folds"][0]["predictions"][3]["label"])
        self.assertEqual(report["coverage"]["duplicate_market_tickers"], 1)

    def test_context_completed_bars_received_late_or_from_other_product_are_excluded(self):
        bars = [{"product_id": "BTC-USD", "timestamp": 180000 + index * 900,
                 "open": 100, "high": 101, "low": 99, "close": 100,
                 "volume": 100, "observed_at": 180000 + index * 900 + 900}
                for index in range(5)]
        target, issued = 183600, 184530
        self.assertIsNotNone(validation.context_feature_vector(bars, target, issued, (1, 2, 4), frozen_features=[.5]*7))
        for change in ({"observed_at": issued + .001}, {"product_id": "ETH-USD"},
                       {"observed_at": target + 899}):
            changed = [*bars[:-1], {**bars[-1], **change}]
            with self.subTest(change=change):
                self.assertIsNone(validation.context_feature_vector(changed, target, issued, (1, 2, 4), frozen_features=[.5]*7))

    def test_context_missing_earliest_features_cannot_select_later_ticker_retry(self):
        self.context_series(10)
        self.update("forecasts", "f6", features_json=None)
        self.add_forecast(6, forecast_changes={"forecast_id": "f6-retry", "forecast_issued_at": market(6)["observed_at"] + 1})
        report = validation.compare_context_models(self.archive, train_size=3, test_size=5, epochs=2)
        self.assertEqual(report["folds"][0]["test_forecast_ids"], ["f3", "f4", "f5", "f7", "f8"])
        self.assertEqual(report["coverage"]["both_feature_sets"], 9)

    def test_context_midpoint_comparisons_use_identical_scored_quote_subset(self):
        self.context_series(15)
        self.update("forecasts", "f10", market_snapshot_json=json.dumps(market(10, observed_at=market(10)["observed_at"] + 1)))
        report = validation.compare_context_models(self.archive, train_size=8, test_size=5, epochs=2)
        self.assertEqual(report["coverage"]["walk_forward_scored"], 5)
        self.assertEqual(report["market_midpoint_baseline"]["count"], 4)
        for metrics in report["market_midpoint_same_subset"].values():
            self.assertEqual(metrics["count"], 4)
        self.assertAlmostEqual(report["market_midpoint_baseline"]["brier_score"], .2401)
        self.assertEqual(report["market_midpoint_same_subset"]["constant_50_percent"]["brier_score"], .25)

    def test_context_numerical_failure_never_exports_nonfinite_model_scores(self):
        self.context_series(15)
        for index in range(15):
            self.update("forecasts", f"f{index}", features_json=json.dumps(
                [(-1 if index % 2 else 1) * 1e308] + [1e308]*6))
        report = validation.compare_context_models(self.archive, train_size=8, test_size=5, epochs=2)
        self.assertEqual(report["folds"][0]["status"], "numerical_failure")
        self.assertEqual(report["coverage"]["walk_forward_scored"], 0)
        json.dumps(report, allow_nan=False)

    def test_context_comparison_is_chronological_and_read_only(self):
        self.series(40)
        with closing(sqlite3.connect(self.archive)) as db, db:
            for index in range(41):
                timestamp = 180000 + index * 900
                db.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)",
                           ("BTC-USD", timestamp, 100 + index, 101 + index,
                            99 + index, 100 + index, 100, timestamp + 900))
        before = self.archive.read_bytes()
        result = validation.compare_context_models(
            self.archive, train_size=10, test_size=5, epochs=2,
        )
        self.assertEqual(result["status"], "descriptive_evaluation")
        self.assertGreater(result["coverage"]["both_feature_sets"], 0)
        self.assertEqual(result["folds"][0]["test_start_index"], 10)
        self.assertIn("current_15m_45m_3h", result["models"])
        self.assertIn("proposed_15m_30m_1h", result["models"])
        self.assertIn("constant_50_percent", result["models"])
        self.assertEqual(self.archive.read_bytes(), before)

    def test_context_comparison_cli_writes_atomic_report(self):
        self.series(40)
        with closing(sqlite3.connect(self.archive)) as db, db:
            for index in range(41):
                timestamp = 180000 + index * 900
                db.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)",
                           ("BTC-USD", timestamp, 100 + index, 101 + index,
                            99 + index, 100 + index, 100, timestamp + 900))
        output = self.directory / "context.json"
        before = self.archive.read_bytes()
        result = subprocess.run([
            sys.executable, str(Path(validation.__file__)), "compare-context",
            "--archive-file", str(self.archive), "--output-json", str(output),
            "--train-size", "10", "--test-size", "5", "--epochs", "2",
        ], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), json.loads(output.read_text()))
        self.assertEqual(self.archive.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

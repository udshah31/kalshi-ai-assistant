import math
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import late_window
import late_window_report as report

AVG = range(-55, 1, 5)
BASE = 1_700_000_100


def window(i, proxy=100_010.0, target=100_000.0, result="yes", expiration=100_000.0,
           ask=0.90, size=100, mid=None):
    mid = proxy if mid is None else mid
    snap = {"yes_ask": ask, "no_ask": 0.20, "yes_ask_size": size, "no_ask_size": size, "error": None}
    return {"close_ts": BASE + 900 * i, "mids": {o: mid for o in range(-600, 1, 5)},
            "target_price": target, "expiration_value": expiration, "result": result,
            "snapshots": {-60: dict(snap), -30: dict(snap), -15: dict(snap)}}


def windows(n, bad=0):
    return [window(i, result="no" if i < bad else "yes") for i in range(n)]


class LateWindowReportTest(unittest.TestCase):
    def test_window_average_requires_all_twelve(self):
        mids = {o: 1.0 for o in AVG}
        self.assertEqual(report.window_average(mids, 0), 1.0)
        mids[-30] = None
        self.assertIsNone(report.window_average(mids, 0))

    def test_incomplete_average_is_excluded_and_counted(self):
        ws = windows(3)
        ws[1]["mids"][-10] = None
        result = report.proxy_check(ws)
        self.assertEqual((result["windows"], result["excluded_incomplete"]), (2, 1))

    def test_proxy_check_thresholds(self):
        self.assertFalse(report.proxy_check(windows(499))["passed"])
        good = report.proxy_check(windows(500, bad=10))
        self.assertTrue(good["passed"])
        self.assertAlmostEqual(good["settlement_error_bps"], 1.0)
        self.assertFalse(report.proxy_check(windows(500, bad=11))["passed"])

    def test_late_probability_edges(self):
        rem = []
        self.assertEqual(report.late_probability([2.0] * 12, 2.0, rem, 0, 0.001, 1.0), 1.0)
        rem = list(AVG)
        p = lambda s: report.late_probability([], 100.0, rem, -60, s, 100.0)
        self.assertAlmostEqual(p(0.001), 0.5)
        above = lambda s: report.late_probability([], 100.0, rem, -60, s, 99.99)
        self.assertGreater(above(0.00001), above(0.001))
        self.assertGreater(above(0.001), 0.5)

    def test_backtest_skips_when_proxy_fails(self):
        self.assertEqual(report.backtest(windows(10))["status"], "proxy_check_failed")

    def test_backtest_applies_edge_and_depth_filters(self):
        ws = windows(500)
        ws[0]["snapshots"][-30]["yes_ask"] = 0.93  # edge 0.04
        ws[1]["snapshots"][-30]["yes_ask_size"] = 24
        result = report.backtest(ws)
        self.assertEqual(result["status"], "evaluated")
        entry = result["by_time"]["-30"]
        self.assertEqual(entry["eligible_windows"], 500)
        self.assertEqual(entry["trades"], 498)
        self.assertFalse(result["success"])  # 5 days, < 1000 windows

    def test_pre_registered_rule_constants(self):
        self.assertEqual(report.COST, 0.03)
        self.assertEqual(report.MIN_DEPTH, 25)
        self.assertEqual(report.MIN_EDGE, 0.05)
        self.assertEqual(report.TIMES, (-60, -30, -15))
        self.assertEqual(report.PRIMARY_TIME, -30)
        self.assertEqual(report.MIN_DAYS, 20)
        self.assertEqual(report.MIN_BACKTEST_WINDOWS, 1000)
        self.assertEqual(report.PROXY_MIN_WINDOWS, 500)
        self.assertEqual(report.PROXY_MIN_AGREEMENT, 0.98)
        self.assertEqual(report.PROXY_MAX_ERROR_BPS, 2.0)

    def test_paper_trade_uses_this_rules_cost(self):
        trade = report.paper_trade(1.0, 0.90, 0.20, 1)
        self.assertEqual(trade["side"], "yes")
        self.assertAlmostEqual(trade["edge"], 0.07)
        self.assertAlmostEqual(trade["pnl"], 0.07)

    def test_sigma_and_probability_on_a_moving_path_with_one_missing_mid(self):
        # log-price returns alternate +2a, -a from -600 s; mean a/2, so each deviation is 1.5a.
        a = 0.001
        offsets = list(range(-600, 1, 5))
        mids = {o: 100_000.0 * math.exp((k // 2) * a + (2 * a if k % 2 else 0)) for k, o in enumerate(offsets)}
        mids[-200] = None  # drops the +2a and -a returns either side of it, leaving 56 of each
        w = {**window(0), "mids": mids, "target_price": 105_780.0}
        observed, spot, remaining, sigma = report._inputs(w, -30)
        expected_sigma = 1.5 * a / math.sqrt(5)
        self.assertAlmostEqual(sigma / expected_sigma, 1.0, delta=1e-4)
        self.assertEqual(remaining, list(range(-25, 1, 5)))
        # n = 12 averaged mids; taus 5..30 s give sum(min(a, b)) = 5 * 91 = 455.
        mean = (sum(observed) + 6 * spot) / 12
        z = (mean - 105_780.0) / (spot * expected_sigma * math.sqrt(455) / 12)
        expected_p = 0.5 * (1 + math.erf(z / math.sqrt(2)))
        p = report.late_probability(observed, spot, remaining, -30, sigma, 105_780.0)
        self.assertAlmostEqual(p, expected_p, delta=1e-4)
        self.assertTrue(0.05 < p < 0.95)

    def test_path_below_min_returns_is_excluded(self):
        w = window(0)
        for o in range(-600, -75, 5):
            w["mids"][o] = None  # -75..-30 leaves 10 mids, 9 returns
        self.assertIsNone(report._inputs(w, -30))
        self.assertEqual(report._evaluate([w], -30)["eligible_windows"], 0)
        w["mids"][-80] = w["mids"][-75]
        self.assertIsNotNone(report._inputs(w, -30))

    def test_incomplete_settlement_average_is_still_eligible_for_the_backtest(self):
        ws = windows(3)
        ws[1]["mids"][-10] = None  # after t, so the trade decision never saw it
        self.assertEqual(report.proxy_check(ws)["windows"], 2)
        self.assertEqual(report._evaluate(ws, -30)["eligible_windows"], 3)
        ws[2]["result"] = None
        self.assertEqual(report._evaluate(ws, -30)["eligible_windows"], 2)

    def test_success_with_enough_days_and_winning_windows(self):
        ws = windows(2000)  # 96 windows a day: 21 UTC days
        result = report.backtest(ws)
        primary = result["by_time"]["-30"]
        self.assertGreaterEqual(primary["eligible_days"], 20)
        self.assertGreaterEqual(primary["eligible_windows"], 1000)
        self.assertEqual(primary["trades"], 2000)
        self.assertAlmostEqual(primary["pnl_per_trade"], 0.07)
        self.assertGreater(primary["pnl_per_trade_interval_95"][0], 0)
        self.assertIs(result["success"], True)

    def test_store_round_trip(self):
        close = 1_800_000_900
        samples = [{"close_ts": close, "offset_s": o, "exchange": e, "bid": 100.0 + i, "ask": 102.0 + i,
                    "exchange_ts": None, "received_at": close + o, "error": "timeout" if e == "gemini" else None}
                   for o in range(-600, 1, 5) for i, e in enumerate(late_window.EXCHANGES)]
        snapshots = [{"close_ts": close, "offset_s": o, "ticker": "KXBTC15M-T", "yes_bid": 0.5, "yes_ask": 0.52,
                      "yes_ask_size": 30.0, "no_bid": 0.46, "no_ask": 0.5, "no_ask_size": 40.0,
                      "observed_at": close + o, "error": None} for o in late_window.SNAPSHOT_OFFSETS]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "lw.sqlite3"
            with closing(late_window.connect_store(path)) as db:
                late_window.write_window(db, {"close_ts": close, "ticker": "KXBTC15M-T", "started_at": close - 700,
                                              "max_drift_seconds": 0.1, "samples": samples, "snapshots": snapshots})
                late_window.record_official(db, {
                    "ticker": "KXBTC15M-T", "close_ts": close, "target_price": 102.5, "expiration_value": 102.0,
                    "result": "no", "settled_at": close + 5, "observed_at": close + 60})
            (loaded,) = report.load_windows(path)
        self.assertEqual(loaded["close_ts"], close)
        self.assertEqual(loaded["mids"][-30], 102.0)  # median of 101, 102, 103; gemini's gap is ignored
        self.assertEqual(len(loaded["mids"]), 121)
        self.assertEqual(loaded["snapshots"][-30]["yes_ask_size"], 30.0)
        self.assertEqual((loaded["target_price"], loaded["expiration_value"], loaded["result"]), (102.5, 102.0, "no"))
        self.assertEqual(report.window_average(loaded["mids"]), 102.0)


if __name__ == "__main__":
    unittest.main()

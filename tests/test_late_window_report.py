import unittest

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


if __name__ == "__main__":
    unittest.main()

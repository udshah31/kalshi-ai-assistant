import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

import late_window

OFFSETS = late_window.PRICE_OFFSETS
EXCHANGES = ("coinbase", "kraken", "bitstamp", "gemini")
SNAPSHOT_OFFSETS = late_window.SNAPSHOT_OFFSETS


def make_window(close_ts, failing=(), snapshot_errors=()):
    samples = [
        {"close_ts": close_ts, "offset_s": o, "exchange": e, "bid": 1.0, "ask": 2.0,
         "exchange_ts": close_ts + o, "received_at": close_ts + o,
         "error": "boom" if (e, o) in failing else None}
        for o in OFFSETS for e in EXCHANGES
    ]
    snapshots = [
        {"close_ts": close_ts, "offset_s": o, "ticker": "KXBTC15M-T", "yes_bid": 0.5, "yes_ask": 0.52,
         "yes_ask_size": 10.0, "no_bid": 0.48, "no_ask": 0.5, "no_ask_size": 8.0,
         "observed_at": close_ts + o, "error": "boom" if o in snapshot_errors else None}
        for o in SNAPSHOT_OFFSETS
    ]
    return {"close_ts": close_ts, "ticker": "KXBTC15M-T", "started_at": close_ts - 600,
            "max_drift_seconds": 0.2, "samples": samples, "snapshots": snapshots}


class LateWindowStoreTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = Path(d.name) / "lw.sqlite3"

    def test_store_path_and_schema(self):
        self.assertEqual(late_window.late_window_path("/x/btc_15m_state.json").name, "btc_15m_state_late_window.sqlite3")
        with closing(late_window.connect_store(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("SELECT value FROM store_metadata WHERE key='schema_version'").fetchone()[0], "1")

    def test_window_is_written_once_with_gap_counts(self):
        window = make_window(close_ts=1_800_000_900, failing={("kraken", -300)}, snapshot_errors={-15})
        with closing(late_window.connect_store(self.path)) as db:
            self.assertTrue(late_window.write_window(db, window))
            self.assertFalse(late_window.write_window(db, window))
            row = db.execute("SELECT sample_gaps, snapshot_gaps FROM windows").fetchone()
            self.assertEqual(row, (1, 1))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM price_samples").fetchone()[0], 121 * 4)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0], 5)

    def test_official_value_is_first_write_wins(self):
        row = {"ticker": "KXBTC15M-T", "close_ts": 1_800_000_900, "target_price": 82738.36,
               "expiration_value": 82813.54, "result": "yes", "settled_at": 1_800_001_000, "observed_at": 1_800_001_100}
        with closing(late_window.connect_store(self.path)) as db:
            self.assertTrue(late_window.record_official(db, row))
            self.assertFalse(late_window.record_official(db, {**row, "expiration_value": 1.0}))
            self.assertEqual(db.execute("SELECT expiration_value FROM official_values").fetchone()[0], 82813.54)


class LateWindowPricesTests(unittest.TestCase):
    def test_parsers_recorded_shapes(self):
        cb = late_window.parse_coinbase({"bid": "100.5", "ask": "101", "time": "2026-10-24T12:00:00.500000Z"})
        self.assertEqual((cb["bid"], cb["ask"], cb["exchange_ts"]), (100.5, 101.0, 1792843200.5))
        kr = late_window.parse_kraken({"error": [], "result": {"XXBTZUSD": {"b": ["100.0", "1", "1.0"], "a": ["101.0", "1", "1.0"]}}})
        self.assertEqual((kr["bid"], kr["ask"], kr["exchange_ts"]), (100.0, 101.0, None))
        bs = late_window.parse_bitstamp({"bid": "100", "ask": "101", "timestamp": "1792858000"})
        self.assertEqual(bs["exchange_ts"], 1792858000.0)
        gm = late_window.parse_gemini({"bid": "100", "ask": "101", "volume": {"timestamp": 1792858000123}})
        self.assertEqual(gm["exchange_ts"], 1792858000.123)

    def test_parser_rejects_crossed_or_nonpositive(self):
        with self.assertRaises(ValueError):
            late_window.parse_bitstamp({"bid": "102", "ask": "101", "timestamp": "1"})
        with self.assertRaises(ValueError):
            late_window.parse_bitstamp({"bid": "0", "ask": "101", "timestamp": "1"})

    def test_sample_survives_one_failing_exchange(self):
        payloads = {
            late_window.EXCHANGES["coinbase"][0]: {"bid": "100", "ask": "101", "time": "2026-10-24T12:00:00Z"},
            late_window.EXCHANGES["bitstamp"][0]: {"bid": "100", "ask": "101", "timestamp": "1"},
            late_window.EXCHANGES["gemini"][0]: {"bid": "100", "ask": "101", "volume": {"timestamp": 1000}},
        }

        def fetch(url):
            if url not in payloads:
                raise TimeoutError("slow")
            return payloads[url]

        rows = late_window.sample_prices(-30, 5000, fetch)
        self.assertEqual({r["exchange"] for r in rows}, set(EXCHANGES))
        self.assertEqual({r["offset_s"] for r in rows}, {-30})
        for r in rows:
            self.assertEqual(set(r), set(late_window.SAMPLE_COLUMNS))
            self.assertEqual(r["close_ts"], 5000)
        bad = [r for r in rows if r["error"]]
        self.assertEqual([r["exchange"] for r in bad], ["kraken"])
        self.assertEqual(bad[0]["error"], "timeout")
        self.assertIsNone(bad[0]["bid"])
        self.assertEqual(sum(r["bid"] == 100.0 for r in rows), 3)

    def test_sample_returns_by_deadline_when_one_exchange_hangs(self):
        release = threading.Event()
        self.addCleanup(release.set)
        ok = {"bid": "100", "ask": "101", "time": "2026-10-24T12:00:00Z",
              "timestamp": "1", "volume": {"timestamp": 1000}}
        kraken_url = late_window.EXCHANGES["kraken"][0]

        def fetch(url):
            if url == kraken_url:
                release.wait(10)
            return ok

        started = time.monotonic()
        rows = late_window.sample_prices(-5, 5000, fetch, timeout=0.2)
        self.assertLess(time.monotonic() - started, 1.0)
        by = {r["exchange"]: r for r in rows}
        self.assertEqual(by["kraken"]["error"], "timeout")
        self.assertIsNone(by["kraken"]["bid"])
        self.assertTrue(all(by[e]["bid"] == 100.0 for e in ("coinbase", "bitstamp", "gemini")))

    def test_parse_failure_is_recorded(self):
        rows = late_window.sample_prices(0, 1, lambda url: {"nonsense": 1})
        self.assertTrue(all(r["error"].startswith("parse: ") for r in rows))

    def test_proxy_mid(self):
        def row(b, a, error=None):
            return {"bid": b, "ask": a, "error": error}
        self.assertEqual(late_window.proxy_mid([row(100, 102), row(101, 101), row(99, 103)]), 101.0)
        self.assertIsNone(late_window.proxy_mid([row(100, 102), row(None, None, "timeout")]))


C = 1_800_000_900


class FakeTime:
    def __init__(self, now):
        self.now = now

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class LateWindowCollectorTests(unittest.TestCase):
    def run_window(self, start=C - 700, stop=None, on_sample=None, snapshot=None):
        t = FakeTime(start)
        calls = []

        def sample(offset, close_ts):
            calls.append(("sample", offset))
            if on_sample:
                on_sample(offset)
            return [{"close_ts": close_ts, "offset_s": offset, "exchange": e, "bid": 1.0, "ask": 2.0,
                     "exchange_ts": None, "received_at": t.now, "error": None} for e in late_window.EXCHANGES]

        def default_snapshot(offset, close_ts):
            calls.append(("snapshot", offset))
            return late_window.snapshot_quote(offset, close_ts, lambda: {
                "ticker": "KXBTC15M-T", "close_time": close_ts, "quote_source": "kalshi_orderbook",
                "yes_bid": 0.5, "yes_ask": 0.52, "yes_ask_size": 10.0, "no_bid": 0.48, "no_ask": 0.5,
                "no_ask_size": 8.0, "observed_at": t.now})

        window = late_window.collect_window(
            C, clock=t.clock, sleep=t.sleep, sample=sample, snapshot=snapshot or default_snapshot,
            stop=stop or threading.Event())
        return window, calls

    def test_offsets_and_next_close(self):
        self.assertEqual(late_window.next_close(1_800_000_000), 1_800_000_900)
        self.assertEqual(late_window.next_close(1_800_000_900), 1_800_001_800)
        self.assertEqual(len(late_window.PRICE_OFFSETS), 121)

    def test_collect_window_samples_every_offset_in_order(self):
        window, calls = self.run_window()
        self.assertEqual(sum(c[0] == "sample" for c in calls), 121)
        self.assertEqual(sum(c[0] == "snapshot" for c in calls), 5)
        self.assertLess(calls.index(("sample", -300)), calls.index(("snapshot", -300)))
        self.assertEqual(window["ticker"], "KXBTC15M-T")
        self.assertEqual(window["started_at"], C - 700)
        self.assertEqual(window["max_drift_seconds"], 0)
        self.assertTrue(all(r["error"] is None for r in window["samples"] + window["snapshots"]))

    def test_restart_midwindow_writes_partial_once(self):
        window, calls = self.run_window(start=C - 100)
        missed = [r for r in window["samples"] if r["error"] == "missed_start"]
        self.assertEqual(len(missed), 4 * len(range(-600, -100, 5)))
        self.assertTrue(all(r["bid"] is None and r["received_at"] is None for r in missed))
        self.assertEqual({r["offset_s"] for r in window["snapshots"] if r["error"] == "missed_start"}, {-300, -120})
        self.assertEqual(len(window["samples"]), 121 * 4)
        self.assertEqual(window["ticker"], "KXBTC15M-T")
        with tempfile.TemporaryDirectory() as d, closing(late_window.connect_store(Path(d) / "x.sqlite3")) as db:
            self.assertTrue(late_window.write_window(db, window))
            self.assertFalse(late_window.write_window(db, window))

    def test_snapshot_for_wrong_close_is_a_gap(self):
        row = late_window.snapshot_quote(-60, C, lambda: {
            "ticker": "T", "close_time": C + 900, "quote_source": "kalshi_orderbook", "yes_bid": 0.5})
        self.assertEqual(row["error"], "wrong_market")
        self.assertIsNone(row["ticker"])
        self.assertIsNone(row["yes_bid"])
        self.assertEqual(late_window.snapshot_quote(-60, C, lambda: None)["error"], "wrong_market")
        self.assertEqual(late_window.snapshot_quote(-60, C, lambda: {
            "close_time": C, "quote_source": "market_summary_only"})["error"], "no_orderbook")

    def test_snapshot_fetch_failure_never_raises(self):
        def boom():
            raise RuntimeError("down")
        self.assertEqual(late_window.snapshot_quote(-60, C, boom)["error"], "fetch: down")

    def test_stop_midwindow_keeps_collected_samples(self):
        stop = threading.Event()
        window, calls = self.run_window(stop=stop, on_sample=lambda o: stop.set() if o == -300 else None)
        for r in window["samples"]:
            self.assertEqual(r["error"], None if r["offset_s"] <= -300 else "stopped")
        self.assertEqual([r["error"] for r in window["snapshots"]],
                         [None, "stopped", "stopped", "stopped", "stopped"])
        self.assertEqual(len(window["samples"]), 121 * 4)
        self.assertTrue(all(r["observed_at"] is None and r["ticker"] is None
                            for r in window["snapshots"] if r["error"] == "stopped"))

    def test_slow_snapshot_turns_late_offsets_into_gaps(self):
        t = FakeTime(C - 700)
        sampled = []

        def sample(offset, close_ts):
            sampled.append(offset)
            return late_window._gap_samples(offset, close_ts, None)

        def snapshot(offset, close_ts):
            if offset == -15:
                t.now += 20
            return late_window._gap_snapshot(offset, close_ts, None)

        window = late_window.collect_window(C, clock=t.clock, sleep=t.sleep, sample=sample,
                                            snapshot=snapshot, stop=threading.Event())
        for o in (-10, -5, 0):
            self.assertNotIn(o, sampled)
            self.assertEqual({r["error"] for r in window["samples"] if r["offset_s"] == o}, {"late"})
        self.assertIn(-15, sampled)

    def test_all_gap_window_has_no_evidence(self):
        gaps = late_window.collect_window(C, clock=FakeTime(C + 5).clock, sleep=lambda s: None,
                                          sample=None, snapshot=None, stop=threading.Event())
        self.assertFalse(late_window.has_evidence(gaps))
        gaps["samples"][0] = {**gaps["samples"][0], "error": None}
        self.assertTrue(late_window.has_evidence(gaps))


class LateWindowFinalizeTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.db = late_window.connect_store(Path(d.name) / "lw.sqlite3")
        self.addCleanup(self.db.close)
        self.now = 10_000.0

    def add(self, close_ts, ticker):
        w = make_window(close_ts)
        w["ticker"] = ticker
        late_window.write_window(self.db, w)

    def market(self, ticker, close_ts, **over):
        m = {"ticker": ticker, "status": "finalized", "result": "yes", "close_time": close_ts,
             "settlement_time": close_ts + 5, "target_price": 82800.0, "expiration_value": "82813.54"}
        m.update(over)
        return m

    def run_finalize(self, fetch, **kw):
        return late_window.finalize_pending(self.db, fetch, self.now, **kw)

    def test_finalized_market_is_recorded(self):
        self.add(1000, "T1")
        n = self.run_finalize(lambda t: self.market(t, 1000))
        self.assertEqual(n, 1)
        row = self.db.execute("SELECT ticker,close_ts,target_price,expiration_value,result,settled_at,observed_at"
                              " FROM official_values").fetchone()
        self.assertEqual(row, ("T1", 1000, 82800.0, 82813.54, "yes", 1005.0, self.now))

    def test_provisional_result_is_not_official(self):
        self.add(1000, "T1")
        self.assertEqual(self.run_finalize(lambda t: self.market(t, 1000, status="determined")), 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM official_values").fetchone()[0], 0)
        self.assertEqual(self.run_finalize(lambda t: self.market(t, 1000)), 1)

    def test_fetch_error_leaves_window_pending(self):
        self.add(1000, "T1")
        self.add(1900, "T2")

        def fetch(t):
            if t == "T1":
                raise OSError("down")
            return self.market(t, 1900)

        self.assertEqual(self.run_finalize(fetch), 1)
        self.assertEqual(self.db.execute("SELECT ticker FROM official_values").fetchall(), [("T2",)])
        self.assertEqual(self.run_finalize(lambda t: self.market(t, 1000)), 1)

    def test_limit_and_oldest_first(self):
        for i, c in enumerate((3700, 1000, 2800, 1900)):
            self.add(c, f"T{c}")
        seen = []
        self.assertEqual(self.run_finalize(lambda t: seen.append(t) or self.market(t, int(t[1:])), limit=3), 3)
        self.assertEqual(seen, ["T1000", "T1900", "T2800"])

    def test_missing_values_ticker_null_and_too_recent_are_skipped(self):
        self.add(1000, "T1")
        self.add(1900, None)
        self.add(self.now - 10, "T3")
        self.assertEqual(self.run_finalize(lambda t: self.market(t, 1000, expiration_value=None)), 0)
        self.assertEqual(self.run_finalize(lambda t: self.market(t, 1000, target_price=0)), 0)
        seen = []
        self.run_finalize(lambda t: seen.append(t) or self.market(t, 1000))
        self.assertEqual(seen, ["T1"])

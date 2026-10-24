import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import late_window

OFFSETS = range(-600, 1, 5)
EXCHANGES = ("coinbase", "kraken", "bitstamp", "gemini")
SNAPSHOT_OFFSETS = (-60, -45, -30, -15, 0)


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

    def test_parse_failure_is_recorded(self):
        rows = late_window.sample_prices(0, 1, lambda url: {"nonsense": 1})
        self.assertTrue(all(r["error"].startswith("parse: ") for r in rows))

    def test_proxy_mid(self):
        def row(b, a, error=None):
            return {"bid": b, "ask": a, "error": error}
        self.assertEqual(late_window.proxy_mid([row(100, 102), row(101, 101), row(99, 103)]), 101.0)
        self.assertIsNone(late_window.proxy_mid([row(100, 102), row(None, None, "timeout")]))

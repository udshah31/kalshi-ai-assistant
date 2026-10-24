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

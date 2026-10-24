"""Late-window snapshot collector for Kalshi BTC 15-minute contracts (paper-only evidence).

Storage section: a separate append-only SQLite file; rows are only ever inserted.
"""
from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import statistics
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

from btc_predictor import DEFAULT_KALSHI_SERIES, INTERVAL_SECONDS, fetch_kalshi_market, timestamp_seconds

# ---- storage -------------------------------------------------------------
SCHEMA_VERSION = "1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS store_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS windows (
 close_ts INTEGER PRIMARY KEY, ticker TEXT, started_at REAL, written_at REAL,
 sample_gaps INTEGER, snapshot_gaps INTEGER, max_drift_seconds REAL);
CREATE TABLE IF NOT EXISTS price_samples (
 close_ts INTEGER NOT NULL, offset_s INTEGER NOT NULL, exchange TEXT NOT NULL,
 bid REAL, ask REAL, exchange_ts REAL, received_at REAL, error TEXT,
 PRIMARY KEY(close_ts, offset_s, exchange));
CREATE TABLE IF NOT EXISTS quote_snapshots (
 close_ts INTEGER NOT NULL, offset_s INTEGER NOT NULL, ticker TEXT,
 yes_bid REAL, yes_ask REAL, yes_ask_size REAL, no_bid REAL, no_ask REAL, no_ask_size REAL,
 observed_at REAL, error TEXT, PRIMARY KEY(close_ts, offset_s));
CREATE TABLE IF NOT EXISTS official_values (
 ticker TEXT PRIMARY KEY, close_ts INTEGER, target_price REAL, expiration_value REAL,
 result TEXT CHECK(result IN ('yes','no')), settled_at REAL, observed_at REAL);
"""
SAMPLE_COLUMNS = ("close_ts", "offset_s", "exchange", "bid", "ask", "exchange_ts", "received_at", "error")
SNAPSHOT_COLUMNS = ("close_ts", "offset_s", "ticker", "yes_bid", "yes_ask", "yes_ask_size",
                    "no_bid", "no_ask", "no_ask_size", "observed_at", "error")
OFFICIAL_COLUMNS = ("ticker", "close_ts", "target_price", "expiration_value", "result", "settled_at", "observed_at")


def late_window_path(state_file: str | Path) -> Path:
    state = Path(state_file)
    return state.with_name(f"{state.stem}_late_window.sqlite3")


def connect_store(path: str | Path) -> sqlite3.Connection:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(destination, timeout=5, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(SCHEMA)
        connection.execute("INSERT OR IGNORE INTO store_metadata VALUES ('schema_version',?)", (SCHEMA_VERSION,))
    except Exception:
        connection.close()
        raise
    return connection


def _insert(table: str, columns: tuple[str, ...]) -> str:
    return f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})"


def write_window(connection: sqlite3.Connection, window: dict) -> bool:
    """Insert a window and all its rows atomically; False (nothing written) if it already exists."""
    samples, snapshots = window["samples"], window["snapshots"]
    connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute("SELECT 1 FROM windows WHERE close_ts=?", (window["close_ts"],)).fetchone():
            connection.execute("ROLLBACK")
            return False
        connection.execute(
            "INSERT INTO windows (close_ts,ticker,started_at,written_at,sample_gaps,snapshot_gaps,max_drift_seconds)"
            " VALUES (?,?,?,strftime('%s','now'),?,?,?)",
            (window["close_ts"], window["ticker"], window["started_at"],
             sum(1 for r in samples if r["error"] is not None),
             sum(1 for r in snapshots if r["error"] is not None),
             window["max_drift_seconds"]))
        connection.executemany(_insert("price_samples", SAMPLE_COLUMNS),
                               [tuple(r[c] for c in SAMPLE_COLUMNS) for r in samples])
        connection.executemany(_insert("quote_snapshots", SNAPSHOT_COLUMNS),
                               [tuple(r[c] for c in SNAPSHOT_COLUMNS) for r in snapshots])
        connection.execute("COMMIT")
        return True
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def record_official(connection: sqlite3.Connection, row: dict) -> bool:
    """First write wins; returns whether a row was added."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        added = connection.execute(
            _insert("official_values", OFFICIAL_COLUMNS).replace("INSERT", "INSERT OR IGNORE", 1),
            tuple(row[c] for c in OFFICIAL_COLUMNS)).rowcount == 1
        connection.execute("COMMIT")
        return added
    except BaseException:
        connection.execute("ROLLBACK")
        raise


# ---- prices --------------------------------------------------------------
def _quote(bid: Any, ask: Any, exchange_ts: float | None) -> dict:
    bid, ask = float(bid), float(ask)
    if not 0 < bid <= ask:
        raise ValueError(f"bad quote bid={bid} ask={ask}")
    return {"bid": bid, "ask": ask, "exchange_ts": exchange_ts}


def parse_coinbase(payload: Any) -> dict:
    ts = timestamp_seconds(payload["time"])
    if ts is None:
        raise ValueError("bad time")
    return _quote(payload["bid"], payload["ask"], ts)


def parse_kraken(payload: Any) -> dict:
    (entry,) = payload["result"].values()  # result key varies (XXBTZUSD); no exchange time
    return _quote(entry["b"][0], entry["a"][0], None)


def parse_bitstamp(payload: Any) -> dict:
    return _quote(payload["bid"], payload["ask"], float(payload["timestamp"]))


def parse_gemini(payload: Any) -> dict:
    return _quote(payload["bid"], payload["ask"], float(payload["volume"]["timestamp"]) / 1000)


EXCHANGES: dict[str, tuple[str, Callable[[Any], dict]]] = {
    "coinbase": ("https://api.exchange.coinbase.com/products/BTC-USD/ticker", parse_coinbase),
    "kraken": ("https://api.kraken.com/0/public/Ticker?pair=XBTUSD", parse_kraken),
    "bitstamp": ("https://www.bitstamp.net/api/v2/ticker/btcusd/", parse_bitstamp),
    "gemini": ("https://api.gemini.com/v1/pubticker/btcusd", parse_gemini),
}


def fetch_json(url: str, timeout: float = 3.0) -> Any:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "btc-15m-predictor/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _error_text(exc: Exception) -> str:
    if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, (KeyError, IndexError, TypeError, ValueError)):
        return f"parse: {exc}"
    return f"{type(exc).__name__}: {exc}"[:80]


def _sample_one(name: str, offset_s: int, close_ts: int, fetch: Callable[[str], Any]) -> dict:
    url, parser = EXCHANGES[name]
    row = {"close_ts": close_ts, "offset_s": offset_s, "exchange": name,
           "bid": None, "ask": None, "exchange_ts": None, "received_at": None, "error": None}
    try:
        row.update(parser(fetch(url)))
    except Exception as exc:  # any failure is a recorded gap, never a crash
        row["error"] = _error_text(exc)
    row["received_at"] = time.time()
    return row


def sample_prices(offset_s: int, close_ts: int, fetch: Callable[[str], Any] | None = None,
                  timeout: float = 3.0) -> list[dict]:
    """Return within ~timeout wall seconds; exchanges still running become error="timeout" rows."""
    fetch = fetch or (lambda url: fetch_json(url, timeout))
    pool = ThreadPoolExecutor(max_workers=len(EXCHANGES))
    futures = {n: pool.submit(_sample_one, n, offset_s, close_ts, fetch) for n in EXCHANGES}
    wait(futures.values(), timeout=timeout)
    pool.shutdown(wait=False, cancel_futures=True)  # hung workers are abandoned, not joined
    now = time.time()
    return [f.result() if f.done() else
            {"close_ts": close_ts, "offset_s": offset_s, "exchange": n, "bid": None, "ask": None,
             "exchange_ts": None, "received_at": now, "error": "timeout"}
            for n, f in futures.items()]


def proxy_mid(rows: list[dict]) -> float | None:
    mids = [(r["bid"] + r["ask"]) / 2 for r in rows if r["error"] is None]
    return statistics.median(mids) if len(mids) >= 2 else None


# ---- collector -----------------------------------------------------------
PRICE_OFFSETS = range(-600, 1, 5)
SNAPSHOT_OFFSETS = (-300, -120, -60, -30, -15)
LATE_TOLERANCE_SECONDS = 2.0
QUOTE_FIELDS = ("yes_bid", "yes_ask", "yes_ask_size", "no_bid", "no_ask", "no_ask_size")


def next_close(now: float) -> int:
    return int(now // INTERVAL_SECONDS + 1) * INTERVAL_SECONDS


def snapshot_quote(offset_s: int, close_ts: int, fetch_market: Callable[[], dict | None]) -> dict:
    row = {"close_ts": close_ts, "offset_s": offset_s, "ticker": None, **dict.fromkeys(QUOTE_FIELDS),
           "observed_at": None, "error": None}
    try:
        market = fetch_market()
        if market is None or timestamp_seconds(market.get("close_time")) != close_ts:
            row["error"] = "wrong_market"
        elif market.get("quote_source") != "kalshi_orderbook":
            row["error"] = "no_orderbook"
        else:
            row.update({k: market.get(k) for k in ("ticker", *QUOTE_FIELDS, "observed_at")})
    except Exception as exc:  # a failed fetch is a recorded gap, never a crash
        row["error"] = f"fetch: {exc}"[:80]
    return row


def _gap_samples(offset_s: int, close_ts: int, error: str) -> list[dict]:
    return [{"close_ts": close_ts, "offset_s": offset_s, "exchange": n, "bid": None, "ask": None,
             "exchange_ts": None, "received_at": None, "error": error} for n in EXCHANGES]


def _gap_snapshot(offset_s: int, close_ts: int, error: str) -> dict:
    return {"close_ts": close_ts, "offset_s": offset_s, "ticker": None, **dict.fromkeys(QUOTE_FIELDS),
            "observed_at": None, "error": error}


def collect_window(close_ts: int, *, clock: Callable[[], float], sleep: Callable[[float], None],
                   sample: Callable[[int, int], list[dict]], snapshot: Callable[[int, int], dict],
                   stop: threading.Event) -> dict:
    """Walk every offset on the wall clock; skipped offsets become gap rows so the window is complete."""
    started = clock()
    samples: list[dict] = []
    snapshots: list[dict] = []
    drift = 0.0
    for offset in sorted(set(PRICE_OFFSETS) | set(SNAPSHOT_OFFSETS)):
        target = close_ts + offset
        error = "missed_start" if target < started else "stopped" if stop.is_set() else None
        if error is None:
            sleep(max(0, target - clock()))
            error = "stopped" if stop.is_set() else None
        for kind in ("price", "snapshot"):
            if kind == "price" and offset not in PRICE_OFFSETS or kind == "snapshot" and offset not in SNAPSHOT_OFFSETS:
                continue
            late = clock() - target
            row_error = error or ("late" if late > LATE_TOLERANCE_SECONDS else None)
            if row_error is None:
                drift = max(drift, late)
            if kind == "price":
                samples.extend(_gap_samples(offset, close_ts, row_error) if row_error else sample(offset, close_ts))
            else:
                snapshots.append(_gap_snapshot(offset, close_ts, row_error) if row_error else snapshot(offset, close_ts))
    ticker = next((r["ticker"] for r in snapshots if r["error"] is None), None)
    return {"close_ts": close_ts, "ticker": ticker, "started_at": started, "max_drift_seconds": drift,
            "samples": samples, "snapshots": snapshots}


def has_evidence(window: dict) -> bool:
    return any(r["error"] is None for r in window["samples"] + window["snapshots"])


def run_collector(state_file: str | Path, series: str, stop: threading.Event) -> None:
    with closing(connect_store(late_window_path(state_file))) as connection:
        while not stop.is_set():
            window = collect_window(
                next_close(time.time()), clock=time.time, sleep=stop.wait, stop=stop, sample=sample_prices,
                snapshot=lambda o, c: snapshot_quote(o, c, lambda: fetch_kalshi_market(series, timeout=3)))
            # first write wins, so never let an evidence-free window block a later full one
            if has_evidence(window):
                write_window(connection, window)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", default="data/btc_15m_state.json")
    parser.add_argument("--kalshi-series", default=DEFAULT_KALSHI_SERIES)
    args = parser.parse_args()
    stop = threading.Event()
    old = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        run_collector(args.state_file, args.kalshi_series, stop)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

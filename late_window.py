"""Late-window snapshot collector for Kalshi BTC 15-minute contracts (paper-only evidence).

Storage section: a separate append-only SQLite file; rows are only ever inserted.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

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

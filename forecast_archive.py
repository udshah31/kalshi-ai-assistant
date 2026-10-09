"""Durable, point-in-time public-data archive; no credentials or order execution.

SQLite is independent of the bounded JSON model state. First-issued forecasts,
first-observed settlements, and committed paper entries are immutable. Older
state records are explicitly imported, never reconstructed from today's prices.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
MARKET_FIELDS = {
    "ticker", "event_ticker", "title", "target", "target_price", "status",
    "open_time", "close_time", "settlement_time", "result", "settlement_value",
    "expiration_value", "volume", "observed_at", "quote_source", "orderbook_error",
    "yes_bid", "yes_ask", "yes_mid", "yes_bid_size", "yes_ask_size",
    "no_bid", "no_ask", "no_mid", "no_bid_size", "no_ask_size",
}
REVIEW_FIELDS = {
    "enabled", "status", "choice", "confidence", "probabilities", "fingerprint",
    "bar_timestamp", "market_ticker", "reviewed_at", "expires_at", "applies_to_current_evidence",
}
MODEL_FIELDS = {"n_features", "learning_rate", "l2", "weights", "bias", "updates"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candles (
 product_id TEXT NOT NULL, timestamp INTEGER NOT NULL,
 open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
 volume REAL NOT NULL, observed_at REAL NOT NULL, PRIMARY KEY(product_id,timestamp));
CREATE TABLE IF NOT EXISTS bars (
 product_id TEXT NOT NULL, timestamp INTEGER NOT NULL,
 open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
 volume REAL NOT NULL, observed_at REAL NOT NULL, PRIMARY KEY(product_id,timestamp));
CREATE TABLE IF NOT EXISTS forecasts (
 forecast_id TEXT PRIMARY KEY, state_id TEXT NOT NULL, bar_timestamp INTEGER NOT NULL,
 market_ticker TEXT, forecast_issued_at REAL, features_json TEXT, probability_up REAL,
 validation_eligible INTEGER NOT NULL, outcome_source TEXT NOT NULL,
 market_snapshot_json TEXT, model_json TEXT, provenance TEXT NOT NULL, captured_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS forecasts_time ON forecasts(forecast_issued_at);
CREATE INDEX IF NOT EXISTS forecasts_ticker ON forecasts(market_ticker);
CREATE TABLE IF NOT EXISTS settlements (
 forecast_id TEXT PRIMARY KEY REFERENCES forecasts(forecast_id), market_ticker TEXT NOT NULL,
 market_close_time REAL NOT NULL, result TEXT NOT NULL CHECK(result IN ('yes','no')),
 available_at REAL NOT NULL, outcome_source TEXT NOT NULL,
 snapshot_json TEXT, provenance TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_entries (
 forecast_id TEXT PRIMARY KEY REFERENCES forecasts(forecast_id), entered_at REAL NOT NULL,
 side TEXT NOT NULL, quantity INTEGER NOT NULL, entry_price REAL NOT NULL,
 cost REAL NOT NULL, cost_allowance REAL NOT NULL, market_snapshot_json TEXT,
 checks_json TEXT, review_json TEXT);
CREATE TABLE IF NOT EXISTS decisions (
 decision_id TEXT PRIMARY KEY, state_id TEXT NOT NULL,
 forecast_id TEXT REFERENCES forecasts(forecast_id), bar_timestamp INTEGER,
 decided_at REAL NOT NULL, market_ticker TEXT, action TEXT NOT NULL,
 probability_up REAL, market_snapshot_json TEXT, checks_json TEXT,
 blockers_json TEXT, review_json TEXT, cost_allowance REAL, quantity INTEGER, cost REAL);
CREATE INDEX IF NOT EXISTS decisions_time ON decisions(decided_at);
CREATE TABLE IF NOT EXISTS legacy_scores (
 score_id TEXT PRIMARY KEY, state_id TEXT NOT NULL, bar_timestamp INTEGER,
 outcome_source TEXT NOT NULL, record_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skipped_forecasts (
 skip_id TEXT PRIMARY KEY, state_id TEXT NOT NULL, bar_timestamp INTEGER,
 market_ticker TEXT, reason TEXT, record_json TEXT NOT NULL);
"""


def archive_path(state_file: str | Path) -> Path:
    state = Path(state_file)
    return state.with_name(state.stem + "_archive.sqlite3")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _timestamp(value: Any) -> float | None:
    from datetime import datetime
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo else None
        except ValueError:
            pass
    return None


def _clean(value: Any) -> Any:
    """JSON-safe public values; unknown numeric evidence stays null."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _json(value: Any) -> str:
    return json.dumps(_clean(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _public(value: Any, fields: set[str]) -> dict[str, Any] | None:
    return {k: _clean(v) for k, v in value.items() if k in fields} if isinstance(value, dict) else None


def _id(*values: Any) -> str:
    return hashlib.sha256(_json(values).encode()).hexdigest()


def forecast_id(state_id: str, bar_timestamp: int) -> str:
    return _id(state_id, int(bar_timestamp))


@contextmanager
def connect_archive(path: str | Path):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(destination, timeout=5)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(SCHEMA)
        version = connection.execute("SELECT value FROM archive_metadata WHERE key='schema_version'").fetchone()
        if version and int(version[0]) != SCHEMA_VERSION:
            raise ValueError("Unsupported forecast archive schema version")
        connection.execute("INSERT OR IGNORE INTO archive_metadata VALUES ('schema_version',?)", (str(SCHEMA_VERSION),))
        connection.commit()
        with connection:
            yield connection
    finally:
        connection.close()


def _valid_ohlcv(timestamp, opened, high, low, closed, volume, interval, now):
    numbers = [_number(v) for v in (timestamp, opened, high, low, closed, volume)]
    if any(v is None for v in numbers):
        return None
    ts, opened, high, low, closed, volume = numbers
    if (ts != int(ts) or ts % interval or ts + interval > now or low <= 0
            or high < max(opened, closed) or low > min(opened, closed) or volume < 0):
        return None
    return int(ts), opened, high, low, closed, volume


def _record_prices(connection, product_id, bars, raw_candles, now):
    for raw in raw_candles or ():
        if not isinstance(raw, (list, tuple)) or len(raw) < 6:
            continue
        ts, low, high, opened, closed, volume = raw[:6]
        values = _valid_ohlcv(ts, opened, high, low, closed, volume, 60, now)
        if values:
            connection.execute("INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?,?)", (product_id, *values, now))
    for bar in bars or ():
        values = _valid_ohlcv(bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume, 900, now)
        if values:
            connection.execute("INSERT OR IGNORE INTO bars VALUES (?,?,?,?,?,?,?,?)", (product_id, *values, now))


def _record_forecast(connection, state_id, record, provenance, now):
    timestamp = _number(record.get("bar_timestamp"))
    if timestamp is None or timestamp != int(timestamp):
        return None
    identity = forecast_id(state_id, int(timestamp))
    features = record.get("features")
    if (not isinstance(features, (list, tuple)) or len(features) != 7
            or any(_number(v) is None for v in features)):
        features = None  # do not backfill/recompute missing historical features
    snapshot = _public(record.get("market_snapshot"), MARKET_FIELDS)
    model = _public(record.get("model_at_issue"), MODEL_FIELDS)
    connection.execute("""INSERT OR IGNORE INTO forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        identity, state_id, int(timestamp), record.get("market_ticker"),
        _number(record.get("forecast_issued_at")), _json(features) if features is not None else None,
        _number(record.get("probability_up")), int(record.get("validation_eligible") is True),
        record.get("outcome_source") or "coinbase_proxy", _json(snapshot) if snapshot else None,
        _json(model) if model else None, provenance, now,
    ))
    # An early quote-discovery retry may bind an already-issued, unbound forecast.
    # Never replace probabilities, features, original issue time, or a known ticker.
    if record.get("market_ticker") and snapshot:
        closed = _timestamp(snapshot.get("close_time"))
        observed = _timestamp(snapshot.get("observed_at"))
        if closed is not None and observed is not None and observed <= now < closed:
            connection.execute("""UPDATE forecasts SET market_ticker=?, outcome_source=?,
                validation_eligible=?, market_snapshot_json=?
                WHERE forecast_id=? AND market_ticker IS NULL
                AND NOT EXISTS (SELECT 1 FROM settlements WHERE forecast_id=?)""", (
                record["market_ticker"], record.get("outcome_source") or "unmatched_research",
                int(record.get("validation_eligible") is True), _json(snapshot), identity, identity,
            ))
    return identity


def _review(value: Any) -> dict[str, Any] | None:
    result = _public(value, REVIEW_FIELDS)
    if result and isinstance(result.get("probabilities"), dict):
        result["probabilities"] = {key: number for key, v in result["probabilities"].items()
                                   if key in {"approve_trade", "wait", "data_issue", "review"}
                                   and (number := _number(v)) is not None and 0 <= number <= 1}
    return result


def _record_settlement(connection, identity, record, provenance, now):
    if record.get("outcome_source") != "kalshi_official" or not identity:
        return
    snapshot = _public(record.get("official_result_snapshot"), MARKET_FIELDS)
    ticker = record.get("market_ticker")
    close = _timestamp(record.get("market_close_time"))
    available = _number(record.get("settled_at"))
    expected_close = int(record["bar_timestamp"]) + 1800
    if (not snapshot or snapshot.get("ticker") != ticker or snapshot.get("result") not in {"yes", "no"}
            or close != expected_close or _timestamp(snapshot.get("close_time")) != expected_close
            or available is None or not expected_close <= available <= now
            or record.get("outcome") != ("UP" if snapshot["result"] == "yes" else "DOWN")):
        return  # a legacy direction is not an official settlement observation
    original = connection.execute("SELECT provenance FROM forecasts WHERE forecast_id=?", (identity,)).fetchone()
    if original and original[0] == "live_archive":
        provenance = "live_observation"
    connection.execute("INSERT OR IGNORE INTO settlements VALUES (?,?,?,?,?,?,?,?)", (
        identity, ticker, close, snapshot["result"], available, "kalshi_official", _json(snapshot), provenance,
    ))


def validation_paths(state_file: str | Path) -> tuple[Path, Path]:
    state = Path(state_file)
    return (state.with_name(state.stem + "_validation.json"),
            state.with_name(state.stem + "_validation_summary.json"))


def read_validation_summary(state_file: str | Path) -> dict[str, Any]:
    _, path = validation_paths(state_file)
    try:
        if path.stat().st_size > 256 * 1024:
            return {"status": "unavailable", "reason": "Validation summary exceeds the display size limit."}
        with path.open(encoding="utf-8") as handle:
            result = json.load(handle)
        if not isinstance(result, dict):
            raise ValueError("invalid summary")
        return result
    except FileNotFoundError:
        return {"status": "pending", "reason": "Awaiting the first scheduled read-only audit."}
    except (OSError, ValueError):
        return {"status": "unavailable", "reason": "Validation summary unavailable; no validation claim."}


def read_context_comparison(state_file: str | Path) -> dict[str, Any]:
    """Project a saved experiment into a small, read-only dashboard payload."""
    state = Path(state_file)
    path = state.with_name(state.stem.removesuffix("_state") + "_context_comparison.json")
    model_names = ("current_15m_45m_3h", "proposed_15m_30m_1h", "constant_50_percent")

    def metric_summary(value):
        if not isinstance(value, dict) or type(value.get("count")) is not int or value["count"] < 0:
            raise ValueError("invalid metric count")
        selected = {"count": value["count"]}
        for key in ("brier_score", "log_loss", "accuracy"):
            number = _number(value.get(key))
            if ((value["count"] > 0 and (number is None or number < 0))
                    or (key != "log_loss" and number is not None and number > 1)
                    or (value["count"] == 0 and value.get(key) is not None)):
                raise ValueError("invalid metric value")
            selected[key] = number
        return selected

    try:
        # A full report includes folds; bound the read and ship only compact metrics.
        with path.open("rb") as handle:
            contents = handle.read(8 * 1024 * 1024 + 1)
        if len(contents) > 8 * 1024 * 1024:
            raise ValueError("report exceeds display size limit")
        result = json.loads(contents.decode("utf-8"))
        if (not isinstance(result, dict) or result.get("schema_version") != 1
                or result.get("status") not in {"descriptive_evaluation", "insufficient_data"}
                or _timestamp(result.get("generated_at")) is None):
            raise ValueError("invalid comparison report")
        config = result["configuration"]
        if config["current_horizons"] != [15, 45, 180] or config["proposed_horizons"] != [15, 30, 60]:
            raise ValueError("unexpected horizon experiment")
        configuration = {key: config[key] for key in ("train_size", "test_size", "epochs")}
        if any(type(value) is not int or value < 1 for value in configuration.values()):
            raise ValueError("invalid configuration")
        models = {name: metric_summary(result["models"][name]) for name in model_names}
        matched = {name: metric_summary(result["market_midpoint_same_subset"][name]) for name in model_names}
        market = metric_summary(result["market_midpoint_baseline"])
        coverage = {key: value for key, value in result["coverage"].items()
                    if type(value) is int and value >= 0}
        if (not {"both_feature_sets", "walk_forward_tested", "walk_forward_scored",
                 "walk_forward_unscored", "market_midpoint_walk_forward"}.issubset(coverage)
                or not coverage["walk_forward_scored"] <= coverage["walk_forward_tested"] <= coverage["both_feature_sets"]
                or coverage["walk_forward_unscored"] != coverage["walk_forward_tested"] - coverage["walk_forward_scored"]
                or any(value["count"] != coverage["walk_forward_scored"] for value in models.values())
                or any(value["count"] != market["count"] for value in matched.values())
                or market["count"] != coverage["market_midpoint_walk_forward"]
                or market["count"] > coverage["walk_forward_scored"]):
            raise ValueError("inconsistent metric subsets")
        successful = [fold for fold in result["folds"] if fold.get("status") != "numerical_failure"]
        next_target = configuration["train_size"] + coverage["walk_forward_tested"] + configuration["test_size"]
        if result["folds"]:
            last = result["folds"][-1]
            if type(last.get("test_start_index")) is int:
                next_target = last["test_start_index"] + configuration["test_size"] * 2
        return {
            "status": result["status"], "generated_at": result["generated_at"],
            "configuration": configuration, "coverage": coverage,
            "models": models, "market_midpoint_baseline": market,
            "market_midpoint_same_subset": matched,
            "fold_count": len(successful), "next_block_feature_target": next_target,
        }
    except FileNotFoundError:
        return {"status": "pending", "reason": "Awaiting the first saved horizon comparison."}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return {"status": "unavailable", "reason": "Saved horizon comparison is unavailable; awaiting the next hourly report."}


def _record_entry(connection, identity, record):
    entered = _number(record.get("trade_committed_at"))
    quantity = _number(record.get("demo_quantity"))
    price, cost = _number(record.get("demo_entry_price")), _number(record.get("demo_cost"))
    allowance = _number(record.get("cost_allowance_per_contract"))
    if not identity or entered is None or quantity is None or quantity < 1 or quantity != int(quantity) or price is None or cost is None:
        return
    review = _review(record.get("typesafe_review"))
    if review is not None and record.get("entry_review_fingerprint"):
        review["entry_fingerprint"] = record["entry_review_fingerprint"]
    connection.execute("INSERT OR IGNORE INTO paper_entries VALUES (?,?,?,?,?,?,?,?,?,?)", (
        identity, entered, record.get("demo_side"), int(quantity), price, cost, allowance if allowance is not None else 0,
        _json(_public(record.get("entry_market_snapshot"), MARKET_FIELDS)),
        _json(record.get("entry_signal_checks") or {}),
        _json(review),
    ))


def _summary(connection, path, now):
    counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in ("candles", "bars", "forecasts", "settlements", "decisions", "paper_entries", "legacy_scores", "skipped_forecasts")}
    return {"status": "ok", "path": str(Path(path)), "schema_version": SCHEMA_VERSION,
            "last_collected_at": now, "counts": counts,
            "note": "Point-in-time collection; state imports are identified, not reconstructed."}


def record_cycle(
    path: str | Path, state: dict[str, Any], *, bars: Iterable[Any] = (),
    raw_candles: Iterable[Any] | None = None, prediction: dict[str, Any] | None = None,
    product_id: str = "BTC-USD", now: float | None = None,
    prices_observed_at: float | None = None,
) -> dict[str, Any]:
    """Archive one completed cycle atomically; caller serializes its JSON state.

    All rows in the current state's bounded historical ledger are imported
    idempotently, so a failed cycle can recover on the next successful write.
    Persist the added state_id with JSON state. No model/account settings change.
    Price receipt time is independent of the later cycle/forecast archive write.
    Callers without receipt evidence retain the conservative write-time default.
    """
    now = time.time() if now is None else now
    observed = now if prices_observed_at is None else _number(prices_observed_at)
    if observed is None or not 0 <= observed <= now:
        raise ValueError("prices_observed_at must be a finite receipt time at or before the archive write")
    if not state.get("state_id"):
        state["state_id"] = str(uuid.uuid4())
    state_id = state["state_id"]
    with connect_archive(path) as connection:
        _record_prices(connection, product_id, bars, raw_candles, observed)
        for score in state.get("paper_trades") or []:
            if score.get("outcome_source") == "kalshi_official":
                identity = _record_forecast(connection, state_id, score, "state_import", now)
                _record_settlement(connection, identity, score, "state_import", now)
            else:
                public_score = {k: score.get(k) for k in (
                    "bar_timestamp", "probability_up", "side", "entry_price", "entry_source", "outcome", "correct", "pnl", "entry_close", "exit_close", "exit_timestamp")}
                connection.execute("INSERT OR IGNORE INTO legacy_scores VALUES (?,?,?,?,?)", (
                    _id(state_id, score.get("bar_timestamp"), score.get("outcome_source") or "coinbase_proxy"),
                    state_id, score.get("bar_timestamp"), score.get("outcome_source") or "coinbase_proxy", _json(public_score),
                ))
        pending = state.get("pending") or {}
        identity = _record_forecast(connection, state_id, pending, "live_archive", now) if pending else None
        if pending:
            _record_entry(connection, identity, pending)
        for entry in (state.get("demo_account") or {}).get("demo_trades") or []:
            # New account settlements carry original entry evidence. Older entries
            # without entry timestamps/quotes are deliberately not reconstructed.
            archived_record = {**entry, "demo_side": entry.get("side"), "demo_quantity": entry.get("quantity"),
                               "demo_entry_price": entry.get("entry_price"), "demo_cost": entry.get("cost")}
            entry_id = forecast_id(state_id, entry["bar_timestamp"])
            if connection.execute("SELECT 1 FROM forecasts WHERE forecast_id=?", (entry_id,)).fetchone():
                _record_entry(connection, entry_id, archived_record)
        for skipped in state.get("skipped_predictions") or []:
            public_skip = {k: skipped.get(k) for k in ("bar_timestamp", "market_ticker", "reason", "recovered_at_bar")}
            connection.execute("INSERT OR IGNORE INTO skipped_forecasts VALUES (?,?,?,?,?,?)", (
                _id(state_id, public_skip), state_id, skipped.get("bar_timestamp"), skipped.get("market_ticker"),
                skipped.get("reason"), _json(public_skip),
            ))
        if prediction:
            market = _public(prediction.get("kalshi"), MARKET_FIELDS)
            linked = identity if pending.get("bar_timestamp") == prediction.get("bar_timestamp") else None
            snapshot = {"state_id": state_id, "bar_timestamp": prediction.get("bar_timestamp"), "at": now,
                        "market": market, "checks": prediction.get("signal_checks"), "action": prediction.get("trade_signal")}
            connection.execute("INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                _id(snapshot), state_id, linked, prediction.get("bar_timestamp"), now, (market or {}).get("ticker"),
                prediction.get("trade_signal", "WAIT"), _number(prediction.get("probability_up")), _json(market),
                _json(prediction.get("signal_checks") or {}), _json(prediction.get("blockers") or []),
                _json(_review(prediction.get("typesafe_review"))),
                _number(prediction.get("cost_allowance_per_contract")), int(prediction.get("demo_quantity") or 0),
                _number(prediction.get("demo_cost")),
            ))
        connection.execute("INSERT OR REPLACE INTO archive_metadata VALUES ('last_collected_at',?)", (str(now),))
        return _summary(connection, path, now)

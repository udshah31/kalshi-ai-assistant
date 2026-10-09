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
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
ANALYTICS_LIMIT = 25
ROLLING_WINDOW = 25
ROLLING_STRIDE = 5
STALE_REPORT_SECONDS = 36 * 60 * 60
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


def _analytics_limit(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= ANALYTICS_LIMIT:
        raise ValueError("analytics limit must be an integer from 1 to 25")
    return value


def _connect_archive_readonly(state_file: str | Path):
    path = archive_path(state_file)
    if not path.is_file():
        return None
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


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


def _analytics_empty_summary() -> dict[str, Any]:
    return {
        "eligible_count": None, "minimum_count": None, "accuracy": None,
        "accuracy_wilson_95": None, "brier_score": None, "constant_50_brier": None,
    }


def _analytics_empty_walk_forward() -> dict[str, Any]:
    return {
        "scored_count": None, "test_target": None,
        "additional_scored_samples_needed": None, "status": None,
    }


def _analytics_empty_coverage() -> dict[str, Any]:
    return {
        "timely": None, "late": None, "missing_ticker": None,
        "missing_features": None, "missing_fresh_quotes": None,
        "interval_gaps": None, "settlement_delay_seconds": None,
    }


def _analytics_base(status: str, reason: str | None = None,
                    trend: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "generated_at": None,
        "report_age_seconds": None,
        "report_freshness": status,
        "freshness": {"status": status},
        "validation_ready": False,
        "summary": _analytics_empty_summary(),
        "walk_forward": _analytics_empty_walk_forward(),
        "coverage": _analytics_empty_coverage(),
        "trend": trend or [],
        "note": "Descriptive archived evidence; not profitability proof.",
    }
    if reason:
        result["reason"] = reason
    return result


def _json_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _timing_from_archive(bar_timestamp: Any, issued_at: Any) -> str:
    bar = _number(bar_timestamp)
    issued = _number(issued_at)
    if (bar is None or issued is None or bar < 0 or bar != int(bar)
            or not 0 <= issued - (bar + 900) <= 120
            or issued >= bar + 1800):
        return "late"
    return "timely"


def _snapshot_timing(snapshot: dict[str, Any] | None, ticker: str,
                     bar_timestamp: Any, issued_at: float) -> str:
    if snapshot is None:
        return "unknown"
    bar = _number(bar_timestamp)
    opened = _timestamp(snapshot.get("open_time"))
    closed = _timestamp(snapshot.get("close_time"))
    if (bar is None or opened != bar + 900 or closed != bar + 1800
            or snapshot.get("ticker") != ticker):
        return "unknown"
    return "timely" if _timing_from_archive(bar, issued_at) == "timely" else "late"


def _snapshot_conflicts(snapshot: dict[str, Any] | None, ticker: str,
                        bar_timestamp: Any) -> bool:
    """Reject supplied identity/interval conflicts while preserving unknown fields."""
    if snapshot is None:
        return False
    bar = _number(bar_timestamp)
    if bar is None:
        return True
    expected_open = bar + 900
    expected_close = bar + 1800
    if "ticker" in snapshot and snapshot["ticker"] != ticker:
        return True
    for key, expected in (("open_time", expected_open), ("close_time", expected_close)):
        if key in snapshot and _timestamp(snapshot.get(key)) != expected:
            return True
    return False


def _snapshot_midpoint(snapshot: dict[str, Any] | None, ticker: str,
                       bar_timestamp: Any, issued_at: float) -> float | None:
    if _snapshot_timing(snapshot, ticker, bar_timestamp, issued_at) != "timely":
        return None
    observed = _timestamp(snapshot.get("observed_at")) if snapshot else None
    if observed is None or not 0 <= issued_at - observed <= 60:
        return None
    bid = _number(snapshot.get("yes_bid")) if snapshot else None
    ask = _number(snapshot.get("yes_ask")) if snapshot else None
    midpoint = _number(snapshot.get("yes_mid")) if snapshot else None
    if (bid is None or ask is None or midpoint is None
            or not 0 <= bid <= ask <= 1 or not 0 <= midpoint <= 1
            or abs(midpoint - (bid + ask) / 2) > 1e-8):
        return None
    return midpoint


def _settlement_values(row: sqlite3.Row, issued_at: float,
                       expected_close: float, ticker: str) -> tuple[str | None, float | None, float | None]:
    result = row["result"]
    available = _timestamp(row["available_at"])
    close = _timestamp(row["market_close_time"])
    snapshot = _json_object(row["settlement_snapshot_json"])
    if (result not in {"yes", "no"} or row["settlement_source"] != "kalshi_official"
            or row["settlement_ticker"] != ticker or close != expected_close
            or available is None or available < close or available <= issued_at
            or (snapshot is not None and (
                ("ticker" in snapshot and snapshot["ticker"] != ticker)
                or ("open_time" in snapshot and _timestamp(snapshot["open_time"]) != expected_close - 900)
                or ("close_time" in snapshot and _timestamp(snapshot["close_time"]) != expected_close)
                or ("result" in snapshot and snapshot["result"] != result)))):
        return None, None, None
    return result, available, available - close


def _canonical_archive_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    rows = connection.execute("""
        SELECT f.forecast_id, f.bar_timestamp, f.market_ticker,
               f.forecast_issued_at, f.probability_up, f.market_snapshot_json,
               s.market_ticker AS settlement_ticker, s.market_close_time,
               s.result, s.available_at, s.outcome_source AS settlement_source,
               s.snapshot_json AS settlement_snapshot_json
        FROM forecasts AS f
        LEFT JOIN settlements AS s ON s.forecast_id = f.forecast_id
        WHERE f.outcome_source = 'kalshi_official'
          AND f.validation_eligible = 1
          AND f.market_ticker IS NOT NULL
          AND f.forecast_issued_at IS NOT NULL
        ORDER BY f.forecast_issued_at ASC, f.forecast_id ASC
    """).fetchall()
    selected: list[dict[str, Any]] = []
    seen_tickers: set[str] = set()
    for row in rows:
        ticker = row["market_ticker"]
        issued_at = _number(row["forecast_issued_at"])
        bar_timestamp = _number(row["bar_timestamp"])
        if (not isinstance(ticker, str) or not ticker.strip() or issued_at is None
                or bar_timestamp is None or bar_timestamp != int(bar_timestamp)):
            continue
        # validation_eligible is the archive's eligibility decision. Recheck its
        # immutable timing inputs so a malformed hand-written row cannot enter the
        # canonical set as a late forecast.
        if _timing_from_archive(bar_timestamp, issued_at) != "timely":
            continue
        snapshot = _json_object(row["market_snapshot_json"])
        if _snapshot_conflicts(snapshot, ticker, bar_timestamp):
            continue
        if ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        result, available, delay = _settlement_values(
            row, issued_at, bar_timestamp + 1800, ticker)
        probability = _number(row["probability_up"])
        if probability is not None and not 0 <= probability <= 1:
            probability = None
        selected.append({
            "forecast_id": str(row["forecast_id"]),
            "issued_at": float(issued_at),
            "market_ticker": ticker,
            "probability_up": probability,
            "result": result,
            "timing_status": _snapshot_timing(snapshot, ticker, bar_timestamp, issued_at),
            "yes_mid": _snapshot_midpoint(snapshot, ticker, bar_timestamp, issued_at),
            "market_midpoint_available": False,
            "settlement_available_at": float(available) if available is not None else None,
            "settlement_delay_seconds": float(delay) if delay is not None else None,
            "bar_timestamp": float(bar_timestamp),
        })
        selected[-1]["market_midpoint_available"] = selected[-1]["yes_mid"] is not None
    return selected


def _recent_result(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in (
        "forecast_id", "issued_at", "market_ticker", "probability_up", "result",
        "timing_status", "yes_mid", "market_midpoint_available",
        "settlement_available_at", "settlement_delay_seconds")}


def read_recent_forecasts(state_file: str | Path, limit: int = 25) -> dict[str, Any]:
    """Read a bounded table of frozen official forecasts without creating evidence."""
    _analytics_limit(limit)
    connection = None
    try:
        connection = _connect_archive_readonly(state_file)
        if connection is None:
            return {"status": "pending", "reason": "Awaiting the first archived forecasts.", "rows": []}
        with closing(connection):
            records = _canonical_archive_rows(connection)
        records.sort(key=lambda row: (row["issued_at"], row["forecast_id"]), reverse=True)
        return {"status": "available", "rows": [_recent_result(row) for row in records[:limit]]}
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError):
        try:
            if connection is not None:
                connection.close()
        except Exception:
            pass
        return {"status": "unavailable", "reason": "Archived forecasts are unavailable.", "rows": []}


def _metric_values(metrics: Any, scored_count: int) -> dict[str, Any]:
    if not isinstance(metrics, dict) or type(metrics.get("count")) is not int:
        raise ValueError("invalid metric count")
    if metrics["count"] != scored_count or metrics["count"] < 0:
        raise ValueError("contradictory metric count")
    result: dict[str, Any] = {}
    for key in ("accuracy", "brier_score"):
        value = metrics.get(key)
        number = _number(value)
        if scored_count and (number is None or not 0 <= number <= 1):
            raise ValueError("invalid metric value")
        if not scored_count and value is not None:
            raise ValueError("invalid empty metric value")
        result[key] = number
    interval = metrics.get("accuracy_wilson_95")
    if interval is not None:
        if not isinstance(interval, dict):
            raise ValueError("invalid accuracy interval")
        lower, upper = _number(interval.get("lower")), _number(interval.get("upper"))
        if (lower is None or upper is None or not 0 <= lower <= upper <= 1):
            raise ValueError("invalid accuracy interval")
        result["accuracy_wilson_95"] = {"lower": lower, "upper": upper}
    else:
        result["accuracy_wilson_95"] = None
    return result


def _report_projection(report: dict[str, Any]) -> dict[str, Any]:
    if (report.get("schema_version") != 1
            or report.get("status") not in {"descriptive_evaluation", "insufficient_data", "missing_evidence"}
            or report.get("calibration_applied_live") is not False):
        raise ValueError("invalid validation report")
    data_quality = report.get("data_quality")
    forward = report.get("forward")
    walk = report.get("walk_forward")
    if not isinstance(data_quality, dict) or not isinstance(forward, dict) or not isinstance(walk, dict):
        raise ValueError("incomplete validation report")
    eligible = forward.get("eligible_forecasts")
    scored = forward.get("scored_count")
    unscored = forward.get("unscored_count")
    minimum = forward.get("minimum_evidence_samples")
    additional = forward.get("additional_scored_samples_needed")
    if any(type(value) is not int or value < 0 for value in (eligible, scored, unscored)):
        raise ValueError("invalid forward counts")
    if type(minimum) is not int or minimum < 1 or type(additional) is not int or additional < 0:
        raise ValueError("invalid evidence target")
    if (scored > eligible or unscored != eligible - scored
            or additional != max(0, minimum - scored)):
        raise ValueError("contradictory forward counts")
    metrics = _metric_values(forward.get("metrics"), scored)
    baselines = forward.get("baselines")
    if not isinstance(baselines, dict):
        raise ValueError("missing validation baselines")
    constant = baselines.get("constant_50_percent")
    if not isinstance(constant, dict) or type(constant.get("count")) is not int or constant["count"] != scored:
        raise ValueError("contradictory baseline count")
    constant_brier = _number(constant.get("brier_score"))
    if scored and (constant_brier is None or not 0 <= constant_brier <= 1
                   or not math.isclose(constant_brier, .25, rel_tol=0, abs_tol=1e-9)):
        raise ValueError("invalid baseline metric")
    if not scored and constant.get("brier_score") is not None:
        raise ValueError("invalid empty baseline metric")

    walk_scored = walk.get("scored_count")
    walk_tested = walk.get("test_count")
    walk_minimum = walk.get("minimum_evidence_samples")
    walk_additional = walk.get("additional_scored_samples_needed")
    if (any(type(value) is not int or value < 0 for value in (walk_scored, walk_tested))
            or type(walk_minimum) is not int or walk_minimum < 1
            or type(walk_additional) is not int or walk_additional < 0
            or walk_scored > walk_tested or walk_tested > eligible
            or walk_additional != max(0, walk_minimum - walk_scored)
            or walk.get("status") != (
                "descriptive_evaluation" if walk_scored >= walk_minimum else "insufficient_data")):
        raise ValueError("contradictory walk-forward counts")

    collection = data_quality.get("collection_quality")
    if not isinstance(collection, dict):
        raise ValueError("missing collection quality")
    coverage_keys = (
        "timely_forecast_rows", "excluded_late_forecasts", "excluded_missing_ticker",
        "missing_feature_vectors", "missing_fresh_market_snapshots", "interval_gap_count")
    if any(type(collection.get(key)) is not int or collection[key] < 0 for key in coverage_keys):
        raise ValueError("invalid collection counts")
    delays = collection.get("settlement_delay_seconds")
    if not isinstance(delays, dict):
        raise ValueError("missing settlement delays")
    delay_values: dict[str, float | None] = {}
    for key in ("median", "p95", "max"):
        value = delays.get(key)
        number = _number(value)
        if value is not None and (number is None or number < 0):
            raise ValueError("invalid settlement delay")
        delay_values[key] = number
    present_delays = [value for value in delay_values.values() if value is not None]
    if present_delays and present_delays != sorted(present_delays):
        raise ValueError("contradictory settlement delays")
    return {
        "generated_at": report["generated_at"],
        "summary": {
            "eligible_count": eligible, "minimum_count": minimum,
            **metrics, "constant_50_brier": constant_brier,
        },
        "walk_forward": {
            "scored_count": walk_scored, "test_target": walk_minimum,
            "additional_scored_samples_needed": walk_additional,
            "status": walk["status"],
        },
        "coverage": {
            "timely": collection["timely_forecast_rows"],
            "late": collection["excluded_late_forecasts"],
            "missing_ticker": collection["excluded_missing_ticker"],
            "missing_features": collection["missing_feature_vectors"],
            "missing_fresh_quotes": collection["missing_fresh_market_snapshots"],
            "interval_gaps": collection["interval_gap_count"],
            "settlement_delay_seconds": delay_values,
        },
    }


def _rolling_trend(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trend: list[dict[str, Any]] = []
    for end in range(ROLLING_WINDOW - 1, len(records), ROLLING_STRIDE):
        window = records[end - ROLLING_WINDOW + 1:end + 1]
        scored = [row for row in window if row["result"] in {"yes", "no"}
                  and row["probability_up"] is not None]
        point: dict[str, Any] = {"issued_at": float(window[-1]["issued_at"]), "count": len(scored)}
        if len(scored) >= 10:
            labels = [int(row["result"] == "yes") for row in scored]
            probabilities = [row["probability_up"] for row in scored]
            point["accuracy"] = sum((probability >= .5) == bool(label)
                                     for probability, label in zip(probabilities, labels)) / len(labels)
            point["brier_score"] = sum((probability - label) ** 2
                                        for probability, label in zip(probabilities, labels)) / len(labels)
        trend.append(point)
    return trend


def _analytics_with_report(report: dict[str, Any], trend: list[dict[str, Any]]) -> dict[str, Any]:
    projection = _report_projection(report)
    generated_at = _timestamp(projection["generated_at"])
    now = time.time()
    if generated_at is None or not math.isfinite(generated_at) or generated_at > now:
        raise ValueError("invalid validation report timestamp")
    age = now - generated_at
    stale = age > STALE_REPORT_SECONDS
    result = _analytics_base("available", trend=trend)
    result.update({
        "generated_at": projection["generated_at"],
        "report_age_seconds": float(age),
        "report_freshness": "stale" if stale else "fresh",
        "freshness": {"status": "stale" if stale else "fresh",
                       "diagnostic_only": stale},
        "validation_ready": False,
        "summary": projection["summary"],
        "walk_forward": projection["walk_forward"],
        "coverage": projection["coverage"],
    })
    if stale:
        result["reason"] = "Validation report is stale; metrics are diagnostic-only."
    return result


def read_historical_analytics(state_file: str | Path, limit: int = 25) -> dict[str, Any]:
    """Read descriptive archive evidence independently of live prediction state."""
    _analytics_limit(limit)
    connection = None
    trend: list[dict[str, Any]] = []
    try:
        connection = _connect_archive_readonly(state_file)
        if connection is None:
            return _analytics_base("pending", "Awaiting the first archived forecasts.")
        with closing(connection):
            records = _canonical_archive_rows(connection)
        trend = _rolling_trend(records)[-limit:]
        report = read_validation_summary(state_file)
        if report.get("status") in {"pending", "unavailable"}:
            return _analytics_base(report["status"], report.get("reason"), trend)
        return _analytics_with_report(report, trend)
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError):
        try:
            if connection is not None:
                connection.close()
        except Exception:
            pass
        return _analytics_base("unavailable", "Historical analytics are unavailable.", trend)


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

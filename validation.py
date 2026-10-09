"""Independent, read-only evaluation of archived forecasts and paper entries.

No network calls, live state, saved models, trade gates, or reconstructed historical
prices are used. Calibration is experimental and is never applied to live output.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from btc_predictor import (
    FEATURE_NAMES,
    INTERVAL_SECONDS,
    MAX_ENTRY_DELAY_SECONDS,
    MAX_QUOTE_AGE_SECONDS,
    MIN_LIVE_SAMPLES,
    MIN_SECONDS_TO_CLOSE,
    OnlineLogisticRegression,
    optional_number,
    timestamp_seconds,
)

SCHEMA_VERSION = 1
OFFICIAL_SOURCE = "kalshi_official"
REQUIRED_ENTRY_CHECKS = (
    "probability", "candle_freshness", "market_alignment", "market_open",
    "quote_freshness", "entry_timing", "confidence", "indicators", "features",
    "market", "orderbook", "edge", "spread", "liquidity", "volatility",
    "risk_validation", "pending_slot", "pending_contract", "budget",
)
_COLUMNS = {
    "forecasts": (
        "forecast_id", "state_id", "bar_timestamp", "market_ticker",
        "forecast_issued_at", "features_json", "probability_up",
        "validation_eligible", "outcome_source", "market_snapshot_json",
    ),
    "settlements": (
        "forecast_id", "market_ticker", "market_close_time", "result",
        "available_at", "outcome_source", "snapshot_json",
    ),
    "paper_entries": (
        "forecast_id", "entered_at", "side", "quantity", "entry_price", "cost",
        "cost_allowance", "market_snapshot_json", "checks_json", "review_json",
    ),
    "bars": (
        "product_id", "timestamp", "open", "high", "low", "close", "volume", "observed_at",
    ),
}


def _number(value: Any) -> float | None:
    try:
        return optional_number(value)
    except OverflowError:
        return None


def _json(value: Any, expected_type: type) -> Any:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError, RecursionError):
        return None
    return parsed if isinstance(parsed, expected_type) else None


def _read_archive(path: Path) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    if not path.is_file():
        raise FileNotFoundError(f"Archive database does not exist: {path}; no file was created")
    rows: dict[str, list[dict[str, Any]]] = {}
    missing: list[str] = []
    # as_uri escapes paths containing spaces, #, ? and other URI characters.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        db.execute("BEGIN")  # one consistent read snapshot while an archiver may append
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, columns in _COLUMNS.items():
            if table not in tables:
                rows[table] = []
                missing.append(f"missing table: {table}")
                continue
            present = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
            missing.extend(f"missing column: {table}.{column}" for column in columns if column not in present)
            selection = ", ".join(f'"{column}"' if column in present else f'NULL AS "{column}"' for column in columns)
            rows[table] = [dict(row) for row in db.execute(f'SELECT {selection} FROM "{table}"')]
    return rows, missing


def _features(value: Any) -> list[float] | None:
    values = _json(value, list)
    if values is None or len(values) != len(FEATURE_NAMES):
        return None
    numbers = [_number(value) for value in values]
    return numbers if all(value is not None for value in numbers) else None


def _snapshot_errors(snapshot: dict[str, Any] | None, ticker: str, opened: float, closed: float) -> list[str]:
    """Check supplied identity/timing evidence; missing optional quotes are not labels."""
    if snapshot is None:
        return []
    errors = []
    if "ticker" in snapshot and snapshot["ticker"] != ticker:
        errors.append("snapshot_ticker_mismatch")
    for key, expected in (("open_time", opened), ("close_time", closed)):
        if key in snapshot and timestamp_seconds(snapshot[key]) != expected:
            errors.append(f"snapshot_{key}_mismatch")
    return errors


def _forecast(row: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    reasons = []
    bar = _number(row["bar_timestamp"])
    issued = timestamp_seconds(row["forecast_issued_at"])
    probability = _number(row["probability_up"])
    ticker = row["market_ticker"]
    if not isinstance(row["forecast_id"], str) or not row["forecast_id"]:
        reasons.append("missing_forecast_id")
    if not isinstance(ticker, str) or not ticker.strip():
        reasons.append("missing_ticker")
    if bar is None or bar < 0 or not bar.is_integer():
        reasons.append("invalid_bar_timestamp")
    if row["validation_eligible"] != 1:
        reasons.append("not_validation_eligible")
    if row["outcome_source"] != OFFICIAL_SOURCE:
        reasons.append("nonofficial_forecast_source")
    if probability is None or not 0 <= probability <= 1:
        reasons.append("invalid_probability")
    if issued is None:
        reasons.append("missing_issue_time")
    opened = bar + INTERVAL_SECONDS if bar is not None else None
    closed = bar + 2 * INTERVAL_SECONDS if bar is not None else None
    if issued is not None and opened is not None and not (opened <= issued <= opened + MAX_ENTRY_DELAY_SECONDS and issued < closed):
        reasons.append("forecast_outside_first_120_seconds")
    snapshot = _json(row["market_snapshot_json"], dict)
    if opened is not None and isinstance(ticker, str):
        reasons.extend(_snapshot_errors(snapshot, ticker, opened, closed))
    if reasons:
        return None, reasons
    return {
        **row, "issued_at": issued, "opened_at": opened, "closed_at": closed,
        "probability_up": probability, "features": _features(row["features_json"]),
        "snapshot": snapshot,
    }, []


def _official_result(forecast: dict[str, Any], row: dict[str, Any] | None) -> tuple[dict[str, Any] | None, list[str]]:
    if row is None:
        return None, ["missing_official_settlement"]
    reasons = []
    available = timestamp_seconds(row["available_at"])
    if row["forecast_id"] != forecast["forecast_id"] or row["market_ticker"] != forecast["market_ticker"]:
        reasons.append("settlement_ticker_or_id_mismatch")
    if row["outcome_source"] != OFFICIAL_SOURCE:
        reasons.append("nonofficial_settlement_source")
    if row["result"] not in ("yes", "no"):
        reasons.append("missing_or_invalid_official_label")
    if timestamp_seconds(row["market_close_time"]) != forecast["closed_at"]:
        reasons.append("settlement_interval_mismatch")
    if available is None or available < forecast["closed_at"] or available <= forecast["issued_at"]:
        reasons.append("invalid_settlement_availability")
    snapshot = _json(row["snapshot_json"], dict)
    reasons.extend(_snapshot_errors(snapshot, forecast["market_ticker"], forecast["opened_at"], forecast["closed_at"]))
    if snapshot is not None and "result" in snapshot and snapshot["result"] != row["result"]:
        reasons.append("settlement_snapshot_label_mismatch")
    if reasons:
        return None, reasons
    return {"label": int(row["result"] == "yes"), "available_at": available}, []


def _market_midpoint(forecast: dict[str, Any]) -> float | None:
    market = forecast["snapshot"]
    if not market or market.get("ticker") != forecast["market_ticker"]:
        return None
    if timestamp_seconds(market.get("open_time")) != forecast["opened_at"] or timestamp_seconds(market.get("close_time")) != forecast["closed_at"]:
        return None
    observed = timestamp_seconds(market.get("observed_at"))
    if observed is None or not 0 <= forecast["issued_at"] - observed <= MAX_QUOTE_AGE_SECONDS:
        return None
    bid, ask = _number(market.get("yes_bid")), _number(market.get("yes_ask"))
    if bid is None or ask is None or not 0 <= bid <= ask <= 1:
        return None
    # Both actual frozen quotes are required; a lone midpoint or complement is not a quote.
    midpoint = (bid + ask) / 2
    recorded = _number(market.get("yes_mid"))
    if "yes_mid" in market and (recorded is None or abs(recorded - midpoint) > 1e-8):
        return None
    return midpoint


def _context_clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def context_feature_vector(
    bars: Sequence[dict[str, Any]],
    bar_timestamp: int | float,
    issued_at: int | float,
    horizons: tuple[int, int, int],
    *, frozen_features: Sequence[float] | None = None,
    product_id: str = "BTC-USD",
) -> list[float] | None:
    """Replace only the two longer returns in an original frozen feature vector.

    ``horizons`` is expressed in 15-minute bars and retains the 15m return.
    All required bars must be complete and observed by issue time. For the
    30m/1h candidate, five consecutive bars suffice; EMA, volatility, volume,
    range and the original 15m return are copied without recomputation.
    """
    if (len(horizons) != 3 or horizons[0] != 1
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in horizons)):
        raise ValueError("horizons must contain three positive integers and retain the 15m return")
    if frozen_features is None or len(frozen_features) != len(FEATURE_NAMES):
        return None
    features = [_number(value) for value in frozen_features]
    if any(value is None for value in features):
        return None
    target = _number(bar_timestamp)
    issue = timestamp_seconds(issued_at)
    if (target is None or not target.is_integer() or target < 0
            or target % INTERVAL_SECONDS or issue is None or target + INTERVAL_SECONDS > issue):
        return None
    required = set(range(int(target) - max(horizons) * INTERVAL_SECONDS,
                         int(target) + INTERVAL_SECONDS, INTERVAL_SECONDS))
    by_timestamp: dict[int, dict[str, float]] = {}
    for raw in bars:
        if raw.get("product_id") != product_id:
            continue
        timestamp = _number(raw.get("timestamp"))
        observed = timestamp_seconds(raw.get("observed_at"))
        values = [_number(raw.get(key)) for key in ("open", "high", "low", "close", "volume")]
        if (timestamp not in required or observed is None
                or not timestamp + INTERVAL_SECONDS <= observed <= issue
                or any(value is None for value in values)):
            continue
        open_price, high, low, close, volume = values
        if close <= 0 or low <= 0 or high < max(open_price, close) or low > min(open_price, close) or volume < 0:
            continue
        timestamp_int = int(timestamp)
        candidate = {"timestamp": timestamp_int, "open": open_price, "high": high,
                     "low": low, "close": close, "volume": volume, "observed_at": observed}
        previous = by_timestamp.get(timestamp_int)
        if previous is None or candidate["observed_at"] < previous["observed_at"]:
            by_timestamp[timestamp_int] = candidate
    if set(by_timestamp) != required:
        return None
    current_close = by_timestamp[int(target)]["close"]
    for index, limit in ((1, 20.0), (2, 30.0)):
        prior_close = by_timestamp[int(target) - horizons[index] * INTERVAL_SECONDS]["close"]
        value = (current_close / prior_close - 1.0) * 100.0
        if not math.isfinite(value):
            return None
        features[index] = _context_clamp(value, -limit, limit)
    return features


def _comparison_metrics(predictions: list[dict[str, Any]], probability_key: str) -> dict[str, Any]:
    scored = [prediction for prediction in predictions if prediction.get("label") is not None]
    return _metrics([prediction[probability_key] for prediction in scored], [prediction["label"] for prediction in scored])


def compare_context_models(
    path: str | Path,
    train_size: int = 100,
    test_size: int = 25,
    epochs: int = 5,
) -> dict[str, Any]:
    """Compare current and alternate context horizons using immutable archive data.

    Both candidates use identical forecast-time feature coverage and chronological
    test blocks. Missing future outcomes remain unscored within their original
    blocks. Training uses only labels observed strictly before the block. This
    descriptive experiment never calibrates, saves, or changes a live model.
    """
    for name, value in (("train_size", train_size), ("test_size", test_size), ("epochs", epochs)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    rows, schema_missing = _read_archive(Path(path))
    settlement_map = {row["forecast_id"]: row for row in rows.get("settlements", [])}
    bars = rows.get("bars", [])
    official: list[dict[str, Any]] = []
    for row in rows.get("forecasts", []):
        forecast, reasons = _forecast(row)
        if forecast is None:
            continue
        settlement, settlement_errors = _official_result(forecast, settlement_map.get(forecast["forecast_id"]))
        forecast["settlement"] = settlement
        forecast["settlement_errors"] = settlement_errors
        forecast["market_midpoint"] = _market_midpoint(forecast)
        forecast["proposed_features"] = context_feature_vector(
            bars, forecast["bar_timestamp"], forecast["issued_at"], (1, 2, 4),
            frozen_features=forecast["features"],
        )
        official.append(forecast)

    official.sort(key=lambda record: (record["issued_at"], record["forecast_id"]))
    distinct: list[dict[str, Any]] = []
    seen_tickers: set[str] = set()
    duplicate_tickers = 0
    unique: list[dict[str, Any]] = []
    for record in official:
        if record["market_ticker"] in seen_tickers:
            duplicate_tickers += 1
            continue
        seen_tickers.add(record["market_ticker"])
        unique.append(record)
        if record["features"] is not None and record["proposed_features"] is not None:
            distinct.append(record)

    folds: list[dict[str, Any]] = []
    all_predictions: list[dict[str, Any]] = []
    skipped = 0
    delayed = set()
    cursor = 0
    while cursor + test_size <= len(distinct):
        testing = distinct[cursor:cursor + test_size]
        block_start = testing[0]["issued_at"]
        prior = [record for record in distinct[:cursor]
                 if record["issued_at"] < block_start and record["settlement"] is not None
                 and record["settlement"]["available_at"] < block_start]
        delayed.update(record["forecast_id"] for record in distinct[:cursor]
                       if record["settlement"] is not None and record["settlement"]["available_at"] >= block_start)
        if len(prior) < train_size:
            skipped += 1
            cursor += 1
            continue
        training = prior[-train_size:]
        models = {
            "current_15m_45m_3h": OnlineLogisticRegression(n_features=len(FEATURE_NAMES)),
            "proposed_15m_30m_1h": OnlineLogisticRegression(n_features=len(FEATURE_NAMES)),
        }
        for _ in range(epochs):
            for record in training:
                label = record["settlement"]["label"]
                models["current_15m_45m_3h"].update(record["features"], label)
                models["proposed_15m_30m_1h"].update(record["proposed_features"], label)
        predictions = []
        for record in testing:
            settlement = record["settlement"]
            prediction = {
                "forecast_id": record["forecast_id"], "market_ticker": record["market_ticker"],
                "issued_at": record["issued_at"], "label": settlement["label"] if settlement else None,
                "available_at": settlement["available_at"] if settlement else None,
                "market_midpoint": record["market_midpoint"],
                "current_probability_up": models["current_15m_45m_3h"].predict_proba(record["features"]),
                "proposed_probability_up": models["proposed_15m_30m_1h"].predict_proba(record["proposed_features"]),
                "constant_probability_up": 0.5,
            }
            predictions.append(prediction)
        model_values = [value for model in models.values() for value in (model.bias, *model.weights)]
        model_values.extend(prediction[key] for prediction in predictions
                            for key in ("current_probability_up", "proposed_probability_up"))
        if not all(math.isfinite(value) for value in model_values):
            folds.append({"status": "numerical_failure", "fold_index": len(folds),
                          "block_start": block_start, "test_start_index": cursor,
                          "train": _boundaries(training), "test": _boundaries(testing),
                          "predictions": []})
            cursor += test_size
            continue
        scored_count = sum(prediction["label"] is not None for prediction in predictions)
        folds.append({
            "status": "descriptive_evaluation" if scored_count == len(testing) else "incomplete_outcomes",
            "fold_index": len(folds),
            "block_start": block_start, "test_start_index": cursor,
            "train_count": len(training), "test_count": len(testing),
            "scored_count": scored_count,
            "train": _boundaries(training), "test": _boundaries(testing),
            "train_forecast_ids": [record["forecast_id"] for record in training],
            "test_forecast_ids": [record["forecast_id"] for record in testing],
            "metrics": {
                "current_15m_45m_3h": _comparison_metrics(predictions, "current_probability_up"),
                "proposed_15m_30m_1h": _comparison_metrics(predictions, "proposed_probability_up"),
                "constant_50_percent": _comparison_metrics(predictions, "constant_probability_up"),
            },
            "predictions": predictions,
        })
        all_predictions.extend(predictions)
        cursor += test_size

    model_metrics = {
        "current_15m_45m_3h": _comparison_metrics(all_predictions, "current_probability_up"),
        "proposed_15m_30m_1h": _comparison_metrics(all_predictions, "proposed_probability_up"),
        "constant_50_percent": _comparison_metrics(all_predictions, "constant_probability_up"),
    }
    scored = [prediction for prediction in all_predictions if prediction["label"] is not None]
    quote_predictions = [prediction for prediction in scored if prediction["market_midpoint"] is not None]
    market_metrics = _metrics(
        [prediction["market_midpoint"] for prediction in quote_predictions],
        [prediction["label"] for prediction in quote_predictions],
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "descriptive_evaluation" if scored else "insufficient_data",
        "configuration": {"train_size": train_size, "test_size": test_size, "epochs": epochs,
                           "current_horizons": [15, 45, 180], "proposed_horizons": [15, 30, 60],
                           "interval_seconds": INTERVAL_SECONDS,
                           "candidate_construction": "Copy frozen original features; replace only 45m/3h returns with 30m/1h returns.",
                           "current_feature_names": list(FEATURE_NAMES),
                           "proposed_feature_names": [FEATURE_NAMES[0], "return_30m_pct", "return_1h_pct", *FEATURE_NAMES[3:]]},
        "coverage": {
            "distinct_eligible_forecasts": len(unique),
            "official_settled_forecasts": sum(record["settlement"] is not None for record in unique),
            "duplicate_market_tickers": duplicate_tickers,
            "both_feature_sets": len(distinct),
            "both_feature_sets_with_outcome": sum(record["settlement"] is not None for record in distinct),
            "market_midpoint_both_feature_sets": sum(record["market_midpoint"] is not None for record in distinct),
            "walk_forward_tested": len(all_predictions),
            "walk_forward_scored": len(scored),
            "walk_forward_unscored": len(all_predictions) - len(scored),
            "market_midpoint_walk_forward": len(quote_predictions),
            "missing_frozen_features": sum(record["features"] is None for record in unique),
            "missing_or_late_proposed_features": sum(record["proposed_features"] is None for record in unique),
            "schema_missing": schema_missing,
            "skipped_blocks_insufficient_known_history": skipped,
            "delayed_labels_excluded_at_block_start": len(delayed),
            "minimum_feature_records_for_first_fold": train_size + test_size,
            "additional_feature_records_for_first_fold": max(0, train_size + test_size - len(distinct)),
            "unused_tail_count": max(0, len(distinct) - cursor),
        },
        "minimum_evidence_samples": MIN_LIVE_SAMPLES,
        "additional_scored_samples_needed": max(0, MIN_LIVE_SAMPLES - len(scored)),
        "models": model_metrics,
        "market_midpoint_baseline": market_metrics,
        "market_midpoint_same_subset": {
            "current_15m_45m_3h": _comparison_metrics(quote_predictions, "current_probability_up"),
            "proposed_15m_30m_1h": _comparison_metrics(quote_predictions, "proposed_probability_up"),
            "constant_50_percent": _comparison_metrics(quote_predictions, "constant_probability_up"),
        },
        "folds": folds,
        "limitations": [
            "Earliest eligible issuance per ticker, chosen independently of eventual settlement availability.",
            "Only exact official archived outcomes are scored; missing/invalid outcomes remain unscored in their forecast-time blocks.",
            "Only the two longer return features change; original 15m, EMA, volatility, volume and range features remain frozen.",
            "Required bars must be complete, from BTC-USD, and observed by issue time. Historical write-time timestamps are not retimed.",
            "Both candidates use identical chronological test blocks and only prior-known labels for training.",
            "Market midpoint metrics use only frozen quote evidence valid at forecast issue time.",
            "A first scored fold is descriptive only; limited samples do not establish a winning model or trading profitability.",
            "Descriptive offline comparison only; no live model, threshold, state, or trade gate is changed.",
        ],
    }


def _metrics(probabilities: Sequence[float], labels: Sequence[int]) -> dict[str, Any]:
    count = len(labels)
    bins = []
    for index in range(10):
        selected = [(p, y) for p, y in zip(probabilities, labels) if min(int(p * 10), 9) == index]
        bins.append({
            "lower": index / 10, "upper": (index + 1) / 10, "count": len(selected),
            "mean_probability": sum(p for p, _ in selected) / len(selected) if selected else None,
            "observed_up_rate": sum(y for _, y in selected) / len(selected) if selected else None,
        })
    if not count:
        return {"count": 0, "accuracy": None, "accuracy_wilson_95": None, "brier_score": None, "log_loss": None, "reliability_bins": bins}
    accuracy = sum((p >= .5) == bool(y) for p, y in zip(probabilities, labels)) / count
    z = 1.959963984540054
    denominator = 1 + z * z / count
    center = (accuracy + z * z / (2 * count)) / denominator
    radius = z * math.sqrt(accuracy * (1 - accuracy) / count + z * z / (4 * count * count)) / denominator
    loss = 0.0
    for probability, label in zip(probabilities, labels):
        probability = min(1 - 1e-15, max(1e-15, probability))
        loss -= math.log(probability) if label else math.log1p(-probability)
    return {
        "count": count, "accuracy": accuracy,
        "accuracy_wilson_95": {"lower": max(0.0, center - radius), "upper": min(1.0, center + radius)},
        "brier_score": sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / count,
        "log_loss": loss / count, "reliability_bins": bins,
    }


def _collection_quality(rows: dict[str, list[dict[str, Any]]], records: list[dict[str, Any]],
                        excluded: Counter[str], missing: Counter[str]) -> dict[str, Any]:
    """Describe collection quality; these metrics never authorize a trade."""
    forecasts = rows.get("forecasts", [])
    official = [row for row in forecasts if row.get("outcome_source") == OFFICIAL_SOURCE]
    issue_delays = []
    for row in official:
        bar = _number(row.get("bar_timestamp")); issued = timestamp_seconds(row.get("forecast_issued_at"))
        if bar is not None and issued is not None:
            issue_delays.append(issued - (bar + INTERVAL_SECONDS))
    issue_delays = [value for value in issue_delays if math.isfinite(value)]
    settlement_delays = []
    for row in rows.get("settlements", []):
        close = timestamp_seconds(row.get("market_close_time")); available = timestamp_seconds(row.get("available_at"))
        if close is not None and available is not None and available >= close:
            settlement_delays.append(available - close)
    settlement_delays.sort()
    timely = [value for value in issue_delays if 0 <= value <= MAX_ENTRY_DELAY_SECONDS]
    def percentile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        return values[min(len(values)-1, int((len(values)-1)*fraction))]
    ordered = sorted(records, key=lambda item: (item["issued_at"], item["forecast_id"]))
    gaps = []
    for previous, current in zip(ordered, ordered[1:]):
        gap = current["bar_timestamp"] - previous["bar_timestamp"]
        if gap > INTERVAL_SECONDS:
            gaps.append({"from_bar": previous["bar_timestamp"], "to_bar": current["bar_timestamp"],
                         "missing_intervals": gap // INTERVAL_SECONDS - 1})
    return {
        "forecast_rows": len(forecasts), "official_forecast_rows": len(official),
        "timely_forecast_rows": len(timely),
        "timely_rate_of_official_rows": len(timely) / len(official) if official else None,
        "excluded_late_forecasts": excluded.get("forecast_outside_first_120_seconds", 0),
        "excluded_missing_ticker": excluded.get("missing_ticker", 0),
        "excluded_proxy_or_other_source": excluded.get("nonofficial_forecast_source", 0),
        "missing_feature_vectors": missing.get("features_missing_or_invalid", 0),
        "missing_fresh_market_snapshots": missing.get("fresh_market_midpoint_evidence_missing_or_invalid", 0),
        "issue_delay_seconds": {"min": min(issue_delays) if issue_delays else None,
                                 "median": percentile(sorted(issue_delays), .5),
                                 "p95": percentile(sorted(issue_delays), .95),
                                 "max": max(issue_delays) if issue_delays else None},
        "settlement_observations": len(rows.get("settlements", [])),
        "settlement_delay_seconds": {"median": percentile(settlement_delays, .5),
                                      "p95": percentile(settlement_delays, .95),
                                      "max": max(settlement_delays) if settlement_delays else None},
        "interval_gaps": gaps[:100], "interval_gap_count": len(gaps),
        "status": "collecting_timely_evidence" if len(timely) < MIN_LIVE_SAMPLES else "timely_sample_target_reached",
        "note": "Descriptive collection health only; late/incomplete records remain excluded from validation and no historical values are filled in.",
    }


def _forward(records: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [record for record in records if record["settlement"] is not None]
    labels = [record["settlement"]["label"] for record in scored]
    probabilities = [record["probability_up"] for record in scored]
    metrics = _metrics(probabilities, labels)
    constant = _metrics([.5] * len(labels), labels)
    market_pairs = [(record["market_midpoint"], record["settlement"]["label"], record["probability_up"]) for record in scored if record["market_midpoint"] is not None]
    if len(labels) < MIN_LIVE_SAMPLES:
        evidence = "insufficient_data"
    elif (metrics["accuracy_wilson_95"]["lower"] > .5 and metrics["brier_score"] < constant["brier_score"] and metrics["log_loss"] < constant["log_loss"]):
        evidence = "preliminary_predictive_evidence_not_profitability"
    else:
        evidence = "no_convincing_predictive_evidence_over_constant_50_percent"
    return {
        "status": "insufficient_data" if len(labels) < MIN_LIVE_SAMPLES else "descriptive_evaluation",
        "eligible_forecasts": len(records), "scored_count": len(labels),
        "unscored_count": len(records) - len(labels), "minimum_evidence_samples": MIN_LIVE_SAMPLES,
        "additional_scored_samples_needed": max(0, MIN_LIVE_SAMPLES - len(labels)),
        "predictive_evidence": evidence, "metrics": metrics,
        "baselines": {
            "constant_50_percent": constant,
            "market_midpoint": {
                "status": "available" if market_pairs else "missing_evidence",
                "eligible_count": len(market_pairs), "missing_count": len(labels) - len(market_pairs),
                "metrics": _metrics([p for p, _, _ in market_pairs], [y for _, y, _ in market_pairs]),
                "forecast_metrics_same_subset": _metrics([p for _, _, p in market_pairs], [y for _, y, _ in market_pairs]),
            },
        },
        "predictions": [{"forecast_id": record["forecast_id"], "market_ticker": record["market_ticker"], "issued_at": record["issued_at"], "probability_up": record["probability_up"], "label": record["settlement"]["label"], "available_at": record["settlement"]["available_at"]} for record in scored],
        "interpretation": "Frozen forward forecasts, not fresh-model results. Scores do not measure trading profitability; small samples are descriptive only.",
    }


def _logit(probability: float) -> float:
    probability = min(1 - 1e-6, max(1e-6, probability))
    return math.log(probability) - math.log1p(-probability)


def _fit_platt(probabilities: Sequence[float], labels: Sequence[int]) -> dict[str, Any]:
    """Two-parameter regularized Platt fit on held-out calibration data only.

    Newton descent minimizes mean log loss plus an identity-centered L2 penalty.
    Regularization keeps even single-class calibration blocks finite.
    """
    scores = [_logit(p) for p in probabilities]
    slope, intercept, penalty = 1.0, 0.0, .01

    def objective(a: float, b: float) -> float:
        total = 0.0
        for score, label in zip(scores, labels):
            value = a * score + b
            total += max(value, 0.0) - label * value + math.log1p(math.exp(-abs(value)))
        return total / len(labels) + .5 * penalty * ((a - 1) ** 2 + b * b)

    for _ in range(80):
        ga, gb = penalty * (slope - 1), penalty * intercept
        haa, hab, hbb = penalty, 0.0, penalty
        for score, label in zip(scores, labels):
            probability = OnlineLogisticRegression._sigmoid(slope * score + intercept)
            residual = (probability - label) / len(labels)
            weight = probability * (1 - probability) / len(labels)
            ga += residual * score
            gb += residual
            haa += weight * score * score
            hab += weight * score
            hbb += weight
        determinant = haa * hbb - hab * hab
        da, db = (hbb * ga - hab * gb) / determinant, (haa * gb - hab * ga) / determinant
        current, step = objective(slope, intercept), 1.0
        while step > 1e-8 and objective(slope - step * da, intercept - step * db) > current:
            step *= .5
        slope -= step * da
        intercept -= step * db
        if max(abs(step * da), abs(step * db)) < 1e-9:
            break
    return {"method": "experimental_held_out_regularized_platt", "slope": slope, "intercept": intercept, "l2": penalty, "count": len(labels), "up_labels": sum(labels), "applied_live": False}


def _boundaries(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(records), "forecast_ids": [record["forecast_id"] for record in records],
        "first_issued_at": records[0]["issued_at"], "last_issued_at": records[-1]["issued_at"],
        "latest_label_available_at": max((record["settlement"]["available_at"] for record in records if record["settlement"]), default=None),
    }


def _walk_forward(records: list[dict[str, Any]], train_size: int, calibration_size: int, test_size: int, epochs: int) -> dict[str, Any]:
    # Test selection uses only forecast-time evidence, not whether a future label
    # ultimately arrives. Unknown/invalid test outcomes remain explicitly unscored.
    usable = [record for record in records if record["features"] is not None]
    folds, skipped, delayed, all_predictions = [], 0, set(), []
    cursor = 0
    while cursor + test_size <= len(usable):
        block_start = usable[cursor]["issued_at"]
        prior = [record for record in usable[:cursor] if record["issued_at"] < block_start and record["settlement"] is not None and record["settlement"]["available_at"] < block_start]
        delayed.update(record["forecast_id"] for record in usable[:cursor] if record["settlement"] is not None and record["settlement"]["available_at"] >= block_start)
        if len(prior) < train_size + calibration_size:
            skipped += 1
            cursor += 1
            continue
        history = prior[-(train_size + calibration_size):]
        training, calibration = history[:train_size], history[train_size:]
        testing = usable[cursor:cursor + test_size]
        model = OnlineLogisticRegression(n_features=len(FEATURE_NAMES))
        for _ in range(epochs):
            for record in training:
                model.update(record["features"], record["settlement"]["label"])
        model_values = [model.bias, *model.weights]
        model_values.extend(model.predict_proba(record["features"]) for record in calibration + testing)
        if not all(math.isfinite(value) for value in model_values):
            # Finite but pathological feature magnitudes must not turn NaN into scores.
            folds.append({"status": "numerical_failure", "block_start": block_start, "train": _boundaries(training), "calibration": _boundaries(calibration), "test": _boundaries(testing), "predictions": []})
            cursor += test_size
            continue
        calibration_fit = _fit_platt([model.predict_proba(record["features"]) for record in calibration], [record["settlement"]["label"] for record in calibration])
        training_prior = sum(record["settlement"]["label"] for record in training) / train_size
        predictions = []
        for record in testing:
            raw = model.predict_proba(record["features"])
            calibrated = OnlineLogisticRegression._sigmoid(calibration_fit["slope"] * _logit(raw) + calibration_fit["intercept"])
            settlement = record["settlement"]
            predictions.append({
                "forecast_id": record["forecast_id"], "market_ticker": record["market_ticker"],
                "issued_at": record["issued_at"], "raw_probability_up": raw,
                "calibrated_probability_up": calibrated, "training_prior_probability_up": training_prior,
                "label": settlement["label"] if settlement else None,
                "available_at": settlement["available_at"] if settlement else None,
            })
        scored = [prediction for prediction in predictions if prediction["label"] is not None]
        labels = [prediction["label"] for prediction in scored]
        folds.append({
            "status": "descriptive_evaluation" if len(scored) == test_size else "incomplete_outcomes",
            "block_start": block_start, "train_count": len(training), "calibration_count": len(calibration),
            "test_count": len(testing), "scored_count": len(scored),
            "train": _boundaries(training), "calibration": _boundaries(calibration), "test": _boundaries(testing),
            "calibration_fit": calibration_fit, "training_prior_probability_up": training_prior,
            "raw_metrics": _metrics([prediction["raw_probability_up"] for prediction in scored], labels),
            "calibrated_metrics": _metrics([prediction["calibrated_probability_up"] for prediction in scored], labels),
            "training_prior_baseline": _metrics([training_prior] * len(labels), labels),
            "predictions": predictions,
        })
        all_predictions.extend(predictions)
        cursor += test_size
    scored = [prediction for prediction in all_predictions if prediction["label"] is not None]
    labels = [prediction["label"] for prediction in scored]
    return {
        "status": "insufficient_data" if len(scored) < MIN_LIVE_SAMPLES else "descriptive_evaluation",
        "train_size": train_size, "calibration_size": calibration_size, "test_size": test_size, "epochs": epochs,
        "eligible_feature_records": len(usable), "missing_feature_records": len(records) - len(usable),
        "fold_count": len(folds), "successful_fold_count": sum("calibration_fit" in fold for fold in folds),
        "test_count": len(all_predictions), "scored_count": len(scored),
        "minimum_evidence_samples": MIN_LIVE_SAMPLES,
        "additional_scored_samples_needed": max(0, MIN_LIVE_SAMPLES - len(scored)),
        "required_known_history_per_fold": train_size + calibration_size,
        "skipped_block_starts_insufficient_known_history": skipped,
        "delayed_labels_excluded_at_block_start": len(delayed),
        "unused_tail_count": max(0, len(usable) - cursor), "folds": folds,
        "raw_metrics": _metrics([prediction["raw_probability_up"] for prediction in scored], labels),
        "calibrated_metrics": _metrics([prediction["calibrated_probability_up"] for prediction in scored], labels),
        "training_prior_baseline": _metrics([prediction["training_prior_probability_up"] for prediction in scored], labels),
        "calibration": {"status": "EXPERIMENTAL", "held_out": True, "applied_live": False},
        "interpretation": "Fresh chronological models and frozen held-out calibration; not the actual forward strategy or a trade backtest. No thresholds were tuned on test outcomes.",
    }


def _entry_errors(entry: dict[str, Any], forecast: dict[str, Any]) -> tuple[list[str], list[str]]:
    reasons, warnings = [], []
    entered = timestamp_seconds(entry["entered_at"])
    quantity = _number(entry["quantity"])
    price, cost, allowance = (_number(entry[key]) for key in ("entry_price", "cost", "cost_allowance"))
    if entered is None or not (forecast["issued_at"] <= entered <= forecast["opened_at"] + MAX_ENTRY_DELAY_SECONDS and forecast["closed_at"] - entered >= MIN_SECONDS_TO_CLOSE):
        reasons.append("invalid_entry_timestamp")
    if entry["side"] not in ("UP", "DOWN"):
        reasons.append("invalid_entry_side")
    if quantity is None or not 1 <= quantity <= 2**63 - 1 or not quantity.is_integer():
        reasons.append("invalid_quantity")
    if price is None or not 0 < price < 1:
        reasons.append("missing_or_invalid_entry_price")
    if allowance is None or allowance < 0 or cost is None or cost <= 0:
        reasons.append("missing_or_invalid_recorded_cost")
    elif quantity is not None and price is not None:
        tolerance = 1e-8 * max(1.0, cost)
        if cost + tolerance < quantity * price or cost + tolerance < quantity * (price + allowance):
            reasons.append("recorded_cost_omits_allowance_or_fill_cost")
    market = _json(entry["market_snapshot_json"], dict)
    if not market:
        reasons.append("missing_entry_market_snapshot")
    else:
        if market.get("ticker") != forecast["market_ticker"] or timestamp_seconds(market.get("open_time")) != forecast["opened_at"] or timestamp_seconds(market.get("close_time")) != forecast["closed_at"]:
            reasons.append("entry_market_interval_or_ticker_mismatch")
        if market.get("status") not in ("open", "active") or market.get("quote_source") != "kalshi_orderbook":
            reasons.append("entry_not_executable_orderbook")
        observed = timestamp_seconds(market.get("observed_at"))
        if entered is None or observed is None or not 0 <= entered - observed <= MAX_QUOTE_AGE_SECONDS:
            reasons.append("entry_quote_stale_or_timestamp_missing")
        side = "yes" if entry["side"] == "UP" else "no"
        bid, ask, depth = (_number(market.get(f"{side}_{key}")) for key in ("bid", "ask", "ask_size"))
        if bid is None or ask is None or not 0 < bid <= ask < 1 or price is None or abs(price - ask) > 1e-8:
            reasons.append("entry_price_not_recorded_executable_ask")
        if depth is None or quantity is None or depth < quantity:
            reasons.append("entry_depth_missing_or_insufficient")
    checks = _json(entry["checks_json"], dict)
    if checks is None or any(checks.get(key) is not True for key in REQUIRED_ENTRY_CHECKS) or any(value is not True for value in checks.values()):
        reasons.append("required_deterministic_checks_missing_or_failed")
    review = _json(entry["review_json"], dict)
    review_enabled = bool((review or {}).get("enabled") or (review or {}).get("status") == "ok" or (checks or {}).get("typesafe"))
    if entry["review_json"] is not None and review is None:
        reasons.append("malformed_review_evidence")
    if review_enabled:
        review = review or {}
        confidence = _number(review.get("confidence"))
        reviewed = timestamp_seconds(review.get("reviewed_at"))
        expiry = timestamp_seconds(review.get("expires_at"))
        fingerprint = review.get("fingerprint")
        approved = (review.get("status") == "ok" and review.get("choice") == "approve_trade"
                    and confidence is not None and .55 <= confidence <= 1
                    and checks is not None and checks.get("typesafe") is True
                    and entered is not None and reviewed is not None and expiry is not None
                    and reviewed <= entered <= expiry and entered - reviewed <= MAX_QUOTE_AGE_SECONDS
                    and isinstance(fingerprint, str) and bool(fingerprint)
                    and review.get("applies_to_current_evidence") is True)
        if not approved:
            reasons.append("enabled_review_not_fresh_approved_bound_evidence")
        expected = review.get("entry_fingerprint", review.get("current_fingerprint"))
        if expected is not None and expected != fingerprint:
            reasons.append("review_fingerprint_mismatch")
        elif approved and expected is None:
            warnings.append("review_binding_is_recorded_assertion_not_independently_reconstructable")
    elif review is None:
        warnings.append("review_enabled_state_not_recorded_no_approval_assumed")
    return reasons, warnings


def _paper_account(entries: list[dict[str, Any]], forecasts: dict[str, dict[str, Any]], settlements: dict[str, dict[str, Any]]) -> dict[str, Any]:
    state_ids = {forecasts[e["forecast_id"]].get("state_id") for e in entries if e["forecast_id"] in forecasts}
    if len(state_ids) > 1:
        runs = {}
        for state_id in sorted(state_ids, key=str):
            run_forecasts = {key: value for key, value in forecasts.items() if value.get("state_id") == state_id}
            run_entries = [entry for entry in entries if entry["forecast_id"] in run_forecasts]
            runs[str(state_id)] = _paper_account(run_entries, run_forecasts, settlements)
        unassigned = [{"forecast_id": entry["forecast_id"], "reasons": ["missing_or_ineligible_original_forecast"]}
                      for entry in entries if entry["forecast_id"] not in forecasts]
        return {"status": "multiple_independent_runs", "run_count": len(runs), "runs": runs,
                "recorded_entry_count": len(entries), "settled_count": sum(r["settled_count"] for r in runs.values()),
                "unresolved_count": sum(r["unresolved_count"] for r in runs.values()),
                "invalid_entry_count": len(unassigned) + sum(r["invalid_entry_count"] for r in runs.values()),
                "invalid_entries": unassigned, "net_realized_pnl": None, "realized_return": None, "winrate": None,
                "interpretation": "Independent state resets are separate $100 paper accounts; their cash, returns, and drawdowns are not combined."}
    settled, unresolved, invalid = [], [], []
    cash_cost, payouts, available_cash = 0.0, 0.0, 100.0
    uncredited_payouts: list[tuple[float, float]] = []
    for entry in sorted(entries, key=lambda item: (timestamp_seconds(item["entered_at"]) or 0, str(item["forecast_id"]))):
        forecast_id = entry["forecast_id"] if isinstance(entry["forecast_id"], str) else None
        forecast = forecasts.get(entry["forecast_id"])
        if forecast is None:
            invalid.append({"forecast_id": forecast_id, "reasons": ["missing_or_ineligible_original_forecast"]})
            continue
        reasons, warnings = _entry_errors(entry, forecast)
        if reasons:
            invalid.append({"forecast_id": forecast_id, "reasons": reasons})
            continue
        entered = timestamp_seconds(entry["entered_at"])
        available_cash += sum(payout for available, payout in uncredited_payouts if available <= entered)
        uncredited_payouts = [(available, payout) for available, payout in uncredited_payouts if available > entered]
        cost = float(entry["cost"])
        if cost > available_cash + 1e-8 * max(1.0, available_cash):
            invalid.append({"forecast_id": forecast_id, "reasons": ["recorded_entry_exceeds_available_paper_cash"]})
            continue
        result, result_errors = _official_result(forecast, settlements.get(entry["forecast_id"]))
        available_cash -= cost
        cash_cost += cost
        detail = {"forecast_id": entry["forecast_id"], "market_ticker": forecast["market_ticker"], "entered_at": timestamp_seconds(entry["entered_at"]), "side": entry["side"], "quantity": int(entry["quantity"]), "entry_price": float(entry["entry_price"]), "cost": cost, "cost_allowance": float(entry["cost_allowance"]), "warnings": warnings}
        if result is None:
            unresolved.append({**detail, "reasons": result_errors})
            continue
        won = (entry["side"] == "UP") == bool(result["label"])
        payout = float(entry["quantity"]) if won else 0.0
        payouts += payout
        uncredited_payouts.append((result["available_at"], payout))
        settled.append({**detail, "won": won, "payout": payout, "net_pnl": payout - cost, "available_at": result["available_at"]})
    settled.sort(key=lambda item: (item["available_at"], item["forecast_id"]))
    starting, equity, peak = 100.0, 100.0, 100.0
    drawdown, drawdown_fraction = 0.0, 0.0
    curve = [{"available_at": None, "equity": starting, "net_realized_pnl": 0.0, "forecast_ids": []}]
    grouped: dict[float, list[dict[str, Any]]] = {}
    for entry in settled:
        grouped.setdefault(entry["available_at"], []).append(entry)
    for available, group in grouped.items():
        equity += sum(entry["net_pnl"] for entry in group)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        drawdown_fraction = max(drawdown_fraction, (peak - equity) / peak)
        curve.append({"available_at": available, "equity": equity, "net_realized_pnl": equity - starting, "forecast_ids": [entry["forecast_id"] for entry in group]})
    pnl = sum(entry["net_pnl"] for entry in settled)
    return {
        "status": "no_trades" if not entries else ("incomplete_audit" if invalid or unresolved else "recorded_entries_audited"),
        "recorded_entry_count": len(entries), "settled_count": len(settled), "unresolved_count": len(unresolved), "invalid_entry_count": len(invalid),
        "starting_equity": starting, "cash_cost": cash_cost, "settled_cash_cost": sum(entry["cost"] for entry in settled),
        "unresolved_cash_cost": sum(entry["cost"] for entry in unresolved), "payouts": payouts,
        "net_realized_pnl": pnl, "ending_realized_equity": starting + pnl,
        "cash_balance_excluding_invalid_entries": starting - cash_cost + payouts,
        "winrate": sum(entry["won"] for entry in settled) / len(settled) if settled else None,
        "realized_return": pnl / starting if settled else None,
        "max_realized_drawdown": drawdown if settled else None,
        "max_realized_drawdown_fraction": drawdown_fraction if settled else None,
        "resolved_equity_curve": curve, "settled_entries": settled, "unresolved_entries": unresolved, "invalid_entries": invalid,
        "interpretation": "Audit of immutable recorded paper entries only; no counterfactual trades or transferred approvals. Curve is realized equity, not mark-to-market; unresolved costs remain committed. Entries must be fundable from the $100 starting cash and previously observed payouts. Invalid entries are listed but excluded from audited totals. Costs are recorded assumptions, not verified exchange fees. This does not prove production profitability.",
    }


def evaluate_archive(path: str | Path, train_size: int = 100, calibration_size: int = 50, test_size: int = 25, epochs: int = 5) -> dict[str, Any]:
    """Return a JSON-safe report without creating or modifying the archive.

    Rolling folds use distinct earlier forecasts with labels observed strictly
    before each test block. Test outcomes never enter that block's model or fit.
    """
    for name, value in (("train_size", train_size), ("calibration_size", calibration_size), ("test_size", test_size), ("epochs", epochs)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    rows, schema_missing = _read_archive(Path(path))
    settlement_map = {row["forecast_id"]: row for row in rows["settlements"]}
    excluded: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    eligible, all_forecasts, rejected = [], {}, []
    for row in rows["forecasts"]:
        forecast, reasons = _forecast(row)
        if forecast is None:
            excluded.update(reasons)
            rejected.append({"forecast_id": row["forecast_id"] if isinstance(row["forecast_id"], str) else None, "reasons": reasons})
            continue
        all_forecasts[forecast["forecast_id"]] = forecast
        if forecast["features"] is None:
            missing["features_missing_or_invalid"] += 1
        if forecast["snapshot"] is None:
            missing["market_snapshot_missing_or_malformed"] += 1
        result, result_errors = _official_result(forecast, settlement_map.get(forecast["forecast_id"]))
        forecast["settlement"] = result
        forecast["settlement_errors"] = result_errors
        missing.update(result_errors)
        forecast["market_midpoint"] = _market_midpoint(forecast)
        if forecast["market_midpoint"] is None:
            missing["fresh_market_midpoint_evidence_missing_or_invalid"] += 1
        eligible.append(forecast)
    # Earliest eligible issuance wins even if its settlement is still missing.
    # Choosing a later reset forecast based on eventual results is selection bias.
    eligible.sort(key=lambda record: (record["issued_at"], record["forecast_id"]))
    records, seen = [], set()
    for forecast in eligible:
        if forecast["market_ticker"] in seen:
            excluded["duplicate_market_ticker"] += 1
        else:
            seen.add(forecast["market_ticker"])
            records.append(forecast)
    collection = _collection_quality(rows, records, excluded, missing)
    absent = list(schema_missing)
    if not rows["forecasts"]:
        absent.append("no archived forecasts")
    if not rows["settlements"]:
        absent.append("no official settlement observations")
    if not any(record["features"] is not None for record in records):
        absent.append("no eligible archived feature vectors for retraining")
    forward = _forward(records)
    walk_forward = _walk_forward(records, train_size, calibration_size, test_size, epochs)
    if forward["status"] == "insufficient_data":
        absent.append(f"forward official scored samples: {forward['scored_count']}/{MIN_LIVE_SAMPLES}; more prospective evidence needed")
    if walk_forward["status"] == "insufficient_data":
        absent.append(f"walk-forward held-out scored samples: {walk_forward['scored_count']}/{MIN_LIVE_SAMPLES}; each fold also needs {train_size + calibration_size} earlier known labels and {test_size} test records")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_quality": {
            "status": "insufficient_data" if forward["status"] == "insufficient_data" else ("missing_evidence" if schema_missing or missing or excluded else "descriptive_evaluation"),
            "counts": {table: len(table_rows) for table, table_rows in rows.items()},
            "eligible_forecasts_before_ticker_dedup": len(eligible), "distinct_eligible_forecasts": len(records),
            "excluded_counts": dict(sorted(excluded.items())), "missing_evidence_counts": dict(sorted(missing.items())),
            "missing_evidence": absent, "rejected_forecasts": rejected,
            "deduplication": "Earliest eligible forecast per ticker, across all state IDs; no best-outcome selection.",
            "timing_evidence": "Official settlement close must equal bar + 1800; implied forecast open is bar + 900. Supplied snapshot open/close/ticker must agree. Missing snapshots cannot supply quote baselines.",
            "collection_quality": collection,
        },
        "forward": forward, "walk_forward": walk_forward,
        "paper_account": _paper_account(rows["paper_entries"], all_forecasts, settlement_map),
        "limitations": [
            "Offline report only: no live probabilities, model files, approvals, or trade gates are changed.",
            "Only exact archived official Kalshi results are scored; Coinbase proxy outcomes and candle-derived labels are not used.",
            "Missing features exclude retraining, not otherwise valid frozen forward scoring. Missing/stale quotes never become invented prices.",
            "Label availability is the first local observation, not market close; strict pre-block availability is required for training and calibration.",
            "Platt calibration is EXPERIMENTAL, fit on held-out past records, frozen for each test block, and never applied live.",
            "Wilson intervals and reliability bins are descriptive; overlapping market conditions, selection effects, and small samples limit statistical conclusions.",
            "Fresh-model forecast metrics are not the actual forward paper strategy and cannot inherit TypeSafe approvals or demonstrate trading profitability.",
            "Paper accounting uses recorded fill/cost assumptions only, starts realized equity at $100, and cannot verify real executions or actual exchange fees.",
            "No historical prices, simulated trades, optimized thresholds, annualized returns, or Sharpe ratios are fabricated.",
        ],
    }


def report_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Small dashboard payload; do not ship all fold predictions every refresh."""
    summary = {"schema_version": report["schema_version"], "generated_at": report["generated_at"],
               "status": report["data_quality"]["status"], "calibration_applied_live": False,
               "limitations": report["limitations"]}
    for section, omitted in (("data_quality", {"rejected_forecasts"}), ("forward", {"predictions"}),
                             ("walk_forward", {"folds"}),
                             ("paper_account", {"resolved_equity_curve", "settled_entries", "unresolved_entries", "invalid_entries", "runs"})):
        summary[section] = {key: value for key, value in report[section].items() if key not in omitted}
    return summary


def _atomic_output(path: Path, report: dict[str, Any], archive: Path) -> None:
    archive = archive.resolve()
    target = path.resolve()
    protected = {archive, *(Path(str(archive) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
    if target in protected or (path.exists() and os.path.samefile(path, archive)):
        raise ValueError("output-json must not replace the archive database or its SQLite sidecars")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "compare-context":
        parser = argparse.ArgumentParser(description="Compare archived 15m/45m/3h and 15m/30m/1h contexts")
        parser.add_argument("compare-context", help=argparse.SUPPRESS)
        parser.add_argument("--archive-file", required=True, type=Path)
        parser.add_argument("--output-json", type=Path)
        parser.add_argument("--train-size", type=int, default=100)
        parser.add_argument("--test-size", type=int, default=25)
        parser.add_argument("--epochs", type=int, default=5)
        args = parser.parse_args(arguments)
        try:
            report = compare_context_models(args.archive_file, args.train_size, args.test_size, args.epochs)
            if args.output_json:
                _atomic_output(args.output_json, report, args.archive_file)
        except (OSError, sqlite3.Error, ValueError) as exc:
            parser.exit(2, f"validation: {exc}\n")
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
        return 0

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-file", required=True, type=Path)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--summary-json", type=Path, help="Save a compact dashboard summary (no fold/entry detail arrays)")
    parser.add_argument("--train-size", type=int, default=100)
    parser.add_argument("--calibration-size", type=int, default=50)
    parser.add_argument("--test-size", type=int, default=25)
    parser.add_argument("--epochs", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        if args.output_json and args.summary_json and args.output_json.resolve() == args.summary_json.resolve():
            raise ValueError("Full report and compact summary must use different output paths")
        report = evaluate_archive(args.archive_file, args.train_size, args.calibration_size, args.test_size, args.epochs)
        if args.output_json:
            _atomic_output(args.output_json, report, args.archive_file)
        if args.summary_json:
            _atomic_output(args.summary_json, report_summary(report), args.archive_file)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(2, f"validation: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

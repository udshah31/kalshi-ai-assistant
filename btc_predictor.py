"""Self-learning BTC 15-minute direction predictor.

This module intentionally predicts only. It does not place trades or submit Kalshi
orders. The model is an online logistic regression: each time ``run`` is called,
it can learn from the previous 15-minute prediction once the next completed window
is available, then produce the next probability estimate.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import sqlite3
import math
import os
import tempfile
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any, Iterable, Sequence


INTERVAL_SECONDS = 15 * 60
CANDLE_SECONDS = 60
DEFAULT_PRODUCT = "BTC-USD"
DEFAULT_KALSHI_SERIES = "KXBTC15M"
DEFAULT_LOOKBACK_MINUTES = 48 * 60
DEFAULT_DEMO_BALANCE = 100.0
DEFAULT_DEMO_BUDGET = 10.0
MIN_TRADE_CONFIDENCE = 0.65
MIN_TRADE_EDGE = 0.08
MIN_LIVE_SAMPLES = 100  # preliminary paper validation, not proof of profitability
MAX_MARKET_SPREAD = 0.08
MIN_MARKET_LIQUIDITY = 25.0
MAX_VOLATILITY_PCT = 3.0
MAX_CANDLE_RANGE_PCT = 5.0
MAX_QUOTE_AGE_SECONDS = 60
MAX_ENTRY_DELAY_SECONDS = 120  # model is a start-of-window forecast, not intrawindow
MIN_SECONDS_TO_CLOSE = 60
PAPER_COST_ALLOWANCE = 0.03  # 2c fees + 1c slippage ASSUMPTION, not an actual fee schedule
MAX_SETTLEMENT_DELAY_SECONDS = 3600
TYPESAFE_API_KEY_ENV = "TYPESAFE_API_KEY"
TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
FEATURE_NAMES = (
    "return_15m_pct",
    "return_45m_pct",
    "return_3h_pct",
    "ema_5_20_gap_pct",
    "volatility_3h_pct",
    "volume_ratio",
    "range_pct",
)


@dataclass(frozen=True)
class WindowBar:
    """OHLCV data for one complete 15-minute window."""

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def iso_timestamp(self) -> str:
        return datetime.fromtimestamp(self.timestamp, timezone.utc).isoformat()


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def aggregate_candles(
    candles: Iterable[Sequence[float]], interval_seconds: int = INTERVAL_SECONDS
) -> list[WindowBar]:
    """Aggregate Coinbase candles into complete, gap-free windows.

    Coinbase returns candles as ``[timestamp, low, high, open, close, volume]``.
    Incomplete or missing-minute windows are ignored so a prediction is never
    trained on a partial target interval.
    """

    by_timestamp: dict[int, tuple[float, float, float, float, float]] = {}
    for raw in candles:
        if len(raw) < 6:
            continue
        try:
            timestamp = int(raw[0])
            low, high, open_price, close, volume = map(float, raw[1:6])
        except (TypeError, ValueError, OverflowError):
            continue
        if (not all(math.isfinite(v) for v in (low, high, open_price, close, volume))
                or low <= 0 or high < max(open_price, close) or low > min(open_price, close)
                or volume < 0 or timestamp % CANDLE_SECONDS):
            continue
        by_timestamp[timestamp] = (open_price, high, low, close, volume)

    grouped: dict[int, list[tuple[int, tuple[float, float, float, float, float]]]] = {}
    for timestamp, values in by_timestamp.items():
        window_start = timestamp - (timestamp % interval_seconds)
        grouped.setdefault(window_start, []).append((timestamp, values))

    bars: list[WindowBar] = []
    expected_count = interval_seconds // CANDLE_SECONDS
    for window_start, rows in sorted(grouped.items()):
        rows.sort(key=lambda item: item[0])
        timestamps = [timestamp for timestamp, _ in rows]
        expected = list(range(window_start, window_start + interval_seconds, CANDLE_SECONDS))
        if len(rows) != expected_count or timestamps != expected:
            continue

        first = rows[0][1]
        last = rows[-1][1]
        bars.append(
            WindowBar(
                timestamp=window_start,
                open=first[0],
                high=max(values[1] for _, values in rows),
                low=min(values[2] for _, values in rows),
                close=last[3],
                volume=sum(values[4] for _, values in rows),
            )
        )
    return bars


def _ema(values: Sequence[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def feature_vector(bars: Sequence[WindowBar], index: int) -> list[float] | None:
    """Build bounded, scale-stable features for a completed window."""

    # 20 windows are needed for the long EMA, and one extra is needed for a
    # return. The 3-hour features need 12 windows of history.
    if index < 20 or index >= len(bars):
        return None

    recent_history = bars[index - 20:index + 1]
    if any(right.timestamp - left.timestamp != INTERVAL_SECONDS
           for left, right in zip(recent_history, recent_history[1:])):
        return None  # a missing 15-minute interval is not a short-term return

    closes = [bar.close for bar in bars[: index + 1]]
    volumes = [bar.volume for bar in bars[: index + 1]]
    current = bars[index]

    return_15m = closes[-1] / closes[-2] - 1.0
    return_45m = closes[-1] / closes[-4] - 1.0
    return_3h = closes[-1] / closes[-13] - 1.0
    ema_gap = _ema(closes, 5) / _ema(closes, 20) - 1.0

    window_returns = [closes[position] / closes[position - 1] - 1.0 for position in range(index - 11, index + 1)]
    volatility = pstdev(window_returns) if len(window_returns) > 1 else 0.0
    prior_volume = volumes[-13:-1]
    volume_average = fmean(prior_volume) if prior_volume else 0.0
    volume_ratio = current.volume / volume_average - 1.0 if volume_average > 0 else 0.0
    range_pct = (current.high - current.low) / current.close

    # Percent-like features are multiplied by 100 and bounded to keep a single
    # volatile candle from destabilizing the online learner.
    return [
        _clamp(return_15m * 100.0, -20.0, 20.0),
        _clamp(return_45m * 100.0, -20.0, 20.0),
        _clamp(return_3h * 100.0, -30.0, 30.0),
        _clamp(ema_gap * 100.0, -20.0, 20.0),
        _clamp(volatility * 100.0, 0.0, 20.0),
        _clamp(volume_ratio, -2.0, 5.0),
        _clamp(range_pct * 100.0, 0.0, 20.0),
    ]


def build_training_samples(
    bars: Sequence[WindowBar],
) -> list[tuple[list[float], int, int]]:
    """Return ``(features, label, timestamp)`` samples in chronological order."""

    samples: list[tuple[list[float], int, int]] = []
    for index in range(20, len(bars) - 1):
        features = feature_vector(bars, index)
        if features is None:
            continue
        if bars[index + 1].timestamp != bars[index].timestamp + INTERVAL_SECONDS:
            continue
        # Coinbase proxy bootstrap only; not the official Kalshi target/label.
        label = int(bars[index + 1].close >= bars[index].close)
        samples.append((features, label, bars[index].timestamp))
    return samples


@dataclass
class OnlineLogisticRegression:
    """Small dependency-free online binary classifier."""

    n_features: int
    learning_rate: float = 0.04
    l2: float = 0.0005
    weights: list[float] = field(default_factory=list)
    bias: float = 0.0
    updates: int = 0

    def __post_init__(self) -> None:
        if not self.weights:
            self.weights = [0.0] * self.n_features
        if len(self.weights) != self.n_features:
            raise ValueError("weights length does not match n_features")

    @staticmethod
    def _sigmoid(value: float) -> float:
        if value >= 0:
            z = math.exp(-min(value, 700.0))
            return 1.0 / (1.0 + z)
        z = math.exp(max(value, -700.0))
        return z / (1.0 + z)

    def predict_proba(self, features: Sequence[float]) -> float:
        if len(features) != self.n_features:
            raise ValueError("feature length does not match model")
        score = self.bias + sum(weight * value for weight, value in zip(self.weights, features))
        return self._sigmoid(score)

    def update(self, features: Sequence[float], label: int) -> float:
        """Apply one labeled example and return the probability before updating."""

        if label not in (0, 1):
            raise ValueError("label must be 0 or 1")
        probability = self.predict_proba(features)
        error = float(label) - probability
        self.bias += self.learning_rate * error
        for index, value in enumerate(features):
            self.weights[index] += self.learning_rate * (
                error * value - self.l2 * self.weights[index]
            )
        self.updates += 1
        return probability

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_features": self.n_features,
            "learning_rate": self.learning_rate,
            "l2": self.l2,
            "weights": list(self.weights),
            "bias": self.bias,
            "updates": self.updates,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OnlineLogisticRegression":
        weights = [float(value) for value in payload["weights"]]
        return cls(
            n_features=int(payload.get("n_features", len(weights))),
            learning_rate=float(payload.get("learning_rate", 0.04)),
            l2=float(payload.get("l2", 0.0005)),
            weights=weights,
            bias=float(payload.get("bias", 0.0)),
            updates=int(payload.get("updates", 0)),
        )


def train_model(
    samples: Sequence[tuple[list[float], int, int]],
    epochs: int = 5,
) -> tuple[OnlineLogisticRegression, dict[str, float]]:
    if not samples:
        raise ValueError("no training samples available")
    model = OnlineLogisticRegression(n_features=len(FEATURE_NAMES))
    for _ in range(max(1, epochs)):
        for features, label, _ in samples:
            model.update(features, label)

    probabilities = [model.predict_proba(features) for features, _, _ in samples]
    labels = [label for _, label, _ in samples]
    accuracy = sum(int((probability >= 0.5) == bool(label)) for probability, label in zip(probabilities, labels)) / len(labels)
    brier = sum((probability - label) ** 2 for probability, label in zip(probabilities, labels)) / len(labels)
    return model, {"samples": float(len(samples)), "accuracy": accuracy, "brier_score": brier}


def fetch_candles(
    product_id: str = DEFAULT_PRODUCT,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    timeout: int = 20,
) -> list[list[float]]:
    """Fetch completed 1-minute candles from Coinbase Exchange's public API."""

    if lookback_minutes < 60:
        raise ValueError("lookback_minutes must be at least 60")
    now = int(time.time())
    end = (now // CANDLE_SECONDS) * CANDLE_SECONDS - CANDLE_SECONDS
    start = end - lookback_minutes * CANDLE_SECONDS
    chunk_seconds = 290 * CANDLE_SECONDS  # stay below Coinbase's 300-candle cap
    collected: dict[int, list[float]] = {}
    cursor = start

    while cursor < end:
        chunk_end = min(cursor + chunk_seconds, end)
        params = urllib.parse.urlencode(
            {"granularity": CANDLE_SECONDS, "start": cursor, "end": chunk_end}
        )
        url = f"https://api.exchange.coinbase.com/products/{urllib.parse.quote(product_id)}/candles?{params}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "btc-15m-predictor/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # provide a useful error without exposing a URL payload
            raise RuntimeError(f"could not fetch BTC candles: {exc}") from exc
        if not isinstance(payload, list):
            raise RuntimeError(f"Coinbase returned an unexpected response: {payload!r}")
        for row in payload:
            if isinstance(row, list) and len(row) >= 6:
                timestamp = int(row[0])
                if start <= timestamp <= end:
                    collected[timestamp] = row
        cursor = chunk_end

    return [collected[timestamp] for timestamp in sorted(collected)]


def fetch_spot_price(product_id: str = DEFAULT_PRODUCT, timeout: int = 10) -> dict[str, Any]:
    """Fetch the current Coinbase spot price for the dashboard."""

    url = f"https://api.coinbase.com/v2/prices/{urllib.parse.quote(product_id)}/spot"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "btc-15m-predictor/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload["data"]
        return {
            "price": float(data["amount"]),
            "currency": data.get("currency", "USD"),
            "source": "Coinbase spot",
        }
    except Exception as exc:
        raise RuntimeError(f"could not fetch live BTC spot price: {exc}") from exc


def optional_number(value: Any) -> float | None:
    """Missing/invalid evidence is unknown, never silently a zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def timestamp_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return optional_number(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except ValueError:
        return None


def normalize_kalshi_market(market: dict[str, Any]) -> dict[str, Any]:
    """Public market metadata; preserve missing price/size fields as None."""
    result = {
        "ticker": market.get("ticker"), "event_ticker": market.get("event_ticker"),
        "title": market.get("title"), "target": market.get("yes_sub_title"),
        "target_price": optional_number(market.get("floor_strike")),
        "status": market.get("status"), "open_time": market.get("open_time"),
        "close_time": market.get("close_time"),
        "settlement_time": market.get("settlement_ts"),
        "result": (str(market.get("result") or "").lower() or None),
        "settlement_value": market.get("settlement_value_dollars"),
        "expiration_value": market.get("expiration_value"),
        "volume": optional_number(market.get("volume_fp")),
    }
    for side in ("yes", "no"):
        for book_side in ("bid", "ask"):
            result[f"{side}_{book_side}"] = optional_number(market.get(f"{side}_{book_side}_dollars"))
            result[f"{side}_{book_side}_size"] = optional_number(market.get(f"{side}_{book_side}_size_fp"))
        bid, ask = result[f"{side}_bid"], result[f"{side}_ask"]
        result[f"{side}_mid"] = (bid + ask) / 2 if bid is not None and ask is not None else None
    return result


def orderbook_quotes(payload: dict[str, Any]) -> dict[str, Any]:
    """Derive asks and their sizes from the opposite bids in ONE snapshot.

    Kalshi documents bid-only books: YES ask = 1 - NO bid (same size),
    and NO ask = 1 - YES bid (same size). Empty is zero depth; absent is unknown.
    """
    book = payload.get("orderbook_fp")
    if not isinstance(book, dict):
        raise ValueError("orderbook_fp unavailable")
    quotes: dict[str, Any] = {}
    best = {}
    for side in ("yes", "no"):
        levels = book.get(f"{side}_dollars")
        if not isinstance(levels, list):
            raise ValueError(f"{side} orderbook unavailable")
        valid = []
        for row in levels:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                raise ValueError("malformed orderbook level")
            price, size = optional_number(row[0]), optional_number(row[1])
            if price is None or size is None or not 0 < price < 1 or size < 0:
                raise ValueError("invalid orderbook price/size")
            if size > 0:
                valid.append((price, size))
        best[side] = max(valid, default=(None, 0.0), key=lambda row: row[0])
    for side, opposite in (("yes", "no"), ("no", "yes")):
        bid, bid_size = best[side]; opposite_bid, ask_size = best[opposite]
        ask = round(1.0 - opposite_bid, 4) if opposite_bid is not None else None
        quotes.update({f"{side}_bid": bid, f"{side}_bid_size": bid_size,
                       f"{side}_ask": ask, f"{side}_ask_size": ask_size,
                       f"{side}_mid": (bid + ask) / 2 if bid is not None and ask is not None else None})
    return quotes


def fetch_kalshi_market_by_ticker(ticker: str, timeout: int = 10) -> dict[str, Any]:
    url = "https://api.elections.kalshi.com/trade-api/v2/markets/" + urllib.parse.quote(ticker, safe="")
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload.get("market"), dict):
        raise RuntimeError("Kalshi market result unavailable")
    return normalize_kalshi_market(payload["market"])


def _fetch_kalshi_markets(
    series_ticker: str,
    status: str,
    limit: int = 20,
    timeout: int = 15,
) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {"series_ticker": series_ticker, "status": status, "limit": limit}
    )
    url = f"https://api.elections.kalshi.com/trade-api/v2/markets?{params}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "btc-15m-predictor/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"could not fetch Kalshi market context: {exc}") from exc
    markets = payload.get("markets", []) if isinstance(payload, dict) else []
    return [market for market in markets if isinstance(market, dict)]


def fetch_kalshi_market(
    series_ticker: str = DEFAULT_KALSHI_SERIES,
    timeout: int = 15,
) -> dict[str, Any] | None:
    """Fetch the nearest active market from Kalshi's public market API."""

    now = time.time()
    active = [m for m in _fetch_kalshi_markets(series_ticker, "open", timeout=timeout)
              if (timestamp_seconds(m.get("open_time")) or float("inf")) <= now
              < (timestamp_seconds(m.get("close_time")) or 0)]
    if not active:
        return None
    active.sort(key=lambda market: market.get("close_time") or "")
    market = normalize_kalshi_market(active[0])
    market["observed_at"] = time.time()
    try:
        url = ("https://api.elections.kalshi.com/trade-api/v2/markets/"
               + urllib.parse.quote(str(market["ticker"]), safe="") + "/orderbook")
        with urllib.request.urlopen(url, timeout=timeout) as response:
            quotes = orderbook_quotes(json.load(response))
        market.update(quotes)
        market["observed_at"] = time.time()
        market["quote_source"] = "kalshi_orderbook"
    except (OSError, ValueError, RuntimeError, KeyError):
        # Do not use mixed-age or incomplete summary prices as executable evidence.
        market["quote_source"] = "market_summary_only"
        for side in ("yes", "no"):
            market[f"{side}_ask_size"] = None
        market["orderbook_error"] = "orderbook unavailable; executable liquidity unknown"
    return market


def fetch_previous_kalshi_market(
    series_ticker: str = DEFAULT_KALSHI_SERIES,
    timeout: int = 15,
) -> dict[str, Any] | None:
    """Fetch the most recently settled BTC 15-minute market."""

    settled = _fetch_kalshi_markets(series_ticker, "settled", limit=20, timeout=timeout)
    if not settled:
        return None
    settled.sort(key=lambda market: market.get("close_time") or "", reverse=True)
    return normalize_kalshi_market(settled[0])


def build_typesafe_request(
    prediction: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    """Build one narrow, typed TypeSafe review question from measured state."""

    recent_trades = (state.get("paper_trades") or [])[-20:]
    review_state = {
        "prediction": {
            "direction": prediction.get("direction"),
            "probability_up": prediction.get("probability_up"),
            "trade_signal": prediction.get("trade_signal"),
            "model_edge": prediction.get("model_edge"),
            "entry_price": prediction.get("market_entry_price"),
            "spread": prediction.get("market_spread"),
            "liquidity": prediction.get("market_liquidity"),
            "indicator_confirmation": prediction.get("indicator_confirmation"),
            "signal_checks": prediction.get("signal_checks"),
        },
        "contract": prediction.get("kalshi"),
        "forecast_bar_timestamp": prediction.get("bar_timestamp"),
        "blocked_reasons": prediction.get("blockers", []),
        "cost_allowance_per_contract": prediction.get("cost_allowance_per_contract"),
        "official_validation": official_validation(state),
        "learning": {
            "settled_samples": len(recent_trades),
            "rolling_accuracy": learning_analysis(state).get("rolling_accuracy"),
            "rolling_brier": learning_analysis(state).get("rolling_brier"),
            "drawdown": learning_analysis(state).get("drawdown"),
        },
    }
    return {
        "state": review_state,
        "model": "jev-latest",
        "questions": {
            "strategy_review": {
                "type": "choice",
                "instructions": {
                    "question": "Given the measured setup and learning history, which paper-trading action is justified?",
                    "focus": "Use only the supplied quantitative state. Do not invent market facts or guarantee an outcome.",
                },
                "criteria": {
                    "approve_trade": "The deterministic checks pass, evidence is coherent, and a small paper trade is justified.",
                    "wait": "The setup is not strong enough, confidence is modest, or indicators conflict.",
                    "data_issue": "The market price, spread, liquidity, or input data is stale, missing, or unusable.",
                    "review": "The setup is unusual or conflicting enough that an automatic paper trade should be withheld for review.",
                },
            }
        },
    }


def typesafe_strategy_review(
    prediction: dict[str, Any], state: dict[str, Any], timeout: int = 10
) -> dict[str, Any]:
    """Review only an eligible candidate; an enabled but failed review cannot approve."""

    api_key = os.environ.get(TYPESAFE_API_KEY_ENV)
    if not api_key:
        return {
            "enabled": False,
            "status": "disabled",
            "reason": f"{TYPESAFE_API_KEY_ENV} is not set",
        }

    if prediction.get("trade_signal") != "TRADE":
        return {"enabled": True, "status": "skipped", "reason": "Deterministic/validation gates already require WAIT."}
    fingerprint = review_fingerprint(prediction)
    request_body = json.dumps(build_typesafe_request(prediction, state)).encode("utf-8")
    request = urllib.request.Request(
        TYPESAFE_ENDPOINT,
        data=request_body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "btc-15m-predictor/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        answer = payload["answers"]["strategy_review"]
        return {
            "enabled": True,
            "status": "ok",
            "model": payload.get("model"),
            "choice": answer.get("choice"),
            "confidence": answer.get("confidence"),
            "probabilities": answer.get("probabilities", {}),
            "fingerprint": fingerprint,
            "bar_timestamp": prediction.get("bar_timestamp"),
            "market_ticker": (prediction.get("kalshi") or {}).get("ticker"),
            "reviewed_at": time.time(),
            "expires_at": time.time() + MAX_QUOTE_AGE_SECONDS,
        }
    except Exception:
        # No response bodies or credentials are logged. The optional enabled gate fails closed.
        return {"enabled": True, "status": "error", "reason": "TypeSafe review unavailable; paper trade withheld."}


def review_fingerprint(prediction: dict[str, Any]) -> str:
    evidence = {k: prediction.get(k) for k in (
        "bar_timestamp", "probability_up", "direction", "model_edge", "market_entry_price",
        "market_spread", "market_liquidity", "signal_checks", "validation_samples",
    )}
    evidence["ticker"] = (prediction.get("kalshi") or {}).get("ticker")
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, allow_nan=False).encode()).hexdigest()


def apply_typesafe_gate(
    prediction: dict[str, Any], review: dict[str, Any], *, now: float | None = None,
) -> dict[str, Any]:
    """A review is valid only for the exact evidence and while unexpired."""
    now = time.time() if now is None else now
    review = dict(review)
    enabled = review.get("enabled", review.get("status") == "ok")
    expiry = optional_number(review.get("expires_at"))
    reviewed_at = optional_number(review.get("reviewed_at"))
    fresh = bool(review.get("status") == "ok" and expiry is not None and reviewed_at is not None
                 and reviewed_at <= now <= expiry and now-reviewed_at <= MAX_QUOTE_AGE_SECONDS
                 and review.get("fingerprint") == review_fingerprint(prediction))
    confidence = optional_number(review.get("confidence"))
    approved = fresh and review.get("choice") == "approve_trade" and confidence is not None and .55 <= confidence <= 1
    review["applies_to_current_evidence"] = fresh
    if prediction.get("trade_signal") == "TRADE" and enabled:
        prediction.setdefault("signal_checks", {})["typesafe"] = bool(approved)
        if not approved:
            reason = (f"TypeSafe did not confidently approve ({review.get('choice', 'unknown')})"
                      if fresh else "TypeSafe review missing, failed, expired, or belongs to different evidence")
            prediction.setdefault("blockers", []).append(reason)
            prediction["trade_signal"] = "WAIT"
            prediction["trade_reason"] = "Wait: " + "; ".join(prediction["blockers"])
            prediction["demo_quantity"] = 0
            prediction["demo_cost"] = 0.0
    prediction["typesafe_review"] = review
    return prediction


def new_demo_account(
    starting_balance: float = DEFAULT_DEMO_BALANCE,
    budget_per_trade: float = DEFAULT_DEMO_BUDGET,
) -> dict[str, Any]:
    return {
        "starting_balance": float(starting_balance),
        "balance": float(starting_balance),
        "budget_per_trade": float(budget_per_trade),
        "total_traded": 0.0,
        "realized_pnl": 0.0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "demo_trades": [],
    }


def ensure_demo_account(state: dict[str, Any]) -> dict[str, Any]:
    account = state.get("demo_account")
    if not isinstance(account, dict):
        account = new_demo_account()
        state["demo_account"] = account
    account.setdefault("starting_balance", DEFAULT_DEMO_BALANCE)
    account.setdefault("balance", account["starting_balance"])
    account.setdefault("budget_per_trade", DEFAULT_DEMO_BUDGET)
    account.setdefault("total_traded", 0.0)
    account.setdefault("realized_pnl", 0.0)
    account.setdefault("trades", 0)
    account.setdefault("wins", 0)
    account.setdefault("losses", 0)
    account.setdefault("demo_trades", [])
    return account


def indicator_confirmation(
    direction: str, features: Sequence[float] | None
) -> dict[str, Any]:
    """Check correlated short-term trend signals; agreement is not independent evidence."""

    if features is None or len(features) < 4:
        return {
            "available": False,
            "agreeing": 0,
            "required": 3,
            "passed": False,
            "reason": "indicator history unavailable",
        }
    values = [float(features[index]) for index in (0, 1, 2, 3)]
    signs = [value > 0 if direction == "UP" else value < 0 for value in values]
    agreeing = sum(signs)
    names = ("15m momentum", "45m momentum", "3h momentum", "EMA trend")
    agreeing_names = [name for name, agrees in zip(names, signs) if agrees]
    return {
        "available": True,
        "agreeing": agreeing,
        "required": 3,
        "passed": agreeing >= 3,
        "indicators": dict(zip(names, signs)),
        "agreeing_indicators": agreeing_names,
        "reason": f"{agreeing}/4 trend indicators agree",
    }


def market_timing_checks(
    market: dict[str, Any] | None, bar_timestamp: int | None, now: float
) -> dict[str, bool]:
    market = market or {}
    opened = timestamp_seconds(market.get("open_time"))
    closed = timestamp_seconds(market.get("close_time"))
    observed = timestamp_seconds(market.get("observed_at"))
    return {
        "candle_freshness": bar_timestamp == int(now // INTERVAL_SECONDS) * INTERVAL_SECONDS - INTERVAL_SECONDS,
        "market_alignment": (bar_timestamp is not None and opened == bar_timestamp + INTERVAL_SECONDS
                             and closed == bar_timestamp + 2 * INTERVAL_SECONDS),
        "market_open": bool(market.get("ticker") and market.get("status") in {"active", "open"}
                            and opened is not None and closed is not None and opened <= now < closed),
        "quote_freshness": observed is not None and 0 <= now - observed <= MAX_QUOTE_AGE_SECONDS,
        "entry_timing": (opened is not None and closed is not None and 0 <= now - opened <= MAX_ENTRY_DELAY_SECONDS
                         and closed - now >= MIN_SECONDS_TO_CLOSE),
    }


def evaluate_trade_signal(
    probability_up: float,
    kalshi_market: dict[str, Any] | None,
    features: Sequence[float] | None = None,
    *, bar_timestamp: int | None = None, now: float | None = None,
) -> dict[str, Any]:
    """Conservative candidate with one explanation for EVERY failing check."""
    now = time.time() if now is None else now
    valid_probability = optional_number(probability_up)
    probability = valid_probability if valid_probability is not None else .5
    direction = "UP" if probability >= .5 else "DOWN"
    confidence = probability if direction == "UP" else 1 - probability
    confirmation = indicator_confirmation(direction, features)
    market = kalshi_market or {}
    side = "yes" if direction == "UP" else "no"
    entry = optional_number(market.get(f"{side}_ask"))
    bid = optional_number(market.get(f"{side}_bid"))
    liquidity = optional_number(market.get(f"{side}_ask_size"))
    gross_edge = confidence - entry if entry is not None else None
    edge = gross_edge - PAPER_COST_ALLOWANCE if gross_edge is not None else None
    spread = entry - bid if entry is not None and bid is not None else None
    valid_features = (features is not None and len(features) == len(FEATURE_NAMES)
                      and all(optional_number(v) is not None for v in features))
    checks = {
        "probability": valid_probability is not None and 0 <= probability <= 1,
        **market_timing_checks(kalshi_market, bar_timestamp, now),
        "confidence": confidence >= MIN_TRADE_CONFIDENCE,
        "indicators": confirmation["passed"],
        "features": valid_features,
        "market": entry is not None and bid is not None and 0 < bid <= entry < 1,
        "orderbook": market.get("quote_source") == "kalshi_orderbook",
        "edge": edge is not None and edge + 1e-9 >= MIN_TRADE_EDGE,
        "spread": spread is not None and 0 <= spread <= MAX_MARKET_SPREAD + 1e-9,
        "liquidity": liquidity is not None and liquidity >= MIN_MARKET_LIQUIDITY,
        "volatility": bool(valid_features and 0 <= features[4] <= MAX_VOLATILITY_PCT
                           and 0 <= features[6] <= MAX_CANDLE_RANGE_PCT),
    }
    messages = {
        "probability": "model probability invalid/unknown",
        "candle_freshness": "latest complete candle is stale or missing",
        "market_alignment": "Kalshi ticker does not match the forecast's exact 15-minute interval",
        "market_open": "market is closed, inactive, or missing timing evidence",
        "quote_freshness": "quotes stale or timestamp unknown (maximum 60 seconds)",
        "entry_timing": "outside first 120 seconds of the forecast interval; wait for the next window",
        "confidence": f"raw model probability {confidence:.1%} is below {MIN_TRADE_CONFIDENCE:.0%}",
        "indicators": confirmation["reason"],
        "features": "feature evidence unavailable or invalid",
        "market": "executable bid/ask unknown or invalid/crossed",
        "orderbook": "executable orderbook unavailable; summary odds alone are insufficient",
        "edge": (f"cost-adjusted edge {edge:.1%} is below {MIN_TRADE_EDGE:.0%}" if edge is not None else "edge unknown"),
        "spread": (f"spread {spread:.1%} is invalid or exceeds {MAX_MARKET_SPREAD:.0%}" if spread is not None else "spread unknown"),
        "liquidity": (f"ask depth {liquidity:.2f} is below {MIN_MARKET_LIQUIDITY:.0f} contracts" if liquidity is not None else "executable ask liquidity unknown, not zero"),
        "volatility": "volatility/range evidence invalid or above the existing safety limits",
    }
    blockers = [messages[key] for key, passed in checks.items() if not passed]
    trade = not blockers
    reason = (f"Paper candidate {direction}: raw probability {confidence:.1%}, net edge {edge:.1%}, "
              f"ask depth {liquidity:.2f}; {confirmation['reason']}" if trade else "Wait: " + "; ".join(blockers))
    return {
        "signal": "TRADE" if trade else "WAIT", "direction": direction, "confidence": confidence,
        "market_probability": entry, "entry_price": entry, "gross_edge": gross_edge,
        "edge": edge, "cost_allowance": PAPER_COST_ALLOWANCE, "spread": spread,
        "liquidity": liquidity, "confirmation": confirmation, "checks": checks,
        "blockers": blockers, "reason": reason,
    }


def new_state(model: OnlineLogisticRegression) -> dict[str, Any]:
    return {
        "version": 1,
        "feature_names": list(FEATURE_NAMES),
        "model": model.to_dict(),
        "pending": None,
        "paper_trades": [],
        "skipped_predictions": [],
        "unresolved_demo_trades": [],
        "demo_account": new_demo_account(),
        "last_trained_bar": None,
        "last_learned_bar": None,
        "last_learning_status": "warming_up",
    }


@contextmanager
def state_transaction(path: str | Path):
    """Serialize dashboard/runner state updates across concurrent processes."""

    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_state(path: str | Path) -> tuple[OnlineLogisticRegression, dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("feature_names") != list(FEATURE_NAMES):
        raise ValueError("state file was created with incompatible feature names")
    return OnlineLogisticRegression.from_dict(payload["model"]), payload


def save_state(path: str | Path, model: OnlineLogisticRegression, state: dict[str, Any]) -> None:
    state = dict(state)
    state["model"] = model.to_dict()
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, delete=False
    ) as temporary:
        json.dump(state, temporary, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary_path = temporary.name
    os.replace(temporary_path, destination)


def archive_and_guard(
    state_path: str | Path, state: dict[str, Any], bars: Sequence[WindowBar],
    prediction: dict[str, Any] | None = None, *, raw_candles: Sequence[Any] | None = None,
    product_id: str = DEFAULT_PRODUCT, now: float | None = None,
    prices_observed_at: float | None = None,
) -> dict[str, Any] | None:
    """Archive independently of rolling JSON; failures must not create paper fills."""
    from forecast_archive import archive_path, record_cycle
    now = time.time() if now is None else now
    try:
        status = record_cycle(archive_path(state_path), state, bars=bars, raw_candles=raw_candles,
                              prediction=prediction, product_id=product_id, now=now,
                              prices_observed_at=prices_observed_at)
        state["archive_status"] = status
    except (OSError, sqlite3.Error, ValueError, TypeError):
        # Never dump exception payloads, credentials, or private state into telemetry.
        status = {"status": "error", "path": str(archive_path(state_path)),
                  "reason": "Durable archive unavailable; new paper entries withheld."}
        state["archive_status"] = status
        if prediction is not None:
            pending = state.get("pending") or {}
            if prediction.get("paper_entry_created") and pending.get("bar_timestamp") == prediction.get("bar_timestamp"):
                for key in ("trade_committed_at", "entry_market_snapshot", "entry_signal_checks", "entry_review_fingerprint"):
                    pending.pop(key, None)
                pending.update({"trade_signal": "WAIT", "demo_quantity": 0, "demo_cost": 0.0})
            prediction["trade_signal"] = "WAIT"
            prediction["demo_quantity"] = 0
            prediction["demo_cost"] = 0.0
            prediction["paper_entry_created"] = False
            prediction.setdefault("signal_checks", {})["archive"] = False
            prediction.setdefault("blockers", []).append(status["reason"])
            prediction = finalize_prediction(prediction, state, prediction.get("typesafe_review"), now=now)
    if prediction is not None:
        prediction["archive"] = status
    return prediction


def settle_demo_trade(pending: dict[str, Any], label: int, next_bar: WindowBar | None) -> dict[str, Any]:
    """Settle one flat-size paper trade; never submits a real order."""

    side = pending.get("demo_side") or ("UP" if float(pending["probability_up"]) >= 0.5 else "DOWN")
    correct = (side == "UP" and label == 1) or (side == "DOWN" and label == 0)
    observed_entry = optional_number(pending.get("demo_entry_price"))
    valid_entry = observed_entry is not None and 0 < observed_entry < 1
    entry_price = observed_entry if valid_entry else 0.5
    pnl = (1.0 - entry_price) if correct else -entry_price
    return {
        "bar_timestamp": int(pending["bar_timestamp"]),
        "side": side,
        "entry_price": entry_price,
        "entry_source": pending.get("demo_entry_source", "kalshi_ask") if valid_entry else "synthetic_0.50",
        "probability_up": float(pending.get("probability_up", 0.5)),
        "outcome": "UP" if label else "DOWN",
        "correct": correct,
        "pnl": pnl,
        "entry_close": float(pending["close"]),
        "exit_close": pending.get("official_expiration_value") if pending.get("outcome_source") == "kalshi_official" else (next_bar.close if next_bar else None),
        "exit_timestamp": int(pending["bar_timestamp"]) + INTERVAL_SECONDS,
        "market_ticker": pending.get("market_ticker"),
        "market_close_time": pending.get("market_close_time"),
        "outcome_source": pending.get("outcome_source", "coinbase_proxy"),
        "forecast_issued_at": pending.get("forecast_issued_at"),
        "features": pending.get("features"),
        "model_at_issue": pending.get("model_at_issue"),
        "validation_eligible": pending.get("validation_eligible", True),
        "market_snapshot": pending.get("market_snapshot"),
        "official_result_snapshot": pending.get("official_result_snapshot"),
        "settled_at": pending.get("settled_at"),
        "scoring_note": "Hypothetical one-contract match; not an account trade or net return.",
    }


def paper_trade_summary(state: dict[str, Any]) -> dict[str, Any]:
    """Summarize settled one-contract prediction matches."""

    trades = state.get("paper_trades") or []
    wins = sum(1 for trade in trades if trade.get("correct"))
    total_cost = sum(float(trade.get("entry_price", 0.5)) for trade in trades)
    pnl = sum(float(trade.get("pnl", 0.0)) for trade in trades)
    return {
        "settled_trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "accuracy": wins / len(trades) if trades else None,
        "pnl": pnl,
        "return_on_cost": pnl / total_cost if total_cost else None,
        "last_trade": trades[-1] if trades else None,
    }


def official_validation(state: dict[str, Any]) -> dict[str, Any]:
    """Do not validate a Kalshi contract strategy with Coinbase proxy outcomes."""
    seen = set()
    trades = []
    for trade in state.get("paper_trades") or []:
        ticker = trade.get("market_ticker")
        probability = optional_number(trade.get("probability_up"))
        if (trade.get("outcome_source") == "kalshi_official" and trade.get("validation_eligible", True)
                and ticker and ticker not in seen
                and probability is not None and 0 <= probability <= 1 and trade.get("outcome") in {"UP", "DOWN"}):
            seen.add(ticker)
            trades.append(trade)
    recent = trades[-100:]
    accuracy = sum(bool(t.get("correct")) for t in recent) / len(recent) if recent else None
    brier = sum((float(t["probability_up"]) - int(t["outcome"] == "UP")) ** 2 for t in recent) / len(recent) if recent else None
    blockers = []
    if len(trades) < MIN_LIVE_SAMPLES:
        blockers.append(f"official Kalshi validation {len(trades)}/{MIN_LIVE_SAMPLES}; proxy history does not count")
    if len(recent) >= 20 and accuracy is not None and accuracy < .50:
        blockers.append(f"official rolling accuracy {accuracy:.1%} is below 50%")
    if len(recent) >= 20 and brier is not None and brier >= .25:
        blockers.append(f"official Brier {brier:.3f} does not beat the constant 50% baseline (0.250)")
    calibration = []
    for lower in (.5, .6, .7, .8, .9):
        group = [t for t in recent if lower <= max(float(t["probability_up"]), 1 - float(t["probability_up"])) < lower + .1 + (1e-9 if lower == .9 else 0)]
        if group:
            calibration.append({"range": f"{lower:.0%}–{lower+.1:.0%}", "samples": len(group),
                                "mean_raw_probability": sum(max(float(t["probability_up"]), 1-float(t["probability_up"])) for t in group)/len(group),
                                "observed_accuracy": sum(bool(t.get("correct")) for t in group)/len(group)})
    return {"samples": len(trades), "minimum_samples": MIN_LIVE_SAMPLES,
            "rolling_samples": len(recent), "accuracy": accuracy, "brier": brier,
            "baseline_brier": .25, "ready": not blockers, "blockers": blockers,
            "calibration": calibration, "probability_status": "uncalibrated model estimate",
            "note": "Preliminary forward paper evidence only; no walk-forward profitability claim."}


def learning_freshness(
    state: dict[str, Any], current_bar_timestamp: int | None = None, *, now: float | None = None,
) -> dict[str, Any]:
    """Report whether the online learner is receiving timely settled outcomes."""

    pending = state.get("pending") or {}
    pending_timestamp = pending.get("bar_timestamp")
    last_learned = state.get("last_learned_bar")
    now = time.time() if now is None else now
    wall_reference = int(now // INTERVAL_SECONDS) * INTERVAL_SECONDS - INTERVAL_SECONDS
    reference = max(current_bar_timestamp, wall_reference) if current_bar_timestamp is not None else wall_reference
    pending_age_bars = None
    last_learned_age_bars = None
    if isinstance(reference, (int, float)):
        if isinstance(pending_timestamp, (int, float)):
            pending_age_bars = max(0, int((reference - pending_timestamp) // INTERVAL_SECONDS))
        if isinstance(last_learned, (int, float)):
            last_learned_age_bars = max(0, int((reference - last_learned) // INTERVAL_SECONDS))

    skipped = state.get("skipped_predictions") or []
    status = state.get("last_learning_status", "warming_up")
    if pending_age_bars is not None and pending_age_bars >= 2:
        status = "STALE / RECOVERY NEEDED"
    elif status == "recovered_skip":
        status = "RECOVERED / WAITING FOR FRESH RESULT"
    elif last_learned_age_bars is None:
        status = "WARMING UP"
    elif last_learned_age_bars <= 2:
        status = "FRESH"
    else:
        status = "STALE / RECOVERY NEEDED"

    return {
        "status": status,
        "pending_bar_timestamp": pending_timestamp,
        "last_learned_bar": last_learned,
        "pending_age_bars": pending_age_bars,
        "last_learned_age_bars": last_learned_age_bars,
        "skipped_predictions": len(skipped),
        "freshness_limit_bars": 2,
    }


def learning_analysis(
    state: dict[str, Any], prediction: dict[str, Any] | None = None, *, now: float | None = None,
) -> dict[str, Any]:
    """Return explainable rolling metrics and a conservative risk gate.

    This is deliberately a short rationale, not hidden chain-of-thought. It
    describes the measurable checks used by the paper strategy.
    """

    trades = state.get("paper_trades") or []
    recent = trades[-20:]
    labels = [1 if trade.get("outcome") == "UP" else 0 for trade in recent]
    probabilities = [trade.get("probability_up") for trade in recent]
    paired = [
        (float(probability), label)
        for probability, label in zip(probabilities, labels)
        if isinstance(probability, (int, float))
    ]
    accuracy = (
        sum(bool(trade.get("correct")) for trade in recent) / len(recent)
        if recent else None
    )
    brier = (
        sum((probability - label) ** 2 for probability, label in paired) / len(paired)
        if paired else None
    )
    rolling_pnl = sum(float(trade.get("pnl", 0.0)) for trade in recent)
    account = ensure_demo_account(state)
    starting_balance = float(account["starting_balance"])
    balance = float(account["balance"])
    drawdown = max(0.0, (starting_balance - balance) / starting_balance) if starting_balance else 0.0

    current_bar_timestamp = (
        int(prediction["bar_timestamp"])
        if prediction and isinstance(prediction.get("bar_timestamp"), (int, float))
        else None
    )
    freshness = learning_freshness(state, current_bar_timestamp, now=now)
    validation = official_validation(state)
    blocks: list[str] = list(validation["blockers"])
    if freshness["status"] in {"STALE / RECOVERY NEEDED", "RECOVERED / WAITING FOR FRESH RESULT"}:
        blocks.append(f"learning freshness is {freshness['status'].lower()}")
    if len(recent) >= 5 and accuracy is not None and accuracy < 0.50:
        blocks.append(f"rolling accuracy {accuracy:.1%} is below 50%")
    if len(paired) >= 5 and brier is not None and brier > 0.27:
        blocks.append(f"rolling Brier score {brier:.3f} is weak")
    if (state.get("archive_status") or {}).get("status") == "error":
        blocks.append("durable data archive unavailable; new paper entries withheld")
    if state.get("unresolved_demo_trades"):
        blocks.append("unresolved committed paper entry requires settlement review; no new account trades")
    if drawdown >= 0.10:
        blocks.append(f"demo drawdown is {drawdown:.1%}")

    risk_gate = "BLOCK" if blocks else "ALLOW"
    if blocks:
        status = "CAUTION / WAIT"
    elif len(recent) < 5:
        status = "WARMING UP"
    elif accuracy is not None and accuracy >= 0.55:
        status = "HEALTHY"
    else:
        status = "CAUTION"

    suggestion = "WAIT"
    reason = "No current model signal."
    if prediction is not None:
        if risk_gate == "BLOCK":
            reason = "Risk guard: " + "; ".join(blocks) + ". Preserve demo balance."
        elif prediction.get("trade_signal") == "TRADE":
            probability_up = float(prediction.get("probability_up") or 0.5)
            confidence = (
                probability_up
                if prediction.get("direction") == "UP"
                else 1.0 - probability_up
            )
            cost = float(prediction.get("demo_cost") or 0.0)
            reason = (
                f"Paper TRADE {prediction.get('direction')} only: confidence "
                f"{confidence:.1%}, edge {float(prediction.get('model_edge') or 0.0):.1%}, "
                f"budget ${cost:.2f}."
            )
            suggestion = "TRADE NOW"
        else:
            reason = str(prediction.get("trade_reason") or "Confidence/edge gate says wait.")

    return {
        "status": status,
        "risk_gate": risk_gate,
        "settled_samples": len(recent),
        "rolling_window": 20,
        "rolling_accuracy": accuracy,
        "rolling_brier": brier,
        "rolling_pnl": rolling_pnl,
        "drawdown": drawdown,
        "learning_freshness": freshness,
        "official_validation": validation,
        "blockers": blocks,
        "metrics_note": "Historical prediction-match metrics include legacy Coinbase proxy results; not account returns.",
        "suggestion": suggestion,
        "reason": reason,
        "checks": {
            "confidence_threshold": MIN_TRADE_CONFIDENCE,
            "edge_threshold": MIN_TRADE_EDGE,
            "min_samples_before_risk_block": 5,
        },
    }


def settle_demo_account(
    state: dict[str, Any], pending: dict[str, Any], label: int, next_bar: WindowBar | None
) -> dict[str, Any] | None:
    """Settle a demo account trade only when the model emitted TRADE."""

    if pending.get("trade_signal") != "TRADE":
        return None
    account = ensure_demo_account(state)
    quantity = int(pending.get("demo_quantity", 0))
    entry_price = optional_number(pending.get("demo_entry_price"))
    cost = optional_number(pending.get("demo_cost"))
    if quantity < 1 or entry_price is None or cost is None or not 0 < entry_price < 1 or cost <= 0:
        return None  # invalid/vetoed paper decisions cannot be turned into fills
    side = pending.get("demo_side") or "UP"
    correct = (side == "UP" and label == 1) or (side == "DOWN" and label == 0)
    payout = float(quantity) if correct else 0.0
    pnl = payout - cost
    trade = {
        "bar_timestamp": int(pending["bar_timestamp"]),
        "side": side,
        "quantity": quantity,
        "entry_price": entry_price,
        "cost": cost,
        "outcome": "UP" if label else "DOWN",
        "correct": correct,
        "payout": payout,
        "pnl": pnl,
        "entry_close": float(pending["close"]),
        "exit_close": pending.get("official_expiration_value") if pending.get("outcome_source") == "kalshi_official" else (next_bar.close if next_bar else None),
        "exit_timestamp": int(pending["bar_timestamp"]) + INTERVAL_SECONDS,
        "market_ticker": pending.get("market_ticker"),
        "outcome_source": pending.get("outcome_source", "coinbase_proxy"),
        "cost_allowance_per_contract": pending.get("cost_allowance_per_contract", 0),
        "entry_market_snapshot": pending.get("entry_market_snapshot"),
        "entry_signal_checks": pending.get("entry_signal_checks"),
        "entry_review_fingerprint": pending.get("entry_review_fingerprint"),
        "typesafe_review": pending.get("typesafe_review"),
        "trade_committed_at": pending.get("trade_committed_at"),
        "forecast_issued_at": pending.get("forecast_issued_at"),
        "official_result_snapshot": pending.get("official_result_snapshot"),
    }
    account["balance"] = float(account["balance"]) + pnl
    account["total_traded"] = float(account["total_traded"]) + cost
    account["realized_pnl"] = float(account["realized_pnl"]) + pnl
    account["trades"] = int(account["trades"]) + 1
    account["wins"] = int(account["wins"]) + int(correct)
    account["losses"] = int(account["losses"]) + int(not correct)
    account["demo_trades"].append(trade)
    account["demo_trades"] = account["demo_trades"][-500:]
    return trade


def demo_account_summary(state: dict[str, Any]) -> dict[str, Any]:
    account = ensure_demo_account(state)
    trades = account["demo_trades"]
    wins = int(account["wins"])
    total = int(account["trades"])
    return {
        "starting_balance": float(account["starting_balance"]),
        "balance": float(account["balance"]),
        "budget_per_trade": float(account["budget_per_trade"]),
        "unresolved_trades": len(state.get("unresolved_demo_trades") or []),
        "total_traded": float(account["total_traded"]),
        "realized_pnl": float(account["realized_pnl"]),
        "trades": total,
        "wins": wins,
        "losses": int(account["losses"]),
        "accuracy": wins / total if total else None,
        "return_on_starting_balance": float(account["realized_pnl"]) / float(account["starting_balance"])
        if float(account["starting_balance"])
        else None,
        "last_trade": trades[-1] if trades else None,
    }


def learn_from_pending(
    model: OnlineLogisticRegression,
    state: dict[str, Any],
    bars: Sequence[WindowBar],
    *, now: float | None = None,
) -> dict[str, Any] | None:
    """Use the EXACT queued Kalshi settlement, never a Coinbase substitute.

    Legacy pending entries retain their original proxy label and are identified
    as such. New unaligned forecasts are research-only and skipped, not scored.
    """
    explicit_now = now is not None
    now = time.time() if now is None else now
    pending = state.get("pending")
    if not pending:
        return None
    pending_timestamp = int(pending["bar_timestamp"])
    expected_timestamp = pending_timestamp + INTERVAL_SECONDS
    expected_close = expected_timestamp + INTERVAL_SECONDS
    next_bar = next((b for b in bars if b.timestamp == expected_timestamp), None)
    current_timestamp = bars[-1].timestamp if bars else int(now // INTERVAL_SECONDS) * INTERVAL_SECONDS - INTERVAL_SECONDS
    source = pending.get("outcome_source", "coinbase_proxy")
    skip_reason = None
    if source == "kalshi_official":
        if now < expected_close:
            return None
        ticker = pending.get("market_ticker")
        if any(t.get("market_ticker") == ticker and t.get("outcome_source") == source
               for t in state.get("paper_trades") or []):
            state["pending"] = None
            return {"status": "already_learned", "bar_timestamp": pending_timestamp}
        settled = None
        try:
            settled = fetch_kalshi_market_by_ticker(str(ticker))
        except (OSError, ValueError, RuntimeError):
            pending["settlement_status"] = "exact-market result unavailable; no proxy fill"
        if (settled and settled.get("ticker") == ticker and settled.get("result") in {"yes", "no"}
                and timestamp_seconds(settled.get("close_time")) == expected_close
                and timestamp_seconds(pending.get("market_close_time")) == expected_close):
            label = int(settled["result"] == "yes")
            pending["official_expiration_value"] = optional_number(settled.get("expiration_value"))
            pending["official_result_snapshot"] = dict(settled)
            pending["settled_at"] = now if explicit_now else time.time()  # response received, not request start
        else:
            pending["settlement_status"] = "waiting for exact-market official settlement; no proxy fill"
            if now < expected_close + MAX_SETTLEMENT_DELAY_SECONDS:
                return None
            skip_reason = "official_settlement_unavailable_or_misaligned"
    elif source == "unmatched_research":
        if now < expected_close:
            return None
        skip_reason = "forecast_not_bound_to_exact_Kalshi_interval"
    elif next_bar is None:
        if current_timestamp < expected_close:
            return None
        skip_reason = "next_bar_unavailable_or_out_of_window"
    else:
        label = int(next_bar.close >= float(pending["close"]))
        pending["outcome_source"] = "coinbase_proxy"

    if skip_reason:
        if pending.get("trade_signal") == "TRADE" and int(pending.get("demo_quantity") or 0) > 0:
            # A missing official label is NOT a cancelled fill or free money.
            state.setdefault("unresolved_demo_trades", []).append(dict(pending))
        skipped = {"bar_timestamp": pending_timestamp, "market_ticker": pending.get("market_ticker"),
                   "reason": skip_reason, "recovered_at_bar": current_timestamp}
        state.setdefault("skipped_predictions", []).append(skipped)
        state["skipped_predictions"] = state["skipped_predictions"][-500:]
        state["pending"] = None
        state["last_learning_status"] = "recovered_skip"
        return {"status": "skipped_stale", **skipped}

    prior_probability = model.update([float(v) for v in pending["features"]], label)
    paper_trade = settle_demo_trade(pending, label, next_bar)
    demo_trade = settle_demo_account(state, pending, label, next_bar)
    state.setdefault("paper_trades", []).append(paper_trade)
    state["paper_trades"] = state["paper_trades"][-500:]
    state["pending"] = None
    state["last_learned_bar"] = expected_timestamp
    state["last_learning_status"] = "learned"
    return {"bar_timestamp": pending_timestamp, "label": label, "outcome": "UP" if label else "DOWN",
            "outcome_source": pending["outcome_source"], "market_ticker": pending.get("market_ticker"),
            "probability_before_update": prior_probability, "paper_trade": paper_trade, "demo_trade": demo_trade}


def bootstrap_from_bars(
    bars: Sequence[WindowBar], epochs: int = 5
) -> tuple[OnlineLogisticRegression, dict[str, Any], dict[str, float]]:
    samples = build_training_samples(bars)
    if len(samples) < 5:
        raise ValueError(
            f"need at least 5 training samples; only {len(samples)} available. "
            "Increase lookback_minutes."
        )
    model, metrics = train_model(samples, epochs=epochs)
    state = new_state(model)
    state["last_trained_bar"] = samples[-1][2]
    return model, state, metrics


def predict_and_queue(
    model: OnlineLogisticRegression,
    state: dict[str, Any],
    bars: Sequence[WindowBar],
    kalshi_market: dict[str, Any] | None = None,
    *, now: float | None = None,
) -> dict[str, Any]:
    """Freeze new forecasts with timely quotes; previews wait for market discovery."""
    now = time.time() if now is None else now
    if not bars:
        raise ValueError("no complete 15-minute bars available")
    current = bars[-1]
    features = feature_vector(bars, len(bars) - 1)
    if features is None:
        raise ValueError("not enough completed bars to build a prediction")
    pending = state.get("pending") or {}
    same_forecast = pending.get("bar_timestamp") == current.timestamp
    # Repeated dashboard refreshes must not rewrite the original out-of-sample call.
    probability_up = float(pending["probability_up"]) if same_forecast else model.predict_proba(features)
    signal = evaluate_trade_signal(probability_up, kalshi_market, features, bar_timestamp=current.timestamp, now=now)
    timing = market_timing_checks(kalshi_market, current.timestamp, now)
    market = kalshi_market or {}
    yes_bid, yes_ask = optional_number(market.get("yes_bid")), optional_number(market.get("yes_ask"))
    midpoint = optional_number(market.get("yes_mid"))
    valid_quotes = (yes_bid is not None and yes_ask is not None and 0 <= yes_bid <= yes_ask <= 1
                    and ("yes_mid" not in market or (midpoint is not None
                         and abs(midpoint-(yes_bid+yes_ask)/2) <= 1e-8)))
    evidence_ready = (all(timing.values()) and market.get("quote_source") == "kalshi_orderbook"
                      and valid_quotes and signal["checks"]["probability"])
    signal["checks"]["forecast_evidence"] = bool(evidence_ready)
    if not evidence_ready:
        signal["blockers"].append("forecast evidence needs the exact active contract and fresh, valid orderbook within the first 120 seconds")
    historical_analysis = learning_analysis(state, {"bar_timestamp": current.timestamp, "trade_signal": "WAIT"}, now=now)
    signal["checks"]["risk_validation"] = historical_analysis["risk_gate"] == "ALLOW"
    signal["blockers"].extend(historical_analysis["blockers"])
    signal["checks"]["pending_slot"] = not pending or same_forecast
    if not signal["checks"]["pending_slot"]:
        signal["blockers"].append("previous exact-market settlement still pending; no overlapping paper position")
    signal["checks"]["pending_contract"] = not pending.get("market_ticker") or pending.get("market_ticker") == (kalshi_market or {}).get("ticker")
    if not signal["checks"]["pending_contract"]:
        signal["blockers"].append("current quotes belong to a different ticker than the frozen forecast")
    if signal["blockers"]:
        signal["signal"] = "WAIT"
        signal["reason"] = "Wait: " + "; ".join(signal["blockers"])
    direction = signal["direction"]
    entry_price = signal.get("entry_price")
    account = ensure_demo_account(state)
    quantity, cost = 0, 0.0
    unit_cost = entry_price + PAPER_COST_ALLOWANCE if entry_price is not None else None
    if signal["signal"] == "TRADE" and unit_cost is not None:
        available = min(float(account["balance"]), float(account["budget_per_trade"]))
        quantity = min(int(available / unit_cost), int(signal["liquidity"]))
        if quantity < 1:
            signal["signal"] = "WAIT"
            signal["checks"]["budget"] = False
            signal["blockers"].append("demo budget cannot buy one contract including assumed costs")
            signal["reason"] = "Wait: " + "; ".join(signal["blockers"])
        else:
            cost = quantity * unit_cost
            signal["checks"]["budget"] = True
    if not pending and evidence_ready:
        state["pending"] = {
            "bar_timestamp": current.timestamp, "close": current.close, "features": features,
            "probability_up": probability_up, "demo_side": direction,
            "trade_signal": "WAIT", "signal_reason": "awaiting final gates",
            "demo_entry_price": entry_price, "demo_entry_source": "kalshi_orderbook_ask" if entry_price is not None else "unknown",
            "demo_quantity": 0, "demo_cost": 0.0, "cost_allowance_per_contract": PAPER_COST_ALLOWANCE,
            "market_ticker": market["ticker"], "market_close_time": market["close_time"],
            "outcome_source": "kalshi_official",
            "forecast_issued_at": now, "validation_eligible": True,
            "market_snapshot": dict(market),
            "model_at_issue": model.to_dict(),
        }
    elif same_forecast and pending.get("outcome_source") == "unmatched_research" and evidence_ready:
        # Preserve already-issued legacy forecasts; their original timestamp is never moved.
        pending.update({"market_ticker": kalshi_market["ticker"], "market_close_time": kalshi_market["close_time"],
                         "outcome_source": "kalshi_official", "validation_eligible": True,
                         "market_snapshot": dict(kalshi_market)})
    queued = state.get("pending") or {}
    current_queued = queued.get("bar_timestamp") == current.timestamp
    current_window_bar = int(now // INTERVAL_SECONDS) * INTERVAL_SECONDS - INTERVAL_SECONDS
    entry_deadline = current_window_bar + INTERVAL_SECONDS + MAX_ENTRY_DELAY_SECONDS
    if current_queued:
        forecast_status = "issued"
    elif queued:
        forecast_status = "awaiting_settlement"
    elif now <= entry_deadline:
        forecast_status = "awaiting_market_evidence"
    else:
        forecast_status = "missed_window"
        skipped = state.setdefault("skipped_predictions", [])
        if not any(record.get("bar_timestamp") == current_window_bar
                   and record.get("reason") == "entry_window_missed" for record in skipped):
            skipped.append({"bar_timestamp": current_window_bar, "market_ticker": None,
                            "reason": "entry_window_missed", "recovered_at_bar": current_window_bar})
            state["skipped_predictions"] = skipped[-500:]
    prediction = {
        "bar_timestamp": current.timestamp, "bar_time": current.iso_timestamp, "close": current.close,
        "probability_up": probability_up, "probability_down": 1 - probability_up, "direction": direction,
        "probability_label": "Raw, uncalibrated estimate; not a measured chance of winning",
        "model_label_note": "Coinbase proxy bootstrap/legacy history; new exact-market examples use official Kalshi labels.",
        "trade_signal": signal["signal"], "trade_reason": signal["reason"], "blockers": signal["blockers"],
        "model_edge": signal["edge"], "gross_model_edge": signal["gross_edge"],
        "cost_allowance_per_contract": PAPER_COST_ALLOWANCE,
        "market_entry_price": entry_price, "market_spread": signal["spread"], "market_liquidity": signal["liquidity"],
        "indicator_confirmation": signal["confirmation"], "signal_checks": signal["checks"],
        "demo_entry_price": entry_price, "demo_entry_source": "kalshi_orderbook_ask" if entry_price is not None else "unknown",
        "demo_quantity": quantity, "demo_cost": cost, "model_updates": model.updates,
        "validation_samples": historical_analysis["official_validation"]["samples"],
        "kalshi": kalshi_market,
        "forecast_status": forecast_status,
        "forecast_issued_at": queued.get("forecast_issued_at") if current_queued else None,
        "forecast_entry_deadline": entry_deadline,
    }
    midpoint = optional_number((kalshi_market or {}).get("yes_mid"))
    prediction["model_minus_kalshi_yes"] = probability_up - midpoint if midpoint is not None else None
    prediction["analysis"] = learning_analysis(state, prediction, now=now)
    return prediction


def finalize_prediction(
    prediction: dict[str, Any], state: dict[str, Any], review: dict[str, Any] | None = None,
    *, now: float | None = None,
) -> dict[str, Any]:
    """One final action used by CLI, dashboard, rationale, and saved paper entry."""
    now = time.time() if now is None else now
    if review is None:
        review = state.get("typesafe_review") or {"enabled": bool(os.environ.get(TYPESAFE_API_KEY_ENV)), "status": "pending"}
    if state.get("typesafe_required") and not review.get("enabled"):
        review = {"enabled": True, "status": "unavailable", "reason": "Configured gate has no current review/key."}
    # Recheck elapsed time AFTER any network review, never approve stale prices.
    if prediction.get("trade_signal") == "TRADE":
        for key, passed in market_timing_checks(prediction.get("kalshi"), prediction.get("bar_timestamp"), now).items():
            if not passed:
                prediction.setdefault("blockers", []).append(f"{key}: evidence expired before final decision")
                prediction.setdefault("signal_checks", {})[key] = False
        analysis = learning_analysis(state, prediction, now=now)
        if analysis["risk_gate"] == "BLOCK":
            prediction.setdefault("blockers", []).extend(analysis["blockers"])
        if prediction.get("blockers"):
            prediction["trade_signal"] = "WAIT"
    prediction["review_input_fingerprint"] = review_fingerprint(prediction)
    prediction = apply_typesafe_gate(prediction, review, now=now)
    if prediction["trade_signal"] != "TRADE":
        prediction["demo_quantity"] = 0
        prediction["demo_cost"] = 0.0
    blockers = list(dict.fromkeys(prediction.get("blockers") or []))
    prediction["blockers"] = blockers
    if blockers:
        prediction["trade_reason"] = "Wait: " + "; ".join(blockers)
    prediction["analysis"] = learning_analysis(state, prediction, now=now)
    market = prediction.get("kalshi") or {}
    opened = timestamp_seconds(market.get("open_time"))
    observed = timestamp_seconds(market.get("observed_at"))
    closed = timestamp_seconds(market.get("close_time"))
    deadlines = [v for v in (opened+MAX_ENTRY_DELAY_SECONDS if opened is not None else None,
                              observed+MAX_QUOTE_AGE_SECONDS if observed is not None else None,
                              closed-MIN_SECONDS_TO_CLOSE if closed is not None else None,
                              optional_number(prediction.get("forecast_entry_deadline"))) if v is not None]
    validation = prediction["analysis"]["official_validation"]
    collection_steps = {
        "awaiting_market_evidence": "Awaiting exact-market evidence; retry within the first 120 seconds. The displayed estimate is not a saved forecast.",
        "missed_window": "Entry window missed; wait for the next 15-minute window. No forecast was queued for this window.",
        "awaiting_settlement": "Wait for the previous exact ticker's official settlement before queuing another forecast.",
    }
    next_step = collection_steps.get(prediction.get("forecast_status")) or (
        f"Collect exact-market settlements: {validation['samples']}/{MIN_LIVE_SAMPLES}; then assess calibration and chronological, cost-aware results."
        if not validation["ready"] else "Use small paper trades only; walk-forward profitability is still unproven."
    )
    prediction["recommendation"] = {
        "action": prediction["trade_signal"], "scope": "paper-only", "market_ticker": market.get("ticker"),
        "stance": "RESEARCH / NOT VALIDATED" if not validation["ready"] else "PRELIMINARY PAPER STRATEGY",
        "blockers": blockers, "checks": prediction.get("signal_checks", {}),
        "valid_until": min(deadlines) if deadlines else now,
        "next_step": next_step,
        "cost_note": "Assumed allowance: 2c fees + 1c slippage per contract; verify actual schedule before use.",
        "settlement_note": "Official result of the queued Kalshi ticker; Coinbase candle direction is not its settlement.",
    }
    pending = state.get("pending") or {}
    prediction["paper_entry_created"] = False
    if pending.get("bar_timestamp") == prediction.get("bar_timestamp") and not pending.get("trade_committed_at"):
        # A filled paper entry is immutable; changing quotes cannot retroactively erase it.
        fields = ("demo_quantity", "demo_cost", "demo_entry_price", "demo_entry_source", "trade_signal")
        pending.update({key: prediction.get(key) for key in fields})
        pending["signal_reason"] = prediction.get("trade_reason")
        pending["typesafe_review"] = prediction.get("typesafe_review")
        if prediction["trade_signal"] == "TRADE":
            pending["trade_committed_at"] = now
            prediction["paper_entry_created"] = True
            pending["entry_market_snapshot"] = dict(market)
            pending["entry_signal_checks"] = dict(prediction.get("signal_checks") or {})
            pending["entry_review_fingerprint"] = prediction["review_input_fingerprint"]
    prediction["open_paper_trade"] = bool(pending.get("trade_committed_at"))
    state["last_recommendation"] = {"action": prediction["trade_signal"], "bar_timestamp": prediction.get("bar_timestamp"),
                                     "market_ticker": market.get("ticker"), "decided_at": now, "blockers": blockers,
                                     "forecast_status": prediction.get("forecast_status"),
                                     "forecast_issued_at": prediction.get("forecast_issued_at")}
    return prediction


def _load_bars(args: argparse.Namespace) -> list[WindowBar]:
    raw = fetch_candles(
        product_id=args.product_id,
        lookback_minutes=args.lookback_minutes,
    )
    args._prices_observed_at = time.time()
    bars = aggregate_candles(raw)
    args._archivable_candles = raw
    if len(bars) < 22:
        raise RuntimeError(
            f"only {len(bars)} complete 15-minute bars were fetched; "
            "increase lookback_minutes or retry later"
        )
    return bars


def command_train(args: argparse.Namespace) -> int:
    bars = _load_bars(args)
    model, state, metrics = bootstrap_from_bars(bars, epochs=args.epochs)
    archive_and_guard(args.state_file, state, bars, raw_candles=getattr(args, "_archivable_candles", None),
                      product_id=args.product_id, prices_observed_at=getattr(args, "_prices_observed_at", None))
    save_state(args.state_file, model, state)
    print(
        json.dumps(
            {
                "status": "trained",
                "bars": len(bars),
                "state_file": str(args.state_file),
                **metrics,
            },
            indent=2,
        )
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    bars = _load_bars(args)
    state_path = Path(args.state_file)
    kalshi_market = None
    kalshi_error = None
    try:
        kalshi_market = fetch_kalshi_market(args.kalshi_series)
    except RuntimeError as exc:
        # BTC prediction remains available if Kalshi's public endpoint is down.
        kalshi_error = str(exc)

    with state_transaction(state_path):
        if state_path.exists():
            model, state = load_state(state_path)
            learned = learn_from_pending(model, state, bars)
            bootstrapped = False
        else:
            model, state, metrics = bootstrap_from_bars(bars, epochs=args.epochs)
            learned = None
            bootstrapped = True
            state["bootstrap_metrics"] = metrics

        state["typesafe_required"] = bool(state.get("typesafe_required") or (state.get("typesafe_review") or {}).get("enabled") or os.environ.get(TYPESAFE_API_KEY_ENV))
        prediction = predict_and_queue(model, state, bars, kalshi_market)
        review = typesafe_strategy_review(prediction, state)
        state["typesafe_review"] = review
        prediction = finalize_prediction(prediction, state, review)
        if kalshi_error:
            prediction["kalshi_error"] = kalshi_error
        prediction = archive_and_guard(state_path, state, bars, prediction,
                                       raw_candles=getattr(args, "_archivable_candles", None),
                                       product_id=getattr(args, "product_id", DEFAULT_PRODUCT),
                                       prices_observed_at=getattr(args, "_prices_observed_at", None))
        save_state(state_path, model, state)
        result = {
            "status": "predicted",
            "bootstrapped": bootstrapped,
            "learned_previous": learned,
            "prediction": prediction,
            "paper": paper_trade_summary(state),
            "demo_account": demo_account_summary(state),
            "state_file": str(state_path),
        }
    print(json.dumps(result, indent=2))
    return 0


def _add_common_options(parser: argparse.ArgumentParser, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument("--product-id", default=default or DEFAULT_PRODUCT)
    parser.add_argument("--kalshi-series", default=default or DEFAULT_KALSHI_SERIES)
    parser.add_argument("--lookback-minutes", type=int, default=default or DEFAULT_LOOKBACK_MINUTES)
    parser.add_argument("--state-file", default=default or "data/btc_15m_state.json")
    parser.add_argument("--epochs", type=int, default=default or 5)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    _add_common_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)
    train_parser = subparsers.add_parser(
        "train", help="train a fresh model from public candle history"
    )
    run_parser = subparsers.add_parser(
        "run", help="learn from the last queued prediction and make the next prediction"
    )
    # Accept options either before or after the subcommand. Suppressed defaults
    # prevent the subparser from overwriting values supplied before it.
    _add_common_options(train_parser, suppress_defaults=True)
    _add_common_options(run_parser, suppress_defaults=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "train":
            return command_train(args)
        if args.command == "run":
            return command_run(args)
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}")
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

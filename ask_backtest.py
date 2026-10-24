"""Paper backtest of the strike formula against frozen issue-time asks.

Run with Python 3.11+: python ask_backtest.py ARCHIVE > REPORT.json
One paper contract per forecast, on the side with the larger net edge, at the
frozen ask plus a 3c cost allowance. Row selection mirrors
``validation.compare_market_models``. No model state, live decisions, or
archive rows are written.
"""
from __future__ import annotations

import argparse
import bisect
import json
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from validation import (
    MAX_CANDLE_AGE_SECONDS, _forecast, _issue_price, _market_midpoint, _normal_cdf,
    _number, _official_result, _read_archive, strike_distance,
)

COST = .03
THRESHOLDS = (0, .02, .05, .10, .15)
MIN_DEPTH = 25


def paper_trade(probability: float, yes_ask: float, no_ask: float, label: int) -> dict:
    """Buy the side with the larger edge after cost; return its edge and net P&L per contract."""
    edges = {'yes': probability - yes_ask - COST, 'no': (1 - probability) - no_ask - COST}
    side = max(edges, key=edges.get)
    won = label == (side == 'yes')
    return {'side': side, 'edge': edges[side], 'won': won,
            'pnl': won - (yes_ask if side == 'yes' else no_ask) - COST}


def _day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


def collect_trades(path: Path) -> list[dict]:
    rows, _ = _read_archive(path)
    settlements = {row['forecast_id']: row for row in rows.get('settlements', [])}
    records = []
    for row in rows.get('forecasts', []):
        forecast, _ = _forecast(row)
        if forecast is None:
            continue
        forecast['settlement'], _ = _official_result(forecast, settlements.get(forecast['forecast_id']))
        forecast['market_midpoint'] = _market_midpoint(forecast)
        records.append(forecast)
    records.sort(key=lambda r: (r['issued_at'], r['forecast_id']))
    candles = sorted((float(c['timestamp']), float(c['close']), float(c['observed_at']))
                     for c in rows.get('candles', []) if c['product_id'] == 'BTC-USD')
    starts = [c[0] for c in candles]
    seen, trades = set(), []
    for record in records:
        if record['market_ticker'] in seen:
            continue  # earliest issuance per ticker, as in compare-market
        seen.add(record['market_ticker'])
        if record['features'] is None or record['market_midpoint'] is None or record['settlement'] is None:
            continue
        low = bisect.bisect_left(starts, record['issued_at'] - 60 - MAX_CANDLE_AGE_SECONDS)
        distance = strike_distance(record, _issue_price(candles[low:], record['issued_at']))
        snapshot = record['snapshot']
        yes_ask, no_ask = _number(snapshot.get('yes_ask')), _number(snapshot.get('no_ask'))
        if distance is None or yes_ask is None or no_ask is None or not (0 < yes_ask < 1 and 0 < no_ask < 1):
            continue
        trade = paper_trade(_normal_cdf(distance), yes_ask, no_ask, record['settlement']['label'])
        trade.update(day=_day(record['issued_at']), spread=yes_ask + no_ask - 1,
                     depth=_number(snapshot.get(f"{trade['side']}_ask_size")) or 0)
        trades.append(trade)
    return trades


def summarize(trades: list[dict]) -> dict:
    days = defaultdict(list)
    for trade in trades:
        days[trade['day']].append(trade['pnl'])
    total = sum(trade['pnl'] for trade in trades)
    interval = None
    if len(days) >= 5:
        rng, clusters, samples = random.Random(20261024), list(days.values()), []
        for _ in range(2000):
            draw = rng.choices(clusters, k=len(clusters))
            samples.append(sum(map(sum, draw)) / sum(map(len, draw)))
        samples.sort()
        interval = [samples[49], samples[1949]]
    return {
        'trades': len(trades),
        'win_rate': sum(trade['won'] for trade in trades) / len(trades) if trades else None,
        'net_pnl': total, 'pnl_per_trade': total / len(trades) if trades else None,
        'pnl_per_trade_interval_95': interval,
        'days_up': sum(sum(values) > 0 for values in days.values()),
        'days_down': sum(sum(values) < 0 for values in days.values()),
    }


def backtest(path: Path) -> dict:
    trades = collect_trades(path)
    days = sorted({trade['day'] for trade in trades})
    split = days[len(days) // 2] if days else None
    edged = [trade for trade in trades if trade['edge'] >= 0]
    return {
        'forecasts': len(trades), 'day_count': len(days),
        'median_spread': sorted(t['spread'] for t in trades)[len(trades) // 2] if trades else None,
        'by_edge': {f'{threshold:.2f}': summarize([t for t in trades if t['edge'] >= threshold])
                    for threshold in THRESHOLDS},
        f'by_edge_depth_{MIN_DEPTH}': {f'{threshold:.2f}': summarize(
            [t for t in trades if t['edge'] >= threshold and t['depth'] >= MIN_DEPTH]) for threshold in THRESHOLDS},
        'stability_edge_0': {'split_day': split,
                             'first_half': summarize([t for t in edged if t['day'] < split]),
                             'second_half': summarize([t for t in edged if t['day'] >= split])},
        'method': ('Strike formula Phi(strike distance) vs frozen fresh asks; one contract per forecast; '
                   f'{COST:.2f} cost; 2000 UTC-day cluster bootstrap draws, seed 20261024; descriptive only'),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    print(json.dumps(backtest(parser.parse_args().archive), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()

"""Offline probability comparisons on a frozen, verified archive snapshot.

Run with Python 3.11+: python brier_experiment.py ARCHIVE > REPORT.json
No model state, live decisions, or archive rows are written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from btc_predictor import OnlineLogisticRegression
from validation import (
    _fit_platt, _forecast, _logit, _market_midpoint, _metrics,
    _official_result, _read_archive,
)


def _fit_weight(model: list[float], baseline: list[float], labels: list[int]) -> float:
    """Least-squares weight on model in baseline + w * (model - baseline)."""
    denominator = sum((p - b) ** 2 for p, b in zip(model, baseline))
    if denominator == 0:
        return 0.0
    numerator = sum((p - b) * (y - b) for p, b, y in zip(model, baseline, labels))
    return min(1.0, max(0.0, numerator / denominator))


def _day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


def _paired(predictions: list[dict], candidate: str, baseline: str) -> dict:
    days = defaultdict(list)
    for row in predictions:
        if row['label'] is not None:
            p, y = row['probabilities'], row['label']
            days[_day(row['issued_at'])].append((p[candidate] - y) ** 2 - (p[baseline] - y) ** 2)
    clusters = [(sum(values), len(values)) for _, values in sorted(days.items())]
    count = sum(n for _, n in clusters)
    interval = None
    if len(clusters) >= 5:
        rng, samples = random.Random(20261021), []
        for _ in range(2000):
            draw = rng.choices(clusters, k=len(clusters))
            samples.append(sum(s for s, _ in draw) / sum(n for _, n in draw))
        samples.sort()
        interval = [samples[49], samples[1949]]
    return {
        'count': count, 'day_count': len(clusters),
        'mean_brier_difference': sum(s for s, _ in clusters) / count if count else None,
        'interval_95': interval,
        'days_better': sum(s < 0 for s, _ in clusters),
        'days_worse': sum(s > 0 for s, _ in clusters),
        'method': 'paired UTC-day cluster bootstrap; 2000 draws; seed 20261021; descriptive only',
    }


def _panel(records: list[dict], *, quotes: bool) -> dict:
    """Consume canonical validated records; select test blocks before outcomes."""
    usable = [r for r in records if not quotes or r['market_midpoint'] is not None]
    names = ['constant_50', 'frozen_model', 'historical_frequency', 'shrinkage', 'platt']
    if quotes:
        names += ['market_midpoint', 'model_market_blend']
    predictions, folds, cursor = [], [], 0
    while cursor < len(usable):
        start = usable[cursor]['issued_at']
        prior = [r for r in usable[:cursor] if r['issued_at'] < start
                 and r['settlement'] is not None and r['settlement']['available_at'] < start]
        if len(prior) < 100:
            cursor += 1
            continue
        history, testing = prior[-100:], usable[cursor:cursor + 25]
        model = [r['probability_up'] for r in history]
        labels = [r['settlement']['label'] for r in history]
        frequency = sum(labels) / len(labels)
        shrink = _fit_weight(model, [.5] * len(history), labels)
        fit = _fit_platt(model, labels)
        blend = _fit_weight(model, [r['market_midpoint'] for r in history], labels) if quotes else None
        folds.append({
            'block_start': start, 'test_count': len(testing),
            'history_forecast_ids': [r['forecast_id'] for r in history],
            'latest_label_available_at': max(r['settlement']['available_at'] for r in history),
            'historical_frequency': frequency, 'shrinkage_weight': shrink,
            'model_market_weight': blend, 'platt_fit': fit,
        })
        for r in testing:
            raw = r['probability_up']
            probabilities = {
                'constant_50': .5, 'frozen_model': raw, 'historical_frequency': frequency,
                'shrinkage': .5 + shrink * (raw - .5),
                'platt': OnlineLogisticRegression._sigmoid(fit['slope'] * _logit(raw) + fit['intercept']),
            }
            if quotes:
                market = r['market_midpoint']
                probabilities.update(market_midpoint=market, model_market_blend=market + blend * (raw - market))
            predictions.append({
                'forecast_id': r['forecast_id'], 'market_ticker': r['market_ticker'],
                'issued_at': r['issued_at'], 'probabilities': probabilities,
                'label': r['settlement']['label'] if r['settlement'] else None,
                'available_at': r['settlement']['available_at'] if r['settlement'] else None,
            })
        cursor += len(testing)
    scored = [r for r in predictions if r['label'] is not None]
    labels = [r['label'] for r in scored]
    metrics = {name: _metrics([r['probabilities'][name] for r in scored], labels) for name in names}
    daily = defaultdict(list)
    for r in scored:
        daily[_day(r['issued_at'])].append(r)
    return {
        'status': 'descriptive_evaluation' if scored else 'insufficient_data',
        'eligible_count': len(usable), 'test_count': len(predictions), 'scored_count': len(scored),
        'unscored_count': len(predictions) - len(scored), 'metrics': metrics,
        'paired': {baseline: {name: _paired(predictions, name, baseline) for name in names if name != baseline}
                   for baseline in (['constant_50', 'market_midpoint'] if quotes else ['constant_50'])},
        'daily': {day: {'count': len(rows), 'brier': {
            name: sum((r['probabilities'][name] - r['label']) ** 2 for r in rows) / len(rows)
            for name in names}} for day, rows in sorted(daily.items())},
        'folds': folds, 'predictions': predictions,
    }


def compare_archive(path: Path) -> dict:
    """Reuse canonical validation rules without modifying the archive."""
    path = Path(path).resolve(strict=True)
    wal = Path(str(path) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('Use a frozen backup snapshot, not a live WAL archive')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    rows, missing = _read_archive(path)
    settlements = {r['forecast_id']: r for r in rows['settlements']}
    eligible = []
    for row in rows['forecasts']:
        record, _ = _forecast(row)
        if record is not None:
            record['settlement'], _ = _official_result(record, settlements.get(record['forecast_id']))
            record['market_midpoint'] = _market_midpoint(record)
            eligible.append(record)
    eligible.sort(key=lambda r: (r['issued_at'], r['forecast_id']))
    canonical, seen = [], set()
    for record in eligible:
        if record['market_ticker'] not in seen:
            canonical.append(record)
            seen.add(record['market_ticker'])
    panels = {'all': _panel(canonical, quotes=False), 'quoted': _panel(canonical, quotes=True)}
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest or (wal.exists() and wal.stat().st_size):
        raise ValueError('Archive changed during comparison; use a frozen snapshot')
    return {
        'schema_version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(),
        'archive_sha256': digest, 'archive_path': str(path),
        'code_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                        [Path(__file__), Path(__file__).with_name('validation.py'),
                         Path(__file__).with_name('btc_predictor.py')]},
        'configuration': {'history_size': 100, 'test_size': 25, 'bootstrap_draws': 2000, 'seed': 20261021},
        'eligible_count': len(canonical), 'duplicate_tickers_excluded': len(eligible) - len(canonical),
        'schema_missing': missing, 'panels': panels,
        'limitations': [
            'Exploratory historical comparison, not an untouched holdout or a profitability test.',
            'Each panel has its own quote coverage, history and test blocks; compare candidates within a panel only.',
            'Adjustments fit past frozen model outputs, unlike the existing fresh-model calibration experiment.',
            '100 prior labels must be available strictly before a block; unresolved test rows remain unscored.',
            'Daily bootstrap intervals are descriptive, unadjusted for multiple comparisons; days may remain dependent.',
            'Issue-time midpoint is a probability benchmark, not an executable fill price.',
            'No live model, gate or service is changed. Prospective confirmation is required.',
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    try:
        report = compare_archive(args.archive)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'Comparison failed: {exc}\n')
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()

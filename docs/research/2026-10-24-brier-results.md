# Brier experiment results (2026-10-24)

**Question:** can a simple adjustment of the frozen live probabilities, fitted
only on past outcomes, beat a constant 50% forecast or the Kalshi midpoint?

**Answer:** no. The frozen model is reliably *worse* than 50%. No adjustment
fixes it, and the issue-time market midpoint beats everything on 16 of 17 days.

## Evidence

- Snapshot: Oracle backup `btc15m-backup-20261024T020419394335Z-18bde94f`,
  verified with `backup_state.py verify`. Archive SHA-256 `addebea7f5b19a16…`.
- Forecasts issued 2026-10-07 to 2026-10-24 UTC; 1,489 eligible distinct tickers.
- Command: `python3 brier_experiment.py ARCHIVE > report.json` (rules in the
  [design](../superpowers/specs/2026-10-21-brier-experiments-design.md)).
  The full JSON report stays local because it contains individual predictions.

## All eligible forecasts (1,361 scored, 18 days)

Paired Brier difference vs. constant 50%. Negative is better. Intervals use
2,000 paired bootstrap draws resampling whole UTC days.

| Candidate | Brier | Difference vs. 50% | 95% interval | Days better / worse |
|---|---|---|---|---|
| Constant 50% | 0.2500 | — | — | — |
| Shrinkage toward 50% | 0.2510 | +0.0010 | [+0.0002, +0.0021] | 1 / 6 |
| Historical YES frequency | 0.2535 | +0.0035 | [+0.0015, +0.0059] | 2 / 16 |
| Frozen model | 0.2539 | +0.0039 | [+0.0016, +0.0060] | 3 / 15 |
| Platt calibration | 0.2544 | +0.0044 | [+0.0020, +0.0075] | 2 / 16 |

The fitted shrinkage weight is zero in 45 of 55 folds (mean 0.12): the best
past-only fit mostly ignores the model.

## Forecasts with a fresh issue-time quote (1,287 scored, 17 days)

| Candidate | Brier | Difference vs. midpoint | 95% interval | Days better / worse |
|---|---|---|---|---|
| Market midpoint | 0.2322 | — | — | — |
| Model/market blend | 0.2329 | +0.0007 | [−0.0001, +0.0017] | 1 / 6 |
| Constant 50% | 0.2500 | +0.0178 | [+0.0116, +0.0234] | 1 / 16 |
| Frozen model | 0.2539 | +0.0217 | [+0.0151, +0.0271] | 0 / 17 |

The midpoint beats 50% by −0.0178 [−0.0234, −0.0117] and wins on 16 of 17 days.
Midpoint accuracy is 60.3% (Wilson 95% 57.6–62.9%). The blend's fitted model
weight is zero in 40 of 52 folds (mean 0.06).

## Interpretation

- These results agree with `validation.py compare-market` (fresh-model folds): the
  seven candle features carry no usable 15-minute signal, and the market prices
  in information the model lacks.
- They support the market-midpoint trade gate (PR #7). They also suggest the
  candle model should not drive live recommendations at all.
- No candidate earns prospective confirmation, so no frozen candidate or live
  change is proposed. Any new model should first beat the midpoint here.

## Limitations

- Exploratory historical comparison, not an untouched holdout or a profitability test.
- Day clusters may still be dependent; intervals are not adjusted for multiple comparisons.
- 18 days is short. Regimes may change.
- The midpoint is a probability benchmark, not an executable price.

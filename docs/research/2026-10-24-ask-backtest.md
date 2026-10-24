# Strike formula vs frozen asks: paper backtest (2026-10-24)

**Question:** the untrained strike formula beats a constant 50% forecast
([compare-market fit results](2026-10-24-compare-market-fit.md)). Does it make
money against the asks actually frozen at issue time, after the 3¢ cost allowance?

**Answer:** no. It loses at every edge threshold. The larger the formula's
claimed edge, the lower its win rate: when it disagrees strongly with Kalshi,
Kalshi is usually right.

## Evidence

- Snapshot: Oracle backup `btc15m-backup-20261024T020419394335Z-18bde94f`,
  verified with `backup_state.py verify`. Archive SHA-256 `addebea7f5b19a16…`.
- Command: `python3 ask_backtest.py ARCHIVE > report.json`. The JSON stays local.
- 1,349 forecasts over 21 UTC days with frozen features, a fresh issue-time
  quote, a strike distance and an official finalized result. Earliest issuance
  per ticker, as in `compare-market`. No training, so no fold history is needed.
- Rule: compute Phi(strike distance); buy one paper contract of the side with
  the larger edge, where edge = probability − frozen ask − 3¢. Settle on the
  official result. Median spread (YES ask + NO ask − 1) is 1¢.
- Intervals: 2,000 bootstrap draws resampling whole UTC days, seed 20261024.

## Results (per contract, net of the 3¢ allowance)

| Edge ≥ | Trades | Win rate | Net | Per trade | 95% interval | Days up / down |
|---|---|---|---|---|---|---|
| 0¢ | 960 | 40.3% | −$35.96 | −3.75¢ | [−5.90, −1.34]¢ | 4 / 17 |
| 2¢ | 792 | 39.0% | −$30.08 | −3.80¢ | [−6.29, −1.39]¢ | 5 / 16 |
| 5¢ | 548 | 34.3% | −$35.06 | −6.40¢ | [−11.28, −1.87]¢ | 7 / 14 |
| 10¢ | 276 | 33.0% | −$11.35 | −4.11¢ | [−10.11, +1.79]¢ | 9 / 10 |
| 15¢ | 129 | 27.1% | −$9.56 | −7.41¢ | [−14.65, +0.28]¢ | 5 / 14 |

Requiring at least 25 contracts of ask depth removes four trades and changes
nothing material.

Edge ≥ 0¢, split at 2026-10-14:

| Period | Trades | Per trade | 95% interval | Days up / down |
|---|---|---|---|---|
| Before | 292 | −0.82¢ | [−4.84, +4.38]¢ | 3 / 7 |
| From | 668 | −5.02¢ | [−7.36, −2.79]¢ | 1 / 10 |

## Interpretation

- Costs are not the problem. Adding the 3¢ allowance back to the same 960
  edge ≥ 0¢ trades still leaves −0.75¢ per contract, and the spread is only 1¢.
  The losses come from choosing the wrong side.
- Win rate falls from 40% to 27% as claimed edge rises. Disagreement with the
  market is adverse selection: the market prices information the formula lacks.
- This closes out the issue-time inputs the project collects (candles, strike
  distance, frozen quotes). An edge would need new information, such as
  late-window settlement-index pricing or options-implied volatility.
- No live change is proposed.

## Limitations

- Exploratory historical comparison, not an untouched holdout or a profitability test.
- Thresholds were not pre-registered, and no correction is made across them.
- Frozen asks are top-of-book at issue time; queue position and fills are not modeled.
- 21 days is short. Regimes may change.

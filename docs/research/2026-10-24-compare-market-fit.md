# compare-market fit results (2026-10-24)

**Question:** did five SGD epochs under-fit the `compare-market` models, and does
an untrained strike/volatility formula beat the constant 50% forecast or the
Kalshi midpoint?

**Answer:** under-fitting mattered on a small sample but not at scale. On 18 days
the converged fit barely changes the market-fed models, and the midpoint still
beats every candidate. The strike formula beats 50%, but only slightly.

## Evidence

- Snapshot: Oracle backup `btc15m-backup-20261024T020419394335Z-18bde94f`
  (the same one as the [Brier results](2026-10-24-brier-results.md)), verified
  with `backup_state.py verify`. Archive SHA-256 `addebea7f5b19a16…`.
- Command: `python3 validation.py compare-market --archive-file ARCHIVE`, run with
  `main` (5 SGD epochs) and with the Newton/IRLS fit (L2 penalty 0.001).
- 1,225 scored forecasts in 49 folds of 25, each trained on the latest 100
  labels known before the fold.

## Results (Brier; negative difference is better)

| Candidate | SGD fit | Newton fit | Newton minus midpoint | Accuracy |
|---|---|---|---|---|
| Market midpoint | 0.2316 | 0.2316 | — | 60.4% |
| `market_only` | 0.2374 | 0.2372 | +0.0056 | 58.8% |
| `market_plus_strike` | 0.2374 | 0.2392 | +0.0076 | 57.6% |
| `strike_formula` (untrained) | — | 0.2461 | +0.0145 | 56.2% |
| Constant 50% | 0.2500 | 0.2500 | +0.0184 | 49.6% |
| `strike_only` | 0.2523 | 0.2519 | +0.0204 | 53.0% |
| `candles_plus_market` | 0.2417 | 0.2535 | +0.0219 | 58.3% |
| `candles_only` | 0.2594 | 0.2740 | +0.0425 | 50.8% |

On the earlier local Oct 12 backup (175 scored), the converged fit had moved
`market_only` from 0.2361 to 0.2311 and `strike_only` from 0.2500 to 0.2469.
That gain does not survive the larger sample.

## Interpretation

- The gap between `market_only` and the raw midpoint is fitting noise, not
  under-fitting. Its fitted midpoint weight ranges 0.45–2.09 across folds
  (mean 1.19; the ideal is about 1). 100 training rows are too few.
- SGD's early stopping acted as shrinkage. Fit fully, the candle models get
  worse, which again suggests the seven candle features carry no usable
  15-minute signal.
- The untrained `strike_formula`, Phi(log(price / strike) / (σ√τ)), is the only
  candidate without market input that beats 50%. It still loses to the midpoint
  by 0.0145, so it is not a trading signal.
- No live change is proposed. A new model should first beat the midpoint here.

## Limitations

- Exploratory historical comparison, not an untouched holdout or a profitability test.
- No intervals are reported here. Fold Brier scores are not independent days.
- 18 days is short. Regimes may change.
- The midpoint is a probability benchmark, not an executable price.

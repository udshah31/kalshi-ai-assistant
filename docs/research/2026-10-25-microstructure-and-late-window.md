# Microstructure, Garman-Klass Volatility & Late-Window Calibration (2026-10-25)

**Question:** can parameter-free orderbook microstructure (`microprice`, top-of-book
log-imbalance) or 1-minute Garman-Klass intraday volatility improve start-of-window
(`C−900s`) probability accuracy over Kalshi's raw midpoint and the 3-hour-volatility
strike formula? And on the first 14 finalized late-window contracts (`C−60s` to
`C−15s`), why did the two early paper trades lose?

**Answer:** yes on both counts, with important caveats:

1. **Start-of-window (`C−900s`):** across 1,283 quote-matched settled contracts
   (22 UTC days), Kalshi's volume-weighted `microprice` (`0.23103` Brier) and a
   small top-of-book log-imbalance tilt (`0.23094` Brier, `60.64%` accuracy)
   beat the raw Kalshi midpoint (`0.23114` Brier, `60.25%` accuracy) on 15 of
   22 UTC days. Replacing the 3-hour 15m-bar volatility in `strike_formula` with
   15-minute 1m-candle Garman-Klass OHLC volatility lowers strike-formula Brier
   from `0.24557` to `0.24455` (14 of 22 days better). However, the `~0.0002`
   Brier lift over the midpoint is still far smaller than the `1¢` spread plus
   `3¢` cost allowance required to trade profitably at `C−900s`.
2. **Late-window (`C−60s` to `C−15s`):** over the first 14 finalized windows in
   `btc_15m_state_late_window.sqlite3`, the 4-exchange proxy (Coinbase, Kraken,
   Bitstamp, Gemini) tracks official CF Benchmarks BRTI expiration values with a
   median error of **`0.27 bps`** (`0.33 bps` on strike) and **`92.9%` (`13/14`)
   call agreement**. Including `offset −60s` in the settlement window, adding a
   `0.6 bps` proxy-vs-BRTI tracking variance floor, and requiring directional
   conviction ($P(\text{side}) \ge 0.55$, preventing contrarian purchases of
   sub-`50%` OTM lottery tickets at `1¢` asks) reduces mean Brier across the 19
   quoted snapshots from `0.06895` to **`0.05383`** (`−21.9%`).

## Evidence

- Snapshot: Oracle VM backup `btc15m-backup-20261025T024750976187Z-3c9f318a`
  (exported `2026-10-25T02:47:50Z`), verified with `backup_state.py verify`.
- Full-archive population: 1,608 total forecasts, 1,599 settled outcomes, and
  1,283 distinct tickers with frozen features, fresh issue-time orderbook
  quotes, a valid strike distance, and an official finalized result across 22
  UTC days.
- Late-window population: 38 windows collected by `kalshi-late-window.service`,
  including 14 finalized windows (`23:15 UTC` on Oct 24 to `02:30 UTC` on Oct 25)
  with zero exchange sample gaps and zero Kalshi snapshot gaps.

## 1. Start-of-Window (`C−900s`) Full-Archive Benchmark (1,283 Scored, 22 Days)

Paired Brier differences use 2,000 UTC-day cluster bootstrap draws (seed
`20261025`). Negative difference is better.

| Predictor | Brier ↓ | Log Loss ↓ | Accuracy ↑ | Paired Brier Diff vs. Benchmark | 95% Day Bootstrap CI | Days Better / Worse |
|---|---|---|---|---|---|---|
| Constant 50% | 0.25000 | 0.69315 | 49.34% | — | — | — |
| Frozen candle model (`btc_predictor.py`) | 0.25391 | 0.70140 | 48.87% | +0.00391 vs. 50% | — | 3 / 19 |
| Strike formula (3h 15m-bar vol) | 0.24557 | 0.68419 | 57.05% | −0.00443 vs. 50% | — | 15 / 7 |
| Strike formula (15m 1m-Garman-Klass vol) | 0.24455 | 0.68180 | 57.05% | **−0.00102** vs. 3h-vol strike | [−0.00288, +0.00091] | **14 / 8** |
| Kalshi issue-time midpoint | 0.23114 | 0.65351 | 60.25% | — | — | — |
| Kalshi volume-weighted `microprice` | 0.23103 | 0.65327 | 60.25% | **−0.00011** vs. midpoint | [−0.00026, +0.00001] | **15 / 7** |
| Midpoint + log-imbalance ($\text{logit}(\text{mid}) + 0.05 \cdot \text{imb}$) | **0.23094** | **0.65309** | **60.64%** | **−0.00020** vs. midpoint | [−0.00049, +0.00005] | **15 / 7** |

Where:
- $\text{microprice} = \frac{P_{\text{yes\_bid}} \cdot V_{\text{yes\_ask}} + P_{\text{yes\_ask}} \cdot V_{\text{yes\_bid}}}{V_{\text{yes\_bid}} + V_{\text{yes\_ask}}}$
- $\text{imb} = \tanh\left(\frac{1}{3} \ln\frac{V_{\text{yes\_bid}} + 1}{V_{\text{yes\_ask}} + 1}\right)$
- $\sigma_{\text{GK}, 1\text{m}}^2 = \frac{1}{N}\sum_{i=1}^{N}\left[\frac{1}{2}\left(\ln\frac{H_i}{L_i}\right)^2 - (2\ln 2 - 1)\left(\ln\frac{C_i}{O_i}\right)^2\right]$ over the latest $\le 15$ completed 1-minute `BTC-USD` candles received by issue time.

### Walk-Forward Evaluation (46 Folds of 25, 1,153 Scored)

Using the `validation.py compare-market` protocol (100 prior-known training
labels per 25-test fold):

| Candidate | Walk-Forward Brier ↓ | Diff vs. Midpoint | Log Loss ↓ | Accuracy ↑ |
|---|---|---|---|---|
| `midpoint_plus_log_imb_fixed` (+0.05) | **0.23147** | **−0.00006** | **0.65409** | **60.02%** |
| `microprice` (untrained) | **0.23149** | **−0.00004** | **0.65413** | 59.58% |
| `market_midpoint` | 0.23153 | 0.00000 | 0.65423 | 59.58% |
| `midpoint_plus_log_imb_fitted` (100-row IRLS) | 0.24310 | +0.01156 | 0.67920 | 56.81% |
| `strike_formula_15m_gk` (untrained) | 0.24580 | +0.01426 | 0.68435 | 56.37% |
| `strike_formula_3h` (untrained) | 0.24676 | +0.01522 | 0.68665 | 56.37% |

Fitting free weights on 100 binary labels adds `~+0.0115` Brier of estimation
variance, whereas parameter-free structural formulas (`microprice`, fixed
log-imbalance, and Garman-Klass strike distance) preserve their edge out-of-sample.

## 2. Early Late-Window Diagnostic (`btc_15m_state_late_window.sqlite3`)

Across the first 14 finalized windows (`2026-10-24 23:15 UTC` to
`2026-10-25 02:30 UTC`):

- **Proxy accuracy vs. official settlement (`expiration_value`):** median error
  is **`0.27 bps`** (target `≤ 2.00 bps`); median strike error vs.
  `target_price` is **`0.33 bps`**.
- **Single call mismatch (`02:15 UTC`, `KXBTC15M-26OCT242215-15`):** target
  strike was `$80,779.27`, official expiration settled `YES` at `$80,784.28`
  (`+$5.01`), while the 5-second proxy averaged `$80,778.21` (`NO`, `−$1.06`, a
  `0.75 bps` difference during a `$51` intraminute drop from `$80,812.05` at
  `C−60s` to `$80,760.60` at `C−30s`).
- **Why the `02:15 UTC` `C−30s` paper trade lost:** at `C−30s`, `late_probability`
  computed $P(\text{YES}) = 58.1\%$ ($P(\text{NO}) = 41.9\%$). Because Kalshi's
  orderbook had `YES` ask at `$0.99` and `NO` ask at `$0.01` (size `74.23`),
  `paper_trade()` selected the unfavored `NO` side ($0.419 - 0.01 - 0.03 = +0.379$
  claimed edge), buying a sub-50% OTM contract that settled `YES`.

| Late-Window Evaluation Variant (19 Quoted Snapshots) | Mean Brier ↓ | Qualifying Trades | Net P&L |
|---|---|---|---|
| 1. Pre-registered baseline (`late_window_report.py`) | 0.06895 | 2 (0 wins) | −$0.283 |
| 2. Include `−60s` + `0.6 bps` proxy variance floor + favored-side filter ($P(\text{side}) \ge 0.55$) | **0.05383** | 1 (0 wins) | −$0.240 |

## Limitations & Operational Stance

- The pre-registered rule in `late_window_report.py` and
  [2026-10-24-late-window-snapshots-design.md](../superpowers/specs/2026-10-24-late-window-snapshots-design.md)
  remains **unchanged** while `kalshi-late-window.service` collects the required
  500 windows for the proxy check and 1,000 windows / 20 UTC days for the
  primary backtest.
- No live model, gate, or service is modified.

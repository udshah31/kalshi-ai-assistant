# Late-window snapshot collection: design (2026-10-24)

## Intent

Every test on issue-time data is negative. On the 18-day snapshot the strike
formula loses money against frozen asks at every edge size, and Kalshi's
midpoint beats every model ([ask backtest](../../research/2026-10-24-ask-backtest.md),
[compare-market fit](../../research/2026-10-24-compare-market-fit.md)).

The one structural opening left is the end of each window. `KXBTC15M` settles
YES when the average of CF Benchmarks' BRTI over the **60 seconds before close**
is at least the average over the 60 seconds before open (Kalshi rules, confirmed
on Coinbase Predict, which offers the same `KXBTC15M` markets through KalshiEX).
Part of the result is therefore already fixed during the final minute. The system
forecasts only at the open, so it has never seen this period.

This project collects paper evidence of late-window prices and quotes, so that a
pre-registered backtest can answer one question: **after costs, are late-window
asks mispriced relative to what public price data already shows?**

## Goals

1. Each window, record public BTC prices from several exchanges at fixed times
   up to the close, and Kalshi orderbook snapshots at fixed times before close.
2. Measure how well a free multi-exchange proxy tracks the official values,
   using the `target_price` (strike average) and `expiration_value` (settlement
   average) that Kalshi already publishes on settled markets.
3. Pre-register one backtest rule before collection starts, then run it once on
   collected data with the `ask_backtest.py` method: frozen asks plus 3¢, UTC-day
   bootstrap intervals.

## Non-goals

- No order placement, no keys, no account access. Paper-only, like the rest of
  the project.
- No change to the existing open-time forecast, model, trade gate, demo account
  or dashboard.
- No live late-window recommendation. That would be a separate decision after the
  backtest.
- No licensed BRTI feed.

## Anti-goals (a failure even if it works)

- Any write path that can block, slow or corrupt the existing runner, state
  file or archive.
- Choosing rules or thresholds after looking at collected outcomes.
- Results that imply profitability without showing executable depth, timing
  and the cost allowance.
- Adding third-party Python dependencies.

## Constraints

- Python standard library only, Python 3.11+, the same as the project.
- Runs on the Oracle VM as a container-managed systemd service, deployed through
  the existing manual, approval-gated workflow.
- Public endpoints only: Kalshi trade API v2 (already used) and public ticker
  endpoints of BRTI constituent exchanges.
- Collection must survive restarts and missed windows, recording gaps rather
  than filling them.

## Decided how

### Separate collector process

A new `late_window.py` runs as its own service, `kalshi-late-window.service`,
with the same `kalshi` user, container image and restart policy as the runner.
It does not run inside `live_runner.py`: the runner polls every 30 seconds and
runs predictions as subprocesses, which is too coarse for a 60-second average
and would couple failures. *(My call.)*

### What is sampled, and when

Times are relative to each window's close `C` (every 15 minutes, UTC).

- **Exchange prices:** every 5 seconds from `C−600 s` to `C` at every boundary.
  Each boundary is one window's close and the next window's open, so the last
  60 seconds give both that window's settlement average and the next window's
  strike average. The earlier samples feed the volatility estimate. Each sample
  records the exchange, its best bid/ask or last trade as published, the
  exchange's own timestamp, and local receipt time. Exchanges: Coinbase,
  Kraken, Bitstamp and Gemini. These are public BRTI constituents with keyless
  REST tickers. *(My call; the builder confirms current constituents.)*
- **Kalshi snapshots:** the active ticker's orderbook at `C−300`, `C−120`,
  `C−60`, `C−30` and `C−15` seconds, using the existing `orderbook_quotes`
  normalization: YES/NO bid and ask, sizes, and observed time.
- **Official values:** once the market is finalized, its `target_price` and
  `expiration_value`, read through the same exact-ticker finalized path as the
  runner. A window counts only with a finalized result.

Samples are buffered in memory and written once per window after `C`, so the
collector writes about four times an hour.

### Storage

A separate SQLite file next to the state file,
`<state-stem>_late_window.sqlite3`, in WAL mode. Tables are append-only:
`windows`, `price_samples`, `quote_snapshots`, `official_values`. The schema has
a version row, as in `forecast_archive.py`. It is separate so that collector
bugs cannot touch the forecast archive. `backup_state.py` is extended to include
this file in verified snapshots when it exists. *(My call.)*

### Proxy check (descriptive, runs first)

`late_window_report.py` (read-only) compares the proxy (median across exchanges
of each 5-second mid, then the mean over the window's last 60 seconds) with
`target_price` and `expiration_value`. It reports error in basis points and
whether the proxy's above/below call matches the official result. It passes
when, over at least 500 finalized windows, the proxy's settlement call matches
the official result in ≥ 98% of windows and the median absolute error versus
`expiration_value` is ≤ 2 basis points. *(My call.)* If it fails, the backtest
is not run.

### Pre-registered backtest rule

Written into this document before collection starts and not changed afterwards:

- At each Kalshi snapshot time `t` in {`C−60`, `C−30`, `C−15`}, estimate the
  settlement average as the realized proxy average so far plus a random-walk
  forecast of the rest, with volatility from all 5-second proxy returns between
  `C−600 s` and `t`. `P(YES)` is the normal probability that the average finishes at
  or above the official `target_price`.
- Buy one paper contract on the larger-edge side when edge = probability − ask −
  3¢ ≥ 5¢ and the ask size is ≥ 25 contracts. Each snapshot time is reported
  separately; there is at most one trade per window per snapshot time.
- Success: mean net P&L per contract > 0 with the lower 95% UTC-day bootstrap
  bound > 0, at a pre-chosen snapshot time (`C−30`), over at least 20 UTC days
  and 1,000 windows. Anything else is a negative result.

## Left to the builder

- Exact endpoints and field mappings for each exchange, with per-exchange
  timeouts so one slow exchange cannot delay others.
- Scheduling within the loop: monotonic sleeps that align to wall-clock `C`
  boundaries, with drift recorded.
- Schema details, indexes and tests: buffering, gap recording and finalization
  matching, following the project's existing unittest style.
- systemd unit, container wiring, `deploy/package.sh` and CI bundle checks.
- README and deploy-guide sections.

## Calls I made (review these)

1. A separate service and SQLite file, rather than extending the runner and
   archive.
2. Four exchanges as the BRTI proxy, sampled every 5 seconds by REST polling,
   not websockets.
3. Kalshi snapshots at `C−300/−120/−60/−30/−15` seconds.
4. The pre-registered rule above: random-walk estimate, 5¢ edge, 25-contract
   depth, `C−30` as the primary time, 20 days and 1,000 windows minimum.
5. The backtest runs only if the proxy check passes: ≥ 98% agreement with
   official results and ≤ 2 bps median error, over ≥ 500 windows.
6. Prices are sampled from `C−600 s` (not `C−120 s`) so volatility uses data
   the collector itself recorded.

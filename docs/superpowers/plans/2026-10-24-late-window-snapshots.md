# Late-Window Snapshot Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect paper evidence of BTC prices and Kalshi quotes near each `KXBTC15M` close, check a free multi-exchange proxy against Kalshi's official values, and run one pre-registered late-window backtest.

**Architecture:** A new always-on collector, `late_window.py`, samples four exchanges every 5 seconds over the last 600 seconds before each 15-minute boundary, takes Kalshi orderbook snapshots at fixed offsets, and writes once per window to its own append-only SQLite file. A separate pass records official `target_price`/`expiration_value` once markets finalize. A read-only `late_window_report.py` runs the proxy check and then the pre-registered backtest, reusing `ask_backtest.paper_trade`.

**Tech Stack:** Python 3.11+ standard library only (`urllib`, `concurrent.futures`, `sqlite3`, `unittest`), SQLite WAL, systemd + the pinned container launcher.

**Spec:** `docs/superpowers/specs/2026-10-24-late-window-snapshots-design.md`

## Global Constraints

- Paper-only: no order placement, no API keys, no account access.
- No third-party Python dependencies; no change to the open-time forecast, model, trade gate, demo account or dashboard.
- Public endpoints only: Kalshi trade API v2 and keyless exchange tickers.
- Price samples every 5 s from `C−600 s` to `C` at every boundary `C` (every 900 s, UTC).
- Kalshi snapshots at `C−300`, `C−120`, `C−60`, `C−30`, `C−15` s.
- Settlement/strike proxy average = mean of the per-sample cross-exchange median mid over samples with time in `(C−60, C]` (12 samples).
- Separate file `<state-stem>_late_window.sqlite3`, WAL, append-only tables, written once per window after `C`.
- Gaps are recorded, never filled.
- Proxy check passes only with ≥ 500 finalized windows, ≥ 98% settlement-call agreement and ≤ 2 bps median absolute error vs `expiration_value`.
- Backtest rule (pre-registered, do not tune): edge ≥ 5¢ after 3¢ cost, ask size ≥ 25, snapshot times `C−60/−30/−15` reported separately, `C−30` primary; success needs mean net > 0 and lower 95% UTC-day bootstrap bound > 0 over ≥ 20 UTC days and ≥ 1,000 windows.

## Review Focus

- **One exchange hangs or returns garbage:** the other exchanges' samples still land on time; the bad one is a recorded gap. (Task 2 test `test_sample_survives_one_failing_exchange`.)
- **Collector restarts mid-window:** the partial window is still written with its gaps, and a window is never written twice. (Task 3 test `test_restart_midwindow_writes_partial_once`.)
- **The Kalshi snapshot returns a different market** (next window already active, or none): the snapshot is a gap, not mis-attributed. (Task 3 test `test_snapshot_for_wrong_close_is_a_gap`.)
- **Market never finalizes, or finalizes days later:** the window stays pending and is retried; provisional (`determined`) values are never stored as official. (Task 4 test `test_provisional_result_is_not_official`.)
- **Window with too few samples in the averaging period:** excluded from the proxy check and backtest, and counted as excluded, never imputed. (Task 5 test `test_incomplete_average_is_excluded_and_counted`.)

---

### Task 1: Late-window store

**Files:**
- Create: `late_window.py` (storage section)
- Test: `tests/test_late_window.py`

**Interfaces:**
- Produces:
  - `late_window_path(state_file: str | Path) -> Path`: `<stem>_late_window.sqlite3` next to the state file.
  - `connect_store(path: str | Path) -> sqlite3.Connection`: WAL, creates schema if missing, `store_metadata(key, value)` with `schema_version = '1'`.
  - Tables (append-only, no UPDATE/DELETE anywhere):
    - `windows(close_ts INTEGER PRIMARY KEY, ticker TEXT, started_at REAL, written_at REAL, sample_gaps INTEGER, snapshot_gaps INTEGER, max_drift_seconds REAL)`
    - `price_samples(close_ts, offset_s INTEGER, exchange TEXT, bid REAL, ask REAL, exchange_ts REAL, received_at REAL, error TEXT, PRIMARY KEY(close_ts, offset_s, exchange))`
    - `quote_snapshots(close_ts, offset_s INTEGER, ticker TEXT, yes_bid, yes_ask, yes_ask_size, no_bid, no_ask, no_ask_size REAL, observed_at REAL, error TEXT, PRIMARY KEY(close_ts, offset_s))`
    - `official_values(ticker TEXT PRIMARY KEY, close_ts INTEGER, target_price REAL, expiration_value REAL, result TEXT CHECK(result IN ('yes','no')), settled_at REAL, observed_at REAL)`
  - `write_window(connection, window: dict) -> bool`: one transaction inserting the `windows` row and all its samples/snapshots; returns `False` (and writes nothing) if `close_ts` already exists. `window` keys: `close_ts, ticker, started_at, max_drift_seconds, samples: list[dict], snapshots: list[dict]`; gap counts are computed from rows with `error`.
  - `record_official(connection, row: dict) -> bool`: `INSERT OR IGNORE`; returns whether a row was added.

- [ ] **Step 1: Write the failing tests**

```python
def test_store_path_and_schema(self):
    self.assertEqual(late_window.late_window_path("/x/btc_15m_state.json").name, "btc_15m_state_late_window.sqlite3")
    with closing(late_window.connect_store(self.path)) as db:
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(db.execute("SELECT value FROM store_metadata WHERE key='schema_version'").fetchone()[0], "1")

def test_window_is_written_once_with_gap_counts(self):
    window = make_window(close_ts=1_800_000_900, failing={("kraken", -300)}, snapshot_errors={-15})
    with closing(late_window.connect_store(self.path)) as db:
        self.assertTrue(late_window.write_window(db, window))
        self.assertFalse(late_window.write_window(db, window))
        row = db.execute("SELECT sample_gaps, snapshot_gaps FROM windows").fetchone()
        self.assertEqual(row, (1, 1))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM price_samples").fetchone()[0], 121 * 4)

def test_official_value_is_first_write_wins(self):
    row = {"ticker": "KXBTC15M-T", "close_ts": 1_800_000_900, "target_price": 82738.36,
           "expiration_value": 82813.54, "result": "yes", "settled_at": 1_800_001_000, "observed_at": 1_800_001_100}
    with closing(late_window.connect_store(self.path)) as db:
        self.assertTrue(late_window.record_official(db, row))
        self.assertFalse(late_window.record_official(db, {**row, "expiration_value": 1.0}))
        self.assertEqual(db.execute("SELECT expiration_value FROM official_values").fetchone()[0], 82813.54)
```

`make_window` is a test helper building 121 offsets (`-600..0` step 5) × 4 exchanges and the 5 snapshot offsets.

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest discover -s tests -k LateWindowStore -v`
Expected: FAIL, `No module named 'late_window'`.

- [ ] **Step 3: Implement the storage functions above in `late_window.py`** following `forecast_archive.connect_archive` style (`closing`, explicit transactions, `PRAGMA journal_mode=WAL`).

- [ ] **Step 4: Run to verify pass** (same command). Expected: OK.

- [ ] **Step 5: Commit** `feat: add append-only late-window store`

---

### Task 2: Exchange price sampling

**Files:**
- Modify: `late_window.py` (prices section)
- Test: `tests/test_late_window.py`

**Interfaces:**
- Produces:
  - `EXCHANGES: dict[str, tuple[str, Callable[[Any], dict]]]` mapping name → (URL, parser). Starting endpoints (verify each still returns these fields before relying on it):
    - `coinbase`: `https://api.exchange.coinbase.com/products/BTC-USD/ticker` → `bid`, `ask`, `time` (ISO)
    - `kraken`: `https://api.kraken.com/0/public/Ticker?pair=XBTUSD` → `result.<pair>.b[0]`, `a[0]`; no exchange time (store `None`)
    - `bitstamp`: `https://www.bitstamp.net/api/v2/ticker/btcusd/` → `bid`, `ask`, `timestamp` (s)
    - `gemini`: `https://api.gemini.com/v1/pubticker/btcusd` → `bid`, `ask`, `volume.timestamp` (ms)
  - Each parser: `parse_<name>(payload) -> {"bid": float, "ask": float, "exchange_ts": float | None}`; raises `ValueError` unless `0 < bid <= ask`.
  - `sample_prices(offset_s: int, close_ts: int, fetch: Callable[[str], Any], timeout: float = 3.0) -> list[dict]`: fetches all exchanges concurrently (`ThreadPoolExecutor(max_workers=4)`), one row per exchange shaped like `price_samples`; a timeout/HTTP/parse failure becomes a row with `error` set and price fields `None`.
  - `proxy_mid(rows: list[dict]) -> float | None`: median of `(bid+ask)/2` over rows without `error`; `None` if fewer than 2 exchanges succeeded.

- [ ] **Step 1: Write the failing tests:** one parser test per exchange using a recorded-shape payload (`assertEqual(parse_bitstamp({...})["exchange_ts"], 1792858000.0)`), a rejection test (`bid > ask` raises), `test_sample_survives_one_failing_exchange` (fetch raises for `kraken`; three rows have prices, kraken row has `error`, all four share `offset_s`), and `proxy_mid` cases (`[100,102],[101,101],[99,103]` mids → `101.0`; one success → `None`).
- [ ] **Step 2: Run to verify failure:** `python3 -m unittest discover -s tests -k LateWindowPrices -v` → FAIL.
- [ ] **Step 3: Implement.** `fetch` is injected; the production fetch is `urllib.request.urlopen` with the `User-Agent` used in `btc_predictor._fetch_kalshi_markets`.
- [ ] **Step 4: Run to verify pass.** Expected: OK.
- [ ] **Step 5: Commit** `feat: sample four public exchange tickers with recorded gaps`

---

### Task 3: Window collector loop

**Files:**
- Modify: `late_window.py` (collector + `main`)
- Test: `tests/test_late_window.py`

**Interfaces:**
- Consumes: `sample_prices`, `write_window`, `connect_store` (Tasks 1–2); `btc_predictor.fetch_kalshi_market(series)`, `btc_predictor.timestamp_seconds`, `INTERVAL_SECONDS`, `DEFAULT_KALSHI_SERIES`.
- Produces:
  - `PRICE_OFFSETS = range(-600, 1, 5)`, `SNAPSHOT_OFFSETS = (-300, -120, -60, -30, -15)`.
  - `next_close(now: float) -> int`: smallest multiple of 900 strictly greater than `now`.
  - `collect_window(close_ts: int, *, clock, sleep, sample, snapshot) -> dict`: walks the merged offsets in order, sleeping until `close_ts + offset` (`sleep(max(0, target - clock()))`), recording `max_drift_seconds` = largest `clock() - target`. Offsets already in the past when it starts are recorded as gap rows, not sampled late. Returns the `write_window` dict.
  - `snapshot_quote(offset_s, close_ts, fetch_market) -> dict`: calls `fetch_market()`; if the market is `None`, or its `close_time` ≠ `close_ts`, or its `quote_source` ≠ `"kalshi_orderbook"`, returns an error row (`error="wrong_market"`/`"no_orderbook"`); otherwise copies `ticker`, the six quote fields and `observed_at`.
  - `collect_window` also takes `stop: threading.Event`; if `stop` is set mid-window it returns at once with every remaining offset as a gap row, `error="stopped"`, so buffered samples are still written.
  - `run_collector(state_file, series, stop: threading.Event)`: loop `close = next_close(time.time())` → `collect_window` → `write_window` until `stop`. CLI: `python3 late_window.py --state-file PATH --kalshi-series KXBTC15M`; SIGTERM sets `stop` (follow `live_runner.main`).

- [ ] **Step 1: Write the failing tests** with a fake clock/sleep (no real waiting):
  - `test_offsets_and_next_close`: `next_close(1_800_000_000)` → `1_800_000_900`; `next_close(1_800_000_900)` → `1_800_001_800`; 121 price offsets.
  - `test_collect_window_samples_every_offset_in_order`: fake sample/snapshot record call order; 121 sample calls, 5 snapshot calls, `C−300` snapshot taken after the `C−300` price sample.
  - `test_restart_midwindow_writes_partial_once`: start clock at `C−100`; offsets before `C−100` are gap rows with `error="missed_start"`; `write_window` then returns `True` once and `False` on repeat.
  - `test_snapshot_for_wrong_close_is_a_gap`: fake market with `close_time` 900 s later → row `error == "wrong_market"`, no quote fields.
  - `test_stop_midwindow_keeps_collected_samples`: set `stop` after the `C−300` sample; result has real rows up to `C−300` and `error == "stopped"` for every later offset.
- [ ] **Step 2: Run to verify failure:** `python3 -m unittest discover -s tests -k LateWindowCollector -v` → FAIL.
- [ ] **Step 3: Implement** the functions above and `main`.
- [ ] **Step 4: Run to verify pass.** Expected: OK.
- [ ] **Step 5: Commit** `feat: collect late-window samples on a wall-clock schedule`

---

### Task 4: Official value finalization

**Files:**
- Modify: `late_window.py`
- Test: `tests/test_late_window.py`

**Interfaces:**
- Consumes: `record_official` (Task 1); `btc_predictor.fetch_kalshi_market_by_ticker`, `btc_predictor.is_finalized_result`, `optional_number`.
- Modifies: `run_collector` (Task 3) calls `finalize_pending(connection, fetch_kalshi_market_by_ticker, time.time())` after each `write_window`.
- Produces: `finalize_pending(connection, fetch_by_ticker, now: float, limit: int = 3) -> int`: selects up to `limit` windows with a ticker, `close_ts <= now - 30`, and no `official_values` row, oldest first; for each, fetches the market and records it only if `is_finalized_result(market, ticker, close_ts, now)` and both `target_price` and `expiration_value` parse as positive numbers. Fetch errors are swallowed per ticker (retried next cycle). Returns the number recorded.

- [ ] **Step 1: Write the failing tests:** `test_finalized_market_is_recorded` (expected row values incl. `expiration_value` `82813.54` from a string field); `test_provisional_result_is_not_official` (`status="determined"` → 0 recorded, stays pending); `test_fetch_error_leaves_window_pending`; `test_limit_and_oldest_first`.
- [ ] **Step 2: Run to verify failure:** `-k LateWindowFinalize` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** `feat: record official strike and settlement values once finalized`

---

### Task 5: Proxy check and pre-registered backtest report

**Files:**
- Create: `late_window_report.py`
- Test: `tests/test_late_window_report.py`

**Interfaces:**
- Consumes: the store schema (Task 1), `proxy_mid` (Task 2), `ask_backtest.paper_trade`, `ask_backtest.summarize`, `validation._normal_cdf`.
- Produces:
  - `window_average(samples_by_offset: dict[int, float | None], close_ts) -> float | None`: mean of proxy mids at offsets `-55..0`; `None` unless all 12 are present.
  - `proxy_check(windows: list[dict]) -> dict`: over windows with both official values and complete averages; `strike_error_bps`, `settlement_error_bps` (median of absolute error), `call_agreement` (fraction where `proxy_settle >= target_price` equals `result == 'yes'`), `windows`, `excluded_incomplete`, `passed` (≥ 500 windows, agreement ≥ 0.98, median settlement error ≤ 2 bps).
  - `late_probability(observed: list[float], spot: float, remaining_offsets: list[int], t_offset: int, sigma_per_s: float, strike: float) -> float`: see algorithm below.
  - `backtest(windows) -> dict`: runs only if `proxy_check(...)["passed"]`, else `{"status": "proxy_check_failed", ...}`. For each `t` in `(-60, -30, -15)`: per window with a valid snapshot at `t` and complete average, `p = late_probability(...)`, `trade = paper_trade(p, yes_ask, no_ask, label)`, keep if `trade["edge"] >= 0.05` and the chosen side's ask size ≥ 25. Report `summarize(trades)` per `t`, `primary = "-30"`, and `success` = primary mean > 0 and interval lower > 0 and ≥ 20 days and ≥ 1,000 windows.
  - CLI: `python3 late_window_report.py STORE > report.json` (read-only URI connection).

Algorithm for `late_probability` (the design's random-walk rule):

```text
n = 12 averaging samples (offsets -55..0); k = observed ones at offsets ≤ t; m = n − k future ones
τ_j = (offset_j − t) seconds for each future offset
mean A = (sum(observed) + m · spot) / n
var A  = (spot · σ_s)² / n² · Σ_i Σ_j min(τ_i, τ_j)     # σ_s = per-second log-return sd
p = Φ((mean A − strike) / sqrt(var A)); if var A == 0: p = 1.0 if mean A >= strike else 0.0
σ_s = pstdev of 5 s log returns of proxy mids from offset −600 up to t, divided by sqrt(5)
```

- [ ] **Step 1: Write the failing tests:**
  - `test_window_average_requires_all_twelve` (11 present → `None`).
  - `test_incomplete_average_is_excluded_and_counted` (`excluded_incomplete == 1`, not in `windows`).
  - `test_proxy_check_thresholds` (499 perfect windows → `passed False`; 500 with 98% agreement and 1 bps error → `True`; 97.8% → `False`).
  - `test_late_probability_edges`: all 12 observed above strike → `1.0`; `t=-60`, `spot == strike`, σ > 0 → `0.5`; larger σ moves `p` toward 0.5.
  - `test_backtest_skips_when_proxy_fails` and `test_backtest_applies_edge_and_depth_filters` (a 4¢-edge trade and a 24-size trade are excluded).
- [ ] **Step 2: Run to verify failure:** `python3 -m unittest discover -s tests -k LateWindowReport -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** `feat: add late-window proxy check and pre-registered backtest`

---

### Task 6: Include the store in verified backups

**Files:**
- Modify: `backup_state.py` (`verify_backup`, `create_backup`)
- Test: `tests/test_backup_state.py`

**Interfaces:**
- Consumes: `late_window.late_window_path`.
- Produces: manifests may carry an optional third role `late_window`; `verify_backup` accepts file roles exactly `{state, archive}` or `{state, archive, late_window}`, checks its checksum, that its name equals `late_window_path(state).name`, and `PRAGMA quick_check` plus `schema_version` `'1'`. `create_backup` copies the store with the SQLite backup API (as for the archive) only when the file exists. The manifest `schema_version` stays `1`, so old backups still verify.

- [ ] **Step 1: Write the failing tests:** `test_backup_includes_late_window_store_when_present`, `test_backup_without_store_still_verifies`, `test_tampered_store_fails_verification`.
- [ ] **Step 2: Run to verify failure:** `-k Backup` → the three new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the whole backup suite to verify pass.**
- [ ] **Step 5: Commit** `feat: back up the late-window store with state and archive`

---

### Task 7: Service, deploy wiring and docs

**Files:**
- Create: `deploy/container-systemd/kalshi-late-window.service`
- Modify: `deploy/container-python` (allow job `late`), `deploy/oracle-container-deploy.sh` (`APP_SERVICES` += `kalshi-late-window.service`; add it to the required bundle members and the unit install loop), `deploy/package.sh` (add `late_window.py`), `.github/workflows/ci.yml` (required set += `late_window.py`, `deploy/container-systemd/kalshi-late-window.service`), `tests/test_oracle_deploy.py` (`UNITS` += the unit; the container-unit loop includes it), `README.md`, `deploy/README.md`.

**Interfaces:**
- Consumes: `late_window.py` CLI (Task 3).
- Produces: unit modeled on `kalshi-runner.service`: `ExecStart=/opt/kalshi/deploy/container-python late /opt/kalshi/late_window.py --state-file /var/lib/kalshi/btc_15m_state.json --kalshi-series KXBTC15M`, `ExecStop=/usr/bin/docker stop --time 30 kalshi-late`, `Restart=always`, `RestartSec=5`, same hardening lines, `ConditionPathExists` on the state file only, no `Requires=` on the runner or dashboard.

- [ ] **Step 1: Update `tests/test_oracle_deploy.py` first** so the new unit is expected; run `python3 -m unittest discover -s tests -k Oracle -v` → FAIL (unit missing).
- [ ] **Step 2: Add the unit and the script/list changes above.**
- [ ] **Step 3: Run** `python3 -m unittest discover -s tests -v`, `bash -n deploy/*.sh`, `bash deploy/package.sh` and the CI bundle check script → all pass except the known local-only dashboard port errors (13 on clean `main`).
- [ ] **Step 4: Docs.** README: a "Late-window collection" section (what it records, paper-only, the report command, the pre-registered rule and that it must not be changed). Deploy README: start/inspect/stop the service and that backups now include the store.
- [ ] **Step 5: Commit** `feat: run the late-window collector as a container service`

Deployment itself stays manual through the approval-gated workflow and is not part of this plan.

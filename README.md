# BTC 15-Minute Self-Learning Predictor

A small, dependency-free Python system with experimental **paper-only** recommendations
for BTC 15-minute Kalshi contracts. It uses Coinbase candle features and online
logistic regression, binds new forecasts to the exact active Kalshi ticker, and
learns new matched examples from that ticker's official YES/NO result.

This project is prediction-only: it does **not** place Kalshi orders, execute trades,
or manage real money.

## How the self-learning loop works

1. `train` downloads recent one-minute BTC/USD candles.
2. Complete 15-minute windows are built from those candles.
3. Bootstrap training uses **Coinbase proxy direction** and seven candle features:
   short-term returns, momentum, EMA gap, volatility, volume, and candle range.
   This is not a historically validated Kalshi forecasting model.
4. `run` selects the currently open BTC 15-minute contract and fetches its public
   orderbook. Its open/close interval must match the forecast exactly.
5. New matched forecasts persist the ticker, interval, issue time, original
   probability, features, and market snapshot. When that exact ticker has an official
   YES/NO result, it supplies the new online label and paper-account settlement.
   If the result is unavailable, the app waits and retries rather than substituting
   Coinbase's next close. It skips unresolved settlements after one hour.
6. Legacy pending entries retain their Coinbase proxy labels. New unbound forecasts
   are research-only and skipped without invented labels. Late forecasts may teach
   the model, but do not count toward entry-time validation.

The initial model is intentionally simple and inspectable. "Self-learning" here
means online updates from new labeled market windows; it is not a guarantee of
profit or a claim of autonomous intelligence.

New official forecasts are frozen only after the exact active ticker and a fresh,
valid orderbook snapshot are available within the first 120 seconds of the window.
Before that, the dashboard shows **AWAITING MARKET EVIDENCE / estimate only** and
the runner retries. A timely retry uses its actual issue time and preserves the
quotes known at that time. If the deadline expires, the window is recorded as
missed without an invented forecast or training label. Quote collection does not
require the candidate to pass confidence, edge, or liquidity trade gates.
Already-issued forecasts keep their original probability and timestamp; historical
quotes observed later remain excluded from issue-time market comparisons.

## Run it

From this directory:

```bash
# Train a fresh model from the previous 48 hours of public candles.
python3 btc_predictor.py train

# Run this every 15 minutes (cron, launchd, or a scheduler).
# It learns the previous queued result and emits the next prediction as JSON.
python3 btc_predictor.py run
```

Useful options:

```bash
python3 btc_predictor.py train \
  --lookback-minutes 2880 \
  --state-file data/btc_15m_state.json \
  --epochs 5

python3 btc_predictor.py run --state-file data/btc_15m_state.json
```

The output includes `probability_up`, `probability_down`, the direction, the
window timestamp, whether a previous prediction was just learned, and (when the
public endpoint is available) the active Kalshi market's `yes_mid` plus the
model-vs-market probability difference. It also records a one-contract prediction match and a separate demo account.

Learning freshness is tracked separately from prediction accuracy. If a pending
window is older than the available candle history, the system marks it as skipped
rather than inventing an outcome, starts a fresh pending window, and reports
`RECOVERED / WAITING FOR FRESH RESULT`. The risk gate remains blocked until a
new outcome is learned.
The demo account starts with $100 and uses up to $10 per eligible trade, including
an **assumed 3¢ per-contract cost allowance** (2¢ fees + 1¢ slippage). This is a
conservative modeling assumption, not a verified fee schedule. Quantity cannot
exceed observed executable ask depth. Account entries require actual orderbook
quotes; missing prices never create synthetic demo-account fills. Settlement uses
the official result of the recorded ticker. A committed paper entry is frozen;
later WAIT recommendations do not retroactively erase it. If its settlement remains
unavailable after one hour, the entry is preserved as unresolved and the account
blocks new trades pending settlement review; it is not treated as a cancelled fill.

Historical one-contract prediction-match scoring is separate from this account.
It includes legacy Coinbase proxy results and may use a synthetic 50¢ price when
no entry quote exists. Its gross P&L is **not money earned or a net-return estimate**.
The dashboard labels these metrics separately from qualifying demo-account P&L.

If `TYPESAFE_API_KEY` is configured, only a deterministically eligible candidate
is sent to `jev-latest` for a narrow Choice review. TypeSafe can veto, never create,
a trade. An enabled gate requires a matching evidence fingerprint, a review no
older than 60 seconds, and `approve_trade` confidence ≥0.55. Failed, expired,
mismatched, or missing reviews withhold the paper entry. Vetoes update the saved
pending decision, the account quantity/cost, the analysis, and the dashboard.
Already-blocked candidates skip the API call to save cost. Once configured, the
state remembers that the gate is required, so losing the key after a reboot does
not silently bypass it. Keep the key in the service environment, never the repository.
With no previously configured gate, TypeSafe remains optional.

## Recommendation eligibility

The dashboard shows **TRADE NOW** only for a paper candidate that passes all checks:

- Raw (uncalibrated) model confidence ≥65%; at least 3/4 correlated trend signals agree.
- **Net** edge ≥8 percentage points after subtracting the selected executable ask
  and the explicit 3¢ cost allowance from the model estimate.
- Spread ≤8 percentage points; executable ask depth ≥25 contracts. Unknown depth
  is not zero, and still blocks trading. A verified empty book really is zero.
- Exact active contract / forecast alignment and fresh, contiguous candle history.
- Orderbook snapshot age ≤60 seconds, entry within the first 120 seconds of the
  forecast window, and at least 60 seconds until market close. A start-of-window
  forecast is not reused as an intrawindow recommendation.
- Existing volatility ≤3%, candle range ≤5%, drawdown, and weak-performance guards.
- At least **100 distinct, entry-time, officially settled Kalshi forecasts**.
  Legacy proxy matches and late forecasts do not satisfy this preliminary gate.
  On ≥20 official samples, rolling accuracy must be ≥50% and Brier must beat the
  constant-50% baseline of 0.250. The app reports reliability bins without claiming
  that the probabilities have been calibrated.
- If configured, a current TypeSafe approval for the exact candidate.

Every failing check has an explicit blocker. The dashboard displays ticker, ask,
executable depth, spread, deadline, official-validation progress, and next step.
Client-side expiry and API failures revoke a displayed TRADE indication rather
than leaving an old recommendation on screen. Until validation is adequate, the
stance is **RESEARCH / NOT VALIDATED**, even when model confidence looks high.
100 samples takes at least 25 hours of continuous, timely collection. This minimum
is a safety heuristic, not statistical proof of a profitable strategy.
These are transparent paper-trading heuristics, not guarantees. Each cycle also
reports a concise self-learning analysis: rolling accuracy, Brier score, recent
P&L, sample count, and a conservative risk gate that can force WAIT after weak
performance. It is an auditable rationale, not a guarantee of correctness. The system uses
chronological forward online updates. An independent walk-forward **forecast**
evaluator is now implemented, but passing the paper-validation gate or evaluating
fresh models does not establish out-of-sample trading profitability.
Kalshi quotes are currently recommendation evidence, not model input features;
official settlement results supply new matched training labels. Quote snapshots
are retained with forecasts in SQLite. No historical quote history is invented;
walk-forward models still use only the seven candle features.

## Durable archive and independent validation

Both `run` and the dashboard collect public evidence in
`data/btc_15m_state_archive.sqlite3` (or `<state-stem>_archive.sqlite3` next to a
custom state file). The archive uses SQLite WAL and transactional writes, with:

- Complete Coinbase minute candles and 15-minute bars, with first receipt times
  captured before forecasting. Archive-write time remains separate; older records
  retain their original timestamps.
- Immutable original forecasts: probability, features, issue time, contract,
  frozen market snapshot, and model-at-issue snapshot for new records.
- Exact official settlement observations, including when their label was first
  received locally. Market close is **not** assumed to be label-availability time.
- Decision/check/TypeSafe snapshots and immutable qualifying paper entries.
- Separately labelled legacy proxy scores, imported official records, and skips.

Duplicates do not overwrite earlier evidence, and pruning/retraining the rolling
JSON state does not delete the archive. Imported legacy records with missing
features or quotes stay missing; today's candles/prices are not used to fill them.
State resets get distinct run IDs. Account audits keep separate $100 runs separate.
If archiving fails, new paper entries are withheld, while already-committed entries
and learned outcomes are preserved. Ensure disk space remains available. Collection
has no automatic retention limit. Verified state/archive snapshots and a seven-copy
daily backup policy are available in the Ubuntu deployment described below.

Run a read-only evaluation at any time:

```bash
python3 validation.py \
  --archive-file data/btc_15m_state_archive.sqlite3 \
  --output-json data/btc_15m_state_validation.json \
  --summary-json data/btc_15m_state_validation_summary.json
```

The runner generates these reports on startup and daily UTC after a successful
collection. The dashboard displays the small summary, its timestamp, and whether
it is stale. It also reports timely-forecast coverage, late/missing-ticker counts,
feature/quote gaps, interval gaps, and settlement-observation delays. Offline
evaluation never edits the live model, state, probabilities, TypeSafe approvals,
or trade gates; stale/insufficient reports cannot enable trades.

The report separates three questions:

1. **Actual frozen forward predictions:** exact timely official outcomes only,
   deduplicated by ticker across runs; accuracy with Wilson 95% intervals, Brier,
   log loss, reliability bins, a constant-50% baseline, and a market-midpoint
   baseline only where fresh aligned frozen bid/ask quotes actually exist.
2. **Fresh-model walk-forward forecasts:** each test block uses the latest 100
   earlier training records and 50 later, held-out calibration records whose
   official labels were available **strictly before** the block began. A fresh
   logistic model and regularized Platt calibration fit are frozen throughout
   the next 25 tests. No test labels enter that block's training/calibration, and
   no latest live model is reused. Fold boundaries, baseline comparisons, raw
   and calibrated scores, and individual predictions are retained in the report.
3. **Recorded paper-account audit:** only immutable actual simulated entries,
   with recorded quote/depth/check/review evidence and costs. Reports net realized
   P&L, unresolved committed costs, cash constraints, win rate, and peak-to-trough
   realized drawdown. It does not invent trades or transfer a TypeSafe approval
   to a freshly retrained counterfactual probability.

**Calibration is experimental and offline only.** Insufficient data is reported
explicitly. The default first fold needs at least 150 earlier known labels plus
25 test records, and reporting 100 held-out scores generally needs at least 250
eligible archived feature records, more if observations are missing/delayed.
Forward imports without original features can be scored but cannot train models.
Small-sample metrics, reliability bins, or a positive paper P&L are not proof of
profitability. Thresholds are not optimized on test outcomes. Historical replay
of the full live trade strategy with execution/queue modeling is not implemented.

For a read-only context-horizon comparison, use the separate experiment command:

```bash
python3 validation.py compare-context \
  --archive-file data/btc_15m_state_archive.sqlite3 \
  --output-json data/btc_15m_context_comparison.json
```

It compares the archived current `15m/45m/3h` context with a proposed
`15m/30m/1h` context using identical chronological folds, the constant 50%
baseline, and valid issue-time market midpoints. Only the 45m/3h returns are
replaced: the original 15m return, EMA, volatility, volume ratio and range stay
frozen. The replacement returns require five consecutive complete `BTC-USD`
bars observed by the original issue time. Historical write-time stamps and
backfilled prices are not moved backward to create missing evidence.

Test blocks are chosen from common forecast-time features, including forecasts
whose official outcomes are still missing. The earliest eligible issuance per
ticker wins across runs; eventual settlement availability cannot select a later
forecast. Training labels must have been observed strictly before the block.
Reports include fold boundaries, individual predictions, unscored outcomes and
matched-subset market coverage. Defaults are 100 training records, 25 test
records and five epochs; the first fold needs at least 125 common records, with
100 earlier labels available before its start. The first block alone is not
enough to establish a winning model. This command is a standalone offline
experiment; the runner's daily report continues to use the standard evaluator.
The Ubuntu deployment includes `kalshi-context-comparison.timer`, which refreshes
the separate comparison report hourly at minute 05 UTC. The timer persists across
reboots and catches up on a missed run. See [deployment operations](deploy/README.md#5-backups-and-operations)
for manual refresh and schedule checks.

The dashboard displays a **Context horizon comparison** card directly below the
live prediction. It shows the current and challenger input horizons, held-out
Brier score/log loss/accuracy, sample counts, report freshness and next-block
collection progress. Switch between **All test outcomes** and **With market
quotes** to compare models against the market on identical issue-time quote
coverage. The card reads the saved report through `/api/context-comparison`,
independently of live price collection. A missing report is shown as pending;
the first scheduled or manual comparison creates it. The BTC/15m heading refers
to the predicted contract duration; 30m/1h are historical input horizons.

## Continuous 24/7 live paper session

The project includes macOS LaunchAgents that keep both the dashboard and the
paper-monitoring runner alive across crashes and user logins:

- `com.kalshi.btc15m.dashboard.plist`
- `com.kalshi.btc15m.runner.plist`

The installed services check health every 30 seconds, run a fresh model cycle at
each 15-minute boundary, learn the exact previous contract settlement, and log
telemetry. They retry lagging candles/entry evidence during the short entry window,
and unresolved prior settlements without requiring the browser to be open. They use live Coinbase/Kalshi public data and update the demo account
only; they never place a real order.

To inspect or stop them:

```bash
launchctl print gui/$(id -u)/com.kalshi.btc15m.dashboard
launchctl print gui/$(id -u)/com.kalshi.btc15m.runner
launchctl bootout gui/$(id -u)/com.kalshi.btc15m.runner
launchctl bootout gui/$(id -u)/com.kalshi.btc15m.dashboard
```

Logs are stored under `~/Library/Logs/Kalshi/`; model state remains in
`data/btc_15m_state.json`. The Mac must stay powered, awake enough to run jobs,
online, and have `/Volumes/Secondary` mounted. A sleeping, powered-off, offline,
or unmounted Mac cannot provide 24/7 coverage.

For a bounded manual session instead:

```bash
python3 live_runner.py --hours 3
```

## Oracle Cloud Ubuntu deployment

The Linux deployment is in [`deploy/README.md`](deploy/README.md). It includes
systemd services for the existing runner/dashboard, a staging installer, and a
daily verified backup timer. The dashboard is accessed through an SSH tunnel.
The scripts are provider-neutral and can run on an Ubuntu 24.04 VM in Oracle
Cloud. Create the VM, then migrate the existing state and archive so the forward
evidence and account run continue together.

Build a code-only upload with `bash deploy/package.sh`; it produces
`dist/kalshi-ubuntu.tgz`. The live model/archive are transferred separately using
a consistent snapshot rather than copying an active SQLite database file.

```bash
python3 backup_state.py create \
  --state-file data/btc_15m_state.json \
  --backup-dir data/backups --keep 7
python3 backup_state.py verify /path/to/the/generated/snapshot
```

Snapshots include committed SQLite WAL data, take the same lock as the live
writers, preserve state bytes and run identity, and check SHA-256 hashes plus
SQLite integrity before publication. Retention only removes this tool's snapshots
for the same source path. Daily VM-local backups should also be copied off-server;
the deployment guide includes an SSH export command.

## CI/CD

GitHub Actions runs the test suite, Python compilation checks, shell syntax checks,
and code-bundle inspection on every push and pull request. Production delivery is
manual and approval-gated through the `production` GitHub Environment. The deploy
workflow updates code and systemd units on the Oracle VM, creates a verified remote
evidence backup first, restarts the services, and checks `/health`; it does not
overwrite or migrate the live state/archive. See
[`deploy/README.md`](deploy/README.md#6-github-actions-cicd).

## Dashboard

Start the local visual app:

```bash
python3 dashboard.py
```

Then open <http://127.0.0.1:8765>. It shows the live BTC spot price and clock, the live model call, active Kalshi
odds, the previous settled Kalshi market and result, the model-versus-market
difference, and demo-trade results. The dashboard refreshes every 30 seconds and
uses the same state file as the CLI by default.

### Historical analytics

The **Historical analytics** panel sits below **Context horizon comparison** and
above **Previous market settlement** and the metric cards. It refreshes at startup,
on manual refresh, and every 30 seconds, independently of live price collection.
Its two read-only JSON APIs are:

| API | Contents |
| --- | --- |
| `GET /api/analytics` | Saved report summary, walk-forward progress, evidence coverage, report freshness, and the newest up to 25 live archive trend points in chronological order. |
| `GET /api/analytics/forecasts?limit=25` | Up to 25 recent archived forecasts, newest issue time first, selecting the earliest eligible issuance per ticker across runs. |

The forecast endpoint defaults to `limit=25`. An explicit limit must be a decimal
integer from **1–25**; blank, signed, fractional, nonnumeric, or out-of-range
values return **HTTP 400** with `{"error":"invalid analytics limit"}`. Invalid
values are not clamped. The limit bounds the returned table, not archive retention.

Read the report-wide counts as different populations:

- **Eligible / sample threshold** (`summary.eligible_count` /
  `summary.minimum_count`): distinct timely official forward forecasts in the saved
  report, including those without a valid observed settlement. The default threshold
  of 100 applies to **scored evidence**, so 100 eligible forecasts alone is insufficient.
- The **Accuracy** card's **Scored count** (`summary.scored_count`) counts original
  frozen forward forecasts with valid official outcomes. Accuracy with its 95%
  Wilson interval and the report's Brier score use this same population. Brier also
  shows the constant-50% baseline (0.250 when scored evidence exists).
- **Walk-forward scored / target** (`walk_forward.scored_count` /
  `walk_forward.test_target`) counts scored held-out predictions from fresh offline
  models, with enough earlier known training/calibration labels and original features.
  Its default target is also 100. It is a subset of the canonical forward scored
  population, distinct from the recent table and each rolling window.

**Live archive trend** plots accuracy (%) and Brier score (0–1) separately. It uses
complete **25-record rolling windows**, with **stride 5**: window ends are records
25, 30, 35, and so on in the chronological, ticker-deduplicated eligible archive.
Each point reports its own scored count; accuracy and Brier require at least **10
valid official labels with usable probabilities** within that window. Below 10,
the point retains its time/count but omits both scores. Global window alignment is
preserved while calculating only the newest **up to 25 points**, displayed oldest to newest;
records after the last complete stride await the next window. **View numeric trend
values** exposes exact window-end issue times, counts, and scores. Unknown values
are `—` and break chart lines; they are never zero-filled. Recorded zero remains zero.
Missing results, timing evidence, and frozen market midpoints also remain unknown.

**Report updated** and **age at refresh** refer to the saved validation report's
`generated_at`, not the latest archive row. Summary and **Evidence coverage** are
report-wide; coverage categories may overlap and need not total the recent 25 rows.
The trend uses live archive **window-end issue times** and can be newer than the
report. The recent table's **Archive read** time is the browser refresh time, while
**Issued** and **Settlement observed** are evidence timestamps. Observation delay
is settlement receipt time minus market close, not time since forecast issuance.
Displayed timestamps are UTC.

**Bounded archive reads and reuse:** both endpoints share an in-process cache of
at most **four archive paths**, with a **10-second TTL** for successful reads and
safe unavailable results. Each request checks the resolved database and nonempty
WAL identity (device, inode, byte size, nanosecond modification/change times).
Replacement, deletion, or changed evidence invalidates reuse immediately; a source
change during a scan discards that scan. An absent and an empty WAL are equivalent
because neither contains committed frames. SHM read marks are not evidence identity.
Only **one analytics scan per process** can run at once; other callers wait at most
**250 ms** for reuse, then receive unavailable. This uses a separate analytics lock
and never acquires the live prediction/state lock. The saved report is read and
validated on every analytics request, independently of the archive cache.

A cold scan has a **1-second cooperative deadline**, **5,000,000 SQLite VM steps**
(checked at most every 1,000 steps, including filtering/sort/join work), and a
**100-ms SQLite busy timeout**. It accepts at most **50,000 streamed candidate rows**, tracks
at most **25,000 distinct eligible tickers**, and permits at most **16 MiB of decoded
field bytes** (text counted as UTF-8, scalar fields as eight bytes), with a **64-KiB
SQLite record/value limit**. One over-limit probe row causes the whole scan to fail.
Duplicates and rejected rows consume the row/byte budget.
SQLite uses a 2-MiB page-cache target and file-backed temporary sorting. The Python
scan retains only the newest **149 canonical rows**, sufficient for 25 windows of
25 records at stride 5, including the incomplete-stride tail. Probability and
forecast identity must be valid before a ticker is claimed; the earliest eligible
unresolved forecast still wins over a later settled retry.

Even `limit=1` verifies the complete global population within these budgets. If a
budget is exceeded, both endpoints fail safely with **unavailable**, empty archive
rows/trend, and cleared report metrics; they never silently truncate the population.
Large histories beyond these caps require a future separately maintained summary
or an explicitly reviewed budget change. The deadline is cooperative, not an OS
hard real-time guarantee; filesystem stalls and thread scheduling can add latency.
`archive_read_at` is the successful scan-completion Unix timestamp, possibly reused
for up to 10 seconds. Browser **Archive read** is request time; report `generated_at`
and the 36-hour report staleness threshold are separate freshness measures.

A valid report **older than 36 hours** stays `status="available"` with
`report_freshness="stale"` and `freshness.diagnostic_only=true`; the UI retains its
statistics with a prominent **Stale report — diagnostic-only** warning.
Available analytics reports have `validation_ready=false`, including fresh ones.
A missing archive or saved summary is **pending**; an unreadable/corrupt archive
or malformed, contradictory, or future-dated summary is **unavailable**. These are HTTP 200 status
payloads, not successful validation claims. Pending/unavailable reports clear
metrics/coverage to unknown; a newly read archive trend can still appear when only
the report is pending/unavailable, and the recent-forecast endpoint works
independently. A valid empty archive returns an available, empty forecast table.
Request failures clear the affected display rather than retaining a previous success.

The endpoints read the existing SQLite archive with `mode=ro` plus `query_only` and the saved
`<state-stem>_validation_summary.json`; they perform no network collection, model
updates, application writes to state/archive/report evidence, or report generation.
SQLite may create/update native **WAL/SHM coordination sidecars** and temporary sort
files; read-only SQL is not a zero-filesystem-mutation guarantee. `immutable=1` is
not used: committed, uncheckpointed WAL evidence remains visible. **Descriptive archived
evidence; not profitability proof.** These charts measure forecast accuracy and
probability error, not profit or paper-account returns, and never authorize paper
entries. See [analytics operations](deploy/README.md#historical-analytics-operations)
for data paths and code-only rollout.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The runtime remains dependency-free: Python's standard library plus inline browser
JavaScript/SVG, with no pip/npm packages or chart-library build step. **Node.js is
optional for development, but required to execute the dashboard renderer/polling
tests** invoked by the Python suite. Put `node` on `PATH` to run them; without it,
that integration test is explicitly skipped locally. Both CI and deployment-build
workflows install/check Node 24 before the suite, so renderer coverage cannot
silently skip there. Node is not needed to serve the dashboard.

## Important limitations

- Coinbase spot candles are used for features; Kalshi BTC contracts settle against
  CF Benchmarks' BRTI, which is a different reference series.
- TypeSafe integration is optional. Set `TYPESAFE_API_KEY` only in the service
  environment if you want the second-opinion gate; never commit the key.
- Kalshi markets, exact-market results, and orderbooks use public endpoints; no
  account credentials or private signing keys are needed. The documented bid-only
  book implies YES ask = 1 − NO bid, with the NO bid's size, and vice versa:
  <https://docs.kalshi.com/getting_started/orderbook_responses>.
- The rolling JSON state retains only 500 outcomes, while the independent SQLite
  archive is durable. Missing pre-archive data cannot be recovered by pretending
  current observations were historical. `train` resets live state and is not
  routine monitoring; it does not remove existing archive evidence.
- Fifteen-minute BTC direction is noisy and can reverse quickly.
- Backtest metrics from the initial training history are descriptive, not proof of
  future performance. Use walk-forward evaluation, paper trading, position limits,
  and transaction-fee/slippage assumptions before considering any real deployment.
- Demo P&L assumes paper fills at observed ask with an explicit cost allowance.
  There are no real executions, queue/fill modeling, or guarantee that those quotes
  remain available. The allowance is not a substitute for a verified fee schedule.
- The online model retains its Coinbase bootstrap/legacy weights while adapting
  to official Kalshi outcomes. This mixed-origin model is still experimental.
- Official reliability bins are descriptive. Experimental Platt calibration is
  fitted on prior held-out calibration records and tested only on later records;
  it is never applied to live forecasts. Full execution-aware strategy replay,
  and actual fee verification remain future work. The Ubuntu deployment supplies
  verified daily backups; off-server copies are configured by the operator.
- The program has no order-placement code by design.

# Historical Analytics Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bounded, read-only historical analytics section and API to the BTC 15-minute dashboard without changing live predictions, trade gates, or archived evidence.

**Architecture:** Extend `forecast_archive.py` with read-only SQLite queries that deduplicate the earliest eligible issuance per ticker, calculate 25-record rolling metrics with stride 5, and return JSON-safe bounded payloads. Extend `dashboard.py` with two read-only endpoints and a dependency-free inline SVG/table presentation that refreshes alongside existing dashboard data. Add focused archive/API/UI payload tests, then run the full regression and CI checks.

**Tech Stack:** Python 3.11+, standard-library `sqlite3`, `json`, `datetime`, existing `unittest`, existing dependency-free HTML/CSS/JavaScript dashboard, inline SVG.

**Spec:** `docs/superpowers/specs/2026-10-08-historical-analytics-dashboard-design.md`

## Global Constraints

- Do not change the live predictor, feature vector, thresholds, or recommendation eligibility checks.
- Do not apply offline calibration to live probabilities.
- Do not present descriptive metrics as profitability proof.
- Do not add real order placement or change paper-account behavior.
- Do not add a third-party charting dependency.
- Do not return the full SQLite archive or unbounded forecast history to the browser.
- Open SQLite in read-only mode and never write to the archive, live JSON state, validation reports, or model files.
- Preserve `None` for unavailable outcomes, quotes, or delays; never substitute zero.
- Treat malformed, stale, or missing reports as unavailable and never imply validation passed.
- Validate and cap API limits at 25.

---

### Task 1: Add read-only archive analytics queries

**Files:**
- Modify: `forecast_archive.py`
- Create: `tests/test_historical_analytics.py`

**Interfaces:**
- Consumes: `state_file`, the existing archive path convention, archive tables
  `forecasts` and `settlements`, and `read_validation_summary`.
- Produces:
  - `read_recent_forecasts(state_file: str | Path, limit: int = 25) -> dict[str, Any]`
  - `read_historical_analytics(state_file: str | Path, limit: int = 25) -> dict[str, Any]`

- [ ] **Step 1: Write failing tests for bounded limits and missing archives**

Add a temporary-directory test fixture that creates a valid archive with
`connect_archive`, plus a separate test with no archive file. Assert:

```python
def test_recent_forecasts_rejects_invalid_and_oversized_limits(self):
    with self.assertRaises(ValueError):
        read_recent_forecasts(self.state_file, 0)
    with self.assertRaises(ValueError):
        read_recent_forecasts(self.state_file, 26)

def test_missing_archive_is_pending_without_creating_a_database(self):
    result = read_historical_analytics(self.missing_state_file)
    self.assertEqual(result["status"], "pending")
    self.assertFalse(archive_path(self.missing_state_file).exists())
```

- [ ] **Step 2: Run the focused tests to verify the expected failure**

Run:

```bash
python3 -m unittest tests.test_historical_analytics.HistoricalAnalyticsTests.test_recent_forecasts_rejects_invalid_and_oversized_limits -v
python3 -m unittest tests.test_historical_analytics.HistoricalAnalyticsTests.test_missing_archive_is_pending_without_creating_a_database -v
```

Expected: FAIL because the analytics functions do not exist.

- [ ] **Step 3: Implement read-only connection and limit validation**

Add private helpers in `forecast_archive.py`:

```python
ANALYTICS_LIMIT = 25
ROLLING_WINDOW = 25
ROLLING_STRIDE = 5
STALE_REPORT_SECONDS = 36 * 60 * 60

def _analytics_limit(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= ANALYTICS_LIMIT:
        raise ValueError("analytics limit must be an integer from 1 to 25")
    return value

def _connect_archive_readonly(state_file: str | Path):
    path = archive_path(state_file)
    if not path.is_file():
        return None
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
```

The read-only helper must not call `connect_archive`, create parent directories,
enable WAL, execute schema creation, or commit.

- [ ] **Step 4: Run the focused limit and missing-archive tests**

Run the two focused unittest commands again. Expected: PASS.

- [ ] **Step 5: Write failing tests for earliest-ticker deduplication and unknown evidence**

Insert duplicate official forecasts for one ticker with different issue times,
one official forecast with no settlement, one late/ineligible forecast, and one
forecast with a frozen quote snapshot lacking `yes_mid`. Assert that the result:

- Includes only the earliest eligible issuance for the duplicate ticker.
- Keeps the unresolved result as `None`.
- Excludes late/ineligible rows from the eligible set.
- Keeps missing midpoint and settlement delay as `None`.
- Orders recent rows newest-first after earliest-ticker selection.

- [ ] **Step 6: Run the deduplication tests to verify failure**

Run:

```bash
python3 -m unittest tests.test_historical_analytics.HistoricalAnalyticsTests.test_recent_forecasts_deduplicate_and_preserve_unknowns -v
```

Expected: FAIL because query transformation is not implemented.

- [ ] **Step 7: Implement the canonical recent-row transformation**

Query official, validation-eligible forecasts with non-null ticker and issue
time, ordered by `forecast_issued_at ASC, forecast_id ASC`, left-joining
settlements by `forecast_id`. Keep the first row for each ticker. For each row,
return only JSON-safe fields:

```python
{
    "forecast_id": str,
    "issued_at": float,
    "market_ticker": str,
    "probability_up": float | None,
    "result": "yes" | "no" | None,
    "timing_status": "timely" | "late" | "unknown",
    "yes_mid": float | None,
    "market_midpoint_available": bool,
    "settlement_available_at": float | None,
    "settlement_delay_seconds": float | None,
}
```

Use the archived market snapshot only for the frozen issue-time midpoint and
timing evidence. Do not fetch current market data or recompute historical
quotes. Return `{ "status": "pending", "reason": ... , "rows": [] }` when
the archive is absent and `{ "status": "available", "rows": [...] }` for a
valid archive, even when individual evidence fields are unknown.

- [ ] **Step 8: Run the deduplication tests to verify success**

Run the focused test from Step 6. Expected: PASS.

- [ ] **Step 9: Write failing tests for fixed rolling metrics**

Create at least 35 chronological eligible rows with known yes/no outcomes and
probabilities. Assert that `read_historical_analytics` creates points at the
25th, 30th, and 35th eligible rows, uses 25-row windows with stride 5, omits
metrics when fewer than 10 labels are known, and computes accuracy/Brier from
the same scored subset. Verify `generated_at`, `summary`, `walk_forward`,
`coverage`, `trend`, and `note` exist and all numeric values are finite.

- [ ] **Step 10: Implement rolling metrics and validation-summary projection**

Build the trend from the canonical earliest-ticker rows in issue order. For each
window, count rows with known official results; omit `accuracy` and
`brier_score` when that count is below 10. Use `read_validation_summary` for
the full-report summary fields, validate its `generated_at`, and mark the
payload `unavailable` if the report is malformed, contradictory, or older than
36 hours. Project only compact safe fields; do not expose fold predictions.

Coverage must report timely, late, missing ticker, missing feature, missing fresh
quote, interval-gap, and settlement-delay values from the saved summary when
valid, with `None`/empty values for unavailable report sections.

- [ ] **Step 11: Run the archive analytics test file**

Run:

```bash
python3 -m unittest tests.test_historical_analytics -v
```

Expected: all archive query, deduplication, trend, stale-report, and read-only
tests pass.

### Task 2: Add bounded dashboard analytics endpoints

**Files:**
- Modify: `dashboard.py`
- Modify: `tests/test_dashboard_context.py` or create `tests/test_historical_analytics.py` endpoint tests

**Interfaces:**
- Consumes: `read_historical_analytics` and `read_recent_forecasts` from Task 1.
- Produces:
  - `GET /api/analytics`
  - `GET /api/analytics/forecasts?limit=25`
  - Existing `/api/status` and `/api/context-comparison` behavior unchanged.

- [ ] **Step 1: Write failing endpoint tests**

Add handler tests using the existing dashboard test style. Assert that:

- `/api/analytics` returns the compact analytics payload without network calls.
- `/api/analytics/forecasts?limit=25` returns bounded rows.
- Invalid limits return HTTP 400 with a safe JSON error.
- Missing/malformed reports return `pending` or `unavailable`, never a success
  payload with fabricated zero values.
- Endpoint calls do not change state-file bytes or archive bytes.

- [ ] **Step 2: Run endpoint tests to verify failure**

Run:

```bash
python3 -m unittest tests.test_historical_analytics -v
```

Expected: FAIL because the routes are not registered.

- [ ] **Step 3: Implement endpoint routing and safe response handling**

Import the two archive functions into `dashboard.py`. Add route branches before
the existing 404 path:

```python
if path == "/api/analytics":
    return self._json(self.app.analytics())
if path == "/api/analytics/forecasts":
    limit = parse_limit(query.get("limit", ["25"])[0])
    return self._json(self.app.recent_forecasts(limit))
```

Use the existing response helper and return HTTP 400 for invalid limits. Catch
archive `OSError`, `sqlite3.Error`, `ValueError`, `KeyError`, and JSON parsing
errors at the dashboard boundary, returning a bounded unavailable payload with
no paths or tracebacks.

- [ ] **Step 4: Run endpoint tests to verify success**

Run the focused endpoint tests. Expected: PASS, with existing dashboard-context
tests still passing.

### Task 3: Build the historical analytics dashboard section

**Files:**
- Modify: `dashboard.py`
- Test: `tests/test_dashboard_context.py` and `tests/test_historical_analytics.py`

**Interfaces:**
- Consumes: the `/api/analytics` and `/api/analytics/forecasts` payloads.
- Produces: accessible analytics markup, an inline SVG trend chart, evidence
  coverage cards, and a bounded recent-forecast table.

- [ ] **Step 1: Write failing markup/payload rendering tests**

Test the HTML template and client-side rendering source for these stable hooks:

```text
#historical-analytics
#analytics-status
#analytics-summary
#analytics-trend
#analytics-coverage
#analytics-forecasts
#analytics-note
```

Assert the markup includes a semantic heading, table headers, an unavailable
state, the research disclaimer, and no order-placement control.

- [ ] **Step 2: Implement the analytics markup and styles**

Add the section below the existing context comparison card and before the
settlement/metric cards. Keep the current navy palette and card language. Add
responsive styles for:

- Four summary metrics.
- Two-column trend/evidence layout on desktop.
- Stacked cards and horizontally scrollable table below 760px.
- Visible keyboard focus and reduced-motion-safe transitions.

Use sentence-case user copy where new text is introduced. Preserve the explicit
disclaimer: “Descriptive archived evidence; not profitability proof.”

- [ ] **Step 3: Implement bounded SVG and table rendering**

Add JavaScript functions that:

- Render `available`, `pending`, `unavailable`, and empty states distinctly.
- Build SVG points only from finite trend values.
- Leave missing metric values as `—`.
- Escape archive-derived strings before DOM insertion, using text nodes or a
  dedicated escaping helper.
- Render no `TRADE NOW` state from analytics data.
- Show report age and last updated time.
- Keep recent rows limited to the server-provided 25 rows.

- [ ] **Step 4: Add analytics polling to the existing refresh cycle**

Fetch `/api/analytics` and `/api/analytics/forecasts?limit=25` during the same
dashboard refresh cycle. Analytics failures must update only the analytics
section and must not replace the existing live status payload or stale live
recommendation behavior.

- [ ] **Step 5: Run markup and dashboard tests**

Run:

```bash
python3 -m unittest tests.test_dashboard_context tests.test_historical_analytics -v
```

Expected: all endpoint and rendering-contract tests pass.

### Task 4: Document the analytics feature

**Files:**
- Modify: `README.md`
- Modify: `deploy/README.md`

**Interfaces:**
- Consumes: the final endpoint names, freshness semantics, and dashboard labels
  from Tasks 1–3.
- Produces: operator documentation for interpreting the analytics section.

- [ ] **Step 1: Add root README documentation**

Add a short section explaining the new analytics panel, the 25-row bounded
history, the 25-record/stride-5 trend calculation, the 36-hour report freshness
rule, and the fact that the metrics do not authorize trades or prove profit.

- [ ] **Step 2: Add deployment/read-only behavior documentation**

Document that the endpoints use the existing archive and saved reports without
network collection or writes, and that the feature can be released through the
existing code-only Oracle deployment workflow without state migration.

- [ ] **Step 3: Run documentation consistency checks**

Search for the exact endpoint names and confirm no documentation says the
analytics metrics authorize paper entries:

```bash
grep -R "api/analytics\|not profitability proof\|read-only" README.md deploy/README.md
```

### Task 5: Run complete verification and release checks

**Files:**
- Test: all changed Python, tests, workflows, and documentation

- [ ] **Step 1: Run the full regression suite**

```bash
python3 -m unittest discover -s tests -v
```

Expected: all existing and new tests pass.

- [ ] **Step 2: Run Python and shell verification**

```bash
python3 -m compileall -q *.py tests
bash -n deploy/*.sh
```

- [ ] **Step 3: Build and inspect the code-only bundle**

```bash
bash deploy/package.sh
python3 - <<'PY'
import tarfile

with tarfile.open("dist/kalshi-ubuntu.tgz", "r:gz") as archive:
    names = archive.getnames()
forbidden = ("data/", ".pem", ".key", "btc_15m_state.json", "btc_15m_state_archive.sqlite3")
bad = [name for name in names if any(token in name for token in forbidden) or name.endswith(".env")]
assert not bad, bad
assert "dashboard.py" in names
print(f"verified {len(names)} bundle entries")
PY
```

- [ ] **Step 4: Run the local health smoke check**

```bash
curl --fail --silent --show-error http://127.0.0.1:8765/health
```

- [ ] **Step 5: Commit the feature and push it to GitHub**

```bash
git add forecast_archive.py dashboard.py tests README.md deploy/README.md
git diff --cached --check
git status --short --branch
git diff --cached --stat
git log --oneline -5
git commit -m "Add historical analytics dashboard"
git push
```

After push, confirm the GitHub CI workflow passes on Python 3.11 and 3.12. Do
not run production CD until the protected environment and Oracle secrets are
configured.

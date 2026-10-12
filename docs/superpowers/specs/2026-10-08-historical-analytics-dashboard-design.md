# Historical Analytics Dashboard

## Status

Feature direction approved in chat. This specification must be reviewed before
implementation planning begins.

## Goal

Add a read-only historical analytics section to the existing BTC 15-minute
dashboard so the operator can understand forecast quality, evidence coverage,
and walk-forward progress without changing live model behavior or trade gates.

## Non-goals

- Changing the live predictor, feature vector, thresholds, or recommendation
  eligibility checks.
- Applying offline calibration to live probabilities.
- Presenting descriptive metrics as profitability proof.
- Adding real order placement or changing paper-account behavior.
- Shipping a third-party charting dependency.
- Returning the full SQLite archive or unbounded forecast history to the browser.

## User-facing behavior

Add an **Historical analytics** section below the current live prediction and
context-horizon comparison cards. It will contain:

1. **Performance summary**
   - Official timely forecast count and minimum target.
   - Accuracy with sample count and Wilson interval when available.
   - Brier score alongside the constant-50% baseline.
   - Walk-forward scored count, test target, and next collection milestone.
2. **Performance trend**
   - A dependency-free inline SVG chart for rolling accuracy and Brier score.
   - Each point identifies the end of a chronological window and its sample
     count; unavailable windows remain gaps rather than zeroes.
3. **Evidence coverage**
   - Timely/late forecast counts.
   - Missing ticker, missing feature, missing fresh quote, and interval-gap
     counts.
   - Settlement-observation delay summary.
4. **Recent forecasts**
   - A bounded table of the latest 25 earliest-eligible ticker forecasts.
   - Columns: issue time, ticker, model probability, actual result, timing
     status, market-midpoint availability, and settlement-observation delay.
   - Unknown labels and missing quote evidence are explicitly shown as unknown.
5. **Research disclaimer**
   - The section states that metrics are descriptive archived evidence and do
     not establish profitability or authorize paper trades.

The section refreshes with the existing dashboard polling cycle. A malformed,
stale, or missing report displays an unavailable state and does not imply that
validation passed.

## Backend architecture

### Archive query layer

Add focused read-only functions to `forecast_archive.py` or a small adjacent
module, following the existing state-file and archive-path conventions:

- `read_historical_analytics(state_file, limit=25) -> dict`
- `read_recent_forecasts(state_file, limit=25) -> dict`

The functions will:

- Open SQLite in read-only mode.
- Validate the positive integer limit and cap it at 25.
- Use the existing archive schema and official/timely eligibility semantics.
- Deduplicate forecasts by ticker using the earliest eligible issuance, matching
  the validation evaluator rather than selecting the best outcome.
- Return finite JSON-safe numbers only.
- Preserve `None` for unavailable outcomes, quotes, or delays.
- Return an explicit `status` of `available`, `pending`, or `unavailable` and a
  human-readable reason for non-available states.
- Never write to SQLite, live JSON state, validation reports, or model files.

The trend series will be calculated from archived chronological eligible
forecasts using a fixed 25-forecast window and a five-forecast stride. Window
membership is determined by issue order and labels already observed in the
archive; later labels must not rewrite an earlier window's prediction or
probability. A window with fewer than 10 scored records will expose `count` but
omit accuracy/Brier values. The recent-forecast query returns an object with
`status`, `reason`, and `rows`, so pending and unavailable states are preserved
for the API as well as the UI.

The saved validation summary is considered stale when its valid
`generated_at` timestamp is more than 36 hours old. A stale summary remains
available as a diagnostic source only when the payload explicitly reports its
staleness; it cannot be represented as current validation evidence.

### Dashboard API

Extend `dashboard.py` with:

- `GET /api/analytics`
- `GET /api/analytics/forecasts?limit=25`

Both endpoints are read-only, use the same state file as the dashboard, and
return compact bounded JSON. The analytics endpoint includes the saved
validation-summary freshness and the collection/evidence counts. The forecast
endpoint returns only the table rows needed by the browser.

The existing `/api/status` and `/api/context-comparison` responses remain
backward-compatible. API failures return an unavailable payload with a safe
message rather than a server traceback or stale success response.

## Frontend architecture

Use the existing inline HTML/CSS/JavaScript in `dashboard.py`:

- Add a semantic analytics section with headings and accessible table markup.
- Keep colors and labels consistent with the current research/wait state.
- Render the trend chart as inline SVG with text labels and a fallback table or
  summary for clients that do not render SVG.
- Escape all archive-derived text before inserting it into HTML.
- Avoid displaying a `TRADE NOW` or success-style message from analytics data.
- Show `last updated` and report freshness explicitly.
- Treat `pending` and `unavailable` as distinct display states.

## Data contract

The analytics payload will have this shape:

```json
{
  "status": "available",
  "generated_at": "2026-10-08T00:00:00+00:00",
  "report_age_seconds": 42,
  "summary": {
    "eligible_count": 237,
    "minimum_count": 100,
    "accuracy": 0.468,
    "accuracy_wilson_95": {"lower": 0.40, "upper": 0.54},
    "brier_score": 0.257,
    "constant_50_brier": 0.25
  },
  "walk_forward": {
    "scored_count": 74,
    "test_target": 100,
    "additional_scored_samples_needed": 26,
    "status": "insufficient_data"
  },
  "coverage": {
    "timely": 237,
    "late": 44,
    "missing_ticker": 7,
    "missing_features": 2,
    "missing_fresh_quotes": 79,
    "interval_gaps": 33
  },
  "trend": [
    {"issued_at": 0, "count": 25, "accuracy": 0.48, "brier_score": 0.26}
  ],
  "note": "Descriptive archived evidence; not profitability proof."
}
```

The implementation may add fields, but it must preserve these meanings and
never substitute zero for unavailable data.

## Error handling and security

- Reject invalid, negative, non-integer, or oversized limits.
- Treat missing archive tables, invalid JSON, non-finite values, contradictory
  counts, and stale reports as unavailable.
- Do not expose filesystem paths, environment variables, webhook URLs, or
  exception tracebacks in API responses.
- Ensure analytics queries cannot block the live writer for an unbounded period;
  use read-only connections and bounded result sets.
- Existing client-side expiry behavior remains in force for live status; the new
  analytics section cannot preserve an old `TRADE NOW` indication.

## Testing

Add tests covering:

1. Archive query read-only behavior and bounded limits.
2. Earliest-ticker deduplication and chronological trend windows.
3. Unknown outcomes/quotes remaining `None`, not fabricated values.
4. Missing, stale, malformed, and contradictory validation summaries.
5. `/api/analytics` and `/api/analytics/forecasts` responses without network
   calls or state mutations.
6. HTML rendering of available, pending, unavailable, and empty states.
7. JSON-safe finite numbers and escaped ticker/reason text.

Run the existing full unittest suite, Python compilation, shell checks, bundle
inspection, and GitHub Actions CI before release.

## Rollout

The feature is read-only and can be released through the existing gated Oracle
deployment workflow. No state/archive migration is required. If the analytics
query fails in production, the live runner and existing `/api/status` endpoint
continue operating; only the analytics section reports unavailable.

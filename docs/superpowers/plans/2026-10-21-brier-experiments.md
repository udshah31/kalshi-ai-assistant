# Brier Experiments Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Produce a reproducible offline comparison and evidence-based next step.

**Architecture:** A standalone CLI imports existing validation helpers, leaving
live paths unchanged. Immutable backup evidence supports two separately aligned
comparison panels; all fitted adjustments use past known outcomes only.

**Tech Stack:** Python standard library, SQLite, unittest, existing calibration.

**Spec:** `docs/superpowers/specs/2026-10-21-brier-experiments-design.md`

## Global Constraints
- Offline only; no deployment, live gate changes, dependencies, or fabricated quotes.
- 100 prior known labels, blocks of 25, unfinished final block retained.
- Earliest eligible forecast per ticker wins before outcome filtering.
- 2,000 seeded day-cluster resamples; fewer than five days yields no interval.
- Local commits only; raw data and full JSON reports remain ignored.

## Review Focus
- Delayed labels must not enter a block's fit; changing test labels cannot change predictions.
- Missing quotes must exclude only the quote panel; no synthetic quote substitution.
- Duplicate tickers and invalid/provisional outcomes reuse canonical validation rules.
- Empty history, missing outcomes and zero-denominator fits remain finite/explicit.
- Reproducibility, same-row baselines and day-level uncertainty must be inspectable.

### Task 1: Freeze evidence and inspect existing experiments
**Files:** ignored `data/brier-2026-10-21/`; final `docs/research/2026-10-21-brier-results.md`.
**Interfaces:** consume existing `backup_state.verify_backup`; produce verified local archive.
- [x] Download latest already-created Oracle snapshot and saved reports read-only.
- [x] Verify checksum/SQLite integrity; inspect existing calibration/context metrics.
- [x] Record snapshot identity and freshness, without substituting local stale data.

### Task 2: Implement the standalone comparison
**Files:** create `brier_experiment.py`, `tests/test_brier_experiment.py`; document CLI in results.
**Interfaces:** `compare_archive(path: Path) -> dict`, `_panel(records: list[dict], *, quotes: bool) -> dict`.
- [x] Add failing tests for chronology, delayed labels, same-panel populations, unscored rows,
  bounded analytic weights, empty inputs, UTC daily grouping and deterministic uncertainty.
- [x] Run `python3 -m unittest discover -s tests -p test_brier_experiment.py -v`; observe missing implementation.
- [x] Implement with existing `_read_archive`, `_forecast`, `_official_result`, `_market_midpoint`,
  `_fit_platt`, `_logit`, `_metrics`; no caller changes in live modules.
- [x] Add an archive integration test for deduplication, invalid outcome exclusion and read-only behavior.
- [x] Run targeted tests then full `python3 -m unittest discover -s tests`.
- [x] Run CLI on verified snapshot; preserve JSON and input SHA256 under ignored data/.

### Task 3: Interpret, review and hand off
**Files:** `docs/research/2026-10-21-brier-results.md`, plan and ledger.
**Interfaces:** consume comparison JSON and existing reports; produce human-readable findings.
- [x] Report fair same-population metrics, uncertainty, daily consistency and limitations.
- [x] Specify a frozen prospective candidate and confirmation criteria only if supported;
  otherwise explain the lack of evidence and the next bounded experiment.
- [x] Fresh reviewer checks code, tests and conclusions; fix important findings, rerun affected checks.
- [x] Commit only this work locally; record completed steps and exact test count.

## Execution decisions
User explicitly requested planning followed by continuation. Execute inline with
a final independent reviewer. Baseline at b97355a: 228 tests passed in preceding turn.
No production rollout is part of this plan; prospective collection is a later phase.

## Completion notes (2026-10-24)
Ran on the verified 2026-10-24 Oracle snapshot (kept in a local scratch directory, not
`data/`). Results: `docs/research/2026-10-24-brier-results.md`. No candidate is supported,
so no prospective candidate is specified. Full suite on main 244f813: all pass except 13
dashboard tests that need local port binding, which was unavailable in that sandbox.

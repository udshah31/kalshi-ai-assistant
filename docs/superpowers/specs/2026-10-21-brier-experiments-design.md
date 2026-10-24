# Offline Brier improvement experiment

The user wants a plan followed by execution to find better probability forecasts.
Current forward evidence does not beat constant 50%. First compare small adjustments
to frozen probabilities before changing features or live behavior.

Use Python standard library and existing validation eligibility, official settlement,
quote validation, calibration, and scoring helpers. A standalone research CLI keeps
this experiment out of dashboard, scheduled evaluation, and deployment paths.

Builder choices: use the latest verified Oracle backup; keep raw evidence locally
under ignored data/. Use rolling 100-known-label history and freeze each adjustment
for the next 25 eligible forecasts. Fit only on labels available strictly before the
block. Keep unresolved test forecasts in the block and report them as unscored.
Evaluate all eligible forecasts and quote-covered forecasts as separate panels;
every candidate within a panel shares the same test population.

Candidates: constant 50%, frozen model, historical YES frequency, probability
shrinkage toward 50%, and Platt calibration of frozen model probabilities. The
quote panel also includes issue-time market midpoint and a convex model/market
blend. Fit shrink/blend weights analytically by least squares on prior history,
clamped to [0,1]; choose zero weight if the denominator is zero. No grid search.

Report counts, Brier/log loss/accuracy, fold parameters and predictions, UTC daily
Brier, and paired Brier differences against constant and (when present) market.
Use 2,000 deterministic UTC-day cluster bootstrap draws for descriptive 95%
intervals, requiring at least five days; state that days may remain dependent.
Archive SHA256 and code version identify each run. Historical findings are
exploratory; no automatic winner selection or live promotion. A prospective
confirmation protocol must be recorded before any production changes.

Non-goals: new features, LLM tuning, live shadow deployment, financial claims,
threshold changes, schema changes, retraining production, sending messages,
pushes or merges. Preserve all existing worktrees.

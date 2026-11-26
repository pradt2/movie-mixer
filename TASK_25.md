# TASK_25 — Index immutable snapshots for ID and interval queries

Prerequisite: TASK_24. Scope: core model/query layer, point and range lookups only (not render planning sweep).

## Work
Create a small snapshot-scoped indexed view built once per immutable Project: track/clip/source dicts by stable ID/key, sorted nonoverlapping intervals per track with bisect for point/range access. Rebuild on a different snapshot, not every scrub; avoid mutation/external global cache. Reuse in measured hot query callers with minimal plumbing. Keep track order explicit for video winner and audio active selection; preserve half-open intervals, enabled/muted rules and current public API or adapter. No database.

## Acceptance
Seeded differential tests for 100+ valid randomly generated small projects (boundary ties, gaps, same- and cross-track intervals, multiple rates, muted/disabled, exact fractions). Compare ID and point/range results with old brute-force implementation; include undo/relink snapshot change. Report operation counts/relative time at 1k/5k/10k vs TASK_24. Run core and timeline tests. If indexes are slower for small cases, no semantic compromise.

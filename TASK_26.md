# TASK_26 — Plan video winners and audio level warnings by event sweep

Prerequisite: TASK_25. Scope: render/plan.py hot planning paths only.

## Work
Use sorted boundary events and active clips to select winner without rescanning all clips at every edge; expire end events before start events at equal times (half-open). Preserve exact global frame ownership and merge contiguous items as before, including quantised zero-length boundaries. Reuse track-priority index. For _peak_level, sweep audio boundaries and piecewise-linear gain/fade breakpoints instead of evaluating every item at every event; compare both sides of discontinuities where needed. If audio warning optimisation is not demonstrated by benchmark, leave it unchanged and document. Do not change graph generation or FFmpeg.

## Acceptance
Differential tests with brute force for random projects, equal endpoints, overlapping tracks, disabled/muted, fractional rates/fades, including zero-frame ranges. Compare complete resolved plan and warnings, not merely counts. Measure growth and operation counts for TASK_24 sizes. Run render-plan/core and picture-identity tests; old output semantics unchanged.

# TASK_12f — A bounded index of actual decoded presentation timestamps

Prerequisite: TASK_12e. Scope: media/frameindex.py and the timestamp probe underneath it, no GUI work.

## Work
A packet PTS is a seek hint, not evidence of a decoded picture. Use `ffprobe -show_frames` restricted to the chosen video stream and bounded read intervals, with integer best_effort_timestamp (PTS fallback only when appropriate) and the measured time_base. Preserve the asset-local axis, exact Fractions, sorting and deduplication. A probe may start at an earlier seek point, return tail packets when past end, or decode farther for B-frames; filter actual results to the requested window and distinguish proven coverage from uncertain/partial answers. Never return a speculative neighbour across an unread gap. Permit a bounded UNKNOWN result rather than claiming end of stream on budget exhaustion.

Keep frame window and probe budget small and explicit, with bisect, per-stream identity-scoped cache and bounded eviction. Correct existing span queries so they progress across uncovered windows or report partial/unknown (never keep requesting the same midpoint). Do not synchronously scan an entire title. A metadata probe is cacheable; do not probe per frame. Subprocess cancellation retrofit is Task 17.

## Acceptance
Use independently decoded PTS to check adjacent frames for CFR, real B-frames, VFR, offset and beginning/end/gap cases. Use fake probe results to check earlier seek, partial coverage, eviction, budget/termination, stream ends, identity invalidation, and no repeated midpoint loop. Run frame-index tests and record real cost for random/sequential queries on a long synthetic title (not just window count). No change to which composition frame is selected yet.

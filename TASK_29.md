# TASK_29 — Partition render pieces by actual graph budget

Prerequisite: TASK_28c. Scope: _chunk_ranges and planning of pieces; no disk strategy changes.

## Work
Partition at **global output frame boundaries**; map corresponding audio samples via exact lattice. Choose/test cuts based on actual planned items, active inputs, graph complexity and command byte length, not count of item ends. No zero-frame pieces. Rebuild candidate plan to verify it fits the configured hard budget before using it. A long overlapping set may exceed budget even in a single frame; refuse with actionable diagnostic (do not silently drop a stream), rather than invent staged mixing this task. Keep progress and phase semantics from earlier tasks.

## Acceptance
Synthetic stress: 100+ edits, short audio cut boundaries, many simultaneous audio tracks, long crossing clips, fractional rates, zero-frame candidate, maximum argument budget. Each accepted piece satisfies limits and pieces cover exactly original output indices; impossible overlap refuses. Forced chunks decoded against single pass near joins (audio samples and picture identities), no extra priming or holes. Run chunk/render tests. Record graph budgets and measured process count.

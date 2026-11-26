# TASK_21 — Make preview and decoration completion session-safe

Prerequisite: TASK_20. Scope: PreviewController, ClipDecorations, their cache/playback callbacks.

## Work
Audit immutable requests, job IDs/epochs and generation gates. Apply the same simple ownership rule as TASK_20 to preview and thumbnail/waveform decorations: no old render, progress, failure, partial cache result or decoration may attach to a new snapshot/asset/session. Cancel/coalesce obsolete requests and preserve bounded queue limits. Check recorded asset identity at request, decode and acceptance. Delete only owned obsolete files, never user media. Retain conservative all-edit invalidation for now; selective cache invalidation comes later.

## Acceptance
Deterministic delayed preview and decoration completions after relink, change track, New/Open, cache hit and no asset. Neither display, timeline nor current cache/pending work polluted. Correct request still completes and plays with existing controller. Test cancellation and coalesced work; run preview/cache/decorations tests.

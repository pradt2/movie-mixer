# TASK_32 — Pin in-use cache files and reuse unchanged stills/previews safely

Prerequisite: TASK_31. Scope: PreviewCache/FrameCache and UI cache lifecycle.

## Work
Implement explicit acquire/release pin for active playback and in-progress owned files; eviction excludes pinned entries, bounded unpinned LRU, budget refusal if all pinned. Release pins on replacement/close after player releases file. Use relevant-plan fingerprints for selective invalidation rather than drop all stills on every edit; still generation/epoch gates from TASK_20 remain active, and identity is checked again before use. Conservatively invalidate anything not proven equivalent. Cache ownership must not remove user files; cleanup tied to actual worker/player lifetime.

## Acceptance
Small budget tests with active playback, concurrent render and eviction; active file survives until release then evictable, non-cache files untouched, bounded state. Distant edit can reuse preview/still; overlapping edit/relink changes key; late old completion never overrides newest. Exercise undo/redo and cache hit during request. Run cache/GUI preview tests and close scenario.

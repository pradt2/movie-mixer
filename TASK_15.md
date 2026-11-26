# TASK_15 — Isolate import completions to their originating project

Prerequisite: TASK_14. Scope: MediaBinController and project New/Open/reset wiring.

## Work
An import request carries a unique job ID and session epoch. Increment epoch at session replacement and close; cancel old jobs, clear only their placeholders. Before any mutation (including a pending-path removal), require the job ID to be registered in the current epoch. Unknown, cancelled or late jobs are ignored. Import success uses the existing AddAssetCommand transaction; duplicate/refused import leaves history/dirty unchanged. Keep job/placeholder state transient, not saved. No worker rewrite.

## Acceptance
Deterministically deliver old success and failure after New/Open and a same-path reimport. New placeholder and new asset unaffected; cancellation of old job cannot affect new job. Current valid job completes exactly once and undo restores catalogue. Test both empty and populated target projects; use barriers/fake completion, not sleep timing. Run media-bin, import GUI and document tests.

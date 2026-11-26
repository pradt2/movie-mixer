# TASK_22c — Prove shutdown actually exits cleanly

Prerequisite: TASK_22b. Scope: repeated subprocess-based verification.

## Work
Add short deterministic independent offscreen subprocess open/edit/close harness with per-process timeout, captured logs, and 20+ iterations. Exercise synthetic media and actual owner cleanup; record every exit code (not just pytest count). Run actual desktop if available, including close while preview/still/import/render in flight. Keep tests independent of global Qt teardown. Run full offscreen pytest and ruff once with logged outputs.

## Acceptance
No segfault, bus error, hang, running QThread destroyed or leaked child across repeated checks; green count with nonzero exit is NOT green. If a crash remains, minimise reproduction, identify owner/thread and request a narrow fix before proceeding (do not rewrite all workers). Record exact environment and any unverified real-desktop behaviour. No product edits unless a tiny independently proven regression.

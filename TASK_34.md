# TASK_34 — Provide a stale-safe transaction seam for proposed edits

Prerequisite: TASK_33. Scope: project/document.py or thin document/controller API, tests and docs. No matching feature.

## Work
Expose read-only immutable EditorDocument snapshot, stable IDs and exact mapping already implemented. A worker may compute a proposal from snapshot S; application accepts a standard DocumentCommand only if expected session epoch and snapshot/revision still match current. Use DocumentSession's existing validation and one undo entry; no second editing path, no preferred source and no persistent correspondence format. Choose conservative revision invalidation (including undo/redo/relink/New/open) instead of rebasing stale proposals. A trivial manual move/split demonstrates seam. Reuse bounded cancellable background primitives for future analysis; do not actually implement matcher.

## Acceptance
Programmatic move/split uses same command boundary as GUI and undo restores all document state. Stale after edit/relink/undo/New or failure leaves document/history/dirty unchanged, even if other snapshot happens to compare equal. Valid proposal previews via same renderer. Document example with actual API code. Run document/commands/GUI tests.

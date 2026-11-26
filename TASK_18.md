# TASK_18 — Move relink probing off the GUI thread

Prerequisite: TASK_17c. Scope: MainWindow._probe_asset/_relink_asset_to and a minimal relink worker using already validated probe/relink logic.

## Work
The user chooses replacement path on GUI thread. Capture session epoch, document snapshot/current asset ID and old identity; probe, validate replacement and prepare RelinkPlan off-thread with cancellation. Return only result data. On GUI thread verify originating session and current asset still match (reject a stale plan rather than overwriting a newer relink); then use the existing atomic relink command. Close/New cancels pending probe. Existing modal decisions remain GUI operations; no nested wait/event-loop pumping to fake asynchrony. Reuse worker primitive; do not add a task framework.

## Acceptance
Hold fake slow probe while event-loop heartbeat and unrelated editing/navigation remain responsive. Cancel or replace project while probe pending; old success/failure has no effect. Successful and rejected relink preserve TASK_10 semantics (one undo, both catalogue and stream metadata restored). Test with real replacement fixture and run relink/document/GUI tests.

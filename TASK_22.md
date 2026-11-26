# TASK_22 — Give every worker a reliable, explicit stop/join contract

Prerequisite: TASK_21. Scope: worker lifecycle methods, not MainWindow close ordering.

## Work
Inventory each long-lived worker/process, Qt owner, player, timers and owned output. For each job owner expose an idempotent stop request that rejects new work, cancels active/queued jobs and eventually emits/returns terminal status. A wait timeout is a false/not-joined result, not success; ensure no QThread or process destruction while running. Join only after cancellation, never depend on __del__ or wait for a GUI callback on a blocked GUI thread. Remove destructors as primary cleanup; leave window-level orchestration to TASK_22b. Use already implemented runner and worker primitives, not a framework. Check each owner before moving to the next.

## Acceptance
Isolated controller tests with a blocked fake child/worker: cancel, terminal signal exactly once, timeout reports not stopped, eventual join reaps, repeated stop safe. New work refused. Execute these tests for import, render, preview, still/step, relink, source extraction and decorations. Record ownership table and APIs for TASK_22b. Do not claim global teardown fixed.

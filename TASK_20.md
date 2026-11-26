# TASK_20 — Make still/source-step request scheduling race-free

Prerequisite: TASK_19. Scope: StillWorker/StillController, SourceStepWorker/Controller and MainWindow._request_still. Do not redesign all preview workers.

## Work
Use a small synchronised latest-request mailbox (lock around take/replace; enqueue/wakeup coalesced). A request owns an immutable snapshot, session epoch, generation, asset identity and mode/clip ID. Cancel superseded expensive decode, deliver only valid current result; ignore stale failure as well as stale success. Increment generation on **every** request, including cache hit, and on edit, relink, New/Open, mode change, close and blank/no-source result. Adapt source-step accumulator preserving the intent to step N actual frames; do not simply drop increments. Cache never overrides an invalid identity. No new thread per click.

## Acceptance
Deterministic barrier test interleaves take and replace; latest request is not erased. A -> B cached while A is in flight; B remains displayed. Rapid source steps, project cleared, edit/relink and mode switch under outstanding work; current image and verified PTS only, Mark controls safe. Worker has one active and bounded pending job, cancellation observed. Run framegrab, viewer and GUI preview tests.

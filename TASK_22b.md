# TASK_22b — Assemble deterministic window close and session shutdown

Prerequisite: TASK_22. Scope: MainWindow shutdown order and owner wiring only.

## Work
Use tested stop/join methods: reject new work and invalidate epochs, stop timers, cancel all job owners, wait for actual termination, release multimedia sources/players, clean owned cache, destroy QObjects while QApplication alive. If a worker is not yet stopped, defer close asynchronously with clear status and retry when terminal; do not destroy running QThread. Keep shutdown idempotent and avoid blocking GUI waiting for a Qt signal that requires its event loop. Ensure test fixture teardown exercises this production path instead of hiding failures by manually destroying live threads.

## Acceptance
Close during import, relink, source extraction, still/step, preview, export verification and decorations using deterministic barriers. All workers stopped/reaped, no late UI callback, owned cache removed after release. Slow termination defers close; second close safe. Run close/lifecycle focused GUI tests. No changes to render/edit semantics.

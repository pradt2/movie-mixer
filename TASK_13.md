# TASK_13 — Build and test one cancellable subprocess primitive

Prerequisite: TASK_12m. Scope: one new stdlib-only helper (e.g. media/process.py or a lower-level tools package), unit tests. Do not migrate callers yet.

## Work
Design a small synchronous runner callable from a worker thread. Inputs: argv list, timeout, shared cancellation Event, optional stdout-line progress callback and bounded stdout/stderr capture policy. Outputs: exit status and captured diagnostics or explicit cancelled/timeout exception. Drain both pipes concurrently, including partial lines; timeout/cancel must be independent of output. TERM, bounded grace, KILL if needed, always wait/reap. No shell, Qt, endless accumulate, or arbitrary thread framework. Allow large binary stdout only with an explicit caller-supplied size limit; for large media measurements use stream processing or a file. Own only the process/pipes, not user destinations; callers clean their staging files.

## Acceptance
Use small Python child programs, no actual FFmpeg: no output + sleep, stderr flood > pipe buffer, mixed stdout/stderr and partial line, explicit exit failure, callback exception, cancellation before launch and during execution, timeout and a child ignoring TERM (Linux). Assert return within a generous explicit bound (e.g. two seconds plus termination grace), child reaped, bounded diagnostics, one terminal result. Check threads joined and no orphan processes. Run only helper tests and ruff. No other feature change.

# TASK_14 — Bound and genuinely cancel media imports

Prerequisite: TASK_13. Scope: media/importer.py, probe adapter and tests; retain public controller shape where possible.

## Work
Replace one daemon thread per file + never-assigned process with a bounded executor/queue (e.g. two workers, documented pending budget). Each job owns a cancellation Event passed through probe_file to the subprocess runner. Queued cancellation removes/skips work; running cancellation terminates and reaps ffprobe. Remove completed jobs from registry on every terminal outcome. Emit exactly one completion (success, failure, cancelled); explicit refusal when queue capacity reached. Provide cancel_all/wait or equivalent so owner can join later. Never publish partial probe metadata. Do not implement session-epoch UI logic yet.

## Acceptance
Fake probe with barriers and process ID: submit 30 jobs, prove concurrency bound, cancel queued and running, failure and success each clean registry, worker exceptions cannot strand jobs; cancellation kills a silent child within TASK_13's bound. Add one real ffprobe integration. Run importer/probe tests. Report configured limits; no Qt redesign.

# TASK_17c — Make source-window extraction cancellable

Prerequisite: TASK_17b. Scope: media/extractor.py only, adapter tests. GUI call site remains TASK_19.

## Work
Use TASK_13 runner, timeout and cancellation Event for source-window extraction and probes; use disposable staging and cleanup on failure/cancel; don't publish partial extraction. Preserve stream, interval, rate/format behaviour and source safety. Do not move UI yet.

## Acceptance
Fake silent and stderr-heavy child cancel and reap; no temp debris, genuine errors actionable. Real video and audio-only fixtures decode expected material; run extractor tests. Document remaining GUI-thread call site for TASK_19.

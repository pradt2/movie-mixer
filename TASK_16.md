# TASK_16 — Make render subprocesses cancellable through validation and publication

Prerequisite: TASK_15. Scope: render/exporter.py and relevant subprocess helpers in render/graph.py; no progress remapping or Qt shutdown yet.

## Work
Use TASK_13's runner for encoder, FFprobe, source-tail measurement, timing/format probes and validation launched by the render job. Pass the render job cancellation token end to end, including tool capability checks and chunk join. Do not buffer decoded whole-film PCM just to count bytes; stream/count or use bounded diagnostics. Preserve existing rendering semantics, source identity guard, consent, atomic staging and final cancel check. Map cancelled vs failed separately; clean owned staging files and previous intermediates, never user outputs. No unbounded wait on progress lines/stderr. If a helper cannot accept cancellation yet, name it and stop rather than claim complete.

## Acceptance
Inject silent, stderr-heavy, hanging encoder/verification and join tools; cancel within bounded time, no leaked children/artifacts, no publication even when cancellation occurs in final verification. Existing unmodified destination survives. Real FFmpeg cancellation, success and malformed/truncated output refusal tests pass. Run renderer/exporter tests; no changes to progress percentage policy yet.

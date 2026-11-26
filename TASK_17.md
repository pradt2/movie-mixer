# TASK_17 — Make still and decoded-PTS probes cancellable

Prerequisite: TASK_16. Scope: media/framegrab.py, frameindex.py, and the existing still/source-step worker call sites. Do not change sampling policies or worker scheduling.

## Work
Thread optional timeout and cancellation Event through all still and frame-index helpers to TASK_13 runner. Bound raw image output by validated dimensions and metadata output by probe window/capture limits. A superseded request or worker stop signals its active token; no partial result. Preserve actual source PTS, legal frame bounds and geometry; a cancelled request is not a decode failure. Existing worker ownership/generation improvements remain TASK_20.

## Acceptance
Silent fake ffprobe and ffmpeg children cancel/reap within bound; real normal fixture returns the same decoded frame ID and timestamp, no output debris. Run frame-index/framegrab/source-step tests. Report any remaining synchronous probe in these paths. No new GUI or graph changes.

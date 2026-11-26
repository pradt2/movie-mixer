# TASK_23 — Make render progress monotonic and meaningful

Prerequisite: TASK_22c. Scope: progress mapping in exporter and consumer display. Do not change chunk partitioning.

## Work
Assign deterministic phase weights for input/checking, chunk render (duration weighted), join and verification/publication, or use explicit phase labels with monotonic overall progress. Reserve 100% for successful validation and publication; encoder reaching end is not success. Clamp duplicate/backward FFmpeg timestamps and never report completed on cancelled/failed run. Ensure single pass and short range behave similarly. Callback remains responsive on silent subprocess through TASK_13/16.

## Acceptance
Fake sequence of progress timestamps and phases including short/long pieces, missing progress, duplicate/backward values, join, failed validation and cancellation. Assert in [0,1], never decreases, no 1 before successful publish. Real forced-chunk export and GUI status smoke test. Run progress/export tests.

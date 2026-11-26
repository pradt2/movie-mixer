# TASK_12m — Picture correctness checkpoint

Prerequisite: TASK_12l. Verification only unless there is a tiny isolated regression whose root cause is proven; otherwise create/request another small corrective task. Do not start the old TASK_13.

## Acceptance
- Run the corrected picture identity suite, direct source/GUI step tests, full offscreen pytest, ruff, and the three unattended workflows listed in TASK_12.md §8. Capture actual exit codes and counts. The current repo's baseline at TASK_12 was not accepted; the new picture suite must now be green. Independent fixture metadata, legal boundaries, source timestamp truth, exact range/full/still agreement, and explicit format limits are mandatory.
- Run a measured long-source navigation experiment: report first request and successive cached requests, input duration/codec, decode window/probe cost, and absence of whole-film GUI-thread scans. Record unresolved latency or shutdown issues separately for the next group; never label the latter as solved.
- Summarise decisions and remaining rounding/format limitations in docs/architecture.md and docs/progress.md with test references. Preserve original TASK_12.md as historical record.
- If no real desktop/media is available say so. No automation or alignment.

Stop if any picture-specific or new regression test fails. The sole allowed lifecycle carry-over into group B is the precisely identified, pre-existing exit-time Qt teardown defect or bounded cancellation limitation which that group is designed to fix: report the test assertions and actual process exit separately and list the reproduction. Do not silently classify a new assertion failure or unexplained hang as that defect. If uncertain, stop for review.

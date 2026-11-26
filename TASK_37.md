# TASK_37 — Final manual-editor readiness gate (before assisted matching)

Prerequisite: TASK_36. Verification and report only; do not add features or declare completion on failed checks.

## Acceptance
- Rerun full pytest with offscreen Qt, ruff, repeated shutdown checks and unattended export/A-B/long-project scripts. Report exact commands, exit codes, counts, skips, platform/tool versions. A failure requires a precisely scoped new correction, not altered acceptance expectations.
- Re-run TASK_08 manual recipe, plus media protection, dirty/undo/relink, linked-group split, differing source rates/sample rates, cut/gap/retime/VFR/offset/anamorphic boundaries, supported/refused formats, cache/reload/cancel/crash recovery, dense timeline and chunk joins. Compare the TASK_24 baseline and post-change measurements; document remaining storage, scaling and format limitations.
- Verify TASK_34's proposed-edit seam accepts current and refuses stale snapshot. No global reference asset or new feature.
- Human real-film smoke check on representative feature-length editions: import both, choose arbitrary audio/picture independently, seek/step/mark several cuts, monitor drift over minutes, listen to retimed speech/music, inspect aspect/colour, test preview/responsiveness/4K export and saved project reopen. Include at least one offset/rate discrepancy and ordinary DVD/HD mix. No real user media without permission; if unavailable, provide a concrete checklist and explicitly mark perceptual readiness PENDING HUMAN. Synthetic automation is not evidence of actual playback or sound quality.
- docs/progress.md: closed issues with regression test names, measured performance, remaining limits/deviations, actual GUI vs offscreen vs synthetic evidence. README and architecture consistent. Project files remain compatible; original media unchanged.

Stop: manual editor ready for assisted alignment only after successful checks and human review. No TASK_38/features until human acceptance.

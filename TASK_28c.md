# TASK_28c — Verify bounded picture-range decoding near a film's end

Prerequisite: TASK_28b. Scope: picture decode work only; don't alter the sampling contract.

## Work
Measure TASK_12j's phase-stable render and still on a long synthetic source with ordinary and long GOP, at far-apart seeks. If the verified fast path is already bounded, only add performance evidence/regression checks. Otherwise optimise only selecting a legal preceding frame and bounded preroll while restoring the same clip/global phase (TASK_12j). Use the decoded index; if sparse VFR/history cannot prove safe bounded selection, report explicit slow fallback/refusal rather than silently decoding whole film or picking wrong frame. No new storage scheme or rendering semantics.

## Acceptance
Same identities as whole/short reference at exact global indices, legal clipped bounds and known tail behaviour; seek near end does not repeatedly decode the entire film, quantified using subprocess/read measurements. Test real long-GOP fixture and VFR edge. Report measured time, bounds and fallback limits, no GUI-thread scans. Run picture/framegrab tests.

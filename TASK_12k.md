# TASK_12k — Composition stills agree with the real render

Prerequisite: TASK_12j. Scope: composition still path only; source navigation remains as in TASK_12g/h.

## Decisions
Use the same global output index and picture path as export, and the same legal geometry. The still's requested/mapped source time is a **prediction**. A timestamp seen upstream from fps does not prove which frame fps later chose. For composition stills, return decoded_source_pts only when that particular returned picture can be associated unambiguously with a measured input stamp (e.g. carry identity through selection). Otherwise use None and show "actual source frame not measured", while retaining the predicted time separately. Do not turn on showinfo for all normal exports or previews; verbose instrumentation belongs in diagnostics. Source marks require the separately verified source frame, not a composition-time prediction.

## Work
Use the phase-stable sampling logic from TASK_12j for the one intended output index including first/last frames and cuts. Handle gaps without decoding. Remove the false claim in _decode_picture that choosing a predecessor among pre-fps stamps necessarily identifies the returned picture. Do not change clip semantics or renderer graph anew. Bump cache version for changed still semantics.

## Acceptance
Compare real decoded lossless export identity to still at every tested global index, including nonaligned playheads, VFR, cuts, origin offsets and fractional retime. For every non-None actual PTS prove it matches the displayed frame ID. Reproduce the check at rate 1000/1001: reviewer saw image #26 (26/25 s) reported as 1 s; a wrong timestamp must never be returned. Verify normal direct source inspection still produces actual PTS; unknown composition metadata does not silently enable source marking. Run picture/framegrab/viewer focused tests.

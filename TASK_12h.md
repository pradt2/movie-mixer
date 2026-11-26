# TASK_12h — Make source stepping and marking truthful in the UI

Prerequisite: TASK_12g. Scope: existing SourceStepController, viewer and Mark In/Out wiring. Reuse the current worker for now; shared cancellation later.

## Decisions
One step moves to the previous or next actual decoded presentation timestamp from the **visible** source frame, independent of output FPS. Rapid steps are accumulated in order; only the newest request valid for the current clip and asset may update the viewer. Failed/unknown/pending picture disables marking. Mark In sets source_in to the verified visible PTS. Mark Out is **exclusive**: it sets source_out to the visible frame's PTS, excluding that frame (label/tooltips say so). To include that picture, step to the next one and mark Out, or use the known stream end for the last picture via existing numeric editing. Do not invent an end beyond known bounds.

## Work
Wire the direct source decoder and bounded decoded index, not the composition-resampled path. Store requested target separately from last displayed image+PTS. Gate by generation, clip/stream/asset identity and pending status; applying a mark uses the current document transaction and leaves other clips unchanged. Do not redesign the viewer or add timeline features. No engine shutdown rewrite in this task.

## Acceptance
Mock a delayed worker deterministically: three rapid forward steps, step back, stale success/failure after clip change, and unknown PTS. Confirm only current image/PTS appears and no edit while pending or failed. Real synthetic source integration: distinct identities and actual PTS at forward/back, retimed VFR clip, Mark In and exclusive Mark Out with undo. Run viewer/source-step/GUI editing tests; stop on a precisely described blocker rather than approximating marks.

# TASK_12g — Decode one source picture with its own actual timestamp

Prerequisite: TASK_12f. Scope: source-frame decode in media/framegrab.py. No composition resampling, GUI or navigation changes.

## Decision
Source inspection means inspecting the source stream independently of clip rate, output FPS, composition horizon and current clip in/out. The stream's decoded picture is authoritative; a pre-fps showinfo tap followed by a guessed lookup is not evidence about an output picture chosen after resampling.

## Work
Use the chosen decoded timestamp as a seek aid, decode the selected actual frame without any fps/tempo/tpad/concat, and return one RGB image plus that **same frame's** measured PTS. Ensure accurate input seek and output of precisely one frame; use a decoder-reported timestamp from the actual selected frame (e.g. showinfo on a one-frame selected path, no filters that discard/reorder pictures after the tap). Reconstruct the asset-local PTS from observed time-base/seek semantics, test offset and decimal seeks. If the actual decoded frame differs from the request, report actual PTS; if correspondence is not provable, report unknown or error. Keep display geometry from TASK_12c and asset identity checks. Do not build a composition still plan merely to read a source picture.

## Acceptance
Test frame identity AND PTS against independent decoded fixture mapping at requested exact and in-between PTS, including B-frames, VFR and nonzero origin. Test source stepping at four different clip rates (1, 25/24, 4, 1/4); same source PTS means same image. Inspect outside the clip bounds but within the stream; reject out-of-stream. If a timestamp parser fails, test explicitly unknown; normal fixture must return verified PTS and matching pixels. Run framegrab tests. No guessed timestamps labelled as measured.

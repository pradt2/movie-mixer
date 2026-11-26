# TASK_12d — Make inspecting a playhead distinct from quantising a render range

Prerequisite: TASK_12c. Scope: pure lattice, still-plan selection and preview horizon. No FFmpeg stage or rate changes.

## Decisions (reviewer)
- All render intervals are half-open. Range [a,b) emits global frame indices ceil(a*R) through ceil(b*R)-1. Frame i is sampled at i/R; the winning clip is the topmost enabled clip containing that instant.
- A still at a playhead t within [0,composition_end) shows the frame currently covering t, i.e. floor(t*R). A separate helper for this inspection must NOT repurpose the ceil boundary function. This avoids showing future footage, and a playhead one tick before the end must not ask for a nonexistent next frame.
- A positive composition starts with frame 0; a final partial frame is emitted. A still at/after the exact composition end refuses; an explicitly extended export emits black/silence outside the composition. A requested subrange with no global frame is refused by preview, not padded with a fake one.
- Keep TASK_11's sample lattice and audio fades untouched.

## Work
Make the ceil boundary and floor playhead helpers explicitly named/documented in render/plan.py. Have still_plan_for build the single frame's actual lattice interval, and adapt its caller/tests to the inspection rule. For preview compute its start/end in global frame indices and cap to the composition horizon. Do not change playback rate or resampler flags. Update docs and cache semantics if observable behaviour changes.

## Acceptance (pure tests first)
At 25 fps: output [0,0.01) has index 0; [0.001,0.02) has none; [1.5,3) has indices 38–74. A cut at 1/3 s switches at index 9. Playhead 0.039 s shows index 0; 0.04 s index 1; playhead 0.079 s in a 0.08 s composition shows index 1; 0.08 s refuses. A clip hidden by a higher enabled clip should not win the overlapping frames. Test muted/disabled tracks and gaps. Tests should name the two different questions; do not replace ceil with floor everywhere. Run plan/framegrab/preview unit tests.

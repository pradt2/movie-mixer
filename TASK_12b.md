# TASK_12b — Repair the picture acceptance tests, not the renderer

Prerequisite: the paused TASK_12 commit `52f586e`. TASK_12.md is a historical record, not an instruction to repeat the old task. The reviewer has superseded its outstanding requirements with this and the following tasks. Read AGENTS.md.

## Scope
Only edit the picture test and fixture code (and progress notes). No production fix, tolerance increase, skip, xfail, or change to the picture contract in this task.

## Work
Repair these independently identified problems in `tests/test_picture_identity.py`:
- Fast-forward: five source seconds at 4x on a 25 fps timeline give ceil(125/4) = **32** frames, not at least 40.
- Cut: source frame 100 at 25 fps is 4 s. Place B at `F(100,25)` to `F(150,25)` (6 s exclusive), not 100–102 seconds. At output 2 s its frame is 100. Use a later legal out point if necessary; do not weaken the edge assertion.
- Gap: use a 100-frame source, clip A [0,1) s and B [2,3) s placed at output 2 s; B begins at source frame 50. Expect 25 picture frames, 25 black frames, 25 picture frames.
- Still-vs-export: `total_frames` belongs to RenderPlan, not Quantisation.
- VFR: 100 frames at 25 fps plus 50 at 12 fps last about 8.167 s, not 9 s; select [3,8) s and verify actual PTS and legal bounds.
- Tail: current [40/25,50/25), 5/4 speed, 25 fps yields eight samples at source times 1.60,1.65,...,1.95 s. Exact expected identities are **[40,41,42,43,45,46,47,48]**, not a forced last frame 49. Retain this as a sampling test. Add a separate genuinely held-last-frame case: [48/25,50/25) at rate 1/2, output 25 fps, expected [48,48,49,49].
- Beyond end: [1.5,3) at 25 fps has indices 38..74 (37 frames), of which 25 black after index 49. First identity 38, not 37.
- Nonzero origin: 100 pictures start at 2.5 s and end at 6.5 s exclusive. Choose [3.5,6.5), not [3.5,7.5). Record misleading probed end (reviewer observed 9 s on FFmpeg 7.1); TASK_12e addresses it.

Make the test oracle independent of the render plan's sampling helpers: use integer global frame indices, exact clip mapping, a measured **decoded presentation** timestamp list (ffprobe -show_frames, best_effort_timestamp with time_base), and bisect. Use known uniform-colour frame identities and check actual fixture durations, decoded IDs and PTS before using them. Verify B-frames really exist (lossless x264 can disable B-frames even with -bf 2); select a high-quality lossy setting and check pict_type B if necessary. Check SAR and VFR independently. Cache fixture metadata once per unchanged file, never probe it per expected frame. For the gap, colours cannot confuse frame zero with black.

Two distinct tests: (a) exact mathematical reference pick, (b) still/range/full render agree on actual identities even in a documented one-frame backend rounding exception. Retain the existing strict bounded residual tests; do not expand exceptions.

## Acceptance / stop
Run only `test_picture_identity.py` and fixture checks. Report each remaining failure with actual and expected IDs, trace and reproduction; these are legitimate red baseline tests for subsequent tasks, not reasons to edit production. Report count, environment, commands and wall time. A test failing on invalid fixture bounds, missing API members or wrong arithmetic is not a valid baseline.

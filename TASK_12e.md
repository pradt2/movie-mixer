# TASK_12e — Establish truthful source bounds for nonzero-origin video

Prerequisite: TASK_12d. Scope: metadata fallback and source-bound validation; not frame selection or broad format support.

## Reproduction
A 100-frame 25 fps lossless Matroska made by make_identity_offset starts at 5/2 s; real stamps end at 323/50 s (6.46 s), material end 13/2 s. Reviewer observed format duration 6.5 s and the parser recording a 9 s end (container_start + duration). Do not assume the format.duration convention is universal.

## Work
Compare raw ffprobe format/stream fields and actual decoded last frame/duration for zero-origin and offset MKV and MP4. Preserve known exact stream durations/starts and measured offsets. Keep a conservative fallback explicitly marked as such rather than fabricating an exact stream end or applying a universal subtraction to all formats. For the demonstrated Matroska case use verified metadata/stream duration tag if valid, or a bounded tail probe to establish the end. If cannot prove the end, keep conservative metadata. For a render reaching a conservative tail, inspect a bounded decoded frame window near the requested end before encoding, similar in intent to TASK_11’s audio tail check. Use actual last frame duration if available; distinguish normal sub-frame rounding from a significant shortage. If the selection needs absent or unmeasurable tail material, refuse with a reason rather than repeat a frame for seconds. Do not make a conservative upper bound an exact one. Distinguish a real frame's duration from its PTS; VFR nominal frame rate is not proof of the tail. Existing project files must still load; if semantics of imported metadata change, explain re-probe/relink for old records. No project schema redesign.

## Acceptance
- Actual decoded timestamps and frame extent match the offset fixture on supported builds. [3.5,6.5) is legal; [3.5,7.5) is invalid/refused before publication because data end is 6.5 s. Do not fix by shifting timestamps or deleting checks.
- Zero-origin, per-stream offset (audio/video), ordinary MP4 and missing metadata regression tests pass. Retain explicit conservative-status distinction.
- If measured tool behaviour differs from the review, record raw JSON and measured last frame, not guessed formula. Stop rather than generalise an unverified fix. Run probe and relevant bounds/placement tests.

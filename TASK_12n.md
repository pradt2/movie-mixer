# TASK_12n — Fine-timescale streams must not lose a held last picture

Prerequisite: TASK_12m (checkpoint passed: 926 passed / 0 failed / 0 skipped). Scope: `render/plan.picture_tick` and the approximate fallback in `render/graph.picture_filter_chain`. No audio, no `ui/`, no project format, no new features.

Found at the TASK_12m checkpoint; named in `docs/architecture.md` item (a) and `docs/progress.md` (TASK_12k entry). Reproduced by the orchestrator with `/tmp/opencode/m12k/fine_tail.py`.

## Defect (measured, not inferred)

An identity MP4 written with `-video_track_timescale 50000000` has container tick `1/50000000`, so `picture_tick` refuses an exact filter tick (`per_second` exceeds `MAX_PICTURE_TICK`) and the picture chain takes its decimal fallback. Composition: clip `[1, 6)` at rate `1000/1001` on a 25 fps output → `ceil(5.005 · 25) = 126` output frames. The lossless export writes all 126, ending at picture #149. The **still of global index 125** — a tail hold of #149, whose real PTS is `149/25`, sampled at `6001/1001` — raises `StillError` ("Input file … Output file is empty"). Cause evidence (`/tmp/opencode/m12k/fine_chain.py`): the fallback chain is `setpts=PTS/0.999000999,fps=round=up:fps=25/1:start_time=0.250250015,tpad=stop_mode=clone:stop=1,…`; the tap sees 6 arrivals, the last at 0.214996 s, **all strictly before** that non-zero decimal `start_time`, so FFmpeg emits `frame=0` and `tpad` has nothing to clone.

This is not the reviewer's permitted tie residual: the two paths do **not** pick the same picture at the same global index — one of them produces nothing.

## Work

1. Re-measure first (`--showinfo` in the diagnostic, plus the repro script): confirm which stage drops the frame and what the decimal `start_time` costs on a one-frame item.
2. Prefer removing the need for the fallback: the exact tick should be derived from the **required precision** (output rate and the tempo's denominator, e.g. `25 · 1001 = 25025` for this case), not from the container's raw time base, whenever `settb` can carry it and the stamps stay whole. Verify with `showinfo` that the retimed stamps arrive whole and at the intended spacing, and check the worst tie error for the chosen tick.
3. If a shape genuinely admits no exact tick, **refuse it by name** (naming stream, rate and why) rather than emitting a chain that writes nothing or fewer frames than the plan promised. Do not widen any tolerance; `RenderJob.inexact_picture` stays as the honest report for whatever remains approximate, and an approximate chain must still emit exactly the promised frame count.
4. Keep the head/tail hold rules, the half-open slice enforced on the source axis, the ceil lattice and the single head-pad rule from TASK_12j/12k. Bump `render/cache.SEMANTICS_VERSION` if produced pixels or range semantics change, and say why.

## Acceptance

- The repro becomes a test: at global index 125 of that composition the still and the export show the **same** picture (#149), with the still's reported source PTS matching that picture's own decoded stamp — or the request is refused with a stated reason, never a wrong or empty result.
- New/extended tests cover: the fine-timescale tail hold; a fine-timescale **one-frame range**; an ordinary stream that still gets the exact chain unchanged; and the refusal path naming its reason. Cross-check against `tests/picture_reference.py` / `media_fixtures.picture_facts`, never against the code under test.
- Run: `tests/test_picture_identity.py`, `tests/test_picture_reference.py`, `tests/test_framegrab.py`, `tests/test_render_plan.py`, `tests/test_render_graph.py`, `tests/test_render_export.py`, `tests/test_render_preview.py`, then `python scripts/diagnostics/picture_phase.py --case all --stills` (must stay 18/18 `identical / identical / 0`, referee unedited), then `ruff check .`. Report exit codes and wall times.
- Do not touch `ui/`, audio, `core/`, `project/`, `media/frameindex.py` or `media/frameprobe.py`.

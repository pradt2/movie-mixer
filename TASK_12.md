# TASK_12 — Truthful frame inspection and a common picture sampling contract

Prerequisite: TASK_11. Precise visual inspection is foundational for later content matching.

## Review findings (baseline 0c6b8f1)
- `ui/main_window.py:_step_source_frame` changes source_pts via dataclasses.replace but retains the old RGB image; its note explicitly says "unchanged picture". _mark_source_edge then trims using that timestamp. This is not frame inspection.
- `_timestamps_for` synchronously probes every packet PTS on the GUI thread, and _step_source_frame rebuilds a full list of SourceFrame objects per click. On a film this can freeze the editor and is unnecessarily linear.
- `media/framegrab.py:source_frame_times` enumerates packet PTS, not decoded frames. Its `extract_composition_still` guesses decoded_source_pts from neighboring packet timestamps after a separate decode. Packet/frame correspondence is not guaranteed. A timestamp and image must be obtained from the same decode, or the timestamp marked unknown.
- `still_plan_for` constructs a fresh one-frame plan starting at an unquantised requested time. `render/graph.py:_picture_item` rebases PTS and restarts fps at each seek/item. Retimed/VFR frames near a cut or within a clip can differ between a still, short range and whole export even when their planned output frame index is identical. `render/preview.py:preview_range` uses ceil horizon count whereas build_plan uses rounded boundaries; it can invent a final frame.
- Rounding a boundary down can map a source_start earlier than clip.source_in; rounding up can map source_end beyond clip.source_out. Current graph does not explicitly enforce these limits. Picture format normalisation resets SAR to 1 after scaling without an explicit display-aspect contract; anamorphic DVD and interlaced material need attention before real film work.

## Implement
1. Document ONE output-frame lattice and rule for deciding the winning clip and source sample at an arbitrary output index. Keep rational times and global quantisation; choose a coherent round/floor/ceil edge rule and make planner, viewer, preview and export use it. A subrange must use the same global indices and phase as a full range. Clamp sampling to legal source material or use a documented gap/edge policy; never silently sample excluded footage. Explicitly decide sub-frame-length compositions and the final partial frame, rather than contradicting preview/export.
2. Use decoded presentation timestamps (e.g. ffprobe -show_frames with best_effort_timestamp/time_base or decoder-reported PTS) for exact frame navigation; keep packet/keyframe indexes only as seek aids. Fetch a bounded neighborhood asynchronously, cache immutable sorted timestamps by asset identity and stream, use bisect for neighbors. Avoid scanning the whole film synchronously. For VFR/B-frame sources choose and test a presentation rule; do not label an estimated timestamp as actual decoded PTS.
3. Implement source-frame stepping as a real asynchronous source-frame decode showing the selected picture and matching actual PTS. Distinguish requested vs decoded times and source vs composition inspection. Disable Mark In/Out while a newer frame is pending or decode failed; only commit an actual verified source position. Specify half-open out-edge semantics (marking last included frame vs exclusive boundary).
4. Share picture sampling/filter geometry code between render and still paths. Seek with appropriate preroll and retain PTS phase; compare decoded output at global indices rather than only color regions. Correct display aspect ratio for non-square-pixel material, preserve letterboxing and define explicit handling/refusal for interlacing and HDR/unsupported color spaces. Do not silently claim faithful 4K HDR output from an SDR-only pipeline; full color management can be deferred if refusal is clear.
5. Bump render cache version. Maintain ordinary editor behaviour; no correspondence algorithm or new editing mode.

## Acceptance
- Fixture with uniquely identifiable frame numbers/colors, B-frames, nonzero start, VFR and rates 25/24 and 1001/1000. Compare exact frame identity at multiple nonaligned playhead times, just before/after cut, after 2nd seek, with whole export and range render; use a lossless output fixture or robust pixel comparison.
- Source-step forward/back changes the actual displayed image and reported PTS together, including rapid clicks and stale completion. Marking a source edge uses the visible verified frame, not a future pending result.
- Test half-frame boundaries, final frame, a source slice ending at its last legal frame, gap, range beyond horizon and anamorphic 720x576 material. No source-boundary leaks. Verify documented handling for interlaced and HDR input.
- Measure navigation responsiveness on a long synthetic source and prove no full-film timestamp probe on GUI thread. Update tests, docs and progress.

## Implementation questions for the reviewer (from the implementer, measured)

Recorded here rather than decided silently, because two of them change what the
composition *promises* and one is a defect I have not resolved.

### 1. Decided: one lattice rule is "snap forward" (ceil), everywhere

`Quantisation.frame_index(t) == frame_count(t) == ceil(t·rate)`, plus
`frame_span`/`sample_span`/`sample_count`. Naming a frame and counting a length
were two different roundings before, and the difference was observable (a
composition 0.01 s long had 0 frames for the exporter and a 1-frame range for the
preview). Cost: a clip edge never snaps backwards, so it moves forward by up to
one frame. Tests that pinned the old rule were corrected deliberately
(`tests/test_render_plan.py`), not papered over.

### 2. Decided: `fps=round=up` — the old chain did not implement its own contract

The plan says an output frame shows "the last real source frame at or before the
sampling time". Measured with fixtures where frame *N* carries *N* as a pixel
colour (so an off-by-one is a different number, not a fuzzy match), over 16
rate/slice/output-rate cases, 2410 rendered frames:

| sampling stage | frames showing a picture other than the contract's |
| --- | --- |
| `fps` default `round=near` (what the repo used) | **777 / 2410** (32%) |
| `fps=round=up` (now) | **26 / 2410** (1.1%) |
| `fps=round=down` | 1593 / 2410 |

`round=near` resolves a grid point to the *nearest* frame, so a 4× speed-up never
showed the first frame of the slice (`[1,5,9,13…]` instead of `[0,4,8,12…]`) and
24 fps material in a 25 fps timeline showed the frame after the planned one. The
26 remaining frames are all at tempos that send retimed source timestamps between
the container's own ticks (`1000/1001` at 25 fps output: 24 frames; `25/24`→
`30000/1001` and `3/2`→`24000/1001`: 1 frame each). Two questions:

- Is "one frame later than the plan names, at tick-inexact tempos" acceptable as
  documented behaviour, with the viewer reporting the source position **measured
  from the decode** (which is what it does now), or should the plan model
  FFmpeg's tick quantisation so its prediction is exact? Modelling it makes the
  plan depend on FFmpeg's internal rounding; not modelling it keeps 1.1% of
  frames one place off at those tempos.
- Is `round=up` the right contract at all, or should the rule be "nearest" and
  the *plan* document that instead?

### 3. Decided: a still at/after the end of the composition is refused, an export range past it is explicit black

Asking the viewer for a frame the composition does not have says so (naming the
end); asking the planner for a longer range answers with explicit black items,
which is the behaviour documented since TASK_05 and now re-tested so the two do
not drift. `prepare_preview` refuses a range holding no output frame.

### 4. Defect found and fixed: an unbounded loop that looked like a hung export

`tests/test_framegrab.py::test_still_agrees_with_the_exported_frame_at_the_same_index`
did not finish. The report first read as "FFmpeg never exits", which was wrong:
the process never even started. The hang was in
`media/frameindex.py::_search` — it moved its search position to the edge of the
window it had just read *before* asking that window the question, so when the
answer was inside the window it re-asked a question the same window could never
answer, without ever spending another probe (the probe budget could not end it).
It was reached from the render path, because placing a picture item's sampling
grid asks the same question (`first_frame_of` → `at_or_after(source_in)`).

Two more defects came out of the same investigation:

* `FrameIndex.frames_in` used `start - 1` as its lower bisect bound — a whole
  **second**, not an epsilon — so a span query returned frames from outside the
  span. Caught by a test that compares the span against the stream's own stamps.
* Measured FFmpeg 8.1.3 behaviour: `ffprobe -read_intervals a%b` **past the end of
  a stream answers with the stream's tail** rather than with nothing — asking a
  6 s Matroska clip for `20%40` returned 25 packets from 5.00 s to 5.96 s. Taken
  at face value that says "there are frames at 20 s" about frames nowhere near
  it, so the end of the stream is never recognised and every search spends its
  whole probe budget. `FrameIndex._probe` now keeps only the stamps inside the
  window it asked for.

Both the infinite loop and the past-the-end read are covered by
`tests/test_frame_index.py` (5 tests, 4.97 s), including the cost claim: one
question costs one probe, and a question with no answer stops inside
`MAX_PROBES_PER_QUERY`.

Status of that defect: **fixed**, together with two more found in the same
investigation (§5). `tests/test_framegrab.py` no longer needs the `--deselect`
workaround, and the whole file now runs green:

```
QT_QPA_PLATFORM=offscreen pytest -p no:randomly -q tests/test_framegrab.py
```

One diagnosis note that cost time and is worth keeping: the suite randomises test
order unless `-p no:randomly` is passed, and this hang was order-dependent only in
the sense that *some* earlier test filled the `FrameIndex` window cache and
therefore changed which question looped. Under random order it looked
intermittent; with a fixed order it was deterministic in one second. Diagnose
apparent hangs with `-p no:randomly` and `faulthandler`, and read the traceback
before blaming the subprocess — the first report here ("FFmpeg never exits") was
wrong because the process had not been started yet.

A note on a check that proved nothing, recorded so it is not repeated: running
`tests/test_framegrab.py` from a `git worktree` at HEAD (`e2173f3`) to see whether
the hang predates this task failed at collection, because the editable install in
`.venv` puts the *working tree's* `video_editor` on the path — the old tests
imported the new `framegrab`. Either run that comparison with `PYTHONPATH` pointed
at the worktree's `src`, or use a separate virtual environment.

---

## State of this task at commit (work deliberately paused for review)

Answering question 2 above (`fps=round=up`) and question 3 are implemented;
question 1 is a decision the reviewer may want to revisit.

Changed or added, not yet run as a green suite:

* `render/plan.py` — one ceil lattice (`frame_index`/`frame_count`/`frame_span`/
  `sample_span`), `PictureSampling` (grid, seek, read bounds, head/tail holds),
  `picture_sampling`/`picture_preroll`, tick-aligned seek helpers.
* `render/graph.py` — one `picture_filter_chain` shared by export, preview and
  stills; `fps=round=up`; `tpad` head/tail holds; measured time-base and
  first-frame lookups; `RenderJob.unanchored`.
* `render/exporter.py` — passes those lookups to every graph (including chunks),
  reports unanchored picture streams as a warning; `render/preview.py` refuses a
  range holding no output frame; `render/cache.py` `SEMANTICS_VERSION` → `"4"`.
* `media/framegrab.py` — stills decoded through the shared chain,
  `extract_source_still`, and a still asked for at or past the end of the
  composition is refused by name; `media/frameindex.py` — new bounded
  packet-timestamp index (20 s windows, ≤6 cached, bisect neighbours) so
  navigation never scans a whole film. Three defects were found and fixed in it
  (§4), with `tests/test_frame_index.py` added: 5 tests, 4.97 s, covering the
  infinite loop, the past-the-end read, the span bounds and the probe budget.
* `ui/` — asynchronous source stepping (`SourceStepController`) that changes
  picture and reported PTS together, accumulates rapid clicks, discards stale
  completions, and disables Mark In/Out while a frame is pending; the viewer says
  what is known about each picture.
* `tests/test_picture_identity.py` + identity-colour fixtures in
  `tests/media_fixtures.py` (constant rate, B-frames, nonzero start, VFR,
  anamorphic 720×576) — written, **not yet passing**. A partial run before the
  corrections failed on test-authoring errors of my own (a missing fixture import,
  a `stream_timing` argument, expectations that assumed the old pick rule and one
  wrong frame count); those are corrected, and the exactness-vs-tolerance rules in
  §2 were applied. **First complete run since: 9 passed / 10 failed in 186.13 s.**
  The failures are not yet diagnosed; the one I did read asserts that a scaled
  720x576 anamorphic picture leaves no black corners in a 1280x720 output, and it
  found black corners — either the display aspect is not honoured by the shared
  chain, or my expectation for that pairing is wrong.

### 5. Corrected: the "measured source position" gap was my parser, and it is fixed

I earlier reported that measured source positions worked on the source-stepping
path but not on `extract_composition_still`. That was wrong, and the real finding
is worse in one way and better in another: **neither path ever measured**. The
`showinfo` line is space-padded (`n:   0 pts:     10 pts_time:0.01`), and
`_decoded_timestamps` split the line on single spaces and looked for a token
starting with `pts:`, so it matched nothing and every measured position came back
`None`. Nothing was fabricated — but the acceptance item "a timestamp and image
from the same decode" was not actually met, and no test had caught it because the
expectations were written to match the implementation's `None`.

Two more defects came out of the same fix:

* The stamps `showinfo` prints are **relative to the requested input seek**
  (measured: `-ss 0.07` on `steps.mkv` prints `pts_time:0.01, 0.05, 0.09` for the
  real frames at 0.08, 0.12, 0.16), while the code compared them with
  `sample_start + seek_source` and then added `seek_source` again — double
  counting, and wrong for an asset whose stream does not start at 0. The
  reconstruction is now `file_seek + input_start + stamp`, snapped back onto the
  container's tick grid to undo the nanosecond error of the decimal seek text.
* The tap already sits before resampling (`showinfo` is the first filter of the
  still decode's chain, so it reports the picture's own PTS, not the output-time
  one). No "measure here" mode is needed for stills. What remains open is a
  **scope** question for the reviewer: should the export and preview paths carry
  the same tap — they use the same chain, so the per-frame source positions of a
  whole render could be reported — or stay untapped and let stills be the only
  measured view? Tapping a whole export means a `showinfo` line per frame.

Measured after the fix: `tests/test_frame_index.py` + `tests/test_framegrab.py` =
**26 passed in 20.62 s**, `ruff check .` clean. Three of the eight earlier
`test_framegrab.py` failures were my `FrameIndex` answering a neighbour from a
cached window it had not actually read across (fixed with a "contiguous reach"
rule); one was an expectation asking for a frame past the end of the composition
(§3 refuses it by design, and the test now checks both the gap and the end); two
were my own test expectations that assumed a frame existed where the fixture's
real millisecond-rounded stamps say it does not (measured stamps around 9 s of the
VFR fixture: 8.75, 8.833, 8.917, 9.0, 9.083, 9.167, 9.25 — a nominal 1/12 step
"should" be 9.0833 and the container carries it at 9.083, so a query at exactly
9 + 1/12 is *past* that frame).

Earlier measured state, before this fix, for the record:
**180 passed** in 68.9 s across `tests/test_render_graph.py`,
`test_render_plan.py`, `test_render_preview.py`, `test_render_export.py`,
`test_frame_index.py`, `test_render_cache.py`, `test_render_exporter.py` (that run
included the new `time_base_of`/`first_frame_of` wiring in the exporter, so the
graph is being built with measured timing in those tests).
The whole suite has not been run since these changes, and
nothing here was verified through a real GUI window (`QT_QPA_PLATFORM=offscreen`
only).

### 6. What is still failing, by name

`tests/test_picture_identity.py` — 9 passed / 10 failed (186.13 s), all after the
§5 fix, so these are the real remaining questions, not the parser bug:

```
test_every_output_frame_shows_the_planned_picture[rate5-4x fast forward-0]
test_a_range_render_shows_exactly_the_frames_of_the_whole_render
test_a_still_is_the_frame_the_export_writes_at_that_index
test_a_still_after_a_cut_shows_the_next_clip
test_a_stream_that_does_not_start_at_zero_keeps_its_own_origin
test_variable_frame_rate_material_uses_the_real_timestamps
test_a_slice_ending_on_the_last_legal_frame_holds_that_picture
test_a_gap_is_black_and_does_not_steal_a_neighbouring_picture
test_a_frame_past_the_end_of_the_composition_is_refused_not_invented
test_anamorphic_picture_is_placed_by_its_display_aspect
```

Only the last was investigated before the work stopped, and the finding is a real
one for the reviewer (the other nine are undiagnosed):

* **The fixture was wrong, and fixing it did not change the result.**
  `make_identity_anamorphic` passed `-vf setsar=64:45`. In an FFmpeg filter
  argument a colon separates *options*, not the terms of a ratio, so the file was
  written with sample aspect **45:1** (measured with `ffprobe`:
  `sample_aspect_ratio "45:1"`, `display_aspect_ratio "225:4"`) — an absurdly wide
  picture that any sane scaling letterboxes. Corrected to `setsar=64/45`, and
  `ffprobe` on the new fixture reports `64:45` / `16:9` as intended.
* **With the corrected fixture the failure is still there**, and it is now
  measured rather than guessed: `showinfo` confirms `sar:64/45 s:720x576` reaches
  the geometry stages of the shared chain
  (`scale=1280:720:force_original_aspect_ratio=decrease`, `setsar=1`,
  `pad=…`), the still is 1280×720 and carries the right frame identity in its
  middle, but every sampled edge pixel — `(x=0, x=1279)` at `y=0`, `y=360` and
  `y=719` — is `(0,0,0)`. So a 16:9 display-aspect source in a 16:9 output leaves
  black borders. That is the review's point that "picture format normalisation
  resets SAR to 1 after scaling without an explicit display-aspect contract": the
  chain's behaviour here is not understood well enough to call correct, and it is
  **not** correct for this case as written. What has not been established is
  whether the bar comes from `scale`'s handling of SAR, from the `pad`, or from
  the source's own encoded border — that is the first thing to measure when this
  resumes.
* The other nine names suggest two clusters — still-vs-export agreement, and
  edge/gap behaviour — but that is a guess, not a measurement.

### 7. Process lessons from this task (for whoever picks it up)

* **Do not report a capability as working from code reading.** I reported that
  measured source positions worked on one path and not another; in fact the
  `showinfo` parser matched nothing and neither path measured anything. Every
  claim in a task record should name the test that measures it.
* **A test that asserts what the implementation happens to return proves
  nothing.** "`decoded_source_pts` is `None`" was being asserted as if it were a
  result. An "unknown" outcome is only a pass when the test's subject *is* the
  unknown path.
* **Write expectations from measured stamps, not from the rate a fixture was
  meant to have.** Two of my own failures assumed a frame existed where the
  container's millisecond-rounded timestamps say it does not (§5). Nominal
  arithmetic (a 1/12 s step, 24 fps in a 25 fps timeline) lands between real
  frames; the identity fixtures exist to make that visible.
* **Identity-colour fixtures need a lift.** Frame 0 is pure `(0,0,0)`, identical
  to the composition's black item, so `IDENTITY_LIFT = 8` (every channel raised
  by 8) is what makes "the gap is black" and "frame 0 is showing" different
  observations.
* **Probe the fixture's own metadata before trusting it.** `setsar=64:45` looked
  right in the source and was silently parsed as two options (§6). A fixture that
  does not contain what its docstring claims turns a real acceptance check into a
  test of nothing; the cheap guard is one `ffprobe` assertion per fixture.
* **Measure the tool at its edges before trusting a flag's name.**
  `ffprobe -read_intervals a%b` past the end of a stream returns the stream's
  tail (§4); `atrim=end_time`/`end_duration` are unusable for audio; `-nostdin`
  is rejected by `ffprobe` in this build.
* **A `git worktree` at HEAD is not an isolated environment** while `.venv`
  installs the project editable: old tests import the *working tree's* modules
  (§4). Use `PYTHONPATH` at the worktree's `src`, or a second venv.
* **Background shell records were lost twice** (`Shell.NotFoundError`) while a
  process kept running. Long verification runs are easier to trust written to a
  file in `/tmp/opencode` and read once afterwards, and `pgrep` before assuming
  an orphan has died.

### 8. What remains before this task can be called done

1. Diagnose the ten failures in §6 — they are the acceptance list for this task.
2. Run the whole suite once (`QT_QPA_PLATFORM=offscreen pytest -q`) and record
   the count; it has not been run since the ceil lattice and the shared chain.
3. Run the three unattended checks once each and record their measured numbers:
   `scripts/desktop_checks/export_check.py`, `scripts/ab_workflow_check.py`,
   `scripts/long_project_check.py`.
4. Get the reviewer's answers to §2 (tick quantisation in the plan vs a
   documented 1.1% residual; `round=up` vs "nearest") and §5 (should the export
   and preview paths carry the measurement tap?), then finalise the wording of
   the contract in `render/plan.py` and `docs/architecture.md`.
5. **Not met, and honest to say so:** the acceptance item "verify documented
   handling for interlaced and HDR input". The only statement in the repository
   predates this task — `docs/architecture.md` (TASK_05 rendering) says colour
   space/transfer conversion and HDR handling are *not claimed*, and README lists
   "no crop, rotation, deinterlace or denoise" under limitations. TASK_12 added
   **no explicit refusal** for interlaced or HDR input and **no fixture** for
   either, so an interlaced or HDR file today goes through the same SDR
   `yuv420p` chain with no complaint beyond what TASK_05 already says. A clear
   refusal, or a documented probe that reports the field order / transfer
   characteristics to the user, is still to be designed.
6. Deliberately **not** in this task: the GUI-thread `ffprobe`/`ffmpeg` calls in
   `_probe_asset` and `extract_stream_window`, cancellation and shutdown
   hardening — that is TASK_13's scope, and its acceptance list already covers
   them.




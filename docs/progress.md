# Progress

Task status, deviations and outstanding checks. Verification is only claimed
when actually executed.

Environment used for development: Fedora 44 (Workstation), Python 3.14.10,
PySide6 6.12.0, ffmpeg/ffprobe 8.1.3, pytest 9.2.0, ruff 0.17.6.

## TASK_00 — Bootstrap a runnable project — DONE

Implemented:

- `pyproject.toml` (hatchling, `src/` layout, `video-editor` console script,
  `dev` extra with pytest + ruff), package `video_editor`, README,
  docs/architecture.md, docs/progress.md.
- `video_editor.app`: Qt main window with title, empty central tab area,
  File > Quit, Help > About, status bar startup diagnostic, global
  excepthook logging. No media controls.
- `video_editor.diagnostics`: ffmpeg/ffprobe availability + version parsing;
  never raises; actionable install hint when missing.
- `video_editor.cli`: `video-editor`, `python -m video_editor`,
  `video-editor doctor`.
- Tests: pure import test, offscreen GUI smoke test (launch, Quit action,
  missing-FFmpeg diagnostic), doctor test.

Verified:

- `pip install -e ".[dev]"` succeeds in a fresh venv (uv, Python 3.14).
- `pytest` passes (see task notes below for run output summary).
- `ruff check .` passes.
- `python -m video_editor doctor` reports ffmpeg 8.1.3 / ffprobe 8.1.3 on PATH.
- Offscreen smoke test executed; both `python -m video_editor` and the
  `video-editor` console script additionally launched under `QT_QPA_PLATFORM=xcb`
  on the desktop session (ran until a 4 s `timeout` killed them; exit 124,
  no crash). Result: 5 passed, ruff clean.

## TASK_01 — Import and inspect media — DONE

Implemented:

- `media/assets.py`: `Asset` (stable uuid, path, size+mtime identity, probe
  metadata), `StreamMetadata` (container stream index, kind, codec, language,
  exact rational frame rates, dimensions, sample rate/channels/layout,
  start/duration/time_base/nb_frames, conservative `end_horizon`),
  editability flags and notes.
- `media/ffprobe.py`: JSON adapter, defensive parsing, exact Fraction
  parsing, duration derivation rules, never raises for bad media.
- `media/importer.py`: per-file threads, per-job/global cancellation via
  subprocess kill, duplicate-safe bookkeeping left to the UI controller.
- `ui/media_bin.py`: `MediaBinController` (Qt-signal bridge from worker
  threads → main thread; duplicate detection; stable IDs) + `MediaBin`
  tree (asset → expandable streams, details, muted styling + tooltips for
  uneditable/conservative streams).
- `ui/main_window.py` (window moved to `ui/`; `app.py` re-exports):
  Import Media… (Ctrl+I), Cancel Import, status-bar progress/errors.
- `ui/format.py`: exact-rational time/rate formatting at the UI boundary.
- `tests/media_fixtures.py`: ffmpeg lavfi fixtures — multiplexed 3-colour
  video + 2 audio streams (eng/fre), audio-only WAV, video-only MP4 @
  24000/1001, whole-asset +2.5 s origin offset (mkv `-output_ts_offset`),
  per-stream +1.0 s audio offset (mkv `-itsoffset`), garbage file.

Verified (`pytest`, offscreen GUI where relevant): 31 passed, ruff clean.

- Parser unit tests: missing fields, `0/0` rates, nonzero `start_time`,
  `N/A`, malformed JSON/structs, derived durations, unknown-vs-horizon
  bounds, `und` language.
- Integration tests against real ffprobe 8.1.3 fixtures: stream kinds and
  indices, languages eng/fre on separate audio streams, exact
  `24000/1001` rate, whole-asset origin ≈2.5 s, audio stream origin
  ≈1.0 s with end ≈3 s, WAV editable with origin 0, garbage → error.
- Importer tests: valid+invalid batch keeps successes, missing file,
  cancellation (killed probe → "cancelled"), three parallel imports each
  complete exactly once with distinct IDs.
- Offscreen GUI test: two-level tree populated, 3 stream children,
  duplicate import rejected with message, failed import reported.

Fixes made during the task (regression-tested): `Fraction` has no `to_int`
(use exact floor+half-up in `format_time`); `wait_imports` must drain queued
signals before asserting.

Notes / deviations:

- MKV/TS containers report no per-stream durations → introduced the
  documented *container horizon* upper bound (see architecture.md) instead
  of disabling editing for every mkv film; UI labels it conservative.
- ffmpeg 8.1.3 reports MKV `nb_frames`/`nb_samples` as absent; the horizon
  path covers this.

## TASK_02 — Non-destructive timeline domain and commands — DONE

Implemented (`core/`, standard library only):

- `core/model.py`: `Project`, `OutputSettings`, `Track`, `Clip`,
  `StreamSource`, `envelope_gain()`, query APIs (video stacking topmost-wins,
  audio contributions at time, interval intersections, output extent,
  exact resolved stream time via `Clip.source_time_at` /
  `output_time_for_source`).
- `core/commands.py`: register source; add/remove/reorder/rename track;
  enable/mute; add/remove/duplicate/move clip (with compatible track
  changes); split; trim (in/out within bounds); set rate/gain/fades;
  link/unlink; atomic `ImportGroupCommand` preserving stream offsets;
  `validate_project()`. All atomic — failures raise `CommandError` with no
  state change. Fade-aware split/trim rejection (no silent envelope jumps).
- `core/history.py`: `ProjectSession` with snapshot undo/redo, redo
  invalidation, no-op skipping, dirty-vs-saved tracking, external-mutation
  guard.
- `core/timeutils.py`: exact parse ("24000/1001"), half-open overlap.

Verified: 104 tests pass (`pytest`), ruff clean. New core coverage (51 core
tests): exact 24000/1001 split/trim/rate math, ~2 h frame accumulation with
zero drift, half-open boundary rules (split at edges rejected, touching
clips allowed), nonzero stream origins (source_in before origin rejected),
gap preservation on move/delete (no ripple), invalid overlaps incl. atomic
rollback identity checks, linked move/split/rate all-or-nothing, linked
trim/split eligibility rejection, unlink independence, fade fit/cut-through
rules, grouped import offsets (incl. audio origin 1.5 s and anchoring at
minimum origin), muted/disabled duration stability, undo/redo round trips
with stable ids, redo invalidation, dirty tracking through undo, external
mutation refusal, track removal cascade + undo, and a picture-A/sound-B
composition edited independently end-to-end.

Notes / decisions (see architecture.md): trim may extend within known
stream bounds; linked trims are inward-only; fade edits are rejected rather
than partially reshaped; history snapshots (not inverse commands).

## TASK_03 — Source viewer and timeline navigation — DONE

Implemented:

- Layout: media bin dock, viewer (Source/Output modes with explicit mode
  combo + provenance labels) above the timeline (QSplitter), inspector dock.
- `ui/viewer.py`: `SourceViewer` (Qt Multimedia play/pause/seek/step, slider,
  exact asset-local readout; stream-specific playback via FFmpeg preview;
  honest backend labels; approximate frame-step labelled as such; backend
  errors reported with the preview fallback suggested). `OutputViewer` is a
  truthful placeholder showing output-time playhead only.
- `media/extractor.py`: bounded (≤60 s) single-stream FFmpeg preview
  extraction; returns None on failure — callers never substitute media.
- `ui/timeline.py`: ruler with adaptive nice-step ticks, headers with
  kind/state text + E/M toggle buttons, viewport-culled clip painting,
  playhead, Ctrl+wheel zoom-at-cursor, wheel scroll, keyboard nudge, exact
  Fraction pixel↔time conversion, selection (Ctrl-click additive),
  clip/track hit testing.
- `ui/inspector.py`: read-only clip/track/asset details incl. exact
  rational rate with its `Fraction` form.
- Placement `Ctrl+G`, `Go to Time…` (accepts `H:MM:SS.mmm`, plain seconds,
  or rationals like `24000/1001`), fit/zoom, add track, remove-empty-track,
  split at playhead (S), delete (Delete), undo/redo actions. `ui/format.py`
  `parse_time_text` for precise entry.

Verified: 125 tests pass, ruff clean.

- New tests: exact pixel↔time roundtrips incl. fractional pps and 2 h
  offsets; zoom centring; zoom clamps; fit; hit testing incl. gaps; ruler
  click sets playhead; selection signals; culling contract with 300 clips;
  mode switching (`viewer_stack` page changes), placeholder honesty text,
  asset-local provenance label; inspector rows; grouped placement preserving
  ~1.0 s stream offset from a real `-itsoffset` fixture; two-asset placement
  to separate tracks with full undo; overlapping placement creating an
  extra track with `validate_project()` clean; extractor tests proving the
  preview contains *only* the selected stream (language `fre` survives),
  window offsets, half-open `covers`, missing source → None.
- Manual desktop checks (`QT_QPA_PLATFORM=xcb`, real session): Qt Multimedia
  (FFmpeg backend) played the 3-stream mkv fixture — PlayingState, 82
  position updates in 4 s, zero errors; full editor window launched with
  Import/Place/Undo actions present.

Notes:

- Source frame stepping is deliberately labelled approximate until the
  preview decoder exists (TASK_06); no VFR PTS stepping claimed yet.
- A placement can create one extra "register source" history entry before
  the clip command; undoing a placement may therefore need 2 undos. Cosmetic
  history granularity issue, safe by LIFO ordering (documented above).

Limitations / notes:

- GUI playback not exercised by tests (offscreen platform has no media
  backend); manual checks happen when a viewer exists (TASK_03+).
- Not a git repository commit workflow: nothing committed (AGENTS.md: do not
  commit unless asked).

## TASK_04 — Manual editing interactions — DONE

Implemented:

- `ui/gestures.py` (pure, widget-free): `snap_candidates`, `snap_time`,
  `drag_delta_from_pixels`, `trim_source_time`, public `SNAP_TOLERANCE_PX = 8`.
- `ui/timeline.py` gesture state machine: press on clip body = move drag,
  press within `EDGE_ZONE_PX` (7) of an edge = trim drag, `DRAG_START_PX` (3)
  threshold so click-to-select never creates an edit, dashed ghost geometry +
  snap marker while dragging, `Esc` cancels, release emits exactly one
  `clipMoveRequested` / `clipTrimRequested`. Track-under-cursor changes are
  limited to same-kind tracks. Hover cursors: `SizeAllCursor` (move),
  `SizeHorCursor` (trim), `ClosedHandCursor` while dragging.
- Snapping: 8 **pixel** tolerance through the rational zoom (constant reach on
  screen), attracts to playhead and to boundaries of clips outside the dragged
  group; on by default, toggled from the Timeline menu; gestures only.
- `core/commands.py`: `DuplicateClipsCommand` (block copy by uniform delta,
  links inside the block regrouped, atomic placement validation) and
  `InsertClipCommand` (explicit ripple on the target track for clips starting
  at/after the insert point, linked mates shifted as a block, insert *inside*
  an existing clip refused with an explanation).
- `ui/inspector.py` is now editable: exact source in/out, output start,
  rational rate, gain (dB), fades, Apply; link/unlink buttons; track rename +
  move up/down. All non-empty fields are parsed before anything is applied;
  invalid input is marked in place and applies nothing.
- `ui/main_window.py`: gesture signals → single core commands; `Ctrl+D`
  duplicate with numeric destination; `Ctrl+Shift+G` ripple insert; snapping
  toggle; track rename/reorder; selection sanitised after every history
  operation (stale clip ids dropped).
- Bug fixes found by the new tests:
  - Negative destinations/positions raised the model's `ValueError` instead of
    `CommandError` in `AddClipCommand`, `InsertClipCommand`,
    `DuplicateClipCommand`, `DuplicateClipsCommand` — the UI's single
    `except CommandError` would have crashed. Guarded + regression tested.
  - `test_placement` depended on `bin_controller.assets` order, which is
    import-completion (thread) order. Now assets are looked up by path.

Verified: 169 tests pass, `ruff check .` clean.

- New pure tests (`tests/test_gestures.py`): candidate exclusion, pixel-defined
  tolerance at two very different zooms, disabled snapping, nearest-candidate
  choice, exact pixel→time delta at `24000/1001`, trim source math with rate 2.
- New core tests (`tests/test_core_commands.py`): block duplicate (uniform
  shift, fresh ids, originals untouched, link regrouping, cross-track
  preservation), bad destinations, ripple confined to one track, fractional
  ripple length at `24000/1001`, insert-inside-clip rejection, linked-mate
  shifting and post-shift conflict aborting the whole insert, kind/bounds
  rejection, and `CommandError`-not-`ValueError` for negative positions.
- New GUI tests (`tests/test_editing_gui.py`, 23 tests): drag move commits one
  command and is one undo step; drag to another compatible track (+undo);
  same-track overlap refused with unchanged project and a status message;
  Escape cancels; snapping lands exactly on a clip boundary; linked pair moves
  together; right/left trim drags give exact source values (+undo); inspector
  populate/apply, exact rate and dB gain, invalid input refused with red field,
  link/unlink; duplicate block with destination and refused overlap; ripple
  insert incl. linked mates; selection survival across undo and track removal;
  gestures driven through **real Qt events** (`QTest` mouse/key), hover cursor
  shapes, snapping toggle changes the landing time, track rename/reorder with
  out-of-range refusal, and a 483-clip / 2-hour project that repaints 10×
  within 3 s while a far-end drag stays exactly `+5 s`.

Manual desktop checks (`QT_QPA_PLATFORM=xcb`, `DISPLAY=:0`, real editor
window, 3-stream `mixed.mkv` synthetic fixture, gestures driven through the
real Qt event pipeline with `QTest`; 18/18 checks passed, no failures):

- Import → place video stream 0 at t=0 and French audio stream 2 at t=2;
  output intervals shown as exact fractions.
- `Esc` during a drag cancels and leaves the project unchanged.
- Drag with snapping on: released 2 px short of the audio clip's start, landed
  **exactly** on it (`output_start == 2`).
- Trim-right drag shortened the clip, kept `output_start`, produced exact
  fractions (`[2, 73/10)` output, `src 0–53/10`).
- Undo of trim = one step, undo of drag = one step, redo restores the drag.
- Inspector: `0:00:07.375` applied as `59/8`; `25 fps` refused with the field
  marked and no edit; `24000/1001` applied with the exact rational output
  duration.
- Dragging the audio clip over the video **row** does not change its track
  (the widget only offers kind-compatible drop targets); the clip moved in
  time only.
- Placing a second video clip on the same track, then dragging it onto the
  first: refused with "Overlaps clip … clips on one track must not overlap
  (use separate tracks)", clip unmoved, nothing added to history.

## TASK_05 — Composition rendering and export — DONE

Implemented:

- `render/plan.py` — pure plan from an immutable `Project` snapshot + half-open
  output interval. `Quantisation` (global frame/sample indices, `round_half_up`,
  item lengths as index differences), `VideoItem`/`AudioItem`, stacking
  resolution (topmost enabled wins), explicit black and silence, per-item source
  ranges with rate, fade anchors in output time, conservative-end warnings, and
  a worst-case peak-level warning for summed audio. No Qt, no FFmpeg.
- `render/graph.py` — plan → one FFmpeg invocation: `[in:v:N]`/`[in:a:N]`
  stream addressing, `-ss` relative to the container's own start, exact
  `trim=end_frame` / `apad`+`atrim=end_sample` counts, aspect-preserving
  scale + pad + `setsar` + `format`, chained `atempo`, `volume=eval=frame`
  envelopes matching `core.model.envelope_gain`, `amix=normalize=0`,
  `normalise_layout()` (refuses layouts outside the documented set),
  `encoder_available()`, presets `MP4_H264_AAC` and `LOSSLESS_INTERMEDIATE`.
- `render/exporter.py` — staging file + rename on success, non-overwrite guard,
  post-render ffprobe duration check against the plan
  (1 frame + 2048/48000 s tolerance), progress from `-progress pipe:1`,
  cancellation that terminates FFmpeg and deletes owned temp files, chunked
  rendering for plans above 96 items (sequential lossless chunks joined by the
  concat demuxer into a single lossy encode) with a disk-space preflight.
- `StreamSource` grew `stream_position` and `input_start` (see
  docs/architecture.md): FFmpeg addresses streams by type + position and measures
  input `-ss` from the container's start, neither of which is the container
  stream index used as domain identity. `media.assets.AssetMetadata.position_of()`
  derives the position, `ui.main_window.core_source_for()` records both, and the
  renderer refuses rather than guessing when they are missing.
- `ui/export_dialog.py` — destination + browse, format, whole/playhead/custom
  range with exact time parsing, live plan summary (duration, frame/sample
  counts, item and black counts, geometry, colour limits, planner warnings),
  invalid range disables Start with the reason in place.
- `ui/render_worker.py` — `QThread` subclass running the render off the UI
  thread, queued progress/finished/failed/cancelled signals, cancel, `__del__`
  safety, `wait_finished()` that pumps the event loop for shutdown/tests.
- `ui/main_window.py` — File ▸ Export Composition… (`Ctrl+E`), Cancel Export,
  status-bar progress bar, overwrite confirmation, plan warnings surfaced in the
  status bar and log, export disabled with a message when ffmpeg is missing, the
  composition is empty, or an asset is missing on disk, closeEvent asks before
  abandoning a running export.

Verified: 298 tests pass (0 skipped), `ruff check .` clean.

- New pure planner tests (`tests/test_render_plan.py`, 26): half-up rounding on
  the frame and sample grids, non-frame-aligned boundaries resolved once by
  global index, stacking/black/silence, ranges, fades anchored absolutely,
  fractional rates (`24000/1001`, `25/24`), conservative ends, kind mismatch,
  unregistered streams, 2-hour plans.
- New graph tests (`tests/test_render_graph.py`, 21): argv shape, exact
  `trim`/`atrim` counts, `fps` normalisation, chained atempo staying in range
  with exact product, envelope expression vs `envelope_gain` at sampled times,
  `amix=normalize=0`, layout refusal, seek relative to container start,
  type+position addressing, refusal when position is unknown, black fill.
- New exporter unit tests (`tests/test_render_exporter.py`, 22): chunk ranges
  contiguous with exact frame/sample totals (picture-heavy and audio-heavy),
  per-chunk item bound, input deduplication bounded by items, missing
  ffmpeg/encoder/asset/directory guards, non-overwrite, temp-space preflight,
  `probe_duration` exact rational parsing and its failure paths, 2-hour planning.
- New FFmpeg integration tests (`tests/test_render_export.py`, 10 and
  `tests/test_render_ffmpeg_features.py`, 7): exports are **decoded back** and
  measured — frame colours at exact frame indices (index-selected, not seeked,
  which measured the neighbouring frame), spectral magnitude of each tone via
  Goertzel over sample-accurately trimmed windows, silence in gaps, fade level
  ramp, half-gain contribution, output stream parameters, render-range exports,
  chunked export frame-for-frame identical to single-pass, cancellation leaving
  no file and no temp files, non-overwrite protection, container-offset
  (`start_time 2.5 s`) and stream-offset fixtures, jittered/irregular PTS
  (VFR-like) sources rebased to constant output rate, a `24000/1001` source at
  25 fps output, and selecting the second audio stream of a 3-stream file with
  resampling from 44.1 kHz.
- **Corrected during TASK_08 validation.** The "fade level ramp" check above only
  covered a clip beginning at the render range start, where FFmpeg's filter clock
  and composition output time agree. A fade on a clip placed anywhere else
  rendered with **no fade at all**: the envelope was applied to the contribution
  *before* the leading silence that positions it, so the clock it was written in
  restarted at 0 and the anchors were never reached. Fixed in `render/graph.py`
  (the envelope is now applied to the positioned stream), measured in a rendered
  file by `test_fades_are_anchored_to_output_time_not_to_the_clip_local_stream`,
  pinned at graph level by `test_the_envelope_is_applied_to_the_positioned_contribution`,
  and old cached previews are invalidated by `render/cache.SEMANTICS_VERSION`.
  Discovered by the two-edition workflow check (`scripts/ab_workflow_check.py`),
  which fades a clip starting at output 2 s.
- New GUI tests (`tests/test_export_gui.py`, 26): dialog plan summary, range
  modes incl. playhead, invalid/unparseable ranges disabling Start and
  re-enabling on correction, missing directory, existing destination warning,
  planner warnings shown, preset extension; worker success/failure/cancel/
  progress signals (deterministic via a fake render), one render at a time,
  render never on the GUI thread; window wiring — modal dialog path, overwrite
  confirmation required before replacement, snapshot stability while editing,
  progress/cancel UI state, failure and cancellation messages, refusals for
  missing ffmpeg, empty composition and missing assets, and a real export
  through the controller writing a real MP4.
- `SetOutputSettingsCommand` (core) and the inspector's **Output (composition)**
  group: width, height, rational frame rate, sample rate and channel layout are
  project state, changed by one undoable edit and never inferred from an asset.
  The core rejects non-positive/empty values and no-ops; encodability is decided
  in the render layer (`normalise_layout` refuses layouts outside the documented
  set) and the inspector offers only those layouts. Field parsing is
  all-or-nothing with the offending field marked.
  (`tests/test_output_settings.py`, 16 tests, including that a settings change
  retimes no clip and that the export summary follows the current settings.)

Manual desktop check — **unattended**, `QT_QPA_PLATFORM=offscreen` (no windows on
the desktop, no human clicks; every modal dialog is located through
`QApplication.activeModalWidget` and answered by the script). Script:
`scripts/desktop_checks/export_check.py`. **53/53 checks passed, 0 failures.**
(The same script run from its scratch location and then from the repository copy
gave the identical 53/53 result.)
Fixtures generated for the run: a 4×1 s colour-block mkv (1440×1080, all-intra,
whose video stream has no container-reported duration → conservative end) and a
2 s 660 Hz stereo wav.

- Import: both files went through the real bin import queue; both reported
  editable streams.
- Composition built purely by UI interaction: Ctrl+G at playhead 1 s (picture)
  and 0 s (sound) → 2 clips on 2 different tracks.
- Output settings (1280×720, 25 fps, 48 kHz, stereo) applied through the
  inspector, i.e. chosen by the project and not inherited from the 1440×1080
  source.
- `ExportDialog` validated by driving it directly: summary said
  "Duration: 5.000s (125 frames @ 25/1 fps, 240000 samples @ 48000 Hz)",
  "Geometry: 1280x720, stereo, aspect preserved with padding, yuv420p",
  "Picture: 2 item(s), 1 black", the plain-sum mix policy and the colour
  non-conversion statement; a reversed range disabled Start with "Render range
  end is before its start"; unparseable input disabled it with the Fraction
  error; a valid custom range re-enabled it.
- Real modal export flow: Ctrl+E → dialog shown and Start clicked by the script
  → existing destination produced the "Replace existing file?" question, answered
  only then → progress reached 1.0 → `RenderResult.duration == 5` → status bar
  "Exported composition.mp4 (5.00s output)" together with the planner's
  conservative-end warning. No failure/cancellation signals, no unexpected modal
  dialogs, no `.partial.`/`.chunk` files left behind.
- Produced file measured: ffprobe reports h264 + aac, 1280×720, 48000 Hz, 2
  channels, `r_frame_rate=25/1`, duration 5.000 s (within 1 frame + AAC padding).
  Frames decoded and sampled by index: frame 0 black (no picture before 1 s),
  frame 25 = source red, 50 = green, 100 = magenta (source palette sampled the
  same way), and the 4:3 source is letterboxed inside the 16:9 frame (black side
  bar at x=40, coloured at x=700). Audio decoded and measured: 660 Hz energy
  share 0.707 while its clip runs, 0.000 in the gap after it, rms 0.088.
- Live gestures and keyboard: click selects the clip; ←/→ move the playhead and
  leave the project object unchanged; the inspector placed the clip at exactly
  7/5 s as one undoable edit; keyboard Undo then Redo worked; body drag moved the
  audio clip (0,2) → (7/2,11/2) with its length preserved and undo restored it;
  dropping an audio clip on the video track left it on its own track with the
  project unchanged; an edge drag trimmed (source_in increased, output end
  unchanged); a right-edge trim released 3 px from the playhead snapped exactly
  to 3/2 s.
- Disabling the picture track did not change the output duration (still 5 s).
- VLC played the result with dummy video/audio outputs at `--verbose 2`: its own
  `mp4 demux` module, "codec (h264) started", reached end of stream/playlist, and
  reported no corrupt/decode errors. **No human eyeballed the frames**; colour
  and audio correctness are asserted by the decoded-frame and spectral
  measurements above. Re-run with `QT_QPA_PLATFORM=xcb … show` to look yourself.

Findings recorded while writing that check (all now covered by tests):

- Keyboard shortcuts only fire for the window that Qt considers active; after a
  modal closes under the offscreen platform `QApplication.activeWindow()` is
  `None`, so the driver re-activates the window before keyboard checks. A real
  desktop does this by itself.
- Binding `Ctrl+Z` *and* `QKeySequence.StandardKey.Undo` (identical on Linux)
  yields "QAction::event: Ambiguous shortcut overload: Ctrl+Z" and Qt then
  triggers **neither** — undo died silently. Undo now has one binding, and redo
  carries both the documented `Ctrl+Shift+Z` and the platform standard (`Ctrl+Y`),
  which the README previously overstated.
- A drag that should trim must start *inside* the clip's rectangle: pressing at
  or past the boundary pixel finds no clip, so no gesture starts. The 7 px trim
  zone is measured inward from the edge.

## TASK_06 — Composition preview and precise inspection — DONE

Implemented:

- `render/plan.py` — `plan_geometry(project, scale)` plus
  `RenderRequest.geometry_scale`, so a preview is the same plan at a smaller
  picture; `Quantisation.frame_count(seconds)` (**round up**: how many whole
  output frames an output interval presents) deliberately distinct from
  `frame_index()` (the half-up *name* of the frame covering a time).
- `render/graph.py` — `PREVIEW_H264_FAST`: same container, codecs, pixel format
  and picture chain as export, encoder options tuned for speed only.
- `render/cache.py` — `render_fingerprint(project, asset_paths, start, end, scale,
  preset)`; `PreviewCache` (owns its directory, adopts pre-existing files by key,
  LRU bounded by entries and bytes, writes `…​.partial.mp4` and renames on
  success, `close()`/`prune_orphans()` delete only files it owns); `FrameCache`
  and `FrameTimesCache` bound decoded frames and probed PTS lists.
- `render/preview.py` — `preview_range()` (window around the playhead, snapped to
  whole output frames with the planner's own quantisation, clamped and walked
  back at the end, at least 2 frames, refused above the preview limit with
  "export that range instead"), `prepare_preview()` (cache hit → reused job, no
  render) and `PreviewJob`.
- `media/framegrab.py` — `extract_composition_still()` (one-frame plan, the
  exporter's picture chain and stream addressing), `still_plan_for()`,
  `picture_filter_chain()`, `source_frame_times()` (times rebuilt as integer
  `pts × time_base + start_time`, never from ffprobe's truncated `pts_time`),
  `frames_around()`/`nearest_frame()`/`presented_frame()`, and `StillFrame`
  carrying output frame index, exact output time, black flag, clip/asset/stream,
  planned `source_pts` and the real `decoded_source_pts`.
- `ui/render_worker.py` — `PreviewController` (one preview at a time, generation
  tags, a superseding request cancels and waits for the previous FFmpeg,
  `wait_finished()`); `StillWorker` + `StillController` + `WorkerThread` (a
  mailbox worker on its own `exec()` event loop, `QMetaObject.invokeMethod` to
  wake it, newest request wins, superseded results dropped, `wait_idle()`,
  `shutdown()`).
- `ui/viewer.py` — preview panel in the **Output** viewer (range fields, picture
  size 1:1…1:8, Render/Cancel/Free preview cache, Play preview, output-frame and
  source-frame stepping, Set clip in/out), three separate lines so
  preview status, inspected frame and provenance never overwrite each other, and
  explicit rendering / current / STALE / pending / failed / cancelled states.
- `ui/main_window.py` — Preview menu (Render Output Preview `Ctrl+R`, Cancel
  Pre&view, Step Output Frame ◀/▶ `Ctrl+←`/`Ctrl+→`, Free Preview Cache),
  fingerprint-based staleness marking, still requests on playhead moves, source
  stepping by real PTS, source-edge marking as ordinary trims, playback of the
  cached preview with preview-relative → output-time mapping (`playback_offset()`:
  playback starts at the playhead, moving the playhead while playing seeks the
  file instead of decoding stills, a playhead outside the rendered range is stated
  rather than silently mis-played), cache freed on
  close unless `VIDEO_EDITOR_KEEP_PREVIEWS=1`, `closeEvent` stopping render and
  decode threads, and `_invalidate_stills()` on every composition or asset change
  (the viewer cannot keep showing a picture the composition no longer makes).

Verified: 388 tests pass (0 skipped), `ruff check .` clean. The unattended desktop
check (`scripts/desktop_checks/export_check.py`, offscreen, every dialog answered by
the script) reports **80/80**, which includes all TASK_05 checks re-run on top of the
preview/still/cache checks listed below.

- New cache tests (`tests/test_render_cache.py`, 29): fingerprint sensitivity to
  timeline values, range, scale, preset and asset identity (and that a track
  rename does *not* change it), staged commit, budget eviction deleting only owned
  files, adoption of stray files, refusal to delete files it does not own,
  `close()`/`prune_orphans()`, frame cache byte budget, the cache handing back the
  caller's own frame object, `clear()` dropping everything, and PTS-list reuse.
- New preview tests (`tests/test_render_preview.py`, 15): range snapping to whole
  frames, default window around the playhead, walking back at the end, minimum 2
  frames and sub-frame compositions, `playback_offset()` mapping output time into
  the rendered range exactly (half-open end, 24000/1001-style grids), refusal on
  empty/over-long ranges, gaps
  counting towards the preview length, `prepare_preview` cache reuse (render
  skipped entirely) and a *different* key after an edit, preview request carrying
  the reduced geometry with untouched timing/frame counts, chunked previews,
  cache files never landing next to a user export and budgets being enforced,
  planning a request without rendering it, and the acceptance comparison: a preview
  of output 1–7 s (spanning the rate-2 overlay's cut, the end of the picture and
  the silent tail) is decoded back and its frames at indices 12/24/25/62/112 match
  the named source colours **in both files**, 137/149 are black in both, and its
  measured audio matches the export window by window (660 Hz present early with 990
  absent, both contributing later at the documented half-gain ratio, tail silent).
- New frame-inspection tests (`tests/test_framegrab.py`, 21), measured against
  real FFmpeg output: a still's sampled colour equals the frame the **exporter**
  writes at the same index across eight output times, frame index equals the
  full-range planner's `frame_index`, reduced `geometry_scale` changes pixels but
  not time, black gaps produce real black without a decoder, oversized geometry
  refused, missing/unregistered assets and unreadable streams reported rather than
  substituted, the second video stream addressed by position, `presented_frame`
  exact-vs-next behaviour, and on a VFR fixture (8 s @25 fps + 4 s @12 fps,
  Matroska `time_base=1/1000`) that 248 real PTS values are recovered, that
  8.4 s is **not** a frame time while 8.333 s and 8.417 s are, that stepping
  offers the real neighbouring frames on either side (and direction 0 returns the
  frame sitting exactly on the time), and that the reported source PTS is the frame
  the decoder really produces. An offset fixture (whole presentation starting at 2.5 s) pins
  that packet timestamps are already asset-local: 50 frames from 2.5 s with
  `1/25` steps, neighbours of 3.02 s being 2.98 s and 3.06 s, and a still of that
  asset reporting `source_pts == decoded_source_pts == 127/50`.
- New GUI tests (`tests/test_preview_gui.py`, 22): range fields track the
  composition until typed over, reversed/unparseable ranges refused without
  rendering, render/cancel/failure messages, cache reuse producing no second
  render, staleness after an edit vs after a rename, playback mapping preview-
  relative time to output time, playback **starting at the playhead** and a
  playhead move seeking the file instead of decoding stills (with a pause ending
  the seeking and an out-of-range playhead being reported), the playhead decoding
  the exact output frame
  (frame 8 at 0.3 s ⇒ source PTS 8/25), decoded-frame reuse instead of a second
  decode, source stepping by real PTS with `Set clip in/out` producing ordinary
  trims, stepping stopping at the end of a stream, black frames offering no source
  stepping, obsolete results dropped during rapid scrubbing, cache freeing deleting
  only cache files, the decoded-frame cache staying inside its RAM budget, and
  shutdown leaving no running thread.
- Unattended desktop check extended (see below): `Ctrl+R` renders the typed range
  at 1:2 into the cache; ffprobe reports h264 + aac at 640×360, `r_frame_rate=25/1`
  and a 2 s duration; sampled preview frames equal the source colours and equal
  the **exported** file's picture at the same output time ((0,127,0) both); the
  660 Hz tone is in the preview mix; moving the playhead decodes frame 50 whose
  centre pixel equals the export's frame 50; `Ctrl+→` moves exactly 1/25 s and
  decodes frame 51; source stepping 26/25 → 27/25 lands on a real probed frame and
  leaves the output frame unchanged; a track rename keeps the preview current
  while a clip move marks it STALE and disables playback; undo + re-render reuses
  the cached file with **zero** new renders; a 1:1 whole-composition request is
  cancelled mid-render, reports the cancellation, and leaves no `.partial.` file;
  Free preview cache deletes every cached file the run created.

Findings recorded while building this (all now covered by tests):

- PySide6 `QThread` has **no public `wake()`**, and a moved-in worker receives
  nothing unless `run()` enters an event loop: the still worker uses `exec()` plus
  `QMetaObject.invokeMethod(..., QueuedConnection)`.
- PySide silently **truncates** signal arguments when the slot declares fewer
  parameters: connecting `failed = Signal(int, str)` to `def _on_preview_failed(
  message)` passed the *generation* as the message, so failures displayed "FAILED —
  1". Every preview/still handler now takes the full argument list.
- Emitting a signal through a helper (`signal.emit(*args)`) raises
  `TypeError: progress(int,double) needs 2 argument(s), 1 given!`; each signal is
  emitted explicitly with the generation bound as a default argument.
- `timeline.set_playhead()` used to emit `playheadChanged` only for
  user-initiated moves, so programmatic moves (and the still frame under them)
  went unnoticed. The signal now always fires; `from_user` only records *how* the
  playhead moved for status messages.
- A still request made before the first asset import (or before any edit) means a
  window whose playhead sits at 0 shows nothing at all; composition changes and
  completed imports now refresh the decoded frames.
- Reporting `at_or_after` (the *next* frame) as the frame a seek produces was an
  off-by-one: `presented_frame()` prefers the frame exactly at the planned time and
  only falls back to the next one, which the VFR fixture shows is 8.417 s for a
  planned 8.4 s.
- Cancelling a preview leaves nothing behind only if the FFmpeg process has really
  exited first (`_terminate()` waits), and the desktop check now clears its own
  scratch cache directory first so an earlier crashed run's debris cannot be
  mistaken for a leak.
- **Real PTS are already asset-local.** An earlier revision computed them as
  `pts × time_base + start_time`, which double-counted the offset: a file whose
  whole presentation starts at 2.5 s reported its first frame at 5 s, so source
  stepping offered times with no frame at them and an edge marked from one was
  seconds away from the picture on screen. Measured on Matroska and MP4 with both
  whole-asset and per-stream offsets, the fix is `pts × time_base` alone, with a
  regression test on the offset fixture (frame at 2.54 s, neighbours of 3.02 s are
  2.98 s and 3.06 s).
- A test session that leaves a window open leaves its still-frame thread running,
  and Qt aborts the interpreter at exit ("QThread: Destroyed while thread is still
  running", then a core dump) *after* printing the results, which made an otherwise
  green run look broken. `tests/conftest.py` now closes leftover windows and stops
  their render/decode workers at session end, and the suite exits cleanly.

Measured latency (this machine, system FFmpeg 8.1.3, CPU decode; not a benchmark,
so the UI can be judged honestly — note Qt Multimedia logs its own bundled FFmpeg
9.0.1, which is not the copy used for renders): one composition still costs about
**0.40 s** at 128×96 output and **0.47–0.51 s** at 1280×720 (1:1 or 1:2) —
dominated by starting a decoder process, not by pixel count. Listing a stream's
real PTS costs about **0.24 s** for 100 packets (cached per stream, so stepping
after the first step is immediate). Still decodes are asynchronous and
newest-wins, so scrubbing stays responsive; each decode is one frame, never a
range.

Limitations, stated rather than hidden:

- Preview playback is a rendered range through Qt Multimedia, not real-time NLE
  playback: starting a preview costs a full render of that range, seeking inside
  it is the media backend's seek, and the picture is deliberately lower
  resolution. The default window is short and the range is capped; longer ranges
  should be exported.
- A decoded still is one frame at a time; there is no continuous output-mode
  playback of the composition and no timeline scrubbing preview.
- Still frames are decoded with the exporter's picture chain, so the same colour
  handling limitations apply (no colour space/transfer conversion, no HDR).
- `Set clip in/out` writes the *planned* asset-local time (the model is
  continuous); the viewer states the real frame the decoder produced alongside it.
- Preview cache files are ordinary MP4/H.264/AAC files owned by the application and
  are deleted only by eviction, Free preview cache, or window close.

## TASK_07 — Project files, recovery and relinking — DONE

Implemented:

- `project/serialize.py` — the versioned document schema (documented in the
  module docstring and README), exact `"num/den"` rational encoding, strict
  decoding that **refuses unknown fields and unknown versions** with diagnostics
  naming the field/version, duplicate-id detection, and the migration policy
  (`SCHEMA_VERSION`, `MIGRATIONS`). `Navigation` (playhead/zoom/scroll/selection)
  is optional, clamped on load and never part of dirty tracking.
- `project/storage.py` — `build_document` / `decode_payload` / `save_document` /
  `load_document`; `relative_reference` (relative only when it stays inside the
  project directory); atomic write (sibling `.tmp`, `flush`+`fsync`,
  `os.replace`, no debris on failure); `AssetState` (`online`/`missing`/
  `changed`) from recorded size + `mtime_ns`; validation that combines schema,
  cross-references, `validate_project` and non-fatal warnings when saved clip
  intervals no longer fit the recorded stream bounds or the frame rate differs.
- `project/recovery.py` — `RecoveryStore` (app-owned directory
  `$VIDEO_EDITOR_RECOVERY_DIR` else XDG, session key = hash of the project path
  or `unsaved`, headers, pruning, corrupt-snapshot skipping, `discard()` that
  removes only its own files), `disk_ahead_of_snapshot()`, `describe_snapshot()`.
- `media/serialization.py` — asset/`StreamMetadata`/`StreamSource` records,
  encoded and decoded with the same strictness, so recorded probe metadata is
  preserved exactly and a project can be inspected offline.
- `media/relink.py` — `probe_replacement` + `validate_replacement` +
  `relink_asset`: same index/kind, bounds covering every clip interval, same
  frame/sample rate, picture size unless knowingly accepted, notes for
  renumbered streams and shifted container start.
- `core/commands.RelinkAssetCommand` — atomic replacement of an asset's
  registered sources; refuses a partial relink or one that would leave a clip
  outside its stream; undoable like any other edit.
- `ui/main_window.py` — Project menu (New/Open/Save/Save As, Media Status…,
  Relink Missing or Changed Asset…, Recovery Snapshots…), dirty-state title with
  `*`, `_confirm_replace`/`_confirm_quit` unsaved-work prompts, `_write_project`
  (atomic, then dirty baseline + snapshot), relink offer/dialog/refusal, offline
  rendering refusal naming asset + state + path, autosave timer
  (`VIDEO_EDITOR_AUTOSAVE_SECONDS`, default 120 s, `0` off), snapshot-on-close
  for unsaved work, startup restore/discard **only** from `run_app`, restore
  refused when the project file is newer, `_missing_asset_names()` made precise.
- `ui/media_bin.py` — `reset`/`add_external`/`reindex`/`rebuild`/`refresh_asset`
  so the bin follows a loaded project and a relink.
- `ui/viewer.py` — `clear_preview_state()` (a preview belongs to one project).
- `ui/timeline.py` — `BASE_PPS` so a saved zoom means the same thing next time.

Verified as closed: `pytest -q` under `QT_QPA_PLATFORM=offscreen` — **517 passed,
0 failed, 0 skipped** in 139 s, repeated three times with the process exiting
cleanly each time; `ruff check .` clean. The unattended desktop
acceptance run passed **126/126** checks (`exit=0`, no crash at exit): import
queue, gestures, export dialog and overwrite consent, measured export frames and
tones, composition preview and still-frame inspection, cache lifetime, then the
project legs — save over an existing file with and without consent, reopen in a
second window and re-export comparing frames/tones, relative references after a
move, missing-asset detection, offline save, relink, refusal of an incompatible
replacement, recovery snapshot → restore → refuse-when-disk-is-ahead → discard,
the unsaved-work quit prompt, and VLC playback. The suites written for this task
are 83 tests: `tests/test_project_storage.py` 36,
`tests/test_project_relink.py` 10, `tests/test_project_save_gui.py` 26 and
`tests/test_core_history.py` 11 (the last two cover the dirty-baseline rules this
task added).

- Storage tests cover: round-trip of every domain value (odd fractions, rates,
  link groups, fades, gains, muted/hidden tracks, unknown stream end staying
  `None`), exact string rationals on disk, optional+clamped navigation, relative
  references only inside the project directory, a moved project+media pair
  resolving again, absolute references staying absolute, missing/changed/probe-
  failed asset states, "recorded metadata is the baseline" (nothing re-probed or
  adopted), warnings when clips no longer fit, future schema refusal naming the
  version and keys and promising the file was not modified, unknown-field refusal,
  missing-version refusal, no-migration refusal for older versions, corrupt/
  truncated/binary/directory inputs, decimal times rejected as inexact, domain
  invariant validation on load, duplicate ids, and atomicity: `os.replace`
  failing leaves the old file byte-identical with no `.*tmp*` debris, a missing
  directory is reported, three successive saves leave exactly one file.
- Relink tests cover: stable asset id and label preserved, timing untouched,
  missing/kind-swapped stream refused with the replacement's actual stream list,
  material the replacement cannot supply refused with the exact clip interval,
  different frame rate refused as a different timing base, picture size needing
  explicit acceptance, renumbered streams reported and used for addressing,
  unprobeable replacement refused, container offset reported without changing
  source times, and the command's atomicity/undo plus its refusals.
- GUI tests cover: save → reopen in a second window with exact project equality
  (ids, order, fractions) plus navigation restore and clean/undo-free state,
  dirty tracking through save/undo/redo, navigation never dirtying the project,
  Save As rewiring references, the overwrite confirmation on both paths, five
  kinds of unreadable document leaving the current work intact, future-schema
  files not rewritten, New/quit unsaved prompts (Cancel keeps the work),
  a failed save leaving the file byte-identical and the session still dirty,
  relative paths after moving the folder, offline load → refusal → save → relink
  → undo → refusal of an incompatible replacement → a relink whose streams match
  exactly completing as a media change without an edit, media status listing,
  autosave firing on a timer (once, not repeatedly) and restore bringing back the
  exact composition + playhead as **unsaved** work, restore refused when the disk
  file is newer, and startup recovery never interrupting a plain window.

Findings recorded while building this (all now covered by tests or documented):

- **The overwrite consent prompt was inverted.** `_confirm_save_target` returned
  `not (path.exists() and self._confirm_overwrite(path))`, which *refused* the
  write when the user answered "Yes" and *performed* it when they answered "No".
  Reading the test result for the "No" case (`_save_project` returned True) is
  what exposed it. Now: proceed unless the file exists and the user refused.
- **Message-box buttons must be named, never counted.** Qt lays a `QMessageBox`
  out by role and platform style, so a Save/Discard/Cancel box really orders as
  Save…, Cancel, Discard. Code that mapped the clicked button to a position in
  `box.buttons()` therefore read "Cancel" as "Discard" — pressing Cancel on the
  unsaved-work prompt destroyed the work. `_ask` now matches the clicked button
  to the *caller's own* button list by label and returns that index; the GUI
  tests and the desktop check answer dialogs by label for the same reason. A
  suppressed or Escape-closed prompt returns "no choice", which every caller
  treats as a refusal, never as consent.
- **`replace_project(keep_dirty=True)` could not make a new session dirty**,
  because the baseline had already been copied from the incoming content at
  construction. Recovered work therefore looked saved, so closing the window or
  starting a New project would drop it without a word. The baseline is now
  `None` when nothing is behind the content (`ProjectSession(saved=False)`),
  and `is_dirty` is true until the first write.
- **A QTimer interval of 0 means "as fast as possible", not "off".** The
  documented `VIDEO_EDITOR_AUTOSAVE_SECONDS=0` produced a continuous snapshot
  loop; in a test session it also let a window left open by one test write into
  another test's recovery directory. The timer is simply not started for 0.
- **The first save wrote absolute media references** because the document was
  built from `self.project_path` (still `None`) instead of the destination that
  was handed to the save. Relative references are now resolved against the
  directory the file will really live in.
- **Opening an unreadable file used to ask you to discard unsaved work before
  reading it**, so a bad click cost the work and then failed anyway. The file is
  now read and validated first; the unsaved-work prompt only appears when the
  work is genuinely about to be replaced.
- `decode_project` is given the **whole document**, so its "unknown field" set
  must include the sections other decoders own (`schema_version`, `application`,
  `assets`, `navigation`); otherwise every real file is rejected as if it were
  from a newer schema. Caught by the round-trip test, not by inspection.
- The unsaved-work prompt turned every existing GUI test that closes a window
  after an edit into a modal that nobody answers — the suite blocked instead of
  reporting. The prompt is therefore gated by `MainWindow.quit_confirm_required`
  (on for `run_app` and for the desktop check, which answers it; off in the test
  fixture), and tests that want the real prompt provoke it deliberately. A
  blocking offscreen `QMessageBox` does not time out: it just hangs.
- Faking a modal in tests cannot be done by replacing `clickedButton` (Shiboken
  ignores a Python attribute of that name) — the chosen button has to be
  `click()`ed, which is also what a user does. And `QMessageBox.exec` has to be
  replaced with a plain function in the class dict, so Qt passes the box it
  opened; a bound method would be handed back unbound and receive no box.
- Recovery snapshots must be keyed **per project** and scanned for both the
  project's key and `unsaved`; a snapshot with no project path still has to be
  findable after a crash, which is the whole point.
- Dirty tracking is value equality against the saved baseline, so undoing back to
  saved content clears the mark — the title's `*` is honest in both directions.
  The title was also not being refreshed on history changes at all, so the mark
  appeared only after a save; `_refresh_history_actions` now refreshes it.
- **A relink to a file whose streams match the recorded ones exactly was reported
  as a failure.** `_relink_asset_to` ran the composition half through `_apply`,
  which returns "the project did not change" — and the window treated that as a
  refused edit, so the media half (bin row, recorded path) was never updated and
  nothing was said. A replacement is allowed to change no stream information at
  all: the relink now completes, states that the composition is unchanged, and
  leaves undo history alone, because a no-op is not an edit. Found while
  connecting the TASK_08 decoration layer to a relink, and covered by
  `test_a_relink_to_an_identical_file_changes_the_media_not_the_composition`.
- **Recovery discovery was keyed so narrowly that it could not do its job.**
  Snapshots are stored per project, and a window only looked under *its own* key
  plus the never-saved one — but after an abnormal exit the application starts
  with no project open, so the snapshot of the film you were editing sat in the
  recovery directory invisible. A window with no project now finds them (and
  restoring reclaims the project file they belong to), while a window that does
  have a project open still sees only its own work and the unsaved one. Pinned by
  `test_a_start_up_window_finds_the_snapshot_of_the_project_that_was_open` and
  `test_an_open_project_is_not_off_another_project_s_recovery_work`.
- **`StandardKey.Redo` is not always Ctrl+Y.** Under a Wayland/Gtk theme it
  resolves to Ctrl+Shift+Z — the same chord the README documents — so the Redo
  action claimed one chord twice, Qt reported "Ambiguous shortcut overload" and
  the shortcut silently stopped working. It looked fine in the offscreen test run
  and failed on the desktop. The chords are now de-duplicated by their text (what
  Qt compares) and Ctrl+Y is listed explicitly, so both documented chords work on
  either platform; the desktop check additionally fails loudly if any two actions
  claim one chord.

The acceptance harness needed fixing too — each of these was found by *running*
it unattended, not by reading it:

- **The documented "headless" invocation was not headless.** It inherited the
  desktop platform, so the run opened a real window on the user's screen and sat
  in front of a modal asking for a click. The script now forces
  `QT_QPA_PLATFORM=offscreen` unless a desktop run was asked for (`show`) or the
  platform was set explicitly.
- **Answered dialogs were remembered by `id()`.** CPython reuses an address as
  soon as the closed dialog is collected, so the *second* identical prompt (the
  second "replace this project file?") looked already handled and was never
  answered — the run blocked waiting for a human. Dialogs are now remembered as
  objects.
- **A step read `assets[].reference` where the document writes `assets[].path`,**
  a `KeyError` that aborted the project leg precisely where it had never
  previously reached. It is fixed, and the legs behind it are now exercised.
- **Teardown closed windows through the same unsaved-work prompt the legs were
  testing,** so a refused close left a window open and every later leg inherited
  the mess. Teardown now states its own decision instead of asking.
- **A dialog the poller cannot see would hang forever.** There is now a watchdog
  that prints which dialog was blocking and exits non-zero: an unattended check
  must fail loudly rather than wait for someone to click.
- **A green suite was also aborting the process.** After printing "517 passed",
  pytest sometimes died with a segfault or "pure virtual method called" (only
  when stdout was a pipe, so it looked intermittent). Cause: tests that drop a
  `MainWindow` without closing it leave its preview/still/render threads and the
  TASK_08 decoration worker alive while the C++ `QApplication` is torn down first
  — and destroying a running `QThread` aborts the process. The session fixture
  now closes every open window through the window's real shutdown path and stops
  any remaining extraction thread (`decorations.shutdown_all()`, backed by a weak
  registry so a forgotten layer is still reachable). Three consecutive full runs
  now finish `exit=0`; the crash was timing-dependent, so one clean run was not
  evidence.

Limitations, stated rather than hidden:

- Recovery is a snapshot safety net, not a session journal: it restores
  composition + navigation, not undo history, selection of docks, or which
  viewer was open.
- Asset identity is `size + mtime_ns` (no content hashing of films), so a
  replacement with the same size *and* mtime is indistinguishable; relinking
  probes and validates instead of trusting identity alone.
- A relink never adjusts clip timing, so a genuinely different edition needs
  manual re-cutting (which is the product's purpose, not a bug) — it will refuse
  rather than silently retimes.
- Project files are readable JSON but not hand-edited-friendly at film scale
  (one asset record per file, full probe metadata); no compression is applied.

## TASK_08 — Manual editor completion and realistic workflow validation — DONE

Implemented:

- `media/waveform.py` — `audio_envelope(path, stream, start, duration, buckets)`:
  peak buckets for one stream over an asset-local range through FFmpeg
  (`asetnsamples` → `astats` → `ametadata=print`), reading only the requested
  window, refusing (`WaveformError`) on a bad range, an unknown stream, more
  buckets than `MAX_BUCKETS`, or output that cannot be parsed. No Qt, no
  fabricated peaks.
- `media/thumbnails.py` — `video_strip(...)`: one `image2pipe`/MJPEG contact
  strip per clip range at the requested granularity (`MAX_FRAMES` cap), with the
  frame times it actually produced, so a decoration can be placed by real PTS.
- `ui/decorations.py` — `ClipDecorations`: plans what the **visible interval**
  needs, on a fixed granularity ladder (`ladder_step`, `thumb_box`), caches per
  asset + stream + **file identity** in byte budgets (`ENVELOPE_BUDGET` 2 MB,
  `STRIP_BUDGET` 6 MB, LRU), queues at most `MAX_QUEUED_JOBS` 32 jobs on **one**
  worker thread per window, delivers by signal, remembers failures
  (`MAX_TRACKED_FAILURES`) instead of retrying every repaint, and has a real
  `shutdown()` (a live `QThread` must never be dropped). `shutdown_all()` exists
  for test/process teardown.
- `ui/timeline.py` — paints delivered envelopes/strips inside the clip shape,
  always draws geometry/labels/selection/playhead, schedules decorations on view,
  selection and project changes, and declares `WIDGET_KEYS` (its own chords)
  beside the handler. Clips narrower than `MIN_CLIP_PIXELS` and clips outside the
  view are not scheduled at all.
- `ui/main_window.py` — `decoration_material()` (asset + stream + resolved file
  for each clip, `None` for material that cannot be read),
  `refresh_decorations()`, `_forget_decorations()` on project/relink changes, and
  decoration failures surfaced in the status bar rather than hidden.
- Help ▸ Keyboard and Workflow… (`help_action`, `_chord_rows()`, `_help_text()`,
  `WORKFLOW_STEPS`): the chord list is read from the live actions, the
  application states its own manual workflow, and About now points there instead
  of carrying a second hand-kept list.
- `ui/viewer.py` — the preview range fields are labelled as the **output interval
  under review** (`preview_range_hint`), and the render button's own label states
  which range clicking it would render.
- `scripts/make_ab_editions.py` — generates the two synthetic editions and prints
  the manual recipe (clip, track, source file/stream, source in/out, output start,
  rate, gain, fade) plus the properties the result must show, all derived from the
  fixture table rather than prose. Refuses to overwrite without `--force`.
- `scripts/ab_workflow_check.py` + `scripts/desktop_checks/editions.py` — the
  A/B acceptance harness: one table (`PICTURE`, `UNDERNEATH`, `SOUND`) both builds
  the composition and states what the rendered file must contain.
- `docs/ab-workflow.md` — the manual recipe a person follows (fixture table, the
  14 clips with exact times, what must and must not appear) and what the automated
  run measures instead.
- `scripts/long_project_check.py` + `scripts/desktop_checks/long_project.py` — the
  long-timeline profile.

Bugs found by this task's validation and fixed, each with a regression test:

- **Fade envelopes were never applied to a clip that did not start at the render
  range start.** `render/graph.py` put `volume=…:eval=frame` on the clip's own
  sliced stream, before the leading silence that positions the clip inside the
  range, while the expression is written in time relative to the range start: the
  filter clock restarted at 0 and the anchors were never reached, so the export
  contained no fade at all. The envelope is now applied to the positioned stream.
  The earlier export test passed because its clip starts at 0, where both clocks
  agree. Measured in a rendered file by
  `test_fades_are_anchored_to_output_time_not_to_the_clip_local_stream`, pinned at
  graph level by `test_the_envelope_is_applied_to_the_positioned_contribution`, and
  `render/cache.SEMANTICS_VERSION` (now 2) stops an old preview being served as
  current. Found by the A/B check, which fades a clip placed at output 2 s.
- **Every playhead move re-stat-ed a file once per clip.**
  `MainWindow._missing_asset_names()` looped over clips and called `is_file()` for
  each, ~5 ms per move at 843 clips pointing at two files; it is a guard on the
  still-frame path, so it ran constantly. Now checked once per asset — playhead
  moves over a 2 h timeline measured **1191 ms per 200 moves before, 38 ms after**.
  Pinned by `test_a_long_timeline_checks_each_asset_once_not_once_per_clip`
  (counts the reads, not the timing).
- **Decoration scheduling consulted every clip in the project** — file check and
  plan arithmetic included — 9 ms per view change at 843 clips. The visible
  interval is now tested first, and the scan costs a comparison per hidden clip.
  Pinned by `test_only_clips_inside_the_view_are_consulted`.
- **The exporter's missing-source message named no file** (`Asset 6ed750c5… is
  missing on disk`): an asset id is not something a user can act on. It now names
  the recorded path, separately for "no path recorded" and "path not readable".
- Two harness defects in the A/B check itself: `Project.output_end` read as a
  property (it is a method, and a truthy bound method made the check pass while
  asserting nothing), and bin assets looked up by filename after a leg had renamed
  the file (now keyed by resolved path). A check that prints a constant detail on a
  PASS was also made honest.

Verified: `pytest -q` under `QT_QPA_PLATFORM=offscreen` — **537 passed, 0 failed,
0 skipped**, process exit 0; `ruff check .` clean. The three unattended desktop
checks each pass with **0 failed checks**: the window acceptance run (**133/133**),
`scripts/ab_workflow_check.py` (**197 checks**) and
`scripts/long_project_check.py`. Results are reported from single runs; where a
result is intermittent it is recorded as an open defect below rather than settled by
repetition.

Measured on this machine by one run of the long-project profile (2 h output,
843 clips, 3 tracks, mixed rates, 128×96, 25 fps):

- whole-timeline planning: 180 150 output frames, 721 picture + 482 sound items
  in **0.77 s**; a 10 s window in the middle of the same timeline in **5.1 ms**
  (3 items — the window does not pay for the timeline);
- a still frame decoded 1.5 h into the timeline: **0.37 s**;
- timeline repaint (median of 3 grabs within that run): **30.9 ms** with the whole
  2 h in view at 0.25 px/s, 16.3 ms at 1 px/s, **10.0 ms** at 50 px/s; **5 child
  widgets for 843 clips**, so the timeline is painted rather than populated;
- 154 clip hit-tests in 88.7 ms; 200 playhead moves in **36.9 ms**; an edit plus
  its undo across 843 clips in 17.6 ms;
- decoration caches after refreshing a long view: **146 kB held against an 8 MB
  budget**, 24 jobs queued against the cap of 32; 15 OS threads in the process
  (one decoration worker, the rest Qt's); peak RSS for the whole check
  **150 MiB**; at most **2** FFmpeg child processes during a render;
- a 290 kB project saved in 0.01 s and read back in 0.03 s with all 843 clips.

These are single-run figures; the checks assert budgets (a repaint under 500 ms, a
cache inside its budget), not these numbers, because timings depend on the machine
and what else is running on it.

What the A/B check measures (12 s output, 128×96, 24 fps, 4 tracks, 14 clips):
the colour on screen at every output frame against the intended cut (including
that covered material on a lower track is never seen and B's white frame never
appears); each tone's presence and level through Goertzel windows, including the
summed 550 Hz/770 Hz region measured as a 0.50/0.50 share, the half-gain clip at
ratio 0.499, the fade-out ramp measured **inside** the ramp, and silence in the
9-10 s gap; a 5-7 s interval rendered on its own and measured; a range outside the
composition refused; a cancelled render leaving no output and no `.partial.`
temporary; rendering refused after a source was moved, then accepted after
relinking; and the saved project reopened with the same composition.

Findings recorded while building this (all now covered by tests or documented):

- **The decoration layer was reporting failures nobody listened to.** It emits
  `complained` with the reason a clip has no picture or waveform, and the timeline
  writes that reason on a wide enough clip, but the window never connected the
  signal — while the documentation promised the status bar would say so. The
  connection and `_on_decoration_failure` now exist, tested by
  `test_a_decoration_failure_is_stated_not_hidden`.
- **OPEN DEFECT — the suite (and possibly the application) can abort during exit
  teardown.** Symptom: the run prints its result and *then* the process dies —
  `Segmentation fault`, `Bus error`, or `QThread: Destroyed while thread
  'clip-decorations' is still running` followed by a core dump — with the failure
  varying between runs of identical code. Cause: a window that is only **closed**
  stays alive, so its threads, media players and child widgets are destroyed while
  PySide6 is already running its own `destroyQCoreApplication()` cleanup at
  interpreter exit, and that ordering is not safe. It reproduced reliably in
  isolation (8 of 8 runs of `tests/test_viewer.py`, which holds three windows,
  while each test in it was clean alone: 0 of 8) and intermittently for the whole
  suite. What was changed: `MainWindow._shutdown_preview()` is idempotent and
  releases the media sources it held; `ClipDecorations.__del__` stops its thread
  even when the worker is unreachable; `run_app()` and the test session fixture now
  destroy the window (`deleteLater()` + processing events) while `QApplication` is
  still alive. Regression tests added for the two window-level guarantees
  (`test_shutting_a_window_down_twice_is_safe`,
  `test_a_layer_dropped_without_shutdown_does_not_take_the_process_with_it`).
  **Status: not proven eliminated.** It has not been reproduced since those
  changes, but a defect that depends on teardown order and object lifetimes cannot
  be declared fixed by the runs that happened to be performed, so it stays listed
  under *Outstanding environment checks* rather than being closed here. Repeating
  the suite was used only to reproduce and then bound the problem during diagnosis;
  it is **not** this project's verification method, and a task is now closed on a
  single reported run with its exit code. Records above that mention three
  consecutive runs describe what was done at the time, not the standing practice.
- A check that only reads back *structure* hides real defects: the fade bug lived
  behind a passing export test because that test's fixture happened to place the
  faded clip at the range start. Fixture tables should state expected **media**,
  and the measurement should be taken somewhere the bug could hide.
- The A/B fixtures need no attenuation for the mix warning to be meaningful:
  lavfi `sine` peaks ≈0.124 (−18 dBFS), so two overlapping contributions measured
  0.2488 after the plain sum — the plan's "can sum to 2.00× full scale" is a
  conservative gain bound, not observed clipping.
- `threading.enumerate()` cannot see Qt worker threads, so a thread-count check
  written against it asserts nothing; the long-project check counts the process's
  own `/proc/self/task` entries instead.
- The timeline's own chords (`+`/`-`, `Home`, arrows, `Esc`, Ctrl+wheel) are not
  `QAction` shortcuts, so a generated chord inventory needs them declared
  explicitly — and an old module docstring claiming `Ctrl+Plus` was wrong.

Limitations, stated rather than hidden:

- Decorations are validated by measurement (bucket counts, byte budgets, PTS
  placement, "nothing painted before delivery") and by the window check confirming
  something arrives; nobody has judged on screen whether a strip is *useful* at a
  given zoom ladder rung. That is a human judgement left for the review.
- The long-project profile uses two 2-minute synthetic sources reused across 843
  clips. It measures timeline scaling, not decode cost of a real feature-length
  file; still-frame seek latency on long-GOP real sources remains unmeasured.
- No full-length render was performed: full-duration *planning* and selected
  windows are what TASK_08 requires, and the chunked path is exercised in tests.
- The real-film smoke-test procedure (README, *Real-film smoke test*) has not been
  carried out: no copyrighted media is bundled and none was used. Perceptual
  checks — glitches at cuts, drift over minutes, how a preview or thumbnail looks
  and sounds — still need a human with real films.

## TASK_09 — Protect source media and enforce asset identity — DONE

Corrective milestone on the review of `0c6b8f1`. No new editing feature.

Findings confirmed against the code, and their fixes:

| Finding (baseline) | Fix |
| --- | --- |
| `RenderRequest.resolve_tools` accepted destination == an imported media path when `overwrite=True`, and `render()` ended in `staging.replace(destination)`; the UI's overwrite prompt claimed imported assets are never modified | `fs.protection.DestinationGuard` is checked in `resolve_tools()`, again before publication, and in `save_document`; a collision raises before anything is staged, regardless of `overwrite` |
| Save As accepted an arbitrary existing filename; `save_document` replaced it | `save_document(..., protected=..., consent=...)`; media collision refused, replacement needs a consent captured from that exact file |
| Existence checked, identity not: a different encode at the same path was used with stale bounds/indexes during a session | `fs.identity` states `ONLINE/MISSING/CHANGED/UNREADABLE`; `MainWindow._availability()` re-checks per asset and feeds media status, stills, stream previews, decorations and render preflight |
| Preflight/validation happened long before publication; an unconditional replace could clobber a file created meanwhile; a cancel after the last progress event could still publish | `fs.protection.publish` (no-clobber `os.link` / consented `os.replace`), `Consent.capture` at preflight re-checked immediately before publishing, and a cancellation check after verification |
| Identity/availability existed only in `project/storage.py`; the UI used `is_file()` | One policy in `video_editor/fs/`, asset helpers in `media/availability.py`, `storage.AssetState` now an alias of `fs.identity.AssetStatus` |

Design notes:

- New lowest package `video_editor/fs/` (stdlib only, imports nothing from this
  project) so `media`, `render`, `project` and `ui` share one rule set; the
  asset-typed layer is `media/availability.py`. Dependency edges added:
  `render → fs`, `project → fs`.
- Scope is honest in both directions: source checks cover the assets the
  rendered **range** reads (`RenderRequest.assets_used(plans)` →
  `check_sources`), destination protection covers **every** asset of the
  catalogue including unplaced/muted ones, plus the project file for renders.
- Sources are validated before decoding **and** again before publication, and
  per asset rather than per clip (the decoration layer likewise: the
  `Job` carries the *recorded* identity and re-checks it at decode time, so a
  file swapped after scheduling cannot be drawn as this project's media).
- Identity is size + mtime_ns — the same policy the project format has always
  written. A file rewritten with an identical size *and* mtime is not
  detectable and the docs say so; what is caught is a re-encode, a replacement
  or a truncated copy.
- Publication rules: nothing there → `os.link` (an `EEXIST` race is a refusal,
  not an overwrite); something there → `Consent.capture`d identity re-verified,
  else refuse; cache-owned destinations → `Consent.as_owner`, allowed only after
  `PreviewCache.owns()`. The previous file is never removed before a successful
  publish; staging leftovers are always cleaned.
- Wording changes users will see: MISSING now says "…is not there: it was moved,
  renamed or is on a volume that is not mounted", CHANGED says "…is not the file
  recorded here: it was X, it is now Y… may be a different encode". Tests that
  asserted the old sentences were updated deliberately.

Verified (single runs):

- `pytest -q` offscreen: **580 passed, 0 failed, 0 skipped**, exit 0
  (537 before this task: 43 new tests in `tests/test_media_protection.py` and
  `tests/test_ui_source_safety.py`, plus additions to the storage, exporter,
  export and framegrab test modules).
- `ruff check .`: clean.
- Unattended desktop checks after the change, each one run: window acceptance
  **133/133** (`exit=0`), two-edition A/B workflow **197 checks / 0 failed**
  (`exit=0`), long-project profile **21 checks / 0 failed** (`exit=0`).
  The A/B check initially failed on this task: it asserts that a moved-source
  refusal *names the state and the file*, and the new wording had dropped the
  state word. The exporter message now carries it
  (`… cannot be used before rendering [missing]: edition_b.mkv (…) is not
  there…`), which is what the check — and the user — needs.

Regression tests added (all with synthetic files, never user media):

- `tests/test_media_protection.py`: identity states (including a directory and
  an uninspectable path as UNREADABLE, and "no recorded identity claims
  nothing"); guard by path, relative alias, symlink, hard link and an
  *outgoing* symlinked directory; `publish` no-clobber, consented replacement,
  changed-after-consent refusal, vanished-then-recreated refusal, protected
  media refused despite consent, owner-authorised cache replacement, directory
  destination.
- `tests/test_render_exporter.py`: export onto an asset (overwrite=True) refused
  with the bytes unchanged and nothing staged; symlink/hard-link aliases;
  an **unused** asset still protected; a destination that appears mid-render
  survives byte-identical; a destination replaced after consent is refused; a
  source swapped mid-render is refused **before publication**; a cancel set
  after the last progress event publishes nothing. Range scoping: an offline
  asset the range never reads does not block the render.
- `tests/test_project_storage.py`: Save onto an asset refused (message names the
  asset, no temporary debris, media bytes unchanged), symlink/hard-link targets
  refused, a document created after the consent is never clobbered, and the two
  existing atomic-save tests now carry the consent they imply.
- `tests/test_framegrab.py`: a still of a replaced source is refused by the
  identity check (proven by decoding the *same bytes* when no identity is
  recorded).
- `tests/test_ui_source_safety.py` (window-level, prompts recorded not shown):
  CHANGED vs MISSING wording; 42 clips cost 3 availability checks; export onto
  imported media refused with the film byte-identical; hard/symlink alias and
  unused-asset protection; export onto the project's own `.vep` refused; Save As
  onto media refused with no debris; saving the project document still works;
  timeline pictures withheld for a changed asset and restored by a validated
  relink; relink refuses an unreadable target and changes nothing.

Deliberate deviations: none. Requirement 3's "do not stat every clip on every
paint" is enforced by the once-per-asset catalogue check and asserted by
`test_availability_is_checked_once_per_asset_not_per_clip`.

## TASK_10 — One transactional project document and usable linked editing — DONE

Corrective milestone on the review of `2205d37`. No new editing feature: the
editing was already there, but its *bookkeeping* was split in two, and that
split was observable as data risk (unsaved imports, half-restored relinks) and
as unusable linked editing (a split left all four halves glued together).

Findings confirmed against the code, and their fixes:

| Finding (baseline) | Fix |
| --- | --- |
| `ProjectSession` tracked only `core.Project`; `_project_document()` serialized a separate mutable `bin_controller.assets`, so an import with no clip changed the saved document without setting dirty (and Save / New / Close then behaved as if nothing had happened) | `project/document.py:EditorDocument` (composition **plus** catalogue) is the snapshot; `DocumentSession` holds the single saved baseline, so import, relink and placement all dirty, undo and autosave together |
| `_relink_asset_to` applied stream changes through history but patched the bin record separately: undo could pair old streams with the new file, and an identical-stream relink left the document un-dirtied while `test_a_relink_to_an_identical_file_changes_the_media_not_the_composition` accepted that | `RelinkDocumentCommand` = `ReplaceAssetCommand` + `RelinkAssetCommand` in one transaction; the test's *distinction* (composition unchanged, media moved) is preserved and its document-level expectation reversed: that relink is dirty and undone as one step |
| `_ensure_sources` applied several commands per gesture and `_find_free_track` added a track *before* placing the clip, so one drop made several history entries and left orphan tracks/registrations when the clip was refused | `PlaceStreamsCommand` + `StreamPlacement` + `TrackChoice` compute the whole result on a candidate and publish once; `group_placements()` is the single group-offset rule shared with `ImportGroupCommand` |
| `SplitClipCommand` gave every member's both halves the **same** `link_group`, so all four stayed linked: moving the right half moved the left, and a second split could be refused | left pieces keep the original id, all right pieces share one new id, a one-member group normalizes to unlinked; `validate_project` reports a lone group member |
| `validate_project` checked clip ids but not duplicate track ids / source keys, or whether a registered stream had a catalogue record; an invalid loaded state became a history baseline | `validate_project` extended; new `validate_document` cross-check; run at **load** (`storage.decode_payload`) and at **commit** (`EditHistory.check` hook) |

Design notes:

- History became generic (`EditHistory[S]`, `SnapshotCommand[S]`) instead of the
  document being special-cased, so the composition commands were reused as they
  are through `EditComposition(...)`. `ProjectSession` stays as a core-level
  convenience; the application never uses it.
- What stays **outside** the document, deliberately: playhead, zoom, scroll,
  selection, import jobs in flight, and disk availability. `check_asset` re-reads
  identity after undo/redo, so an undone relink cannot inherit the other file's
  state word.
- `AddAssetCommand` refuses a second record for a file the project already has —
  same asset id, same resolved path, or the same file under another name (TASK_09
  `same_file`), with wording that names the existing entry.
- Placement never *re-describes* a stream that is already registered: the
  project's own record wins, and the refusal tells the user to relink if the
  file really has changed. Re-description would silently invalidate bounds that
  existing clips were validated against.
- `TrackChoice.ALLOCATE` reuses the topmost free compatible lane (so a long film
  does not gain a track per clip); `FRESH` always adds one and is what whole-file
  import uses, so each imported edition keeps its own lanes.
- The bin is a projection (`MediaBinController.assets` reads the document,
  `MediaBin.sync` redraws). The "already imported" path index is resynced from
  the document on every accepted change (`MainWindow._sync_bin`), so undoing an
  import frees the file for import again and an undone relink re-claims the old
  path; `MediaBinController.reindex` (a manual patch of one entry) is gone, and
  `finalize_finished` no longer claims a path the document might refuse.
- Availability is recomputed at that same moment — one `stat` per asset, never
  per clip — so an undone relink cannot inherit the state word of the file it no
  longer points at. `_media_records` is refreshed by `_sync_bin`, which runs on
  install, import, relink, undo and redo.
- Recovery snapshots and autosave therefore cover the media list as well; a
  project with imported-but-unplaced media is offered as unsaved work at startup.
- **Schema v1 unchanged** — the format already held both halves, so no migration
  was needed. What changed is that the loader no longer accepts the two halves
  disagreeing, and a snapshot with registered streams but no asset record is
  refused instead of warned about.

Wording/behaviour changes users and tests will see (tests updated deliberately):

- A project file whose registered streams name an asset it does not record is
  **refused**: `streams 0, 1 of asset X are registered, but that asset is not in
  the media catalogue`. It used to load with a warning and produce clips that
  could never render.
- A command whose result would be structurally unsound is refused with
  `the edit would leave an invalid project: …` rather than becoming a baseline.
- History refuses a session whose document was replaced outside it:
  `Document changed outside history; undo unavailable`.
- Re-importing a file the project already has says `… is already imported as
  '…'` and records nothing.
- Duplicate-id/key breaks are named at the field level by the decoder
  (`tracks[4].id 'vtop' is duplicated in this file`) before the validator's
  broader `invalid project` report gets a chance.
- The old `test_linked_split_splits_all_members_atomic` expectation (one group
  for all four halves) was **rewritten**, because it asserted the bug.

Verified (single runs):

- `pytest -q` offscreen: **622 passed, 0 failed, 0 skipped**, exit 0 (580 before
  this task: 42 new tests in `tests/test_document_session.py`,
  `tests/test_ui_document_edits.py` and the new corrupt-file cases in
  `tests/test_project_storage.py`).
- `ruff check .`: clean.
- Unattended desktop checks, each one run after the change: window acceptance
  **134/134** (`exit=0`, one check added — see below), two-edition A/B workflow
  **197 checks / 0 failed** (`exit=0`), long-project profile **21 checks / 0
  failed** (`exit=0`).
- The cost of validating the whole document at commit is measured, not assumed:
  an edit plus its undo on the 843-clip / two-hour project stays at
  **25.6 ms** in the long-project check, and the whole-timeline plan, repaint and
  playhead budgets are unchanged.
- Two desktop-check expectations were **rewritten because they encoded the old
  behaviour**: `export_check.py` asserted that after undoing a relink the asset
  still pointed at the relinked file (exactly the half-restored state TASK_10
  removes). It now asserts the opposite and new — undo puts back the path *and*
  the recorded identity, which makes that asset **missing** again, and a
  *refused* relink leaves that untouched. One check was added for it.
- Nothing in the suite may open or block on a window. One new test did exactly
  that during development: the unsaved-work prompt answered "Save…" reached a
  real `QFileDialog.getSaveFileName` and hung the run. Diagnosed with a
  `SIGABRT` faulthandler dump of the wedged pytest, then fixed in the test
  fixture (see *Regression / acceptance tests added*): no modal path in these
  tests is left real.

Regression / acceptance tests added:

- `tests/test_document_session.py` (pure, no Qt, no FFmpeg): import with nothing
  placed is undoable, dirtying, stable-id-preserving work; a failed import
  dirties nothing; duplicate id / same path / hard-link alias refused; relink
  with **changed** stream metadata restores path + identity + bounds together on
  undo, and a path-only relink is dirty while `project` compares equal;
  `PlaceStreamsCommand` registers + allocates + creates as one entry and a
  refused placement leaves no registration, no lane, no history entry; a group
  keeps the container's own offsets (picture starting 2 s into the file);
  `ImportGroupCommand` and `group_placements` agree exactly;
  `CompositeDocumentCommand` publishes once or not at all (the seam a later
  assisted edit will use); `validate_document` names duplicate asset/track/
  source/clip ids, overlaps, out-of-bounds intervals and lone link-group
  members; offline media is **not** a structural problem.
- `tests/test_ui_document_edits.py` (window, generated media, prompts recorded):
  import-only project is dirty, Save-able, undone/redone, autosaved, prompted
  for at New, written by the "Save…" answer, and reopened as media; a garbage
  import does not dirty; one undo reverses a whole placement (clip + lane +
  registration) and redo restores the same clip id; a refused insert leaves no
  lane and no registration; a group placement is one action; a linked split
  leaves two independently movable and re-splittable pairs with three group
  ids, and undo restores the original one; a relink of identical streams is
  dirty, saves, reopens and undoes with path **and** recorded identity; saving
  after history navigation writes what history selected; a file naming
  unrecorded media, an unknown schema version and a non-JSON file are each
  refused without touching the open document.
- `tests/test_project_storage.py`: eight parametrised corrupt-file cases
  (duplicate track/clip/source, unknown stream, bounds beyond the stream,
  source before origin, kind mismatch, missing track), the unrecorded-catalogue
  refusal, and that offline-only media still loads.
- `tests/gui_helpers.py`: the supported way for a fixture to give a window a
  document (`install_document`, `media_record`, `offline_asset`, `import_file`),
  replacing the old "poke the session and append to the bin" pattern that could
  manufacture a state the application itself cannot reach.

Deliberate deviations: none. The task's "retain the current project schema if
possible" is met by keeping v1 unchanged; the task's "reuse `ImportGroupCommand`
where practical" is met by sharing its offset rule rather than routing the UI
through the older command, so the UI gets the candidate-then-publish behaviour.

## TASK_11 — Exact audio slicing, sample-rate conversion, fades and output validation — DONE

Corrective milestone on the review of `6a09c8b`. No new user feature: what audio
was asked for was already in the project, but the render path never *stated* the
slice it would produce, fades were applied at filter-frame resolution rather than
sample resolution, and a finished export was judged by nothing but its container
duration. Everything below is measured on decoded samples or counted packets, on
synthetic fixtures with distinct tones per second.

| Finding (baseline `6a09c8b`) | Fix |
| --- | --- |
| one `atrim …:end_sample=<output samples>` cap, written at a point in the chain where the samples being counted were whatever rate FFmpeg had negotiated there; the input was also read to the end of the file (`-ss` with no `-t`) | `AudioItem` states the whole contract (source slice in exact source seconds, `clip_samples`, range-clipped `start_sample`/`samples`/`pre_samples`, fades in output samples); the graph converts rate and layout **before** any counting, bounds the input with `-ss`/`-t`, and caps the read in **source** samples (`_source_sample_cap`) |
| fades as `volume=expr:eval=frame`, recomputed once per filter frame, and written in render-range time | `afade=t=in\|out:curve=tri:start_sample=…:nb_samples=N` over the whole clip, anchored to the clip rather than the range, then the range is cut |
| no bound on what a contribution could read, no statement of which axis a count belonged to | the chain order in `docs/architecture.md`; `_check_source_slice` refuses a clip reaching past a recorded stream end |
| an export was accepted when `format.duration` was close enough | frames counted per stream (`-count_packets`) and compared exactly with `plan.total_frames`; sound decoded and measured (`measure_decoded_span`) and compared with `plan.total_samples`; sample rate and channel count checked; a produced file with no packets or an undecodable stream is refused |
| a source whose container overstates its own end produced invented silence at the end of a clip and nobody said so | `RenderRequest.check_source_tails`: the last 2 s of the sound a clip presents up to its recorded end is decoded before encoding; more than `SOURCE_TAIL_TOLERANCE` (1/24 s) short → refused, less → warned, unmeasurable → warned |
| `normalise_layout("5.1(side)") == "5.1"` — side speakers renamed to rear speakers | `SIDE_LAYOUTS` refused with the reason; `plan._check_layout` warns where a remap is a guess (unrecorded layout or `…(side)` source into a surround output) |

Measured on FFmpeg 8.1.3 (all of it re-runnable through the tests named below):

- **Old fades did not exist at audio resolution.** A 40 ms fade-in of a constant
  0.5 signal came out as exactly two levels — `0.0`, then `0.5` from sample 4096
  — a worst per-sample error of **0.42656** of full scale against
  `core.model.envelope_gain`. With `afade` the same ramp is right to **< 1e-6**
  per sample (measured with `pcm_f32le`: `pcm_s16le` quantises at 1/65536, the
  same size as the error under test).
- **The old extent happened to be right, and that is not a pass.** The review
  described a 4 s item at 44.1→48 kHz requesting 192 000 *input* samples. That
  exact shape was rendered through `6a09c8b` (an 8 s stream of 2 s sections at
  330/660/990/1320 Hz, selection `[1, 5)`): the result held 330→660→990 in the
  right places, exactly 4 s, with silence after the clip rather than the 1320 Hz
  block, and 44.1→48, 96→44.1 and 48→48 two-second selections behaved the same.
  FFmpeg auto-inserts `aresample` before the old `atrim` when `aformat` is the
  only rate hint, so the promise belonged to negotiation rather than to the graph.
  It is now written out and locked by `test_a_selection_is_heard_throughout_its_whole_extent`
  (12 cases: 44.1/48/96 kHz sources × 44.1/48 kHz output × rates 1 and 25/24,
  measured at the first, middle and last quarter of the contribution) and
  `test_nothing_outside_the_selection_leaks_into_the_following_gap`.
- **Chunk and preview joins are exact for lossless material.** Rendering
  `[0, x)` + `[x, end)` and concatenating the decoded samples differs from one
  render by exactly **0.0** at rates 1, 25/24 and 24/25, including a boundary in
  the middle of a 1 s fade and inside a 12 s retimed clip (`CHUNK_JOIN_EXACT`).
- **`atrim` time options are broken here**: `end_time`/`end_duration` returned 0
  or 42 samples for a 2 s window, while `end_sample=100000` cut an AAC source at
  an exact sample mid-frame. Only whole-sample addressing is used.
- **A PTS rebase was needed after the range cut**: measured before it, exporting
  output `[2, 6)` produced a container of **3.0 s for a 2.0 s plan**, because
  `atrim` keeps the timestamps of what it keeps.
- **Packet sizes are a container detail, not a codec constant.** PCM in Matroska
  writes multi-sample blocks — 25 packets for a 96 000-sample render, i.e. 3840
  samples each — while AAC writes 1024. The first version of the output check multiplied packets by a per-preset
  constant and therefore refused a perfect lossless render ("25 samples at
  48 000 Hz, but the plan calls for 96 000"). Replaced by measurement: decoded
  audio is **+0 samples** for the lossless intermediate, **+256 samples at
  48 kHz** and **+888 at 44.1 kHz** for AAC (allowed slack `2048/48000` s).
  Picture frames are counted exactly: 50 packets for 50 planned frames at
  2 s/25 fps, single-pass and chunked.
- **A truncated file does admit nothing.** A Matroska rendered whole and then cut
  to 70 % of its bytes still reports `duration 6.000000` per stream and per
  format (Cues are written at the end, so the copy lost its index too); decoding
  `-ss 4 -t 2` from it yields **1.81167 s**. That is refused by
  `check_source_tails`; the same cut applied to our *own* MP4 output is refused by
  `verify_output`, even though `probe_duration` on it still returns a nonzero
  duration.

Verified (single runs, `QT_QPA_PLATFORM=offscreen`, nothing left to repeat):

- `pytest -q`: **672 passed, 0 failed, 0 skipped**, exit 0 in a single run
  (622 before this task: 50 new tests, 21 of them the real-FFmpeg measurements in
  `tests/test_render_audio_timing.py`). `ruff check .`: clean.
- Unattended desktop checks, each re-run after the final change: window
  acceptance **134/134**, two-edition A/B workflow **0 failed**, long-project
  profile **0 failed** (peak memory 150 MiB, 843-clip project 290 023 bytes).
- No new delivery codec was added: the delivery preset, the preview preset and
  the lossless chunk intermediate are the same three as before; only the graph
  and the checks around them changed.
- `render/cache.SEMANTICS_VERSION` is now `"3"`, so preview files made by the old
  chain are never reused.

Regression / acceptance tests added or deliberately rewritten:

- `tests/test_render_audio_timing.py` (new, 21 FFmpeg cases): selection heard
  through its whole extent across sample rates and clip rates; nothing outside
  the selection leaking into the following gap; fade-in and fade-out straight
  lines at sample resolution; a range cut *inside* a fade continuing the ramp
  (three rates); a retimed clip across a chunk boundary staying within the
  documented (measured zero) deviation while staying free of holes; two tracks
  from 44.1 kHz and 96 kHz streams, one of them a stream that starts at 1 s,
  placed at `1/3 s` with a 20 ms fade, `1/5` and `2/5` gains — checked for
  silence before the first clip, both tones present in the overlap at the gain
  ratio, the surviving tone after the other clip ends, and silence after the
  timeline.
- `tests/test_render_graph.py`: `test_fades_are_sample_accurate_afade_stages`
  and `test_fades_are_anchored_to_the_clip_then_cut_to_the_range` **replace**
  `test_fades_become_frame_evaluated_volume_envelopes` and
  `test_the_envelope_is_applied_to_the_positioned_contribution`, which asserted
  the frame-stepped envelope and its range-relative clock — those assertions
  encoded the defect. New: `test_the_source_slice_is_bounded_on_the_streams_own_axis`
  (input `-t`, source-sample cap before `aresample`, output count after),
  `test_a_range_inside_a_clip_still_builds_the_whole_clip` (including the PTS
  rebase), `test_side_speakers_are_not_treated_as_rear_speakers`.
- `tests/test_render_exporter.py`: the tail measurement (complete / 20 ms short /
  a whole second short / unmeasurable), that a clip which never reaches its
  stream's end pays no probe, `probe_source_material` against a real file, and
  the output-validation rules one at a time — missing stream, wrong geometry,
  missing frames, empty sound stream, wrong sample rate, wrong channel count,
  undecodable/short/long measured sound — plus the encoder-padding edges.
- `tests/test_render_export.py`: counted frames and measured samples for a range
  render, for a chunked render, and for the lossless intermediate (which must
  measure exactly the plan, the case packet arithmetic got wrong); a truncated
  output refused although it reports a duration; a source shorter than its own
  header refused rather than padded, and a clip that stops short of the damaged
  end rendering normally.
- `tests/test_output_settings.py`: the `5.1(side)` expectation **reversed** — the
  core still stores whatever the file said, the render layer refuses.
- `tests/media_fixtures.py:make_truncated_audio` — a lossless Matroska written
  whole and cut short, whose container still claims its full length.

Deliberate deviations: two, both recorded rather than quietly taken.

1. The task asks for an audio tolerance "derived from sample rate and codec" when
   validating the output. The first implementation did exactly that — a per-preset
   `audio_frame_samples` multiplied by counted packets — and it was wrong in
   practice: PCM in Matroska writes 3840-sample packets, so a perfect lossless
   render was refused as holding 25 samples. The check now *measures* the decoded
   sound and allows a time-based slack (`AUDIO_PADDING_SECONDS`) that the measured
   encoder padding (+256 samples at 48 kHz, +888 at 44.1 kHz) sits inside. A codec
   constant is not needed and the `audio_frame_samples` field was removed.
2. The task allows "bounded preroll/overlap" to keep tempo phase across chunks.
   Chosen instead: each range rebuilds the clip from that clip's own start and cuts
   the range afterwards, which is the "render contiguous tempo-processed slices
   once" option and measured as a zero difference. No preroll or overlap window
   exists, so there is nothing extra to tune; the remaining limit (lossy sources
   seek to their own boundaries) is listed under *Outstanding environment checks*.

Notes in the spirit of recording rather than hiding: the first output-validation
implementation in this very task used packet arithmetic and was replaced after
being measured wrong (PCM in Matroska), which is why both the measurement and its
regression test are in the suite; and the tail probe is a guard rather than a
proof — on a file whose index was itself destroyed the measured window can start
earlier than asked (the truncated Matroska above measured 1.81167 s rather than
the true 0.2 s), though still short enough to be refused.

## TASK_12 — Truthful frame inspection and a common picture sampling contract — IN PROGRESS, NOT ACCEPTED

Corrective milestone on the review of `0c6b8f1`. Work was **deliberately paused
with failures standing** so the reviewer can rule on two questions that change
what the composition promises (`TASK_12.md` §2 and §5). The full record — decisions,
measured tables, the defects found, the failing test names and the process lessons —
is `TASK_12.md` §1–§8; the binding decisions are in `docs/architecture.md`
▸ *Truthful frame inspection and one picture sampling contract (TASK_12)*.
Nothing in this section was verified through a real GUI window
(`QT_QPA_PLATFORM=offscreen` only).

| Finding (baseline `0c6b8f1`) | State at this commit |
| --- | --- |
| source stepping changed `source_pts` while keeping the old picture, and a cut was then trimmed with that timestamp | asynchronous source stepping (`ui/render_worker.SourceStepController`): newest request wins, rapid clicks accumulate, stale completions are discarded, `Mark In`/`Mark Out` are disabled while a frame is pending, and the cut uses the *measured* PTS of the picture on screen |
| every packet PTS probed synchronously on the GUI thread, a full `SourceFrame` list rebuilt per click | `media/frameindex.FrameIndex`: bounded 20 s windows, ≤4 probes per question, ≤6 cached windows, `bisect` neighbours; the cost is asserted in `tests/test_frame_index.py` (one question, one probe) |
| `extract_composition_still` inferred a decoded timestamp from neighbouring *packet* timestamps after a separate decode | `showinfo` taps the still decode itself, before resampling; `StillFrame.decoded_source_pts` is measured from that decode and snapped to the container's tick grid, `None` (with a stated reason) when it cannot be measured |
| a fresh one-frame plan at an unquantised time; `fps` restarted at each seek, so stills, short ranges and whole exports could disagree at the same output index | one ceil lattice (`Quantisation.frame_index == frame_count == ceil(t·rate)`) and one `PictureSampling` contract (grid, tick-aligned seek, read bound, head/tail holds); one `picture_filter_chain` shared by export, chunks, preview and stills; `SEMANTICS_VERSION` → `"4"` |
| the picture chain did not implement its own stated rule | `fps=round=up`, chosen by measurement (777/2410 frames wrong with the previous `round=near`, 26/2410 now); the 26 are a documented residual awaiting a reviewer decision |
| rounding could sample outside `[clip.source_in, clip.source_out)`; edge policy implicit | sampling clamped to the legal slice; where the lattice asks for material the slice lacks, `tpad` clones the nearest legal picture; a still at/after `project.output_end()` is refused by name while export ranges keep the TASK_05 explicit-black rule; `prepare_preview` refuses a zero-frame range |
| SAR reset without a display-aspect contract; anamorphic/interlaced/HDR unaddressed | **open**: the anamorphic case is now tested and fails (black borders from a 16:9-DAR source into a 16:9 output, cause not established); interlaced and HDR still have no refusal and no fixture |

Measured at this commit:

* `ruff check .` clean. `tests/test_frame_index.py` + `tests/test_framegrab.py`
  **26 passed in 20.62 s**. Render/cache/exporter scope
  (`test_render_graph`, `test_render_plan`, `test_render_preview`,
  `test_render_export`, `test_frame_index`, `test_render_cache`,
  `test_render_exporter`) **180 passed in 68.9 s**.
* `tests/test_picture_identity.py` (the acceptance suite for this task, new
  identity-colour fixtures: constant rate, B-frames, nonzero start, VFR,
  anamorphic): **9 passed / 10 failed in 186.13 s**. The failures are listed by
  name in `TASK_12.md` §6; one was investigated and is a genuine chain question,
  nine are undiagnosed.
* Three defects found and fixed while chasing a test that never finished — an
  infinite loop in `FrameIndex._search` that presented itself as a hung export,
  a `frames_in` lower bound of a whole second, and `ffprobe -read_intervals`
  answering past-the-end requests with the stream's tail
  (`tests/test_frame_index.py` is the regression net). Separately, the
  `showinfo` parser matched nothing at all, so **no** path had ever measured a
  source position, and no test had noticed because the expectations asserted the
  implementation's `None` (`TASK_12.md` §5).
* The anamorphic fixture itself was wrong — `-vf setsar=64:45` is parsed as two
  options, so the file carried SAR `45:1` / DAR `225:4` (measured with
  `ffprobe`). Corrected to `setsar=64/45`; the test still fails, now for a real
  reason (`TASK_12.md` §6).

Not run before this commit, and therefore unverified: the whole suite,
`scripts/desktop_checks/export_check.py`, `scripts/ab_workflow_check.py`,
`scripts/long_project_check.py`, and the acceptance item "measure navigation
responsiveness on a long synthetic source" beyond the bounded-cost tests above.
The GUI-thread `ffprobe`/`ffmpeg` calls in `_probe_asset` and
`extract_stream_window` are still there by design: cancellation, bounded
executors and shutdown hardening are TASK_13's scope.



## Outstanding environment checks

- **Open: TASK_12 is not accepted.** Its acceptance suite
  (`tests/test_picture_identity.py`) reports 9 passed / 10 failed, and the whole
  suite plus the three unattended check scripts have not been run since the
  sampling lattice and picture chain changed. The display-aspect behaviour of the
  shared chain (anamorphic 720×576 / SAR 64:45 into 1280×720) is measured to
  produce black borders and is not understood. Interlaced and HDR inputs have no
  explicit refusal and no fixture. See `TASK_12.md` §6–§8.
- **Open: the audio path has never been *heard*.** TASK_11 measured slice extent,
  resampling, fades, chunk joins and mix levels on decoded samples, and the
  desktop checks measure tones and levels in the rendered file; nobody has
  listened to a resampled or retimed clip on real speakers. What only a human can
  judge is whether FFmpeg's resampler and `atempo` sound acceptable on speech and
  music (as opposed to on sine tones), and whether a 44.1 kHz source inside a
  48 kHz mix is audibly worse than an ordinary resample.
- **Open: chunk joins are proven for lossless material only.** The measured
  zero-deviation result covers PCM sources; a *lossy* source (AC3/AAC/DTS) read
  from two chunks seeks to a keyframe/priming boundary, and that difference has
  never been measured. The documented promise is a bounded tolerance, and no test
  asserts it for a lossy source.
- **Open: the new output checks are timed only on short fixtures.** Decoding the
  produced sound and the two-second source-tail probes are cheap here; the cost on
  a feature-length export (and on many clips that each end at their stream's end)
  is unmeasured.
- **Open: exit-time abort in GUI runs.** A test run (and, on the same code path, a
  real quit) can die *after* its result is known: SIGSEGV, bus error, or
  `QThread: Destroyed while thread 'clip-decorations' is still running` followed by
  a core dump, while destroying Qt objects during interpreter shutdown. It was
  reproducible at will in one configuration and intermittent elsewhere; the shutdown
  paths were made idempotent, media sources released on close, and windows destroyed
  while `QApplication` is still alive (see TASK_08 findings). It has not been seen
  since, and it has not been proven gone: nothing in this project repeatedly runs the
  suite to hunt for it, so treat it as unresolved. What would settle it is a
  deterministic teardown — windows destroyed by the code that created them, no
  QObject outliving its owner — rather than a teardown order that happens to work.
- The two-edition workflow (TASK_08) has been validated on synthetic editions
  only: the fixtures are measurable, but nobody has yet run the recipe on two real
  cuts of one film. The procedure for that is README ▸ *Real-film smoke test*;
  the perceptual judgements it asks for (glitches at cuts, drift over minutes,
  whether a thumbnail or envelope is readable at a given zoom) cannot be automated
  away. See the TASK_08 limitations above.
- Preview and still-frame correctness was verified by measurement (container
  parameters, index-selected frame colours compared with the export, spectral
  audio share, playback position mapping in tests). What has **not** been verified
  is perceptual: the checks run with dummy video/audio outputs, so nobody has
  watched or listened to a preview on real speakers, and the visual quality of the
  speed-tuned preview preset and Qt Multimedia's seeking inside a preview file are
  unverified by eye and ear. Run
  `QT_QPA_PLATFORM=xcb DISPLAY=:0 python scripts/desktop_checks/export_check.py show`
  on a desktop to do that.
- The still-frame latency figures above come from short synthetic fixtures. A real
  feature-length source with long GOPs can force a longer decode before the
  requested frame (input `-ss` decodes and discards from the previous keyframe);
  that cost has not been measured on real films.
- Preview cache budgets were exercised in tests with small budgets, not by a long
  editing session over real films.
- Qt Multimedia's FFmpeg playback backend was verified manually in TASK_04
  (source mode: PlayingState, position updates, no errors). What has *not* been
  verified is long-form real-film playback or seeking under load.
- Export was verified end-to-end (GUI → worker → FFmpeg → VLC). Not yet
  exercised on real feature-length films, HDR sources, or >2-channel output;
  those remain untested and are expected to fail clearly rather than work.
- Chunked rendering is exercised by tests with a low item limit, not by a real
  feature-length export that exceeds the 96-item graph bound.
- Project save/reopen, relinking and recovery are verified on synthetic fixtures
  and in the unattended desktop check (including a second window reopening a
  saved project and re-exporting it). Not yet exercised: a multi-hour session over
  real films with several discs/paths, a project file on a network share or
  removable volume (where `mtime_ns` granularity and `os.replace` semantics can
  differ), and Windows path handling (`relpath` across drives is coded to fall
  back to absolute references but has never been run there).
- Recovery after an *actual* crash (SIGKILL, power loss) has not been simulated
  end-to-end; the tests write snapshots through the same code path the timer
  uses, and the restore path is exercised in-window, but no test has killed the
  process and restarted the application.

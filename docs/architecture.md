# Architecture

Decisions, contracts and invariants. Updated as tasks land. See
`docs/progress.md` for task-by-task status.

## Layering (as planned by AGENTS.md)

```
core/     domain model + commands + history        standard library only
fs/       file identity + write protection         standard library only
media/    probing, decode helpers, FFmpeg adapter  no Qt
render/   render plan from immutable snapshot      no Qt
project/  serialization, document session, relink  no Qt
ui/       Qt widgets; calls core commands only
```

Dependency direction: `ui → {core, fs, media, render, project}`,
`render → {core, fs, media}`, `project → {core, fs, media}`,
`media → {core, fs}`. `core` imports nothing from the other layers; `fs` imports
nothing from this project at all, so every layer enforces the same file-identity
and write-protection rules (TASK_09). Only `ui/` may import Qt. The one
cross-layer exception inside the media/render pair is
`media/framegrab → render.plan`, which reuses the composition plan builder to
grab a single output frame rather than decode one stream.

Not-yet-implemented layers are absent from the tree; they are created by the
task that first needs them (no elaborate stubs).

## Current decisions

### TASK_00 — bootstrap

- Python >= 3.12 declared; development runs on 3.14. Build backend: hatchling,
  `src/` layout, package `video_editor`.
- Entry points `video-editor` (console script) and `python -m video_editor`
  both call `video_editor.cli:main`. The `doctor` subcommand reports Python,
  PySide6 and ffmpeg/ffprobe paths + versions and exits non-zero when the
  media backend is unavailable.
- FFmpeg detection (`video_editor.diagnostics`) never raises. Availability
  check runs `ffmpeg -version` / `ffprobe -version` with subprocess argument
  lists (never shell strings) and a timeout. Missing tools are surfaced in the
  window status bar; the empty window remains fully usable.
- Media tool versions in use during development: ffmpeg 8.1.3, ffprobe 8.1.3.
- Qt tests run offscreen (`QT_QPA_PLATFORM=offscreen` set by `tests/conftest.py`).
  Real playback must be checked manually on a desktop session; CI cannot
  verify A/V output.
- The main window is intentionally empty beyond menus/status bar until the
  domain layers exist: no fake timeline or media controls.

### TASK_02 — core domain, commands, history

- `core/` imports nothing outside the standard library. `Project`, `Track`,
  `Clip`, `StreamSource`, `OutputSettings` are frozen dataclasses; commands
  return new projects (structural sharing makes snapshots cheap and safe to
  hand to a concurrent renderer).
- `StreamSource` is the core's view of an imported stream (kind, origin,
  end-or-None, conservative flag, rates) so core never imports the media
  layer. UI registers sources at import time.
- **Rate** = source seconds per output second (AGENTS definition).
  `output_duration = (source_out - source_in) / rate`. Rate 2 plays source
  at 2× speed; 24000/1001 etc. stay exact.
- **Split** at output time `t`: source position = `source_in + (t -
  output_start) * rate` (exact). Left piece keeps the clip id.
- **Trim** sets one edge to an asset-local source time, inward *or* outward
  (reclaiming material) but never outside known stream bounds and never
  across the opposite edge. Left trim preserves output end + source out;
  right trim preserves output start + source in.
- **Fade policy** (linear ramps anchored at clip edges, output seconds):
  edits may not cut through a fade region — split/trim inside a fade is
  rejected with an explanation; on split, fade_in stays with the left piece
  and fade_out with the right; `fade_in + fade_out ≤ output duration` is
  always enforced (`envelope_gain()` is the single envelope definition used
  by UI, preview and export).
- **Link groups**: `Clip.link_group` id. Move/split/trim/rate apply to the
  whole group atomically; any incompatible member rejects the whole
  transaction (linked trims never extend material; linked track changes
  require unlink). Removing one clip does not propagate. Unlinking below 2
  members dissolves the group.
- **No implicit ripple**: moves/trim/rate never touch other clips or gaps.
  Overlaps on one track are rejected; different tracks intentionally
  overlap. Deleting a populated track is an explicit undoable command that
  cascades to its clips.
- History (`ProjectSession`): before/after snapshot entries; redo stack
  cleared by new edits; undo/redo refuses if the project was mutated outside
  history; no-op commands don't grow history. Dirty state compares to the
  saved *content* (undo back to saved ⇒ clean). `replace_project()` (New/
  Open) clears history — history does not survive reopening (documented).
- Track order: `tracks[0]` = top of video stack; output end counts clips on
  disabled/muted tracks so toggles never shift duration.
- `ImportGroupCommand` places several streams of one asset at their relative
  stream origins (earliest origin anchors the requested output time),
  creating tracks when needed; fully atomic.

### TASK_03 — viewer + timeline navigation

- Viewer has two strictly separate modes: **Source** (media file, asset-local
  timebase labelled in the UI) and **Output** (composition timebase). The
  output page is an explicit placeholder until the renderer (TASK_06) exists
  — it never simulates composition playback.
- Source playback uses Qt Multimedia (FFmpeg backend). The provenance label
  always states what is audible: *whole file* (backend picks elementary
  streams) or *FFmpeg preview of stream N* produced by
  `media/extractor.py` when the user explicitly requests a specific stream.
  If extraction fails, nothing is played and the status bar says so — never
  a substitute stream.
- Frame stepping in source mode is labelled **approximate** (nominal
  frame-rate nudge); exact PTS stepping arrives with the preview decoder.
- Timeline (`ui/timeline.py`): one `QAbstractScrollArea` painting ruler,
  track rows, clips and playhead; only clips intersecting the viewport are
  drawn; no per-frame widgets. Zoom range 400 s/px … 4000 px/s covers
  multi-hour films to sub-frame. All times leaving the widget are exact
  `Fraction`s (`time_at_pixel`, `pixel_at_time`, `zoom_at`).
- Placement (`Ctrl+G`): selected bin streams are placed at the playhead via
  core commands; a compatible free track is reused, otherwise a new track is
  created — never an overlap on one track. Multi-stream selections use the
  atomic `ImportGroupCommand` preserving stream offsets.
- Sources are registered into the project (`RegisterSourceCommand`) as
  history entries when placement first needs them; placement re-ensures
  them, so undoing registration cannot break the composition (LIFO).
- Playhead/zoom/scroll/selection live only in the UI, never in history.
- Track enable/mute toggles are header buttons routed through commands
  (undoable), with `[hidden]`/`[muted]` text and shape cues, not colour only.

### TASK_04 — manual editing interactions

- **Gesture math is pure** (`ui/gestures.py`): snap candidate collection,
  snapping, pixel→time drag deltas and trim-boundary→source-time conversion.
  `ui/timeline.py` contains only the gesture state machine and painting; the
  window turns each finished gesture into exactly one core command. Widgets
  never mutate clips and contain no edit arithmetic.
- **Snapping is defined in screen pixels** (8 px), converted through the
  rational pixels-per-second, so its reach is identical at every zoom.
  Candidates are the playhead plus the boundaries of clips *not* in the
  dragged group. A snapped time is the candidate itself (exact); an unsnapped
  time is `offset + pixels/pps` (exact). Snapping applies to gestures only —
  never to numeric inspector entry or go-to-time.
- **One drag = one undoable edit.** Press selects; movement below 3 px is not
  a drag; the gesture shows dashed ghost geometry plus a snap marker; `Esc`
  cancels; release emits one request. The ghost is *tentative* — the core
  command re-validates, so an illegal drop is refused with a status message
  and the project is unchanged (verified in tests).
- **Move vs ripple.** Dragging and `MoveClipCommand` never ripple. Ripple
  exists only where explicitly requested: `Ctrl+Shift+G` / `InsertClipCommand`
  shifts clips starting at or after the insert point on the target track, and
  linked mates as a block. Inserting *inside* an existing clip is refused
  rather than silently slicing it.
- **Track changes during a drag** are limited to compatible (same-kind)
  tracks: the widget never offers an incompatible drop target, so the clip
  simply keeps its track. Changing tracks for a linked group is refused —
  unlink first (consistent with TASK_02 link semantics).
- **The ghost never promises an invalid edit.** A dragged group is clamped so
  its earliest member stops at output time 0 (rather than previewing a
  position `MoveClipCommand` would reject); a drag that ends where the command
  finds a conflict is refused with a status message and creates no history
  entry.
- **Duplicate** (`Ctrl+D`) copies the selected block by a uniform delta,
  destination entered numerically (where the copy starts). Links inside the
  block are regrouped so the copy links to itself; placement is validated
  atomically.
- **Invalid user input is always a `CommandError`, never a model
  `ValueError`.** Model invariants still raise `ValueError` for programming
  errors, but commands now pre-validate destinations/positions (negative
  starts on add/insert/duplicate) so the UI's single `except CommandError`
  path stays correct. Regression-tested.
- **Inspector precision edits** parse every non-empty field before applying
  anything, then apply in a fixed order (trims → move → rate → gain → fades)
  as separate undoable commands; bad input marks the field red and applies
  nothing. Gain is entered in dB and converted to a rational approximation at
  1e-6 (documented) — thereafter all audio math stays exact.
- **Hover cursors and shapes carry state, not colour only:** ↔ near a clip
  edge (trim), four-arrow on the body (move), closed hand while dragging.
- **Selection is sanitized** after every history operation: clip ids that no
  longer exist (undo, track removal) are dropped rather than left dangling.
- **The trim grab zone is measured inward from the edge.** A clip is hit-tested
  by its half-open pixel rectangle, so pressing exactly at or past the boundary
  pixel selects nothing and no gesture starts; the 7 px edge zone therefore lies
  inside the clip (measured in the desktop check, where an outward grab silently
  did nothing).
- **Shortcut bindings must be unique.** Binding `Ctrl+Z` *and*
  `QKeySequence.StandardKey.Undo` — the same chord on Linux — makes Qt print
  "Ambiguous shortcut overload" and trigger **neither**, i.e. undo dies
  silently. Undo keeps a single binding; redo carries the documented
  `Ctrl+Shift+Z` plus the platform standard (`Ctrl+Y`). Keyboard tests also have
  to re-activate the window: window-context shortcuts only fire for
  `QApplication.activeWindow()`, which becomes `None` after a modal closes under
  the offscreen platform.

## Rendering and export (TASK_05)

- **Quantisation is defined exactly once**, in `render/plan.py`, and every
  consumer uses it. `Quantisation` maps output time to a **global** frame or
  sample index with `round_half_up(t · rate)`; an item's length is the
  *difference of two global indices*, never an independently rounded duration.
  Consequences: boundaries between successive items cannot open or close a gap,
  and error never accumulates along a timeline (worst case ½ frame at each
  rendered boundary, independent of clip count). Long timelines cost memory in
  items, not in frames: a 2-hour plan is built and asserted in tests without a
  film.
- **Picture and audio are two independently anchored timelines.** Picture: the
  topmost enabled clip per output frame is resolved into `VideoItem`s, gaps
  become explicit black items, and the items are `concat`enated with exact
  `trim=end_frame=N` counts. Audio: every unmuted contribution is padded/trimmed
  to an exact sample count and placed at its **global** sample offset (leading
  `anullsrc` silence), then summed. Doing both inside one `concat` would let the
  rounding of one stream drag the other, which is exactly what AGENTS.md
  forbids.
- **Audio mixing policy is a plain sum**: `amix=normalize=0`. No per-clip
  normalisation is applied, ever. The planner computes the worst-case peak
  (`_peak_level`) and emits a warning when contributions can exceed full scale;
  FFmpeg's `volume` filter saturates the overflow. The warning is surfaced in
  the export dialog and the status bar.
- **Fades are evaluated in output time inside FFmpeg** with
  `volume=volume='<piecewise linear expression>':eval=frame`, built from the
  same anchor arithmetic as `core.model.envelope_gain`, so the rendered envelope
  matches what the inspector shows. Anchors are absolute output times, therefore
  a chunked render cannot reset a fade at a chunk boundary.
  *Superseded by TASK_11:* `eval=frame` re-evaluated the level once per filter
  frame, which measured as a single step for a short fade; fades are now
  sample-addressed `afade` stages anchored to the clip, and they are built from
  `AudioItem.fade_in_samples`/`fade_out_samples` instead of a time expression.
  **A fade must not restart when the render range does not start at the clip.**
  Under TASK_05/08 the expression was written in time relative to the start of
  the rendered range, so it had to be applied to the *positioned* stream (after
  the leading silence that places the clip inside the range was concatenated);
  applied to the sliced clip, `t` restarted at 0 and a clip that did not begin at
  the range start silently rendered with no fade. That was wrong until the A/B
  workflow check measured it (`docs/progress.md`, TASK_08). TASK_11 removed the
  clock problem instead of managing it: the whole clip is built, faded in clip
  samples, and only then is the render range cut out of it, so a preview window
  or a chunk that starts mid-fade continues the ramp. Pinned by
  `test_fades_are_anchored_to_output_time_not_to_the_clip_local_stream` (measured
  in a rendered file) and, for filter order, by
  `test_fades_are_anchored_to_the_clip_then_cut_to_the_range`.
- **Tempo** uses chained `atempo` stages kept inside FFmpeg's documented
  [1/2, 2] range; the product of the stages equals the clip rate to within the
  filter's decimal input tolerance, and `rate` keeps its domain meaning (source
  seconds per output second).
- **Stream addressing is explicit, and the domain model had to grow for it.**
  FFmpeg labels filtergraph inputs as `[in:v:N]` / `[in:a:N]`: stream *type*
  plus position within that type, which is **not** the container stream index
  kept as clip identity. `StreamSource` therefore carries both
  `stream_index` (domain identity) and `stream_position` (render addressing),
  plus `input_start` (the container's own `start_time`). A source without a
  recorded position is a hard error — guessing could silently render a
  different soundtrack. `media.assets.AssetMetadata.position_of()` derives it,
  `ui.main_window.core_source_for()` records it, and
  `tests/test_render_ffmpeg_features.py` proves the difference on a
  three-stream fixture (container index 2 = audio position 1).
- **Input seeking (measured on FFmpeg 8, asserted by tests).** `-ss` before
  `-i` is measured from the **presentation start** of the file, not from 0, so
  the seek is `source_start − input_start`: a file whose timeline begins at
  2.5 s yields its first frame for `-ss 0`. It is also frame accurate (it seeks
  to the preceding keyframe and decodes-and-discards), so no compensating frame
  skipping is added — adding one would drop real frames.
- **VFR / irregular PTS**: source frames are addressed by presentation time and
  normalised to the output rate by `fps`, which is documented nearest-frame
  sampling, not a claim of frame-exact VFR reproduction. Output is always
  constant frame rate. Geometry is normalised (scale preserving aspect + pad,
  `setsar=1`, `format=yuv420p`).
- **Explicitly not claimed**: colour space/transfer conversion or HDR handling,
  bit-exact/copy passthrough, lossless delivery, arbitrary channel layouts.
  Layouts outside `SUPPORTED_LAYOUTS` are refused rather than silently
  rechannelled; since TASK_11, spellings that name side speakers (`5.1(side)`)
  are refused by name instead of being normalised to `5.1`.
- **One FFmpeg process, one encode pass** for plans up to `MAX_ITEMS_PER_GRAPH`
  (96) items. Larger plans are rendered as sequential **chunks**: each chunk
  renders to a lossless FFV1 + PCM Matroska (no encoder delay or tail padding),
  and the chunks are joined by the concat demuxer in the single lossy encode, so
  the lossy encoder is initialised exactly once and joins cannot introduce
  priming gaps. Chunk boundaries come from re-running the same planner on the
  same immutable snapshot, so stacking, black/silence fill and fade anchors are
  identical to a single-pass render; contiguity and exact frame/sample totals
  across chunks are asserted in tests.
- **Temporary storage is bounded and preflighted.** Because lossless
  intermediates are large, `estimate_intermediate_bytes()` (uncompressed
  picture + PCM, deliberately an over-estimate) is compared with
  `shutil.disk_usage()` before the second pass starts; a render that would fill
  the disk is refused with the estimate and the suggestion to export a shorter
  range. Only one FFmpeg process and one intermediate exist at a time.
- **Output safety**: rendering writes `name.<token>.partial.<ext>` next to the
  destination and renames only on success; an existing destination is refused
  unless the caller passes `overwrite=True`, which the UI sets only after an
  explicit question. After rendering, the produced file is probed and its
  duration compared with the plan: allowed difference is **one output frame +
  `AUDIO_PADDING_SECONDS` (2048/48000 s)** for AAC frame padding; beyond that
  the render is reported as failed. Inputs are never written. Cancellation
  terminates FFmpeg and deletes every owned temporary file.
- **The GUI never renders on the UI thread.** `ui/render_worker.py` runs the
  exporter in a `QThread` subclass whose `run()` renders directly (a plain
  `QThread` + `started` → moved-in worker would sit in an event loop this job
  does not need — measured behaviour of PySide6). Signals are queued to the GUI
  thread; `wait_finished()` pumps the event loop for shutdown/test use. The
  request carries the immutable `Project` snapshot, so editing during an export
  cannot change what is being rendered (asserted in tests).

- **Output settings are project state, edited like any other value.**
  `SetOutputSettingsCommand` changes size, rational frame rate, sample rate and
  channel layout as one undoable transaction; the core only rejects structurally
  impossible values (non-positive size/rate, empty layout) and a no-op. Whether a
  layout can be encoded is decided in the render layer (`normalise_layout`), which
  refuses anything outside `SUPPORTED_LAYOUTS` instead of silently rechanneling.
  The inspector's Output group is the UI for it, offering only encodable layouts,
  parsing all five fields as one unit (bad input marks the field and applies
  nothing) and re-syncing its fields from the project after undo/redo.

## Composition preview and frame inspection (TASK_06)

- **A preview is a normal render, not a second renderer.** `render/preview.py`
  builds a `RenderRequest` for a frame-snapped range with
  `geometry_scale < 1` and the `PREVIEW_H264_FAST` preset and hands it to the same
  `render()` used by export. Nothing about timing, stacking, gaps, trims, rates or
  the audio mix is preview-specific: the only differences are picture size and
  encoder speed settings, both of which are part of the cache key. `preview_range()`
  snaps its boundaries with the planner's own `Quantisation`, so a preview boundary
  coincides with a rendered frame instead of introducing a second rounding rule.
- **Counting vs naming.** `Quantisation.frame_count(seconds)` rounds **up**: the
  number of whole output frames an output interval of that length contains
  (`frame_count(1/50) == 1`, because a positive length always presents at least one
  frame). This is deliberately different from `frame_index(t)`, which is the
  half-up *name* of the frame covering `t`; a preview range's horizon and length are
  counts, and `preview_range` walks the window back at the end of the composition so
  a requested window is kept whenever the composition is long enough.
- **Cache identity is the composition fingerprint**, not a timestamp:
  `render/cache.render_fingerprint(project, asset_paths, start, end, scale, preset)`
  hashes timeline values (clips, tracks, output settings, sources) plus resolved
  asset paths with size and `mtime_ns`, **plus `SEMANTICS_VERSION`** — what the
  renderer guarantees about the media it produces. Cached media is only valid for
  the renderer that wrote it, so the version is raised whenever the *meaning* of a
  project changes (it went to 2 when fade anchors were fixed to follow output
  time), otherwise previews written by an older build would be served as current.
  `PreviewCache` owns its directory
  (`$VIDEO_EDITOR_PREVIEW_DIR/video-editor-previews`, else under the system temp
  dir), adopts pre-existing files by key, is LRU-bounded (12 entries / 1 GiB),
  writes to `<key>.<token>.partial.mp4` and renames on success, and deletes only
  files it owns. `RenderRequest.cache` registers the output and evicts to budget;
  the window's *staleness* truth is a recomputed fingerprint, which is why a track
  rename (no media change) does not go stale while a one-frame move does.
  Preview files are deleted at window close unless `VIDEO_EDITOR_KEEP_PREVIEWS=1`;
  user media is never in that directory and never deleted.
- **Still frames come from the composition, never from the preview file.**
  `media/framegrab.extract_composition_still()` builds a one-frame plan with
  `still_plan_for()` and decodes with the same picture chain (`picture_filter_chain`)
  and the same FFmpeg stream addressing (`0:v:<position>`, `-ss` relative to
  `format.start_time`) as the exporter, so the acceptance rule "the frame the
  inspector shows == the frame the exporter writes at that index" is measurable and
  is asserted that way against exported files. Black gaps never touch a decoder:
  they yield a truthful black `StillFrame`.
- **Real PTS, not printed decimals.** `source_frame_times()` derives each packet's
  time as integer `pts × stream time_base`, because ffprobe's printed `pts_time` is
  truncated (a Matroska 1/1000 time base prints milliseconds), and stepping from
  printed values drifts off real frames. Packet timestamps are **not** offset by the
  stream's `start_time`: measured on Matroska and MP4, for a whole-asset offset
  (`-output_ts_offset 2.5` → first packet `pts 2500` at `1/1000`) and a per-stream
  offset (`-itsoffset` → first packet `pts 1000` while `start_time` reads 1.0),
  packet PTS already sit on the container presentation timeline, which *is* the
  asset-local axis the model uses; adding `start_time` shifted every frame of such a
  file by its own start time. Source stepping walks actual PTS neighbours
  (`nearest_frame`), which is what makes VFR material land on real pictures; the
  fixture used in tests is 8 s @25 fps + 4 s @12 fps in Matroska, where the nominal
  25 fps grid has drifted off the real frames (8.4 s is not a frame time there, while
  8.333 s and 8.417 s are), and an offset fixture shows its frame grid shifted by the
  asset's own start too (3.02 s is a frame there, 3.0 s is not). Stepping always
  offers the neighbouring frames, never the one exactly under the cursor.
- **What the decoder actually presents** is reported separately:
  `StillFrame.source_pts` is the planned asset-local time, `decoded_source_pts` is
  the frame an input `-ss` to that time really yields — the frame exactly there if
  the stream has one, otherwise the first later frame (`presented_frame`, because
  input `-ss` decodes-and-discards earlier pictures). Reporting only the planned
  time would mislabel the source edge one frame late exactly where cut differences
  are being compared. `Set clip in/out` writes the *planned* time into the clip: the
  model is continuous, and re-seeking that time reproduces the same picture.
- **Still-frame work is one dedicated thread, not a thread per click.**
  `StillWorker` lives in a `WorkerThread` whose `run()` enters `exec()` (without an
  event loop a moved-in worker receives nothing). Requests are a mailbox: only the
  newest is kept, delivered through `QMetaObject.invokeMethod(..., QueuedConnection)`
  (PySide6 exposes no public `QThread.wake`). Results carry a generation tag and a
  superseded result is dropped, so a rapidly scrubbing user gets the frame they
  stopped on. A decode already in flight cannot be interrupted — that costs at most
  one decode of latency and never a wrong picture. Composition edits clear the
  decoded-frame cache and re-request the frame under the playhead, so the viewer
  cannot keep showing a picture the composition no longer makes.
- **Two separate status lines.** The output viewer's state line is about the
  preview (rendering / current / stale / failed / cancelled); the frame line and
  provenance line are about the inspected still. They never overwrite each other,
  because both are true at the same time.
- **Preview playback is a file, and the mapping is one function.** While a preview
  file plays, the picture on screen belongs to that file, not to the composition.
  `render.preview.playback_offset(range_start, output_time, range_end)` is the only
  place that translates output time into file-local seconds, exactly (no float
  accumulation), and returns `None` outside the half-open range — in which case the
  window says so instead of playing a different part of the composition while
  claiming the playhead position. Seeking into a freshly loaded file waits for
  Qt's `LoadedMedia` status, because a position set before the file is loaded is
  dropped.
- **Bounded memory for inspection.** Decoded stills live in `render/cache.FrameCache`
  (byte budget + frame-count cap, and it hands back the caller's own object rather
  than making the render layer know about UI records); probed PTS lists live in
  `FrameTimesCache` keyed by asset identity + stream, so a replaced file cannot
  serve stale timestamps. Neither grows with the length of a film.

## Project files, relinking and recovery (TASK_07)

- **The document is data, not a dump of live objects.** `project/serialize.py`
  encodes/decodes the schema (documented in that module's docstring and in
  README): `schema_version`, output settings, assets (id, reference, stat
  identity, recorded probe metadata), registered sources, tracks in stacking
  order, clips, and optional `navigation`. Every rational is the string
  `"num/den"`; `null` means unknown and is never replaced by 0. **Unknown fields
  are refused, not ignored** — a field this build does not know means a newer
  schema, and silently dropping it would destroy data on the next save.
- **Migration policy:** `SCHEMA_VERSION` is the highest readable version; a
  higher version is refused with a diagnostic that names the version and lists
  the keys it could not interpret; a lower version needs an explicit entry in
  `MIGRATIONS` (none exist for the first schema). Additive optional keys keep the
  version; any change in the *meaning* of a key bumps it and adds a migration.
- **Paths are resolved explicitly.** `path_mode` is `relative` (resolved against
  the directory of the project file) or `absolute`. Relative references are only
  written when they do not escape the project directory, because the point of a
  relative path is that the pair (project, media) can move together.
- **Atomic save with no debris.** Write `.<name>.<token>.tmp` in the same
  directory → `flush` + `fsync` → `os.replace`. The finally-block unlink is a
  no-op after a successful rename, so a failed save leaves the previous file
  byte-identical and no temporary file behind.
- **Load is all-or-nothing.** Schema, duplicate ids, cross-references
  (clip → stream → asset, clip → track), `core.commands.validate_project`
  invariants and (as warnings) "the saved clip no longer fits the recorded
  stream bounds" are checked before anything is applied; the caller's project is
  never partially replaced.
- **Media state is reported, never repaired.** Each asset record is compared
  with the file at its reference: `online` / `missing` / `changed` (size or
  `mtime_ns` differ). Recorded metadata is kept as the baseline, so an offline
  project is still inspectable and saveable; rendering that material is refused
  by the planner/graph with the asset name, state and path.
- **Relinking is explicit and validated against the material in use.**
  `media/relink.validate_replacement` requires the same container stream index,
  the same kind, known bounds that cover **every clip interval** already using
  that stream, the same frame/sample rate, and the same picture size unless
  `allow_geometry_change` is set knowingly. Renumbered streams (the file's stream
  order differs) are reported and the new FFmpeg `v:N`/`a:N` position is recorded.
  The asset keeps its stable id; `RelinkAssetCommand` swaps the registered
  sources atomically and refuses a partial replacement or one that would leave a
  clip outside its stream. Clip timing is never adjusted automatically.
- **Recovery snapshots are a separate mechanism from saving.** They live under
  the application's own directory (`$VIDEO_EDITOR_RECOVERY_DIR`, else
  XDG data), keyed by a hash of the project path (`unsaved` for untitled work),
  carry a header (when, why, which project file and its `mtime_ns` at capture)
  and are pruned. **Discovery follows need, not only the key:** a window with no
  project open — the state the application is in straight after a crash — finds
  every snapshot, because the one that matters belongs to a project it has not
  opened yet, and restoring reclaims that project path. A window that does have a
  project open sees only that project's snapshots plus the never-saved ones, so
  one project's window is never offered another project's work. Only the
  application start-up offers restore; a window never interrupts its user.
  Restored work is loaded as **unsaved**.
  `disk_ahead_of_snapshot()` refuses a restore when the project file on disk is
  newer than the snapshot, so recovery can never silently overwrite newer work.
  `VIDEO_EDITOR_AUTOSAVE_SECONDS` tunes the interval (0 disables).
- **Dirty tracking is relative to saved content**, not to edit count:
  `ProjectSession` keeps the project as it was at the last save, so undoing back
  to that content clears the dirty mark and redoing it returns. Playhead, zoom,
  scroll and selection are saved as convenience but never make a project dirty.
- **Undo history does not survive a load** (New/Open/restore): history is a live
  session feature; the composition, its stable ids and its navigation do
  survive. This is deliberate and documented rather than accidental.
- **A prompt is consent, and consent must name the button.** `MainWindow._ask`
  is the one place that builds a message box; it returns the index of the
  clicked button **in the caller's own button list**, matched by label, because
  Qt orders buttons by role and platform style. Every refusal path (Cancel,
  Escape, a suppressed dialog) is a negative answer: no caller may treat "no
  button chosen" as agreement. Tests and the unattended check answer dialogs by
  label for the same reason.
- **Never ask before you know the answer is needed.** A file is read and
  validated before any prompt that would replace current work, so an unreadable
  or refused file costs nothing. Replacing an existing project file always asks,
  whoever chose the path — except writing the very file this project is already
  saved as.
- **Dirty means "no file holds this content".** `ProjectSession` compares the
  composition with the content saved last; `None` (recovered or never-saved
  work) is always dirty until it is written. Recovery restore sets that baseline
  to `None`, so a restored composition cannot be closed away silently.
- **`VIDEO_EDITOR_AUTOSAVE_SECONDS=0` disables the timer** (a `QTimer` interval
  of 0 would fire continuously); closing a window with unsaved work still writes
  one snapshot with that reason recorded.

## Timeline decorations, discoverability and long-timeline cost (TASK_08)

- **Decorations are derived views, never composition state.** Thumbnails and
  waveforms are not stored in a project, cannot change a render plan and never
  change clip geometry: the timeline draws shapes, labels, selection and the
  playhead whether or not any picture has arrived. `core/` and `render/` know
  nothing that they exist.
- **Extraction is media-layer, scheduling is UI-layer.** `media/waveform.py`
  (peak buckets through `asetnsamples` + `astats` + `ametadata`) and
  `media/thumbnails.py` (one `image2pipe`/MJPEG contact strip per clip range) are
  Qt-free FFmpeg adapters: given a stream, an asset-local range and a requested
  granularity they either answer or raise. `ui/decorations.py` decides what is
  worth asking for — the visible interval, one rung of a fixed granularity ladder
  (`ladder_step`, so zooming does not re-decode at every pixel), and whether the
  answer is already cached.
- **Bounded by budget, not by the length of the film.** Envelopes and strips are
  LRU-cached inside byte budgets (`ENVELOPE_BUDGET` 2 MB, `STRIP_BUDGET` 6 MB)
  keyed by asset + stream + **file identity**, so a replaced file cannot be drawn
  with the previous file's pictures. The pending-job queue is capped
  (`MAX_QUEUED_JOBS` 32), one worker thread serves a window, and a plan that
  failed is remembered for a bounded while (`MAX_TRACKED_FAILURES`) instead of
  being retried on every repaint.
- **Nothing is fabricated.** A clip with no registered material, an unknown
  container stream position, or an extraction that failed is drawn as a plain
  shape and the reason is reported (`WaveformError` / `ThumbnailError` are
  surfaced, not swallowed). A decoration is painted only once it has actually
  been delivered; `paintEvent` never starts work, scheduling happens on view,
  selection and project changes.
- **Per-asset, not per-clip, file checks.** Anything on an interaction path
  (still-frame requests, decoration scheduling, missing-asset reporting) must
  stat each asset at most once, not once per clip: 843 clips pointing at two
  files measured ~5 ms per playhead move before this was fixed.
- **Scans are bounded by the view.** Decoration scheduling tests the visible
  interval before touching the file system or the plan arithmetic, so hidden
  clips in a long timeline cost a comparison each, not a decode.
- **Documented chords are the bound chords.** Help ▸ Keyboard and Workflow…
  enumerates the window's live `QAction` shortcuts, and the timeline declares its
  own keys in `timeline.WIDGET_KEYS` beside the handler that implements them. A
  hand-typed list is how a documented chord came to be dead (the redo defect in
  TASK_07), so tests assert the two sets agree in both directions.
- **The acceptance harness is table-driven.** `scripts/desktop_checks/editions.py`
  states the two editions and the target composition once (`PICTURE`,
  `UNDERNEATH`, `SOUND`); the same table builds the project *and* says what the
  rendered file must contain, so a check cannot pass by measuring something the
  fixture never had. `scripts/make_ab_editions.py` generates those fixtures and
  prints the recipe for doing the same work by hand;
  `scripts/long_project_check.py` profiles a 2 h / 843-clip project.

## Media safety: protection, publication and identity (TASK_09)

Corrective milestone after review of `0c6b8f1`. Two review findings were data
risks, not cosmetics: an export or Save As could be pointed **at an imported
asset** (the overwrite prompt even promised the opposite), and a *different*
encode at a recorded path was accepted anywhere except at load.

- **A new lowest layer: `video_editor/fs/`.** `fs/identity.py` and
  `fs/protection.py` are standard-library only and know nothing about assets, Qt,
  `core`, `media`, `render` or `project`, so every layer enforces the same rules
  instead of each inventing its own. Asset-typed helpers (`check_asset`,
  `check_catalogue`, `protected_files`, `same_file`) sit on top of them in
  `media/availability.py`. The only new dependency edges are `render → fs` and
  `project → fs`; `fs` imports nothing from this project.
- **Availability is a state, not a boolean.** `ONLINE` / `MISSING` / `CHANGED` /
  `UNREADABLE`, decided by one `stat` against the recorded identity
  (**size + mtime_ns**, the policy project files have always written — explicitly
  *not* a content hash: a file rewritten with the same size *and* mtime is
  indistinguishable, and no claim is made otherwise).
  `project.storage.AssetState` is now an alias of that same enum, so load-time,
  UI and render-time agree on what "changed" means.
- **Identity is re-checked during the session, not only at load.**
  `MainWindow._availability()` checks **once per asset id** (never per clip: 843
  clips over three files cost ~5 ms of pure `stat` per playhead move before
  TASK_08's fix) and refreshes `_media_records`, so the media-status dialog, the
  export refusal and the decoration layer all speak from one observation. A
  `CHANGED` asset makes stills, stream previews, thumbnails, waveforms and
  rendering refuse with an actionable sentence; the way back is an explicit,
  validated relink.
- **Sources are protected as destinations, unconditionally.**
  `DestinationGuard` holds every asset path of the catalogue — including assets
  that are muted, disabled or never placed — plus the project's own file for
  renders. Comparison is by **resolved path and device/inode**, so a relative
  alias, a symlink to the film and a hard link to the same inode are all caught.
  A collision is refused **even with `overwrite=True`**: confirming that an output
  may be replaced is not consent to destroy a source.
- **Publication is its own step** (`fs.protection.publish`), replacing the bare
  `staging.replace(destination)` in both the exporter and project save:
  * nothing at the destination → `os.link`, which fails with `EEXIST` rather than
    clobbering a file that appeared while the render was running;
  * something there → only with a `Consent` captured **from that exact file**
    (`Consent.capture`, taken after the user agreed), re-checked immediately
    before publishing; an appeared, vanished or changed file is refused;
  * the previous file is never deleted before a successful publish, and
    cancellation is honoured **again after the last progress event** — the file
    must not appear once the user has said stop.
  Cache-owned preview destinations use `Consent.as_owner`, granted only after
  `PreviewCache.owns(destination)` confirms the file is ours.
- **Render scope is honest in both directions.** Source validation covers the
  assets the *rendered range* reads (a range that ignores an offline asset is
  renderable), while destination protection covers the whole catalogue (relaxing
  that would be a data-loss risk). Sources are validated before decoding **and
  again before publishing**: a file swapped mid-render is refused rather than
  published as a mix of two encodes.
- **Residual limits, stated.** These checks close the window that matters
  (preflight → render → publish) on a local filesystem, and the rename itself is
  atomic, but no filesystem offers atomic check-then-create against *every* other
  client on a network or removable mount. What is guaranteed is that this
  application never knowingly writes onto a source, or onto a file the user did
  not approve, and that a refusal leaves existing bytes untouched.

## The project document: one snapshot, one baseline, one undo (TASK_10)

Corrective milestone after review of `2205d37`. The session tracked only
`core.Project`, while the file on disk also held the media catalogue — so an
import with no clip placed could change what a Save would write without setting
the dirty mark, a relink changed registered streams through history but the
catalogue behind them separately, and one drop on the timeline could produce
several history entries plus abandoned tracks and stream registrations when the
clip itself was refused.

- **`project/document.py` holds the persistent unit.** `EditorDocument` is a
  frozen pair: the `Project` composition **plus** the ordered tuple of `Asset`
  records. Order is import order and is preserved — re-sorting the catalogue by
  name, format or language would quietly rank one edition first, which AGENTS.md
  forbids. Everything transient stays out: playhead, zoom, scroll, selection,
  import jobs in flight, and what the disk currently says about a file
  (availability is recomputed from TASK_09 identity checks, never trusted from
  the snapshot, so undo cannot make an offline file look online or vice versa).
- **History is generic over the snapshot, not tied to `Project`.**
  `core.history.EditHistory[S]` stores and swaps opaque immutable values;
  `SnapshotCommand[S]` is the protocol it accepts (`apply(snapshot) -> snapshot`,
  `describe() -> str`). `core.commands.Command` satisfies it for `Project`,
  `project.document.DocumentCommand` for `EditorDocument`. `ProjectSession`
  survives as a thin subclass for core-level work and tests; the application uses
  `DocumentSession`. No domain class was rewritten and `core` still imports no
  media or Qt code — the pairing lives one layer up, as AGENTS.md asks.
- **`DocumentSession.check()` is the commit gate.** `EditHistory.apply` computes
  the candidate and calls `check(candidate)` before recording anything;
  `DocumentSession` raises when `validate_document` finds a structural problem.
  Result: a structurally unsound document never becomes a history baseline, so
  undo cannot be asked to restore a state that could not have been saved.
  `EditComposition(command)` is the bridge that lets every existing core command
  take part in document history unchanged.
- **Document commands are the transaction API for later assisted work.**
  `AddAssetCommand` (one import), `ReplaceAssetCommand` (the media half of a
  relink; the asset **id** never changes, because clips and cache keys speak of
  assets by id), `RelinkDocumentCommand` (catalogue + registered streams in one
  step, from a `RelinkPlan` that `media.relink` already probed and validated) and
  `CompositeDocumentCommand` (N steps against a candidate, published once, one
  history entry). The composite is deliberately available now and deliberately
  unused by any matching feature: TASK_15 keeps automated assistance out of scope.
- **The media bin is a projection, not a second catalogue.** `ui/media_bin.py`
  reads `document.assets` (`assets`, `index()`, `sync()`); it owns no list that
  could drift, and there is no `add_external` route that could record a file
  outside history. Import completion, relink and undo/redo all just re-sync it.
- **Placement is one composite core command.** `PlaceStreamsCommand` takes
  `StreamPlacement`s (a stream record, where it starts, optional explicit track,
  optional source interval), optionally links them, and does registration →
  lane choice → clip creation on a **candidate** `Project`; it publishes once or
  raises without effect. `TrackChoice.ALLOCATE` reuses the topmost compatible
  lane that is free at the requested range (a 2-hour film must not gain a track
  per clip); `TrackChoice.FRESH` always adds one, which is what whole-group
  import uses so an imported edition keeps its own lanes. `group_placements`
  is the single place group offsets are computed — streams of one asset keep the
  container's own relative offsets, anchored by the earliest origin — and
  `ImportGroupCommand` now shares that rule instead of repeating it.
- **A known stream is never silently re-described.** Placing a stream whose
  recorded bounds or format differ from the project's own record is refused with
  a pointer to relinking: existing clips were validated against that record, and
  swapping it underneath them is exactly the kind of quiet inconsistency this
  task set out to remove.
- **Splitting a linked group leaves usable groups.** The left pieces keep the
  original `link_group`; **all** right pieces share exactly **one** new stable
  id, so the right half can be moved or split again as a unit while the left
  half stays put. A group reduced to one member normalizes to unlinked, and
  `validate_project` reports a lone group member as a leftover. Undo/redo
  restore both identities. The old test that asserted all four halves stayed in
  one group was rewritten on purpose — that assertion encoded the bug.
- **Validation is split into structure and availability, at both ends.**
  `validate_project` (composition: unique track/clip ids and source keys,
  references resolve, kind matches track, source intervals inside recorded
  bounds, no same-track overlap, fades fit, sane rates) runs at **load** and at
  **commit**. `validate_document` adds the cross-check the file makes possible:
  every registered stream's asset must exist in the catalogue, and duplicate
  asset ids and empty paths are named. A registered stream naming an unrecorded
  asset used to be a warning; it is now a refusal, because such a clip can never
  render. A file that is **missing on disk is not** a structural problem: the
  document loads, the clip stays where it was placed, and availability says why
  it cannot be read right now.
- **Schema v1 is kept.** The document already serialized both halves, so no
  migration was needed; what changed is that the loader trusts neither half
  alone. `build_document(project, assets, navigation, version, project_file)`
  takes the catalogue explicitly, which is why test fixtures that used to save a
  composition with an empty asset list now carry records for the streams they
  name (a fixture with no catalogue is a corrupt file, not a shortcut).
- **Test-facing consequence (recorded, not hidden):** `DocumentSession` has no
  `replace_project`; installing a document is `install_document` on the window or
  `replace_document` on the session, and `MediaBinController.assets` is a tuple
  that cannot be mutated. `tests/gui_helpers.py` provides the supported ways to
  build a window's document (`install_document`, `media_record`, `offline_asset`,
  `import_file`) so fixtures cannot manufacture a state the application itself
  cannot reach.

## Audio slicing, resampling, fades and proof of the output (TASK_11)

Corrective milestone after review of `6a09c8b`. The composition could already be
built and exported; what was missing was that the audio path **stated** what it
would produce, and that an export was checked for anything other than its length.
Four separate gaps, each fixed and measured:

| Gap | Now |
| --- | --- |
| one sample-count cap, whose meaning depended on where FFmpeg inserted a resampler | sample-rate conversion written explicitly, then every count in one unambiguous axis (`render/plan.py:AudioItem`, `render/graph.py:_audio_item`) |
| fades as `volume=…:eval=frame` expressions — one level per filter frame | `afade` addressed by `start_sample`/`nb_samples`, anchored to the whole clip |
| the input was read to the end of the file for every clip | `-ss <clip start>` plus `-t <clip source duration>`, and a cap in **source** samples |
| an export was believed if its duration matched | frames counted in the file, sound decoded and measured, the end of a depended-on source stream measured before encoding |

**The chain, per contribution, in the order it is written.** Input `-ss`/`-t`
(the clip's own slice, nothing more decoded than needed) → `asetpts=PTS-STARTPTS`
→ `atrim=start_sample=0:end_sample=ceil(source_duration × source rate)` (the
stream's own axis) → `aresample=<output rate>` → `aformat=sample_fmts=fltp:…`
→ `atempo` chain → `apad,atrim=start_sample=0:end_sample=clip_samples` (the
output axis, exactly the clip's length whatever the resampler and tempo left
behind) → `afade` stages → constant `volume` when the clip has gain → the render
range cut `atrim=start_sample=pre:end_sample=pre+samples` followed by
`asetpts=PTS-STARTPTS` → leading silence `concat` → tail `apad` →
`amix=inputs=N:normalize=0`.

- **Why the clip is built whole and the range cut afterwards.** A chunk or a
  preview window that starts inside a clip must continue that clip, not restart
  it: the fades, the tempo phase and the sample counts then come out the same as
  in a single-pass render. Measured for lossless PCM sources: rendering
  `[0, x)` and `[x, end)` and concatenating the decoded samples is **bit
  identical** to one render — worst sample-for-sample difference exactly `0.0`
  at rates 1, 25/24 and 24/25, including a boundary placed inside a fade and
  inside a retimed clip. The documented promise stays a bounded tolerance,
  because a lossy *source* can differ at a seek point.
- **Why the cut is followed by a PTS rebase.** `atrim` keeps the timestamps of
  what it keeps. Cutting away the first 2 s of a contribution and muxing it left
  the container running past the composition: measured before the rebase, an
  export of output `[2, 6)` reported 3.0 s for a 2.0 s plan.
- **Fades are the domain envelope, at sample resolution.** `afade=t=in|out:
  curve=tri:start_sample=…:nb_samples=N` scales the *k*-th sample of a ramp of
  *N* by exactly `k/N`, which is what `core.model.envelope_gain` computes; the
  measured difference is under `1e-6` when the intermediate is `pcm_f32le`
  (`pcm_s16le` quantises at `1/65536 ≈ 1.5e-5`, the same size as the error being
  asked about). A fade is quantised half-up to whole samples and a positive fade
  never rounds to zero, so the clip's first sample stays silent.
- **Padding versus inventing.** The graph always produces the number of samples
  the plan promises, so missing source material can only arrive as silence.
  Therefore: `_check_source_slice` refuses a clip that reaches past its stream's
  recorded end (tolerance one source sample), and `RenderRequest.check_source_tails`
  decodes the last moments of any stream a clip depends on — a shortfall larger
  than `SOURCE_TAIL_TOLERANCE` (1/24 s) is refused, a smaller one is a warning
  that the renderer's own padding filled it, and an unmeasurable stream is a
  warning rather than a guess.
- **Proof of what a render holds.** `probe_streams` counts each stream's packets
  (`-count_packets`) and the picture must have exactly `plan.total_frames`;
  the sound is *decoded and measured* (`measure_decoded_span`, `-f null` with
  `-progress`), because packet size is a container detail — measured here as
  **3840 samples per packet for PCM in Matroska** (25 packets for a 96 000-sample
  render) and 1024 for AAC, so packets × frame-size
  arithmetic refused a perfect lossless render (25 packets read as 25 samples)
  and would have hidden real shortfalls. Allowed slack is what encoders add and
  nothing more: measured AAC overhang **+256 samples at 48 kHz** and **+888 at
  44.1 kHz**, against `AUDIO_PADDING_SECONDS = 2048/48000` s; the lossless
  intermediate is exact. A truncated file is caught this way: a Matroska cut to
  70 % of its bytes still reports `duration 6.000000` (its index sits at the end
  of the file), while a `-ss 4 -t 2` decode of it yields 1.81167 s.
- **Channel layouts are not renamed.** `normalise_layout` used to strip
  `(side)`, which silently claimed that side and rear speakers are the same
  position. Spellings that name side speakers (`5.1(side)`, `quad(side)`, …) are
  now refused with the reason and the supported list. In the planner,
  `_check_layout` warns where the remap really is a guess — a source with **no
  recorded layout** going into a surround output, or a `…(side)` source going into
  surround — and stays quiet about ordinary mono↔stereo up/down mix, which FFmpeg
  documents.
- **`atrim` time options are not used.** Measured on FFmpeg 8.1.3 in these
  graphs, `atrim=end_time`/`end_duration` returned 0 or 42 samples instead of the
  requested window, while `end_sample=` cut to an exact sample even mid-frame
  (100000 samples out of an AAC source). Every bound in the chain is therefore a
  whole-sample count; nothing depends on the broken form.
- **Caches know.** `render/cache.SEMANTICS_VERSION` went from `"2"` to `"3"`, so
  a preview file rendered by the previous chain can never be reused as if it were
  current.
- **What was measured about the old chain, honestly.** Rendering the same extent
  cases through `6a09c8b` showed the **correct** extent for PCM sources in this
  FFmpeg: with `aformat` as the only rate hint, FFmpeg inserts `aresample` before
  the old `atrim`, so the count happened to be taken in output samples. The
  reviewer's exact shape was reproduced — a 4 s item from a 44.1 kHz stream into a
  48 kHz output, in a file whose 2 s sections are 330/660/990/1320 Hz and whose
  selection is `[1, 5)` — and the render held the 330→660→990 sections in the
  right places, exactly 4 s, with silence (not the 1320 Hz block) after the clip.
  A 96 kHz source into 44.1 kHz behaved the same way. The defect was that the
  graph never said so — the promise belonged to format negotiation, and nothing
  bounded what the decoder read. The fades were not a negotiation artefact: a
  40 ms fade-in of a constant signal came out as exactly **two levels** (0.0,
  then 0.5 from sample 4096), worst per-sample error **0.427** of full scale.
- **The convention at a clip edge is silence, not a borrowed sample.** A source
  slice whose end falls between source samples is bounded generously
  (`ceil(source_duration × source_rate)`), the output count is then enforced
  exactly (`apad` + `atrim` to `clip_samples`), so the remainder of a partial
  sample is padding. What is never allowed is the neighbouring material: the leak
  tests measure the windows after `source_out` and require silence, and a clip
  that would need material past a recorded stream end is refused by the planner
  rather than completed with either.

Test-facing consequences: `verify_output(ffprobe, ffmpeg, path, plan)` returns
`OutputFacts(video, audio, audio_samples)` and `validate_output(plan, streams,
audio_seconds)` takes the measured sound rather than a preset; `CodecPreset` no
longer carries `audio_frame_samples` (it encoded a false container assumption);
`tests/test_render_audio_timing.py` is the measurement suite for slice extent,
leak, fades, chunk joins and mixed-rate sums, always on decoded samples at both
edges of a contribution.

Honest limit of the tail probe: `measure_decoded_span` reports how much *can* be
decoded from a seek point. On a file whose index was itself destroyed the seek
can start earlier than asked, so the number can be optimistic. What the check
rules out is the common failure — a declared duration whose tail is absent — and
the same primitive refuses a produced file whose own sound cannot be decoded.

## Truthful frame inspection and one picture sampling contract (TASK_12)

Corrective milestone, **not accepted**: the sampling contract, the shared chain,
the bounded frame index and the measured picture timestamps are implemented and
measured, but `tests/test_picture_identity.py` still reports failures and the
display-aspect question is open. The task's own record, including what was
decided, what was measured and what is unresolved, is `TASK_12.md` §1–§8; this
section states the decisions that bind the code.

**One lattice, two named questions.** For everything the renderer produces, the
rule is snap-forward: `Quantisation.frame_index(t)`, `frame_count(t)`,
`render_range_frames(a, b)` (half-open: indices `ceil(a·R)` … `ceil(b·R)-1`),
`sample_span` and `sample_count` all use `ceil(t · rate)`. Naming a frame and
counting a length used to use different roundings, which was observable (a
composition 0.01 s long had no frames for the exporter and a one-frame range for
the preview). Cost: an edge never snaps backwards, so a cut moves forward by up to
one output frame. Preview, export, chunked render and stills all use this lattice,
and a subrange uses the same global indices and phase as a whole render.
**What a playhead shows is the other question** (`Quantisation.frame_at_playhead`,
`floor(t·R)`, TASK_12d): the frame *covering* the playhead, never the next one, so
inspection cannot show footage the playhead has not reached and a playhead one
tick before the end does not ask for a frame that does not exist. The two must not
be conflated in either direction — no caller of the range boundary uses the floor
helper, and the only inspection caller is the still path.

**One sampling contract per picture item** (`render/plan.PictureSampling`, built
by `picture_sampling`): the reading window `[seek_source, read_source_end)`, the
sampling grid (`sample_start`, `sample_step`), the head/tail holds the item needs,
and whether the grid is *anchored* at all. The seek is placed on an exactly
representable container timestamp (`floor_to_time_base`) because input `-ss`
quantises: an unaligned seek is moved to a container tick, which shifts the whole
grid. `picture_preroll` reads far enough back that the grid's first point has a
real frame at or before it, instead of starting at the first thing the seek
happened to find. Where the lattice asks for material the slice does not contain,
the rule is "hold the nearest legal picture" (`tpad` clone), so an item is exactly
as long as the plan promises.

**The chain is one function** (`render/graph.picture_filter_chain`) used by
export, chunked render, preview and stills, in this order: tick-aligned input seek
and `-t` bound → `setpts=PTS/rate` →
`fps=round=up:fps=<output rate>:start_time=<grid start>` → `tpad` head/tail →
`setpts=PTS-STARTPTS` → `picture_scale_fit` (fit by **display** ratio:
`scale=w='trunc(min(OW,OH*dar)/2)*2':h='trunc(min(OH,OW/dar)/2)*2'`) → `setsar=1`
→ pad → pixel format → `trim` to the promised frame count. `fps=round=up` is not
cosmetic:
measured over 16 tempo/slice/output-rate cases and 2410 rendered frames, the
default `round=near` showed a picture other than the contract's in **777** of
them (a 4× speed-up showed frames 1, 5, 9… instead of 0, 4, 8…); `round=up`
disagrees in **26**, all at tempos that retimed source timestamps between the
container's own ticks, where FFmpeg re-quantises before choosing.

**That 26-frame "residual" is not allowed to stand** (TASK_12i decision). The only
permitted mathematical residual is a documented backend timestamp-quantisation *tie*
near a real frame boundary, the picture must be legal, **and** every production path
must pick the same frame at the same global index; a tie is quantified by timestamp
distance, never by a count of failing frames, and exact source inspection is not
subject to it at all. Measured over 18 cases and 1,727 frames, paths disagree at
*every* deviating index, so no residual applies anywhere and the truncation itself is
the defect (TASK_12j). `tests/picture_reference.py` is the independent statement of
the contract for frame `i`: `t = i/R`, `wanted = source_in + (t - output_start)·rate`,
then the **last decoded PTS ≤ wanted** inside `[source_in, source_out)` — preceding
frame, not nearest; hold the first legal picture if `wanted` precedes it; hold the
last within genuinely available material; a slice with no legal picture is an error,
never a long accidental hold. It reads the decoded PTS list and `Fraction` arithmetic
only, importing nothing from `render/` and never FFmpeg's `fps`.
`scripts/diagnostics/picture_phase.py` compares that against (A) one render from the
clip origin trimmed by global index and (B) independently sought ranges mapped back to
the same global indices. Measured mechanism of the disagreement: `setpts=PTS/<rate>`
carries retiming only at the **source tick**, so a 1000/1001 slowdown feeds `fps` the
steps 0, 40, …, 960, 1001 instead of 40.04, and a rounded input seek (`-ss 0.334` for
`1/3`, compensated by `start_time=-0.000640000`) displaces the grid by ~1/1500 s — so
every range sharing that seek is wrong at the same indices and ranges with other seeks
are right there. A stable origin therefore removes the *inconsistency* between ranges,
not the error. TASK_12j removed the truncation itself (see the exact-tick chain below):
the same 18 cases now report `oracle vs A identical / oracle vs B identical / A vs B 0`,
and the referee is re-run as a check, not rewritten by the code it grades.

**Geometry fits by display ratio, then declares square pixels** (TASK_12c).
Measured: `scale=OW:OH:force_original_aspect_ratio=decrease` preserves the input's
sample aspect (720×576 SAR 64:45 → 900×720 SAR 64:45), so the following
`setsar=1` squashed the picture — a marker painted 256×256 in the source decoded as
226×320 — and `pad` then invented the bars (a 16:9 display-aspect source lit only
columns 190..1089 of a 1280×720 output). The fitted size now comes from `dar`, in
even pixels, with a `max(2, …)` floor because a computed `0` is not an error to
`scale` but means "use the input size" (measured: a 3×2000 source came out 3×720,
shape inherited from the file). `dar` was verified to be the *input display*
aspect, not the coded ratio (1777 for SAR 64:45, 1333 for SAR 16:15, 1000 where the
container records no SAR). Borders the source itself carries stay as they are;
nothing is cropped or stretched. Export, chunked render, preview and stills all go
through this one function — `media/thumbnails.py` does **not**, and its own
`scale=box_w:box_h` still stretches anamorphic media-bin thumbnails (logged defect).

**A reported source position is measured, or it is not reported** (TASK_12k). For a
**composition** still, `decoded_source_pts` is established by carrying the picture's
identity through *this item's own chain arithmetic in integers*, not by looking at a
timestamp: the `showinfo` tap sits **upstream** of `settb`/`select`/`setpts`/`fps`, so its
stamps say what the decoder handed over, never which picture `fps` later chose — and
`showinfo` reports stamps **relative to the requested input seek** (measured: `-ss 0.07`
prints `pts_time:0.01, 0.05, 0.09` for the frames at 0.08, 0.12, 0.16), space-padded so
`pts:` and its value are separate tokens. The old rule ("greatest pre-`fps` stamp ≤ the
sampling instant, else the first stamp") is a false association with an unbounded fallback:
measured on a fine-timescale MP4 (`-video_track_timescale 50000000`, so the exact tick is
refused and the chain truncates), it reported `100000001/50000000` for a picture whose real
PTS is `2`, and `296000047/50000000` for one at `148/25` — values belonging to **no**
picture. Now: tap counter × the base that tap reported (only if `-ss`'s text re-reads exactly
as the requested seek) → the arrival must be a whole number of `settb` ticks → it must
satisfy the same `select` bounds the graph writes → the shown picture is the kept arrival with
greatest `PTS` satisfying `PTS*b <= phase*a` (integers; or the first kept arrival for the
documented head hold) → stamp `= sampling.seek_source + offset` exactly. Anything less exact
returns `None`, and the UI says *"source position not measured (asks for {prediction}s)"*
while keeping the plan's prediction separately labelled as a prediction; source marking stays
gated on the separately verified source frame from TASK_12g/h, never on a composition
prediction. A black gap decodes nothing at all.

**Source inspection is a different decode, on purpose** (TASK_12g). Reading one
source picture must not go through the composition: `extract_source_still` places an
accurate input seek **just above** the wanted container-grid point
(`+ min(1/100000, tick/4)`), runs with **`-copyts`**, and applies only geometry
(`picture_scale_fit` → `setsar=1` → pad → `format=rgb24`) — no `fps`, no `setpts`
tempo, no `tpad`, no `trim`, no composition still plan and no horizon refusal, because
the source stream has its own bounds. `-copyts` is measured, not stylistic: without it
the tap's stamps are relative to a rebase point FFmpeg reaches by rounding the seek
text through microseconds *and* the stream grid, and the containers round differently —
a seek text a hair under half a tick below a frame stamped that frame **one tick late**
on the MP4 fixture (`time_base` 1/12800) while stamping `0` on Matroska (1/1000), which
nothing downstream could detect. With `-copyts` each kept picture carries its true
asset-local container timestamp (offset stream starting at 2.5 s: seek 0.5195 s →
stamp `151/50`), and frame selection did not change. The returned stamp is accepted
only if it lands on the container's tick grid and is ≥ the request; otherwise the
picture is still returned and the position is reported `None`/`grid_exact=False`.
Exactly-one-frame output is enforced by byte count, and a request outside the stream
is refused while a request outside the clip's in/out but inside the stream is allowed.
Same source PTS ⇒ same bytes: picture 47 through clips at rates 1, 25/24, 4 and 1/4 is
byte-identical.

**Navigation reads bounded windows of decoded pictures, never a whole film**
(TASK_12f). `media/frameprobe.py` reads pictures with `ffprobe -show_frames` over
bounded `-read_intervals` — integer `best_effort_timestamp` × the **stream**
`time_base` (per-frame `time_base` is no longer printed by FFmpeg 8.1.3) — and
`media/frameindex.py` answers neighbour questions from those with `bisect`, a 10 s
window, a small probe budget, and an identity-scoped cache with bounded eviction.
**A read is trusted only from `min(read_from, first stamp)` to its last decoded
picture, never to the bound it asked for**, because measurement says reads seek to
the preceding keyframe, stop 1–2 pictures short of `to_time` on B-frame/VFR material
(0.08 s of reorder), and can run *past* `to_time` across a short hole. The decisive
case: on a file with a 36 s presentation break (pictures to 3.96 s, then 40.04–44 s),
windows asked at 10–12, 20–22 and 38–40 s all answered with only 3.00–3.96 s —
indistinguishable in shape from a read past a real end — so "there is no picture that
way" is licensed only by a real limit: the asset-axis origin when searching earlier
(not the stream's declared start, which a misreporting container can fake), or a read
asked past the container's declared end when searching later. Anything else is
`UNKNOWN`, never a claimed end of stream. Overlapping reads (1 s) join spans;
unanswered questions are cached rather than re-probed; a span query returns
`complete=False` with its coverage instead of a silent partial list. Cost measured on
a 15-minute title: 0.23–0.29 s per decoded read at any width from 2 s to 20 s
(packets 0.20–0.22 s), so width is nearly free — hence a 10 s window; a cold
single-frame question costs 0.36–0.44 s against 0.33 s for packets, a warm one
0.15–0.18 s. Every timestamp the old packet-based index returned is still returned
(verified side by side on CFR, real B-frame, VFR, offset, hole, truncated and
long fixtures); what changed is what an answer is allowed to claim.

**A stream's end is a measurement plus a status, never an arithmetic habit**
(TASK_12e). Measured: for Matroska/WebM the container `Duration` **is an end on the
asset-local axis** — a 100-frame 25 fps file written with `-output_ts_offset 2.5`
reports `start_time 2.5` and `duration 6.5`, and its last real picture runs to
`13/2` s, so the old `start_time + duration` reading recorded **9 s** and invented
2.5 s of material — while Matroska's per-track `DURATION` tag is a *per-stream*
candidate, so the tighter of the two wins. For every other family (MP4/MOV, and
anything not measured such as MPEG-TS) the span reading `start_time + duration` is
kept because it is the looser, never tighter, bound; MP4's own exact stream
`duration` is still recorded exactly. Container-derived bounds carry
`bounds_are_conservative` and nothing promotes an upper bound to exact — a
byte-truncated file's tag still overstates by 1.84 s, which is exactly why the tail
check exists. Where a render reaches such an end,
`RenderRequest.check_picture_tails` decodes one bounded window
`[needed-2 s, needed+2 s)` per stream and compares the last real frame's own end
(integer stamps × `time_base`, plus that frame's **duration**, never a nominal rate)
with what the clip needs: a shortage larger than one output frame refuses naming the
measured times, a smaller one warns, an unmeasurable tail warns, and an empty window
refuses. Measured cost 0.23–0.26 s per probe on the fixtures and 0.50 s for 60 s of
1280×720 H.264 with a 10 s GOP; a clip that never reaches its stream's end is not
probed. Picture tolerance is zero-plus-one-output-frame; audio keeps TASK_11's single
sample. Old project files keep their stored (looser) metadata and load unchanged —
what changes is that the render is refused before encoding, and a relink re-probes to
the corrected end and refuses if a clip reads past it.

**Ends are two different questions, deliberately.** A still asked at or after
`project.output_end()` is refused by name ("no output frame at …: the composition
ends at …; the last frame it has is at …"), and `prepare_preview` refuses a range
holding no output frame; an export *range* that reaches past the end is answered
with the explicit black items documented since TASK_05. A gap inside the
composition is a legitimate black frame. A still plan is built over the single
frame's **own lattice interval** `[i/R, (i+1)/R)` where `i = frame_at_playhead(t)`,
not over a one-frame window starting at the requested time.
`render/cache.SEMANTICS_VERSION` is `"8"`: `"4"` for the lattice, picture chain and
seek rule, `"5"` for the display-ratio geometry fit, `"6"` for separating playhead
inspection from range quantisation, `"7"` for the picture chain's timing mechanism
(TASK_12j: fine filter tick, source-axis `select`, phase in `setpts`,
`fps=start_time=0`), `"8"` for *which* filter tick is proved exact (TASK_12n: an item's
tempo may now be proved from the picture stamps its reading window really holds, so a
stream on a fine container tick samples on its own exact grid instead of the
container-quantised fallback, and the fallback's seek can be moved to a point FFmpeg
puts the timestamps back on). `"7"`→`"8"` is not conservative either: the fallback
shifts every stamp by up to a whole container tick, which decides a tie in favour of the
neighbouring picture, so a preview cached under `"7"` for such a stream shows the
picture either side of the one this composition names.

**The sampling grid's phase is carried on an exact filter tick, not through `fps`
(TASK_12j).** For every picture item the plan proves a `tick` on which *every* retimed
source timestamp and the grid's own first sampling time are whole numbers
(`plan.picture_tick`, with `MAX_PICTURE_TICK`/`MAX_PICTURE_TICK_PRODUCT` refusals when
the arithmetic cannot be done exactly), then the chain is
`settb=expr=1/<d>` → `select='lt(pts,<ceiling ticks>)'` →
`setpts=PTS*b/a ± phase` → `fps=round=up:fps=R:start_time=0` → tail `tpad` →
`setpts=PTS-STARTPTS` → geometry → `trim=start_frame=0:end_frame=<count>`. FFmpeg's
frame choice therefore becomes **integer equality** between grid point and stamp, and
the placement rides on the phase, which is exact, instead of on
`fps=…:start_time=<decimal>`, which is rounded onto the output grid and shifted the
whole grid by up to ~1/1500 s (measured: `-ss 0.334` for a `1/3` slice edge with
`start_time=-0.000640000` compensating). Consequences that are now invariants: the
half-open legal source slice is enforced **on the source axis** by `select` — input
`-t` is packet-granular and proves nothing about an exclusive out-point — an off-grid
slice edge is written as the ceiling integer tick (`[1/3, 19/3)` → `lt(pts,149984)`,
the same comparison for an integer `pts`); the item is trimmed to the plan's exact
frame count *after* the pick; and there is **no head `tpad`** on this path, because
`fps` already holds the first readable picture and padding there would add frames the
plan never promised. Range seeks stay cheap and stay phase-identical: a 100-frame range
near the end of a 15-minute 1280×720 title renders in 0.45–0.52 s against 64.19 s for
the whole 21,600-frame composition.

**A timing fallback is reported, never presented as exact.** A stream with no usable
measured time base, or a ratio whose exact tick exceeds the bound, falls back to the
old decimal `setpts` chain; `setpts` *truncates*, so that fallback is approximate by up
to a whole container tick (1 ms on a 1/1000 stream, against 40 ms frame spacing). Such
an item is named in `RenderJob.inexact_picture` and the export warns. At a tie the
exact chain's error is **zero**; there is no frame-count tolerance anywhere.

**The tick is derived from what has to divide, and the weaker rule needs evidence
(TASK_12n).** `plan.picture_tempo` returns the exact retiming for one item, or a reason
that names the numbers, and the chain spells it one of two exact ways
(`render/graph.py::picture_filter_chain`):

* `setpts=PTS*b/a ± phase` — the **factored** spelling, valid when every incoming stamp
  is already a whole multiple of the tempo's numerator `a` on that tick. This is the
  rule that is provable *without knowing the stream's stamps*, and it is what the chain
  has always written, so an ordinary stream's chain text is unchanged (byte-identical,
  asserted by `test_the_ordinary_chain_text_does_not_move_when_the_window_is_measured`).
* `setpts=(PTS*b ± offset)/a` — the **combined** spelling, where only the *difference*
  between a picture and a grid point has to divide. That difference is
  `(stamp − sample_start)·b / time_base`: it names the picture and the grid point and
  **not the seek**, so it is unaffected by how far back the reading window reaches.

The second rule is tried only with measured stamps (`PictureSampling.window_pictures`,
from the exporter's `FrameIndex.answer_frames_in(...)` when the span is `complete`, and
from one bounded `frameprobe.decoded_picture_times` read in the still path — asked only
when the stamp-free rule found no tick, so an ordinary stream is never probed for it).
Measured on MP4 written with `-video_track_timescale 50000000` at tempo `1000/1001`,
the stamp-free rule wants `50 000 000 000` filter ticks per second — past
`MAX_PICTURE_TICK`, and past what an `AVRational` denominator can even state — while
every stamp of the window is already exact on the container's own `1/50000000`. That is
why `MAX_PICTURE_TICK` stays a real guard: the rule that changes is *how fine a tick has
to be*, not whether an impossible tick is allowed. A stream whose window was never read
keeps the answer the stamp-free rule can give, and is reported as approximate.

**The seek must be a point FFmpeg can put the timestamps back on.** `-ss` carries a
whole number of microseconds and the container's grid is applied on top of it: measured
on a `1/50000000` stream, a seek to `5.74500498` s rebases by `5.745004` s — 49
container ticks away from the requested point — while on a `1/12800` stream a seek to
`9601/12800` s (not a whole microsecond) rebases exactly, because the container grid
restores what the truncation lost. `plan.seek_is_reproducible` states that condition and
`picture_read_window` moves the seek back to the coarsest grid that is both whole
container ticks and whole microseconds (`seek_granularity`); the reading window is
decided in that one function and `picture_sampling` calls it, so a caller sizing a probe
cannot disagree with the plan about what the chain decodes.

**An item that cannot be filled is refused by name, before any media is written.** Two
shapes, both measured rather than assumed. A reading window the stream leaves empty is
decisive for either chain (`plan.picture_window_holds_a_picture`): no decoded frame
means no output frame, whatever the time base, and `tpad` has nothing to clone. The
*approximate* chain alone can also place its first grid point out of reach of its own
arrivals (`plan.picture_chain_reaches_its_first_grid_point`): measured on the fine
fixture with six arrivals, the chain wrote 7 pictures with the grid `0.62` output frame
periods past the last arrival and **0** pictures at `0.63` — same decoded pictures, same
`tpad` request — so the rule's threshold `5/8` sits inside the measured transition and a
grid measured to be served is never refused. Both refusals are `RenderConfigError` with
the numbers in the text, and the still and export paths raise the same shape.
Independently re-measured by the orchestrator on a colour-coded fixture (`clip [1, 6)`
at tempo `1000/1001`, 25 fps out, 126 frames): at `37f4f39` the tail-hold still at
output index 125 raised `StillError` and indices 123/124 reported
`decoded_source_pts=None, grid_exact=False`; through this code all three answer with the
hand-worked pictures 147/148/149 and `decoded_source_pts` equal to each picture's own
stamp.

**Interlaced and HDR picture is refused, and unknown is never guessed**
(TASK_12l). One media-layer module, `media/capability.py`, measures a picture stream
(stream header plus two bounded `-show_frames` windows) and decides; `check_picture_capabilities`
is called by all three picture paths — `render/exporter.py::render` before any chunk or
`.partial` file exists, `render/preview.py::prepare_preview` **before** `cache.lookup` (so a
stale cache file cannot serve refused picture), and `media/framegrab.py::extract_composition_still`
after the explicit-black early return — each re-raising in its own exception type with
**byte-identical text** (`test_the_three_paths_say_exactly_the_same_thing`). Keys are tokens
or decoded frames only: interlaced = `field_order ∈ {tt,bb,tb,bt}` **or** any sampled
picture reporting `interlaced_frame=1` — measured necessary, because `-x264-params tff=1`
leaves the container saying `progressive` while the frames report field coding — and HDR =
`color_transfer` of `smpte2084`/`arib-std-b67` **or** a named HDR side-data record
(`Content light level metadata` is named by x265; the MDCV record surfaces only as an
*unregistered SEI*, which the rules must not match, and every x264/x265 file carries one).
An absent or `unspecified` value is never read as progressive/SDR: it warns and renders, and
the warning is emitted only when there is something to be wrong about (depth > 8 or BT.2020
stated) so ordinary media stays quiet. Import is never blocked by this — an unusable picture
stream is still listed, inspectable, and still usable for **sound**, and only a clip carried
by `plan.video_items` after stacking and range clipping can cause a refusal (a muted video
track still paints, so it still participates). The preflight **re-probes the file it is about
to decode**, because schema v1 stores none of these fields and an absent field must not read
as "progressive SDR"; v1 stays v1 with seven optional additive stream keys, an omitted key
decoding as unknown rather than as a default the renderer could act on. A stream that cannot
be probed at all is **not** a capability refusal: the pre-existing resolution/decode error
survives, because six tests already owned those messages and a format claim about a missing
file is a lie. Measured cost: two bounded probes per range, not per frame, and +24 s across
the 168-test picture group.

**Closed at TASK_12j, and how it was verified.** The two picture tests that were red by
design are green: joined ranges and a whole render now agree at every global index
(previously output 87/111/135 disagreed, 98 vs 99, 123 vs 124, 148 vs 149), and a still
of the last frame of a slice that ends at the stream end decodes instead of raising
`StillError` ("Output file is empty") — 89 of 89 still probes decode. The check is the
independent oracle plus the phase diagnostic, both under `tests/`/`scripts/`, and
`tests/test_picture_identity.py` passes with its original assertions untouched.

**Checkpoint position after TASK_12m — the remaining rounding and format limits, with
the tests that hold the line.** Rounding: the sampling grid is exact integer arithmetic on a
proven fine tick (`tests/test_render_plan.py::test_every_picture_timestamp_stays_whole_on_the_chosen_tick`,
`test_picture_tick_refuses_arithmetic_it_cannot_do_exactly`), the contract oracle
(`tests/test_picture_reference.py`) agrees with export, preview and stills at every index
(`tests/test_picture_identity.py::test_a_range_render_shows_exactly_the_frames_of_the_whole_render`,
`test_a_still_is_the_frame_the_export_writes_at_that_index`,
`test_every_output_frame_shows_the_picture_the_arithmetic_names`), and there is **no**
frame-count tolerance anywhere: the only approximation left is (a) below. Timestamps: a
composition still reports a source PTS only when identity is carried through the selection
(`tests/test_framegrab.py`, `test_the_1000_1001_still_never_borrows_another_pictures_timestamp`);
a source picture's PTS is measured from its own decode (TASK_12g). Format limits: interlaced
and HDR are refused with the same words on all three paths
(`tests/test_picture_capability.py::test_the_three_paths_say_exactly_the_same_thing`) and
unknown flags are said, not guessed. Still open, in order of severity:

(a) **Closed by TASK_12n.** The approximate **fallback** chain (a stream whose time base
is too fine for an exact tick, or a ratio that needs an unbounded one) could not hold a
picture on a **one-frame item**: on a 50 MHz-timescale MP4, clip `[1, 6)` at rate
1000/1001 exported 126 frames ending at #149, but the still of global index 125 — a tail
hold — raised `StillError` because every arrival preceded the decimal
`fps=…:start_time=0.250250015` and `tpad` had nothing to clone (`frame=0`). It now gets an
exact tempo proved from the stamps its own reading window holds, and an item that still
cannot be filled is refused by name instead of writing empty media
(`tests/test_picture_identity.py::test_a_fine_timescale_stream_holds_its_last_picture`,
`test_a_one_frame_range_of_a_fine_timescale_stream_writes_that_one_frame`,
`tests/test_framegrab.py::test_the_last_frame_of_a_fine_timescale_stream_reports_the_picture_it_holds`).
(b) `media/frameindex.py::_answer` can return `UNKNOWN` for a question the read it just
performed answers, when a known span lies further left — it breaks its probe loop on the
extent over *all* spans and never re-picks, so an export item far from the clip edge falls
back to nominal preroll (correct pictures, ~10 extra decoded pictures per item). (c) Interlaced and HDR refusal is in place (TASK_12l), but three of its rules are proven at
**rule level only**, because this FFmpeg cannot author the input: DOVI side data
(`-dovi_profile` is unrecognised, the `dovi_rpu` bitstream filter exposes only
`-strip`/`-compression`) and HDR10+ metadata (`-x265-params dhdr10-info=…` aborts inside
x265 and leaves a 0-byte file), and a *named* `Mastering display metadata` record, which
x265 writes as an unregistered SEI instead. Only `Content light level metadata` is proven
end to end. (d) Interactive playback has never been checked by hand — everything in this
section is measured offscreen. (e) A stream whose stated colour range differs from the
delivery path is decoded with FFmpeg's own conversion; nothing here claims colour
correctness, and measured on tagged fixtures a BT.709-tagged 64×48 file lifts a painted 8 to
a read 26 — which is why capability tests compare against the source file's *own* decoded
centres, tolerance 8 codes against a 51-code step, and cannot detect a systematic range shift.
(f) **A GUI-thread subprocess scan remains**: `ui/viewer.py` builds a source preview with a
synchronous `extract_stream_window(...)` whose duration is the whole stream when its end is
known — open since TASK_12 §6 item 6 and assigned to the lifecycle group, not solved.
(g) `render/plan.ceil_to_time_base` raises `AttributeError` when handed a float instead of
refusing it by name, even though the domain rule is exact rationals (found by a measurement
script passing `duration - 4 - 1/25`); validation should say what is wrong.



## One subprocess runner for every background job (TASK_13, adoption pending)

`media/process.py::run_process` is the intended single owner of child processes for all
background media work. It exists and is tested; **no caller uses it yet** — every path in
`media/` and `render/` still runs its own `subprocess` call, and moving them onto it is
TASK_14–23. Its binding properties, all measured (see the TASK_13 record in
`docs/progress.md`):

- argv list only, never a shell string; it owns the process and its two pipes and
  nothing else, so staging files and destinations remain the caller's responsibility.
- both pipes drained by their own thread to EOF, and the wait loop only polls: timeout
  and cancellation must never depend on output, because a Linux pipe holds 65 536 bytes
  and a runner that reads one stream at a time deadlocks when the other fills.
- capture is bounded — one pipe-full per stream by default — with `Keep.TAIL` for
  diagnostics and `Keep.STRICT` where truncation would lie (JSON, images). Bulk output
  requires a caller-stated byte count.
- termination is `SIGTERM`, a bounded grace, `SIGKILL`, then `wait()` on **every** path,
  including an internal fault; a child is never left running, orphaned or unreaped, and
  a raising progress callback is re-raised as `ProcessCallbackFailed` only after the
  child is stopped and reaped.
- one terminal outcome per call: a `ProcessResult` (a non-zero exit is an answer the
  caller interprets) or exactly one typed exception — cancelled (before launch vs while
  running is distinguishable), timeout, overflow, callback failure, launch failure.

A group-B invariant while migration proceeds: a call site is not migrated by wrapping it
in a thread or by adding a `wait()` after its kill; it is replaced by this runner, whose
tests already pin reaping, thread joining and bounded diagnostics.


## Invariants (binding, from AGENTS.md — restated where relevant to code)

- Exact rational seconds via `fractions.Fraction` throughout the core,
  including frame rates; never accumulate floats as timestamps. Persisted as
  `numerator/denominator`.
- Half-open intervals `[in, out)`.
- Asset-local presentation-time origin; stream offsets preserved relative to
  it; unknown bounds stay unknown (never treated as zero).
- All edits are atomic transactions with undo/redo; selection/playhead are UI
  state and never enter edit history. The snapshot under history is the **whole
  persistent document** — composition plus media catalogue — so an import, a
  relink or a placement is one undo, and "unsaved work" means the same thing to
  the title bar, Save, autosave and the New/Close prompt (TASK_10).
- **Splitting a linked group keeps both halves usable** (TASK_10): every left
  piece keeps the original `link_group` id, every right piece joins **one** new
  id, and a group reduced to a single member normalizes to unlinked. A/V links
  remain an editing convenience, never a media restriction.
- Never overwrite imported assets or existing outputs without confirmation —
  and **source media is never a write destination at all**, confirmation or not
  (TASK_09): the guard compares resolved paths and device/inode, and publication
  may not clobber a file that appeared after the approval was given.
- **A window owns its background work.** Every render, preview, still, import
  and decoration thread belongs to a UI object that cancels and waits for it when
  that object is closed or destroyed; destroying a running `QThread` aborts the
  process, so nothing may be dropped without its shutdown path running.
- **Closing a window is not finished until it is destroyed.** A closed window is
  still alive, holding threads, players and cache files; if it survives to
  interpreter exit, Qt's own `destroyQCoreApplication()` teardown destroys them in
  an order that aborts the process — after the application already reported
  success. Any process that creates a window must run the window's shutdown path
  and then let the object actually be deleted (`deleteLater()` plus processing
  events) while `QCoreApplication` is alive. `run_app` does this on quit, as does
  the test session fixture.

### TASK_01 — media model and probing

- **Asset-local presentation-time axis** (`video_editor.media.assets` module
  docstring): seconds on the container's raw presentation timeline, exactly
  what `ffprobe` reports as `start_time`/`duration`/`pts_time` and what
  FFmpeg filter graphs see by default. Streams that begin later carry a
  nonzero `start_time` relative to the axis origin. A *missing*
  `start_time` (WAV/MP3-style containers without timestamp metadata) is
  defined to be 0 — a documented convention, not fabricated data.
- Stream identity is the **actual container stream index** from ffprobe.
- Durations: use the stream's own `duration`; else derive only from reliable
  counts (`nb_frames/rate`, `nb_samples/sample_rate`). If still unknown,
  apply the **container horizon**: end ≤ container start + container duration,
  stored as `end_horizon` and surfaced as *conservative* bounds
  (`bounds_are_conservative`). Container duration is never silently used as
  a stream duration; the UI says so. If even the horizon is unknown the
  stream is inspectable but not editable (`editability_note`).
- Rational frame rates parsed exactly from `avg_frame_rate` (e.g.
  `24000/1001`); `0/0`/`N/A` → unknown. `r_frame_rate` kept as a hint; a
  mismatch with `avg_frame_rate` is displayed as "frame rate varies".
- Languages: ffprobe tag `und`/missing → unknown (None); never inferred.
- Import: one background thread per file (`media.importer`), `ffprobe`
  subprocess killable on cancel. UI bridge marshals results onto the main
  thread via Qt queued signals — completions cannot race. Duplicate imports
  (resolved path) are rejected with a message. Assets are referenced, never
  copied; file identity = size + mtime_ns (no content hashing of films).
- Probe failures never crash: `ProbeResult.error` carries an ffprobe hint.

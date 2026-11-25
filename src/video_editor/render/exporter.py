"""Execute a render plan with FFmpeg: progress, cancellation, safe output.

Guarantees and limits
--------------------
* Output is written to a temporary sibling file
  (``name.<token>.partial.<ext>``) and **published** only on success, so a
  cancelled or failed render never leaves a half-written movie. Publication
  (``video_editor.fs.protection.publish``) never clobbers: with no approval it
  links the file into place and fails if something appeared in the meantime, and
  an approved replacement is re-checked against the file that stood there when
  the user agreed. Cancellation is honoured once more immediately before
  publication, because the last progress event is not the last thing that
  happens.
* **Source media is never a destination.** The request carries a
  :class:`~video_editor.fs.protection.DestinationGuard` covering every asset of
  the project — placed, muted or unused — and a destination that resolves to one
  of those files (by path, symlink or hard link) is refused even when
  ``overwrite=True``. Confirmation authorises replacing an output, not destroying
  a source.
* **Sources are validated, not assumed.** Every asset this range actually reads
  is checked against its recorded identity (size + mtime_ns) before decoding and
  again before publication: a file replaced mid-render is refused rather than
  published as a mix of two encodes. Assets outside the rendered range are not
  required to be present.
* After rendering, the produced file is probed and its duration compared with
  the plan. Allowed difference: **one output frame** plus the documented audio
  encoder padding (``AUDIO_PADDING_SECONDS``). Beyond that the render is
  reported as failed rather than accepted silently.
* Cancelling terminates FFmpeg and removes temporary files.
* Plans with many items are rendered in **chunks**: each chunk renders to a
  lossless intermediate (FFV1 + PCM, which have no encoder delay or tail
  padding), and the chunks are demuxed together for one final encode, so joins
  cannot introduce per-chunk priming gaps. Chunk ranges are produced by the
  same planner, so clip boundaries, black/silence fill and fade anchors stay
  identical to a single-pass render.
* Only the MP4/H.264/AAC delivery preset is claimed, and encoder availability
  is checked up front. No bit-exact passthrough, HDR or arbitrary channel-layout
  support is claimed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from ..core.model import Project
from ..core.timeutils import ONE
from ..fs.identity import Identity, check
from ..fs.protection import (
    Consent,
    DestinationGuard,
    DestinationSafetyError,
    publish,
)
from .cache import PreviewCache
from .graph import (
    LOSSLESS_INTERMEDIATE,
    MP4_H264_AAC,
    CodecPreset,
    RenderJob,
    build_job,
    encoder_available,
)
from .plan import RenderPlan, build_plan

F = Fraction
ZERO = F(0)

#: AAC pads the tail to a whole audio frame; together with the half-frame
#: quantisation allowance this is the documented duration tolerance.
AUDIO_PADDING_SECONDS = F(2048, 48000)

#: Plans with more items than this are rendered through lossless chunks.
MAX_ITEMS_PER_GRAPH = 96

#: Free space kept free besides the estimated temporary requirement.
TEMP_MARGIN_BYTES = 512 * 1024 * 1024


def estimate_intermediate_bytes(plan: RenderPlan) -> int:
    """Worst-case size of the lossless intermediates for ``plan`` (upper bound).

    FFV1 compresses, so this is deliberately an over-estimate: uncompressed
    4:2:0 picture plus 16-bit PCM sound. It is used only to refuse a render that
    would fill the disk, never to allocate anything.
    """
    frames = plan.total_frames
    picture = frames * plan.width * plan.height * 3 // 2
    seconds = float(plan.duration)
    channels = {"mono": 1, "stereo": 2}.get(plan.channel_layout, 6)
    audio = int(seconds * plan.quantisation.sample_rate * channels * 2)
    return picture + audio + 4096 * (len(plan.video_items) + len(plan.audio_items) + 1)

ProgressCallback = Callable[[float], None]


class RenderError(Exception):
    """Rendering failed; the destination was left untouched."""


class RenderCancelled(Exception):
    """Rendering was cancelled; temporary files were removed."""


@dataclass(frozen=True)
class RenderResult:
    path: Path
    duration: Fraction
    #: duration measured on the produced file
    produced_duration: Fraction
    warnings: tuple[str, ...] = ()
    chunks: int = 1


@dataclass
class RenderRequest:
    """What to render, from where, to where."""

    project: Project
    #: asset_id -> current filesystem path
    asset_paths: dict[str, Path]
    destination: Path
    out_start: Fraction = ZERO
    out_end: Fraction | None = None
    preset: CodecPreset = MP4_H264_AAC
    overwrite: bool = False
    ffmpeg: str | None = None
    ffprobe: str | None = None
    chunk_item_limit: int = MAX_ITEMS_PER_GRAPH
    #: picture-size multiplier; 1 for export, < 1 for a faster preview. Timing,
    #: stacking, gaps and mixing are identical either way (see plan_geometry).
    geometry_scale: Fraction = ONE
    #: when set, a successful render is registered with this cache (and evicted
    #: from it later if it falls outside the cache's budgets). Only cache-owned
    #: destinations may be used, so a preview never deletes a user's export.
    cache: PreviewCache | None = None
    #: asset id -> the file identity (size + mtime_ns) recorded when the asset was
    #: read. When supplied, every source this render reads is re-checked against it
    #: before decoding and again before publishing: a re-encode dropped at the same
    #: path mid-render is refused, not cut together from two different files.
    asset_identities: dict[str, Identity] = field(default_factory=dict)
    #: paths this render must never write to — every imported asset (placed or
    #: not) plus other protected project files. Enforced regardless of
    #: ``overwrite``: confirming a replacement is not consent to destroy a source.
    guard: DestinationGuard = field(default_factory=DestinationGuard)

    def plan(self) -> RenderPlan:
        return self.plan_for(self.out_start, self.out_end)

    def plan_for(self, out_start: Fraction, out_end: Fraction) -> RenderPlan:
        """Plan one sub-range with this request's settings applied.

        Chunking re-plans sub-ranges through the same entry point, so a preview
        cannot drift from the composition semantics it is supposed to preview.
        """
        return build_plan(self.project, out_start, out_end, self.geometry_scale)

    # ---------------------------------------------------------------- safety

    def capture_consent(self) -> Consent | None:
        """Record what stands at the destination now, as the approval's basis.

        A cache-owned destination is ours to replace, so ownership is the
        authorisation. Otherwise ``overwrite`` means the user approved replacing
        *the file they were shown*, and that file's identity is what publication
        is compared against a render later.
        """
        if self.cache is not None:
            if not self.cache.owns(self.destination):
                raise RenderError(
                    f"{self.destination} is a cache destination this cache does not "
                    "own; refusing to replace it"
                )
            return Consent.as_owner(self.destination)
        if self.overwrite:
            return Consent.capture(self.destination)
        return None

    def check_destination(self) -> None:
        """Refuse a destination that is this project's own media."""
        try:
            self.guard.check(self.destination, "write rendered media")
        except DestinationSafetyError as exc:
            raise RenderError(str(exc)) from exc

    def check_sources(self, asset_ids: set[str], when: str = "before rendering") -> None:
        """Validate every source *asset_ids* actually reads — once per asset.

        Only assets this range reads are checked (a range that ignores an offline
        asset can still be rendered), while destination protection covers every
        asset in the project: relaxing one would be a data-loss risk, relaxing
        the other is simply correct scoping.
        """
        for asset_id in sorted(asset_ids):
            path = self.asset_paths.get(asset_id)
            # an asset id is not something the user can act on: name the file the
            # project was saved with, so a moved source is findable
            if path is None:
                raise RenderError(
                    f"Asset {asset_id} is missing on disk: no file is recorded for it. "
                    "Relink it before exporting."
                )
            entry = check(
                Path(path), self.asset_identities.get(asset_id), Path(path).name
            )
            if entry.usable:
                continue
            # the state word is part of the message: "missing" and "changed"
            # need different remedies, and the user should not have to infer it
            raise RenderError(
                f"Asset {asset_id} cannot be used {when} [{entry.status.value}]: "
                f"{entry.describe()} Relink or reimport it, then render again."
            )

    @staticmethod
    def assets_used(plans: Iterable[RenderPlan]) -> set[str]:
        """Asset ids whose material a set of plans will decode."""
        used: set[str] = set()
        for plan in plans:
            for item in plan.video_items:
                if item.clip is not None:
                    used.add(item.clip.asset_id)
            for item in plan.audio_items:
                used.add(item.clip.asset_id)
        return used

    def resolve_tools(self) -> tuple[str, str]:
        """Locate ffmpeg/ffprobe, check codecs and the destination."""
        ffmpeg = self.ffmpeg or shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RenderError("ffmpeg is not available on PATH; install it to export")
        ffprobe = self.ffprobe or shutil.which("ffprobe")
        if ffprobe is None:
            raise RenderError("ffprobe is not available on PATH; cannot verify exports")
        for codec in (self.preset.video_codec, self.preset.audio_codec):
            if not encoder_available(ffmpeg, codec):
                raise RenderError(
                    f"this FFmpeg cannot encode {codec}; {self.preset.display} is "
                    "unavailable"
                )
        # protection first: an existing file is only replaceable if it is not media
        self.check_destination()
        if self.destination.exists() and not self.overwrite:
            raise RenderError(
                f"{self.destination} already exists; choose another name or confirm "
                "replacement"
            )
        if not self.destination.parent.is_dir():
            raise RenderError(f"Output directory does not exist: {self.destination.parent}")
        return ffmpeg, ffprobe

    def check_temp_space(self, plan: RenderPlan) -> None:
        """Refuse a chunked render that would fill the disk, before starting it.

        Chunked renders write lossless intermediates, which are large; failing
        with an estimate is better than running out of space half way through.
        """
        needed = estimate_intermediate_bytes(plan)
        try:
            free = shutil.disk_usage(self.destination.parent).free
        except OSError as exc:  # pragma: no cover - unreadable mount point
            raise RenderError(f"cannot check free space for {self.destination}: {exc}") from exc
        if needed + TEMP_MARGIN_BYTES > free:
            raise RenderError(
                f"this composition needs two FFmpeg passes and about "
                f"{needed / 1_000_000_000:.1f} GB of temporary space; only "
                f"{free / 1_000_000_000:.1f} GB is free. Export a shorter range, or "
                "free space, instead"
            )


def render(
    request: RenderRequest,
    on_progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> RenderResult:
    """Render ``request`` to ``request.destination``.

    Order matters: the destination is proven safe, the approval is recorded, and
    the sources are validated *before* anything is decoded; the same checks run
    again immediately before publication, and a cancellation is honoured there
    too. A cancelled, refused or failed render never writes to the destination,
    and never replaces a file that appeared or changed while it was running.
    """
    ffmpeg, ffprobe = request.resolve_tools()
    plan = request.plan()
    if plan.total_frames <= 0:
        raise RenderError("Nothing to render: the requested range is empty")

    consent = request.capture_consent()
    plans: list[RenderPlan] = [plan]
    request.check_sources(request.assets_used(plans))

    token = uuid.uuid4().hex[:12]
    staging = request.destination.with_name(
        f"{request.destination.stem}.{token}.partial.{request.preset.extension}"
    )
    owned: list[Path] = [staging]
    sources = {(s.asset_id, s.stream_index): s for s in request.project.sources}
    try:
        item_count = len(plan.video_items) + len(plan.audio_items)
        if item_count <= request.chunk_item_limit:
            job = build_job(plan, request.asset_paths, sources, staging, ffmpeg, request.preset)
            _run(job, on_progress, cancel_event)
            chunks = 1
        else:
            request.check_temp_space(plan)
            chunks = _render_chunked(
                request, plan, sources, staging, token, ffmpeg, owned, on_progress,
                cancel_event, plans,
            )
        produced = probe_duration(ffprobe, staging)
        tolerance = F(1) / plan.quantisation.frame_rate + AUDIO_PADDING_SECONDS
        if abs(produced - plan.duration) > tolerance:
            raise RenderError(
                f"rendered {float(produced):.3f}s but the plan calls for "
                f"{float(plan.duration):.3f}s (tolerance {float(tolerance):.3f}s); the "
                "source may end earlier than its recorded bounds"
            )
        if cancel_event is not None and cancel_event.is_set():
            # the last progress event is not the last thing that happens: a file
            # must not appear after the user asked for nothing
            raise RenderCancelled("render cancelled before the file was written")
        # the sources are re-validated: a file swapped mid-render would otherwise
        # publish a composition assembled from two different encodes
        request.check_destination()
        request.check_sources(request.assets_used(plans), when="after rendering")
        try:
            publish(
                staging,
                request.destination,
                guard=request.guard,
                consent=consent,
                action="write the rendered file",
            )
        except DestinationSafetyError as exc:
            raise RenderError(str(exc)) from exc
        owned.remove(staging)
        if request.cache is not None:
            request.cache.commit(request.destination)
        return RenderResult(
            path=request.destination,
            duration=plan.duration,
            produced_duration=produced,
            warnings=plan.warnings,
            chunks=chunks,
        )
    finally:
        for path in owned:
            path.unlink(missing_ok=True)


def _render_chunked(
    request: RenderRequest,
    plan: RenderPlan,
    sources,
    staging: Path,
    token: str,
    ffmpeg: str,
    owned: list[Path],
    on_progress: ProgressCallback | None,
    cancel_event: threading.Event | None,
    plans: list[RenderPlan] | None = None,
) -> int:
    """Lossless chunks, then one final encode (lossy encoders start once)."""
    pieces = _chunk_ranges(plan, request.chunk_item_limit)
    intermediates: list[Path] = []
    for index, piece in enumerate(pieces):
        intermediate = staging.with_name(f"{staging.stem}.chunk{index:03d}.mkv")
        intermediates.append(intermediate)
        owned.append(intermediate)
        piece_plan = request.plan_for(piece[0], piece[1])
        if plans is not None:
            plans.append(piece_plan)
        job = build_job(
            piece_plan, request.asset_paths, sources, intermediate, ffmpeg, LOSSLESS_INTERMEDIATE
        )
        _run(job, _scaled_progress(plan, piece[0], on_progress), cancel_event)
    list_file = staging.with_name(f"{staging.stem}.concat.txt")
    owned.append(list_file)
    # single-quoted paths only: FFmpeg's concat list format has no other escaping
    escaped = [p.as_posix().replace("'", "'\\''") for p in intermediates]
    list_file.write_text("".join(f"file '{p}'\n" for p in escaped), encoding="utf-8")
    job = _join_job(plan, list_file, staging, ffmpeg, request.preset)
    _run(job, on_progress, cancel_event)
    return len(pieces)


def _scaled_progress(
    plan: RenderPlan, piece_start: Fraction, on_progress: ProgressCallback | None
) -> ProgressCallback | None:
    if on_progress is None:
        return None
    total = float(plan.duration)

    def report(fraction: float) -> None:
        base = float(piece_start - plan.output_start) / total
        on_progress(min(1.0, base + fraction * (1.0 - base)))

    return report


def _chunk_ranges(plan: RenderPlan, limit: int) -> list[tuple[Fraction, Fraction]]:
    """Split the range at **item** boundaries with ~``limit`` items per chunk.

    Both picture and audio items are counted: an audio-heavy composition must
    not defeat the bound, because each item is a filter chain and (per distinct
    asset/stream/seek) a decoder input in the FFmpeg graph. Cut points are the
    exact output times at which some item ends, so re-planning each chunk from
    the same snapshot yields contiguous ranges whose frame and sample totals add
    up to the whole plan exactly.
    """
    items = len(plan.video_items) + len(plan.audio_items)
    if items <= limit:
        return [(plan.output_start, plan.output_end)]
    q = plan.quantisation
    base_frame = q.frame_index(plan.output_start)
    base_sample = q.sample_index(plan.output_start)
    ends = sorted(
        [q.frame_time(base_frame + item.end_frame) for item in plan.video_items]
        + [
            q.sample_time(base_sample + item.start_sample + item.samples)
            for item in plan.audio_items
        ]
    )
    ranges: list[tuple[Fraction, Fraction]] = []
    start = plan.output_start
    count = 0
    for end in ends:
        count += 1
        if count >= limit and start < end < plan.output_end:
            ranges.append((start, end))
            start = end
            count = 0
    if start < plan.output_end:
        ranges.append((start, plan.output_end))
    return ranges or [(plan.output_start, plan.output_end)]


def _join_job(
    plan: RenderPlan, concat_list: Path, destination: Path, ffmpeg: str, preset: CodecPreset
) -> RenderJob:
    q = plan.quantisation
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-c:v",
        preset.video_codec,
        "-pix_fmt",
        preset.pixel_format,
        *preset.extra_video_args,
        "-r",
        f"{q.frame_rate.numerator}/{q.frame_rate.denominator}",
        "-c:a",
        preset.audio_codec,
        "-ar",
        str(q.sample_rate),
        *preset.extra_audio_args,
    ]
    if preset.muxer == "mp4":
        args.extend(["-movflags", "+faststart"])
    args.extend(["-progress", "pipe:1", "-nostats", str(destination)])
    return RenderJob(
        args=args,
        duration=plan.duration,
        video_items=len(plan.video_items),
        audio_items=len(plan.audio_items),
        warnings=plan.warnings,
    )


def probe_duration(ffprobe: str, path: Path) -> Fraction:
    """Container duration as an exact rational (parsed from ffprobe's text)."""
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_format", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError(f"cannot probe the rendered file: {exc}") from exc
    if proc.returncode != 0:
        raise RenderError(f"cannot probe the rendered file: {proc.stderr.strip()[:400]}")
    try:
        raw = json.loads(proc.stdout)["format"]["duration"]
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise RenderError("ffprobe did not report a duration for the rendered file") from exc
    return Fraction(raw)


def _run(
    job: RenderJob,
    on_progress: ProgressCallback | None,
    cancel_event: threading.Event | None,
) -> None:
    """Run one FFmpeg job, streaming progress and honouring cancellation."""
    try:
        proc = subprocess.Popen(
            job.args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except OSError as exc:  # pragma: no cover - environment specific
        raise RenderError(f"cannot start ffmpeg: {exc}") from exc
    assert proc.stdout is not None and proc.stderr is not None
    total = float(job.duration)
    try:
        for line in proc.stdout:
            if cancel_event is not None and cancel_event.is_set():
                _terminate(proc)
                raise RenderCancelled("render cancelled")
            line = line.strip()
            if on_progress is None or total <= 0:
                continue
            microseconds = _progress_microseconds(line)
            if microseconds is not None:
                on_progress(min(1.0, microseconds / 1_000_000 / total))
        proc.wait()
    except RenderCancelled:
        raise
    except BaseException:  # never leave a stray ffmpeg behind
        _terminate(proc)
        raise
    if proc.returncode != 0:
        raise RenderError(_ffmpeg_message(proc.stderr.read().strip()))
    if on_progress is not None:
        on_progress(1.0)


def _progress_microseconds(line: str) -> int | None:
    key, _, value = line.partition("=")
    if key == "out_time_us":
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - stubborn ffmpeg
        proc.kill()
        proc.wait(timeout=5)


def _ffmpeg_message(stderr: str) -> str:
    """Useful, bounded FFmpeg diagnostics (never media payloads)."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    tail = " | ".join(lines[-4:]) if lines else "no diagnostic output"
    return f"ffmpeg failed: {tail[:600]}"

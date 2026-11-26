"""Frame-accurate single-picture access: composition stills and real PTS stepping.

Two related jobs, each bounded to exactly one picture (or one packet list):

* :func:`extract_composition_still` renders the still frame of the **output**
  (composition) at a given output time, and :func:`extract_source_still` renders
  one *source* picture of a clip at a chosen source time. Both build a one-frame
  range with the same planner the exporter uses and run the exporter's own picture
  chain - same seek rule, same sampling grid, same scaling - so the picture
  returned is the picture the export path would put there. If FFmpeg cannot decode
  the source the failure is reported, never papered over.
* :func:`source_frame_times` lists **actual** presentation timestamps of a video
  stream's packets, in a bounded window. It is the primitive
  :mod:`video_editor.media.frameindex` builds on: composition frame stepping
  advances by the output frame rate, source frame stepping advances between
  neighbouring real PTS values, and neither may assume a constant frame duration.

Every timestamp reported about a decoded picture is measured from that decode
(FFmpeg's ``showinfo``), not derived from a nominal frame rate; where it cannot be
measured the result says so instead of filling the gap with a plausible number.

Returned picture data is plain RGB bytes with no Qt involvement: the UI turns it
into an image at its own boundary.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from ..core.model import Clip, Project, StreamSource
from ..core.timeutils import ONE, ZERO
from ..fs.identity import Identity, check
from ..render.graph import picture_filter_chain
from ..render.plan import (
    PictureSampling,
    Quantisation,
    RenderPlan,
    VideoItem,
    build_plan,
    floor_to_time_base,
    picture_preroll,
    picture_sampling,
)

F = Fraction

#: ``showinfo`` pads its fields, so a frame's tick count never touches its label:
#: ``n:   0 pts:     10 pts_time:0.01``. Matching the label plus its run of spaces
#: is what picks out the tick; ``pts_time:`` cannot match because of the underscore.
_PTS_TICK = re.compile(r"\bpts:\s*(-?\d+)")

#: A still is only useful at a sane size; the UI asks for the plan's own (already
#: scaled) geometry, so this only guards misuse.
MAX_STILL_PIXELS = 8_000_000


class StillError(Exception):
    """The still frame or frame list could not be produced (reported, not guessed)."""


def decimal(value: Fraction) -> str:
    """Fixed-point text for the FFmpeg/probe options that cannot take rationals."""
    return f"{float(value):.9f}"


def _to_time(value: object) -> Fraction | None:
    """Parse ffprobe time text exactly ('N/A'/junk -> None)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Fraction(value)
    text = str(value).strip()
    if not text or text in {"N/A", "none", "0/0", "inf", "-inf"}:
        return None
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None


def _to_rational(value: object) -> Fraction | None:
    """Parse an ffprobe ``a/b`` rational (time bases); junk -> None."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if "/" not in text:
        return None
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None


# ------------------------------------------------------------------- structures


@dataclass(frozen=True)
class SourceFrame:
    """One real source picture, identified by its container presentation time."""

    pts: Fraction
    #: 0-based index in packet order as reported by ffprobe
    index: int

    @property
    def pts_text(self) -> str:
        return f"{float(self.pts):.6f}s (#{self.index})"


@dataclass(frozen=True)
class StillFrame:
    """One decoded picture, with what is known about where it came from.

    Every "which frame is this" field is measured or planned here rather than
    worked out in the interface: a viewer that guessed would present a wrong
    frame number to a user who is choosing where a cut goes.
    """

    width: int
    height: int
    rgb: bytes
    #: exact output time this frame presents, per the planner's quantisation
    output_time: Fraction = ZERO
    #: global output frame index (the same count the exporter uses)
    frame_index: int = 0
    #: True when the composition has no picture here: a truthful black frame
    black: bool = False
    #: asset-local source time the plan resolved for this frame
    source_pts: Fraction | None = None
    #: presentation time of the source picture actually shown, read from this
    #: decode (FFmpeg's ``showinfo``): the last real frame at or before
    #: ``source_pts``, which is the composition's rule. ``None`` when the decode
    #: reported no usable timestamp - then the picture is still the composition's,
    #: but its source position is unknown and the interface has to say so.
    decoded_source_pts: Fraction | None = None
    #: True when the sampling grid for this picture was placed on an exactly
    #: representable container timestamp. False means the container's time base
    #: could not be read, so a sampling time falling exactly on a frame time may
    #: select the neighbouring picture: the picture is honest, its position is not
    #: guaranteed, and that difference is shown rather than hidden.
    grid_exact: bool = True
    #: True when this is a *source* frame the composition does not itself present
    #: (stepped by hand to choose a cut): ``frame_index`` still names the output
    #: frame being inspected, but the picture comes from ``decoded_source_pts``.
    source_only: bool = False
    #: clip whose picture is presented, None on black frames
    clip_id: str | None = None
    asset_id: str | None = None
    stream_index: int | None = None

    @property
    def nbytes(self) -> int:
        return len(self.rgb)


@dataclass(frozen=True)
class SourceNeighbours:
    """The real frames bracketing an asset-local time."""

    at_or_before: SourceFrame | None
    at_or_after: SourceFrame | None
    #: every real frame in the probed window, in PTS order (stepping walks these)
    frames: tuple[SourceFrame, ...] = ()


# ------------------------------------------------------------------ probe tools


def _ffprobe(ffprobe: str | None) -> str:
    tool = ffprobe or shutil.which("ffprobe")
    if tool is None:
        raise StillError("ffprobe is not available")
    return tool


def _run(args: list[str], timeout: float, what: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StillError(f"cannot {what}: {exc}") from exc


@dataclass(frozen=True)
class StreamTiming:
    """How a container quantifies one video stream's timestamps."""

    #: container stream index (the domain model's identity)
    stream_index: int
    #: position among video streams, which is what FFmpeg/ffprobe address by
    position: int
    #: exact timestamp granularity of the container for this stream
    time_base: Fraction
    #: the stream's own presentation start on the asset-local axis (ffprobe's
    #: ``start_time``): reported for context and validation, **not** added to
    #: packet PTS, which already sit on this axis (see ``source_frame_times``)
    start_time: Fraction = ZERO


def stream_timing(
    path: Path, stream_index: int, ffprobe: str | None = None, timeout: float = 30
) -> StreamTiming:
    tool = _ffprobe(ffprobe)
    proc = _run(
        [
            tool, "-v", "error", "-show_streams",
            "-select_streams", "v",
            "-show_entries", "stream=index,time_base,start_time",
            "-of", "json", str(path),
        ],
        timeout,
        f"probe the timestamp basis of {path.name}",
    )
    if proc.returncode != 0:
        raise StillError(f"cannot probe {path.name}: {proc.stderr.strip()[:300]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise StillError(f"unparseable ffprobe output for {path.name}: {exc}") from exc
    streams = payload.get("streams", [])
    for video_position, stream in enumerate(streams):
        if int(stream.get("index", -1)) == stream_index:
            base = _to_rational(stream.get("time_base"))
            if base is None or base <= 0:
                raise StillError(
                    f"stream {stream_index} of {path.name} has no usable time base; "
                    "its frame timestamps cannot be computed exactly"
                )
            return StreamTiming(
                stream_index=stream_index,
                position=video_position,
                time_base=base,
                start_time=_to_time(stream.get("start_time")) or ZERO,
            )
        # streams before this one that are video do not count: this loop is over
        # video streams only (``-select_streams v``), so ``enumerate`` gives the
        # position FFmpeg itself would use
    for stream in streams:  # index exists but is not video -> say so precisely
        if int(stream.get("index", -1)) == stream_index:
            raise StillError(f"stream {stream_index} of {path.name} is not video")
    raise StillError(f"stream {stream_index} is not present in {path.name}")


def stream_position_of(
    path: Path, stream_index: int, ffprobe: str | None = None, timeout: float = 30
) -> int:
    """Container stream index -> FFmpeg/ffprobe ``v:N`` position (measured rule)."""
    return stream_timing(path, stream_index, ffprobe, timeout).position


def source_frame_times(
    path: Path,
    stream_index: int,
    ffprobe: str | None = None,
    from_time: Fraction | None = None,
    to_time: Fraction | None = None,
    timeout: float = 60,
) -> list[SourceFrame]:
    """Actual presentation timestamps of a video stream's packets, ascending.

    Integer packet ``pts`` values are multiplied by the container's exact
    ``time_base`` rather than reading ffprobe's printed decimals: a
    millisecond-based container (Matroska) prints 8.083 for a frame that really
    sits at 8.0833…, and a parsed 8083/1000 would put a source step slightly off
    the real frame. Irregular (VFR) timestamps are therefore preserved exactly.

    Packet PTS are **not** offset by the stream's ``start_time``: measured on
    Matroska and MP4, with both a whole-asset offset (``-output_ts_offset 2.5``,
    where the first video packet reads ``pts 2500`` at ``1/1000``) and a
    per-stream offset (``-itsoffset`` on the video input, first packet ``pts
    1000`` while ``start_time`` reads 1.0), packet timestamps already sit on the
    same presentation timeline as ``start_time`` - which is exactly the asset-local
    axis the domain model uses. Adding it again would double the offset and put
    every source step seconds away from the real picture. Packets without a PTS are
    skipped: they cannot be addressed. When ``from_time``/``to_time`` are given,
    probing is limited to that window so a long film is not fully scanned.
    """
    timing = stream_timing(path, stream_index, ffprobe)
    tool = _ffprobe(ffprobe)
    args = [
        tool,
        "-v",
        "error",
        "-select_streams",
        f"v:{timing.position}",
        "-show_entries",
        "packet=pts",
        "-of",
        "json",
    ]
    if from_time is not None and to_time is not None:
        args += ["-read_intervals", f"{decimal(from_time)}%{decimal(to_time)}"]
    args.append(str(path))
    proc = _run(args, timeout, f"read the frame timestamps of {path.name}")
    if proc.returncode != 0:
        raise StillError(
            f"cannot read frame timestamps of {path.name}: {proc.stderr.strip()[:300]}"
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise StillError(f"unparseable ffprobe output for {path.name}: {exc}") from exc
    stamps: list[Fraction] = []
    for packet in payload.get("packets", []):
        raw = packet.get("pts")
        if raw is None:
            continue
        try:
            counter = int(raw)
        except (TypeError, ValueError):
            continue  # 'N/A'
        stamps.append(counter * timing.time_base)
    stamps.sort()
    return [SourceFrame(pts=value, index=i) for i, value in enumerate(stamps)]


# ----------------------------------------------------------- reading real frames

#: The composition's rule ("show the last real picture at or before the sampling
#: time") needs the stream's real timestamps, which :mod:`video_editor.media.
#: frameindex` reads in bounded windows. ``source_frame_times`` is the primitive
#: it builds on: one bounded probe, exact timestamps.


# ------------------------------------------------- composition still-frame grab


def still_plan_for(
    project: Project,
    output_time: Fraction,
    geometry_scale: Fraction = ONE,
) -> tuple[RenderPlan, Fraction, int]:
    """One-frame plan for ``output_time`` plus its quantised time and index.

    The frame covering an output time is the one the planner itself puts first in
    the range ``[output_time, output_time + 1/frame_rate)`` - the frame that
    *starts* at or after it, the planner's single rounding rule - so the still in
    the viewer and the frame the exporter writes at that index are the same frame
    by construction, not by two modules agreeing on a rounding of ``output_time``.
    """
    if output_time < 0:
        raise StillError("Output time must be non-negative")
    rate = project.output.frame_rate
    horizon = project.output_end()
    if output_time >= horizon:
        # There is no picture there. Rendering black would show a frame the export
        # never writes at that index, and a viewer note cannot make that honest:
        # the last frame of the composition is a real place to stop looking.
        quant = Quantisation(rate, project.output.sample_rate)
        last = quant.frame_time(max(0, quant.frame_index(horizon) - 1))
        raise StillError(
            f"no output frame at {float(output_time):.6f}s: the composition ends at "
            f"{float(horizon):.6f}s; the last frame it has is at {float(last):.6f}s"
        )
    plan = build_plan(project, output_time, output_time + F(1) / rate, geometry_scale)
    if plan.total_frames < 1:
        raise StillError(
            f"the planner produced no output frame for {float(output_time):.3f}s: "
            "nothing can be shown for that time"
        )
    index = plan.quantisation.frame_index(output_time)
    frame_time = plan.quantisation.frame_time(index)
    if not output_time <= frame_time < output_time + F(1) / rate:
        raise StillError(
            f"the planner's frame {index} at {float(frame_time):.6f}s does not cover "
            f"{float(output_time):.6f}s; viewer and export would disagree"
        )
    return plan, frame_time, index


def _snap_to_ticks(value: Fraction, time_base: Fraction) -> Fraction | None:
    """Nearest representable timestamp, or None if ``value`` is not that close.

    A decoded frame's timestamp is a whole number of container ticks by
    definition, while FFmpeg's own seek arithmetic is decimal text: reconstructing
    a PTS from "what it reported plus what we asked it to seek to" is therefore
    right to within a nanosecond and wrong in the last digits. Snapping recovers
    the exact timestamp; the tolerance check keeps this from inventing one when the
    arithmetic and the container disagree by more than half a tick.
    """
    if time_base <= 0:
        return None
    whole = floor_to_time_base(value, time_base) / time_base
    snapped = whole * time_base
    for candidate in (snapped, (whole + 1) * time_base, (whole - 1) * time_base):
        if abs(candidate - value) <= time_base / 2:
            return candidate
    return None


def _decoded_timestamps(stderr: str, time_base: Fraction | None) -> list[Fraction]:
    """Picture times as the decode reported them, relative to the seek point.

    ``showinfo`` is what makes this measurement possible: it prints each frame's
    timestamp as it enters the filter chain, before any resampling, so the number
    is the picture's own, not one inferred from a nominal frame rate (measured on
    FFmpeg 8.1.3: seeking a Matroska file to 0.07 s and printing the first frames
    gives ``pts:10 pts_time:0.01``, ``pts:50 pts_time:0.05``, …, i.e. the real
    frames at 0.08 s and 0.12 s shifted by exactly the requested seek).

    The ticks are multiplied by the exact ``time_base`` rather than reading the
    printed ``pts_time`` decimals, for the same reason packet timestamps are: a
    millisecond container prints 8.083 for a frame that really sits at 8.0833…

    ``showinfo`` pads its fields, so ``pts:`` and its value are separated by
    running whitespace - an earlier revision split on single spaces and matched
    nothing at all, which silently reported every measured position as unknown.
    ``None`` time base (neither showinfo's own line nor a probe gave one) means
    the ticks cannot be turned into seconds, which is reported as "unknown"
    rather than answered with a guess.
    """
    base = time_base
    for line in stderr.splitlines():
        if "config in time_base:" in line:
            token = line.split("config in time_base:", 1)[1].split(",")[0].strip()
            base = _to_rational(token) or base
    if base is None or base <= 0:
        return []
    stamps: list[Fraction] = []
    for line in stderr.splitlines():
        if "showinfo" not in line:
            continue
        match = _PTS_TICK.search(line)
        if match is not None:
            stamps.append(int(match.group(1)) * base)
    return stamps


def _picture_stream(
    project: Project, clip: Clip, asset_paths: dict[str, Path],
    asset_identities: dict[str, Identity] | None,
) -> tuple[Path, StreamSource, int]:
    """Resolve the file, stream record and FFmpeg position for a clip's picture."""
    source = next(
        (s for s in project.sources if s.key == (clip.asset_id, clip.stream_index)), None
    )
    if source is None:
        raise StillError(
            f"stream {clip.stream_index} of asset {clip.asset_id} is not registered in "
            "the project: its frame cannot be resolved"
        )
    if source.kind.value != "video":
        raise StillError(f"clip {clip.id} addresses a {source.kind.value} stream as picture")
    path = asset_paths.get(clip.asset_id)
    if path is None:
        raise StillError(
            f"asset {clip.asset_id} has no file recorded: reimport or relink it before "
            "inspecting frames"
        )
    # not just "a file is there": the file must still be the one whose stream
    # bounds and frame rate this project recorded, or the frame shown would be a
    # guess from a different encode
    entry = check(Path(path), (asset_identities or {}).get(clip.asset_id), Path(path).name)
    if not entry.usable:
        raise StillError(f"cannot inspect a frame of {clip.asset_id}: {entry.describe()}")
    position = source.stream_position
    if position is None:
        position = stream_position_of(path, source.stream_index)
    return Path(path), source, position


def _decode_picture(
    ffmpeg: str,
    path: Path,
    source: StreamSource,
    position: int,
    clip: Clip,
    plan: RenderPlan,
    item: VideoItem,
    timeout: float,
    first_frame: Fraction | None = None,
) -> tuple[bytes, Fraction | None, PictureSampling]:
    """One picture through the export path's own chain, plus what it really was.

    Returns the raw RGB24 frame, the asset-local presentation time of the source
    picture actually shown (measured from this decode, ``None`` when the decode
    reported no usable timestamp) and the sampling contract used.

    The chain, the seek and the reading bound all come from
    :func:`video_editor.render.graph.picture_filter_chain` and
    :func:`video_editor.render.plan.picture_sampling` - the same calls the exporter
    makes - because a still that sampled differently from an export would be a
    picture the project does not contain.
    """
    rate = plan.quantisation.frame_rate
    base: Fraction | None = None
    try:
        base = stream_timing(path, source.stream_index, timeout=timeout).time_base
    except StillError:
        base = None  # the picture can still be decoded; exactness is reported below
    sampling = picture_sampling(
        clip,
        item,
        rate,
        time_base=base,
        preroll=picture_preroll(clip, source, rate),
        first_frame=first_frame,
    )
    chain = picture_filter_chain(plan, clip, item, sampling, "rgb24")
    # input -ss is measured from the file's presentation start, not from 0
    file_seek = max(sampling.seek_source - source.input_start, ZERO)
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-ss",
        decimal(file_seek),
        "-t",
        decimal(sampling.read_duration),
        "-i",
        str(path),
        "-map",
        f"0:v:{position}",
        "-frames:v",
        "1",
        "-vf",
        f"showinfo,{chain}",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StillError(f"cannot decode the source frame: {exc}") from exc
    expected = plan.width * plan.height * 3
    if proc.returncode != 0 or len(proc.stdout) < expected:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:300]
        raise StillError(
            f"cannot decode the frame of {path.name} at asset-local "
            f"{float(sampling.sample_start):.6f}s: "
            f"{detail or f'short read ({len(proc.stdout)} of {expected} bytes)'}"
        )
    stderr = proc.stderr.decode("utf-8", "replace")
    shown: Fraction | None = None
    if sampling.anchored and base:
        # measured, not planned: the frames this decode actually read. ``showinfo``
        # reports them relative to the requested input seek, and that seek is sent
        # as decimal text, so the reconstruction below can be off by the difference
        # between the exact seek and what the text rounded to (< 1 ns): snapping to
        # the container's own tick grid recovers the exact timestamp.
        stamps = _decoded_timestamps(stderr, base)
        absolute = [file_seek + source.input_start + s for s in stamps]
        # the picture shown is the last one at or before the first sampling time:
        # measured rule of the ``fps`` stage, applied to this decode's own frames
        before = [s for s in absolute if s <= sampling.sample_start]
        pick = before[-1] if before else (absolute[0] if absolute else None)
        if pick is not None:
            shown = _snap_to_ticks(pick, base) or pick
    return proc.stdout[:expected], shown, sampling


def extract_composition_still(
    project: Project,
    asset_paths: dict[str, Path],
    output_time: Fraction,
    geometry_scale: Fraction = ONE,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    timeout: float = 30,
    asset_identities: dict[str, Identity] | None = None,
    first_frame: Fraction | None = None,
) -> StillFrame:
    """Decode the composition's still frame at ``output_time``.

    Black gaps produce a truthful black frame without touching a decoder, and a
    missing/unregistered asset is an error rather than a substitute picture.
    *asset_identities* (asset id -> recorded size + mtime_ns, as
    :func:`video_editor.media.availability.recorded_identity` returns it) makes
    "missing" mean "missing **or replaced**": a file rewritten at the same path
    is refused instead of decoded against the recorded stream bounds.
    *first_frame* is the clip's first readable picture when a caller has measured
    one (:class:`video_editor.media.frameindex.FrameIndex`): it decides how many
    head frames hold that picture instead of guessing from the lattice.

    ``ffprobe`` is unused except through the media helpers this calls; it is kept
    in the signature because callers pass it through to every media tool.
    """
    encode = ffmpeg or shutil.which("ffmpeg")
    if encode is None:
        raise StillError("ffmpeg is not available: still frames cannot be decoded")
    del ffprobe  # every probe below goes through helpers that resolve it themselves
    plan, frame_time, index = still_plan_for(project, output_time, geometry_scale)
    if plan.width * plan.height > MAX_STILL_PIXELS:
        raise StillError(
            f"still frame would be {plan.width}x{plan.height}: refusing to allocate "
            "an oversized picture (lower the preview size)"
        )
    item = plan.picture_at(frame_time)
    if item is None or item.is_black:
        return StillFrame(
            width=plan.width,
            height=plan.height,
            rgb=bytes(plan.width * plan.height * 3),
            output_time=frame_time,
            frame_index=index,
            black=True,
        )
    clip = item.clip
    assert clip is not None
    path, source, position = _picture_stream(project, clip, asset_paths, asset_identities)
    rgb, shown, sampling = _decode_picture(
        encode, path, source, position, clip, plan, item, timeout, first_frame
    )
    return StillFrame(
        width=plan.width,
        height=plan.height,
        rgb=rgb,
        output_time=frame_time,
        frame_index=index,
        black=False,
        source_pts=item.source_start,
        decoded_source_pts=shown,
        grid_exact=sampling.anchored,
        clip_id=clip.id,
        asset_id=clip.asset_id,
        stream_index=clip.stream_index,
    )


def extract_source_still(
    project: Project,
    asset_paths: dict[str, Path],
    clip_id: str,
    source_pts: Fraction,
    output_time: Fraction,
    geometry_scale: Fraction = ONE,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    timeout: float = 30,
    asset_identities: dict[str, Identity] | None = None,
    first_frame: Fraction | None = None,
) -> StillFrame:
    """Decode one *source* picture of a clip, at ``source_pts``.

    This is what stepping through a source frame by frame means: the picture comes
    from the stream at that time, not from the composition's frame under the
    playhead, so a user can see the frame they are about to cut to. It still uses
    the project's geometry and the export's sampling rule, so the picture is the
    one an export of that region would contain.

    The clip's own in/out do **not** limit the step: cutting means moving those
    edges, so the readable range is the stream's. The stream's recorded bounds do
    limit it, because material the stream does not have cannot be decoded and a
    viewer that filled the gap with black would be showing a frame that does not
    exist.
    """
    encode = ffmpeg or shutil.which("ffmpeg")
    if encode is None:
        raise StillError("ffmpeg is not available: still frames cannot be decoded")
    del ffprobe
    clip = next((c for c in project.clips if c.id == clip_id), None)
    if clip is None:
        raise StillError(
            f"clip {clip_id} is no longer in the project: its source frames cannot be "
            "inspected"
        )
    source = project.source(clip.asset_id, clip.stream_index)
    if source is None:
        raise StillError(
            f"stream {clip.stream_index} of asset {clip.asset_id} is not registered in "
            "the project: its frame cannot be resolved"
        )
    if not source.placeable or source.end is None:
        raise StillError(
            f"stream {clip.stream_index} of asset {clip.asset_id} has no recorded "
            "bounds: a source frame cannot be located in it"
        )
    if source_pts < source.origin or source_pts >= source.end:
        raise StillError(
            f"{float(source_pts):.6f}s is outside asset {clip.asset_id} stream "
            f"{clip.stream_index} (which runs {float(source.origin):.6f}s to "
            f"{float(source.end):.6f}s on its own axis)"
        )
    # geometry and size come from the composition at the frame being inspected
    plan, frame_time, index = still_plan_for(project, output_time, geometry_scale)
    if plan.width * plan.height > MAX_STILL_PIXELS:
        raise StillError(
            f"still frame would be {plan.width}x{plan.height}: refusing to allocate "
            "an oversized picture (lower the preview size)"
        )
    # the slice becomes the whole stream: the point of stepping is to move a cut,
    # so the current edge must not be the limit; the rate and identity stay the
    # clip's, because the picture shown is that clip's material
    widened = replace(clip, source_in=source.origin, source_out=source.end)
    item = VideoItem(
        start_frame=0,
        frames=1,
        clip=widened,
        source_start=source_pts,
        source_end=source_pts + clip.rate,
        output_start=frame_time,
        output_end=frame_time + F(1) / plan.quantisation.frame_rate,
    )
    path, stream, position = _picture_stream(project, clip, asset_paths, asset_identities)
    rgb, shown, sampling = _decode_picture(
        encode, path, stream, position, widened, plan, item, timeout, first_frame
    )
    return StillFrame(
        width=plan.width,
        height=plan.height,
        rgb=rgb,
        output_time=frame_time,
        frame_index=index,
        black=False,
        source_pts=source_pts,
        decoded_source_pts=shown,
        grid_exact=sampling.anchored,
        source_only=True,
        clip_id=clip.id,
        asset_id=clip.asset_id,
        stream_index=clip.stream_index,
    )

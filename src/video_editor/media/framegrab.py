"""Frame-accurate single-picture access: composition stills and real PTS stepping.

Two related jobs, each bounded to exactly one picture (or one packet list):

* :func:`extract_composition_still` renders the still frame of the **output**
  (composition) at a given output time. It builds a one-frame range with the same
  planner the exporter uses, so the picture it returns is the picture the export
  path would put on that frame: the winning clip and its source slice come from
  quantised global frame indices, and the FFmpeg addressing is the one
  ``render/graph.py`` uses (input ``-ss`` measured from ``format.start_time``,
  picture addressed by kind + position, aspect-preserving scale + pad). If FFmpeg
  cannot decode the source the failure is reported, never papered over.

* :func:`source_frame_times` lists **actual** presentation timestamps of a video
  stream's packets. Composition frame stepping advances by the output frame rate;
  source frame stepping must not assume a constant frame duration, so it advances
  between neighbouring real PTS values, which is what makes variable-frame-rate
  material land on real frames.

Returned picture data is plain RGB bytes with no Qt involvement: the UI turns it
into an image at its own boundary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ..core.model import Clip, Project, StreamSource
from ..core.timeutils import ONE, ZERO
from ..fs.identity import Identity, check
from ..render.plan import RenderPlan, build_plan

F = Fraction

#: A still is only useful at a sane size; the UI asks for the plan's own (already
#: scaled) geometry, so this only guards misuse.
MAX_STILL_PIXELS = 8_000_000

#: Default +/- probe window when listing frame timestamps, so a long film is not
#: fully scanned just to step one frame.
DEFAULT_PROBE_WINDOW = F(2)


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
    """One decoded output frame."""

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
    #: real PTS of the source picture that seek produces: the frame exactly at
    #: ``source_pts`` if the stream has one, otherwise the first later frame
    #: (input ``-ss`` discards earlier pictures). Differs from ``source_pts`` on
    #: VFR material or when the planned slice start falls between two real frames.
    decoded_source_pts: Fraction | None = None
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


def _stream_timing(
    path: Path, stream_index: int, ffprobe: str | None, timeout: float = 30
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
    return _stream_timing(path, stream_index, ffprobe, timeout).position


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
    timing = _stream_timing(path, stream_index, ffprobe)
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


def frames_around(
    path: Path,
    source: StreamSource,
    around: Fraction,
    window: Fraction = DEFAULT_PROBE_WINDOW,
    ffprobe: str | None = None,
) -> SourceNeighbours:
    """Real frames bracketing an asset-local time (bounded probe window)."""
    start = max(ZERO, around - window)
    frames = source_frame_times(
        path, source.stream_index, ffprobe=ffprobe, from_time=start, to_time=around + window
    )
    return SourceNeighbours(
        at_or_before=nearest_frame(frames, around, -1),
        at_or_after=nearest_frame(frames, around, +1),
        frames=tuple(sorted(frames, key=lambda f: f.pts)),
    )


def nearest_frame(
    frames: list[SourceFrame], current: Fraction, direction: int
) -> SourceFrame | None:
    """Neighbouring real frame for stepping (+1 later, -1 earlier, 0 exact).

    Returns None when there is no neighbouring frame in that direction, i.e. the
    playhead sits on the first/last real frame of the stream.
    """
    if not frames:
        return None
    if direction > 0:
        later = [f for f in frames if f.pts > current]
        return min(later, key=lambda f: f.pts) if later else None
    if direction < 0:
        earlier = [f for f in frames if f.pts < current]
        return max(earlier, key=lambda f: f.pts) if earlier else None
    exact = [f for f in frames if f.pts == current]
    return exact[0] if exact else None


def presented_frame(frames: list[SourceFrame], at: Fraction) -> SourceFrame | None:
    """The picture an input seek to ``at`` actually yields.

    FFmpeg's input ``-ss`` discards pictures before the requested time, so the frame
    shown is the one whose PTS equals ``at`` when the stream has one, and otherwise
    the first later frame. Reported by :attr:`StillFrame.decoded_source_pts` so the
    difference between the planned time and the real picture is visible rather than
    hidden - on variable-frame-rate material the two differ often.
    """
    return nearest_frame(frames, at, 0) or nearest_frame(frames, at, +1)


# ------------------------------------------------- composition still-frame grab


def still_plan_for(
    project: Project,
    output_time: Fraction,
    geometry_scale: Fraction = ONE,
) -> tuple[RenderPlan, Fraction, int]:
    """One-frame plan for ``output_time`` plus its quantised time and index.

    The frame covering an output time is the one with global index
    ``round_half_up(t·rate)`` — the planner's own rule — so the still shown in the
    viewer and the frame written by the exporter are the same frame.
    """
    if output_time < 0:
        raise StillError("Output time must be non-negative")
    rate = project.output.frame_rate
    plan = build_plan(
        project, output_time, output_time + F(1) / rate, geometry_scale
    )
    index = plan.quantisation.frame_index(output_time)
    frame_time = plan.quantisation.frame_time(index)
    if plan.total_frames < 1:
        raise StillError(
            f"the planner produced no output frame for {float(output_time):.3f}s: "
            "nothing can be shown for that time"
        )
    return plan, frame_time, index


def picture_filter_chain(clip: Clip, plan: RenderPlan) -> str:
    """The export path's picture chain, ending at RGB instead of YUV.

    Kept as one function so a still cannot drift from what export does to the same
    source slice (rate change, output-rate normalisation, aspect-preserving
    geometry).
    """
    rate = plan.quantisation.frame_rate
    return ",".join(
        [
            "setpts=PTS-STARTPTS",
            f"setpts=(PTS-STARTPTS)/{decimal(clip.rate)}",
            f"fps={rate.numerator}/{rate.denominator}",
            f"scale={plan.width}:{plan.height}:force_original_aspect_ratio=decrease",
            "setsar=1",
            f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2",
            "format=rgb24",
        ]
    )


def extract_composition_still(
    project: Project,
    asset_paths: dict[str, Path],
    output_time: Fraction,
    geometry_scale: Fraction = ONE,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    timeout: float = 30,
    asset_identities: dict[str, Identity] | None = None,
) -> StillFrame:
    """Decode the composition's still frame at ``output_time``.

    Black gaps produce a truthful black frame without touching a decoder, and a
    missing/unregistered asset is an error rather than a substitute picture.
    *asset_identities* (asset id -> recorded size + mtime_ns, as
    :func:`video_editor.media.availability.recorded_identity` returns it) makes
    "missing" mean "missing **or replaced**": a file rewritten at the same path
    is refused instead of decoded against the recorded stream bounds.
    """
    encode = ffmpeg or shutil.which("ffmpeg")
    if encode is None:
        raise StillError("ffmpeg is not available: still frames cannot be decoded")
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
        position = stream_position_of(path, source.stream_index, ffprobe)
    input_seek = max(item.source_start - source.input_start, F(0))
    args = [
        encode,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        decimal(input_seek),
        "-i",
        str(path),
        "-map",
        f"0:v:{position}",
        "-frames:v",
        "1",
        "-vf",
        picture_filter_chain(clip, plan),
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
            f"{float(item.source_start):.6f}s: "
            f"{detail or f'short read ({len(proc.stdout)} of {expected} bytes)'}"
        )
    decoded: Fraction | None = None
    try:
        neighbours = frames_around(path, source, item.source_start, ffprobe=ffprobe)
    except StillError:  # the picture is still valid; the PTS note is optional
        neighbours = None
    if neighbours is not None:
        shown = presented_frame(list(neighbours.frames), item.source_start)
        decoded = shown.pts if shown is not None else None
    return StillFrame(
        width=plan.width,
        height=plan.height,
        rgb=proc.stdout[:expected],
        output_time=frame_time,
        frame_index=index,
        black=False,
        source_pts=item.source_start,
        decoded_source_pts=decoded,
        clip_id=clip.id,
        asset_id=clip.asset_id,
        stream_index=clip.stream_index,
    )

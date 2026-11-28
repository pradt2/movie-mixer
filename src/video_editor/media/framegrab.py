"""Frame-accurate single-picture access: composition stills and real PTS stepping.

Two related jobs, each bounded to exactly one picture (or one packet list):

* :func:`extract_composition_still` renders the still frame of the **output**
  (composition) at a given output time. It asks the planner which single output
  frame is wanted - the frame **already covering** the playhead,
  ``Quantisation.frame_at_playhead`` (floor), never the render-range boundary
  (ceil), so a still is never a picture from up to one frame in the future and
  never a frame past the end of the composition - and then builds that frame's own
  lattice interval, ``[i/frame_rate, (i+1)/frame_rate)``, as the one-frame range the
  exporter would be given. It runs the exporter's own picture chain - same seek
  rule, same sampling grid, same scaling - so the picture returned is the picture
  the export path would put there. If FFmpeg cannot decode the source the failure is
  reported, never papered over. And because that picture *is* the composition's own
  media, the still passes the same capability preflight export and preview pass: a
  frame of interlaced or HDR source picture is refused with the reason and the
  offending value, not shown as if this renderer could produce it. The preflight
  runs before the decode; nothing about the chain, the seek or the reported stamps
  changes because of it.
* :func:`extract_source_still` decodes one picture of the **source stream** at a
  chosen asset-local time, and asks nothing of the composition for the picture
  itself: no still plan, no sampling grid, no tempo, no resampling. An accurate
  input seek plus exactly one decoded picture behind a ``showinfo`` tap *is* the
  stream's answer to "what do you hold here" - at any clip rate, outside the
  clip's own edges, independent of the output frame rate and of whether the
  composition reaches this far. Its timestamp comes from that same decode; the
  display geometry (``render.graph.picture_scale_fit`` → ``setsar=1`` → pad) is
  the composition's, so the picture has the same shape the export would give it.
  It is deliberately **not** capability-refused: this is how a source sitting in
  the bin is inspected, including a source that cannot be rendered, and a file
  that merely exists must stay usable. Refusal belongs to the paths that produce
  the composition's own media - export, preview and the composition still.
* :func:`source_frame_times` lists **actual** presentation timestamps of a video
  stream's packets, in a bounded window. A packet says where to seek, not that a
  picture is there to show, so it is not what the composition navigates by:
  :mod:`video_editor.media.frameindex` builds on
  :func:`video_editor.media.frameprobe.decoded_picture_times` instead, which decodes
  the window. What stays true of both is the discipline they share - one bounded
  probe, exact integer timestamps, no assumed constant frame duration, and
  composition frame stepping advancing by the output frame rate while source frame
  stepping advances between neighbouring real timestamps.

Every timestamp reported about a decoded picture is measured from that decode
(FFmpeg's ``showinfo``), not derived from a nominal frame rate; where it cannot be
measured the result says so instead of filling the gap with a plausible number.
What "measured" means for a *composition* still is narrower than it looks, and is
stated where it is decided (:func:`_shown_picture_stamp`): a timestamp seen
upstream of the ``fps`` stage says which pictures the decoder handed over, not
which one ``fps`` later chose, so a composition still reports a source stamp only
when the returned picture's identity survives the chain's own selection in whole
ticks, and reports ``None`` - with the plan's own time kept as the prediction it
is - whenever it does not.

Returned picture data is plain RGB bytes with no Qt involvement: the UI turns it
into an image at its own boundary.

**Every child here belongs to** :func:`video_editor.media.process.run_process`
(TASK_13's runner), which owns its two pipes, drains them on their own threads and
always terminates and reaps. Two consequences, both deliberate (TASK_17):

* A caller hands a ``cancel`` :class:`threading.Event`` down the helper chain and the
  child that is running is stopped by it — a superseded viewer request or a worker
  stop does not have to wait out a decode. Cancellation is its own answer,
  :class:`StillCancelled`, and never a decode failure: a stopped request returns no
  picture, no partial picture and no "could not be measured" answer either, because
  that is how a request nobody wants would be reported as an ordinary result.
* Every capture has a stated byte limit: the raw ``-f rawvideo`` answer of a still is
  limited to **exactly one picture of the validated geometry** - ``width * height *
  3`` bytes for the RGB24 the chain was asked to write - and probe answers to the
  limits below, measured against real answers in the task record. An answer past its
  limit is refused by name, never parsed from truncated bytes.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ..core.model import Clip, Project, StreamSource
from ..core.timeutils import ONE, ZERO
from ..fs.identity import Identity, check
from ..render.graph import (
    RenderConfigError,
    picture_filter_chain,
    picture_scale_fit,
    tick_number,
)
from ..render.plan import (
    PictureSampling,
    Quantisation,
    ReadWindow,
    RenderPlan,
    VideoItem,
    build_plan,
    ceil_to_time_base,
    floor_to_time_base,
    picture_preroll,
    picture_read_window,
    picture_sampling,
    plan_geometry,
)
from .capability import PictureCapabilityError, check_picture_capabilities
from .process import (
    CapturePolicy,
    Keep,
    ProcessCancelled,
    ProcessError,
    ProcessLaunchError,
    ProcessOverflow,
    ProcessResult,
    ProcessTimeout,
    run_process,
)

F = Fraction

#: ``showinfo`` pads its fields, so a frame's tick count never touches its label:
#: ``n:   0 pts:     10 pts_time:0.01``. Matching the label plus its run of spaces
#: is what picks out the tick; ``pts_time:`` cannot match because of the underscore.
_PTS_TICK = re.compile(r"\bpts:\s*(-?\d+)")

#: A still is only useful at a sane size; the UI asks for the plan's own (already
#: scaled) geometry, so this only guards misuse.
MAX_STILL_PIXELS = 8_000_000

# ------------------------------------------------------ capture limits, all stated
#
# Measured at ``dd99cf2`` with ``/tmp/opencode/m17/measure_head.log`` (the same
# fixtures the picture tests use), these are the largest answers a real FFmpeg or
# ffprobe actually wrote in these paths.  Each limit is that measurement with room
# for a real film, so no legitimate answer is refused while nothing unbounded is
# kept either.

#: Bytes of RGB24 one picture of the validated geometry occupies: the chain is asked
#: for ``format=rgb24`` and ``-frames:v 1``, so this is exactly what a still decode
#: may write to stdout.  Width and height come from the plan (or from
#: ``plan_geometry`` for a source still), both already refused past
#: :data:`MAX_STILL_PIXELS`, so this count is bounded before the child starts.
def rgb24_frame_bytes(width: int, height: int) -> int:
    """The exact stdout size one RGB24 picture of this geometry occupies."""
    return width * height * 3


#: A decoded-window or tail read answers one record per picture: measured 133 bytes
#: per record (19 937 bytes for a 6 s window of 150 pictures) and 19 998 bytes the
#: largest answer seen.  Both probes require a bounded window, and the widest one any
#: caller here asks for is 10 s — 1 200 pictures at an extreme 120 fps — so 8 MiB
#: holds ~65 000 records, ~50x the widest window, and is refused rather than parsed
#: short if a window were ever asked past that.
WINDOW_PROBE_JSON_LIMIT = 8 * 1024 * 1024

#: A packet-list read may be asked **without** a window
#: (:func:`source_frame_times`), so its ceiling is stated rather than derived from a
#: window: measured 50 bytes per ``{"packet": {"pts": …}}`` record, so 32 MiB is
#: ~670 000 records — 7.5 h of 25 fps picture, longer than any real film.
PACKET_PROBE_JSON_LIMIT = 32 * 1024 * 1024

#: ``-show_streams`` and ``-show_format`` answers: measured 1 041 and 91 bytes.
#: 1 MiB covers a container with a thousand streams.
METADATA_PROBE_JSON_LIMIT = 1 * 1024 * 1024

#: The ``-loglevel info`` log a still decode writes, whose ``showinfo`` tap is the
#: evidence a decoded picture's timestamp is read from: measured at most 4 564 bytes
#: (two taps for a 0.29 s reading window).  A reading window's worth of ~230-byte
#: tap lines stays under 100 KB, so 4 MiB is over 40x the worst case.  If it were
#: ever reached the dropped count is reported and the timestamp is answered as
#: **not measured**: a tap list whose beginning was cut off must not be paired with a
#: picture.
DECODE_LOG_LIMIT = 4 * 1024 * 1024


class StillError(Exception):
    """The still frame or frame list could not be produced (reported, not guessed)."""


class StillCancelled(StillError):
    """The request was superseded or its worker stopped; nothing was produced.

    This is its own answer, not a decode failure: the child was stopped by the
    caller's own token, so there is nothing to report about the media.  Callers that
    catch :class:`StillError` generally still catch this (a probe that was cancelled
    must not escape a path that only handles failures), so anything that turns a
    :class:`StillError` into "not measured", a warning or a decoded-picture answer
    has to re-raise this type first.
    """


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
    #: asset-local source time the **plan** resolved for this frame - a prediction
    #: of where in the stream this output frame asks for, never an observation of
    #: what was decoded - or, for a source-only frame, the time that was
    #: **requested** of the stream
    source_pts: Fraction | None = None
    #: presentation time of the source picture actually shown, measured from this
    #: decode (FFmpeg's ``showinfo``) - never a number looked up from a nominal
    #: rate. For a source-only frame it is this picture's own timestamp, which is
    #: equal to ``source_pts`` when the request named a real frame and otherwise
    #: the first real frame at or after the request - reported as it was decoded,
    #: not as it was asked for. For a composition frame it is the stamp of the
    #: picture the chain put on the output, and it is reported **only** when that
    #: picture's identity survives the chain's own selection (see
    #: :func:`_shown_picture_stamp`); the plan's sampling time is *not* evidence
    #: about it, and is kept separately in ``source_pts`` as the prediction.
    #: ``None`` when the decode cannot associate this particular picture with a
    #: measured input stamp - then the picture is still the real decoded frame, but
    #: its source position is unknown and the interface has to say so.
    decoded_source_pts: Fraction | None = None
    #: True when the picture's position is placed on the container's own tick grid:
    #: for a composition frame, that the sampling grid landed on an exactly
    #: representable timestamp *and* this picture's position was established on it;
    #: for a source-only frame, that the picture's own stamp was read from the
    #: decode, verified on that grid and at or after the request (False: the
    #: position is not guaranteed).
    #: False means the container's time base could not be read or the measurement
    #: contradicted the rule: the picture is honest, its position is not guaranteed,
    #: and that difference is shown rather than hidden.
    grid_exact: bool = True
    #: True when this is a *source* frame the composition does not itself present
    #: (stepped by hand to choose a cut): ``frame_index``/``output_time`` label the
    #: output frame being inspected - whether or not the composition covers it -
    #: while the picture is the stream's real frame at ``decoded_source_pts``.
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


def _run(
    args: list[str],
    timeout: float,
    what: str,
    cancel: threading.Event | None = None,
    *,
    stdout_limit: int = WINDOW_PROBE_JSON_LIMIT,
) -> ProcessResult:
    """One probe child, owned by :func:`video_editor.media.process.run_process`.

    This replaced ``subprocess.run(capture_output=True)`` (TASK_17), which could not
    be cancelled, could not see a token, and kept however many megabytes the child
    answered.  Here the same argv runs on the runner, so:

    * the two pipes are drained on their own threads, so a child that writes more
      than a pipe-full of stderr cannot block and cannot hide the request;
    * ``cancel`` is read before launching and while waiting, independent of output,
      and a running child is terminated, reaped and reported as
      :class:`StillCancelled`;
    * stdout is kept **strictly** up to *stdout_limit*: a truncated JSON answer would
      be parsed as if it were a complete enumeration of a stream's pictures, so a
      probe past its limit is a named failure instead;
    * stderr is kept as a tail (where FFmpeg's own error line is) with what fell out
      counted, and the count is reported in the message.

    A non-zero exit is returned, not raised: each caller says what that means for its
    question.
    """
    try:
        return run_process(
            args,
            timeout=timeout,
            cancel=cancel,
            capture=CapturePolicy(
                stdout_limit=stdout_limit,
                stdout_keep=Keep.STRICT,
                stderr_limit=DECODE_LOG_LIMIT,
                stderr_keep=Keep.TAIL,
            ),
        )
    except ProcessCancelled as exc:
        raise StillCancelled(f"cannot {what}: {exc}") from exc
    except (ProcessTimeout, ProcessLaunchError, ProcessOverflow, ProcessError) as exc:
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
    path: Path, stream_index: int, ffprobe: str | None = None, timeout: float = 30,
    cancel: threading.Event | None = None,
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
        cancel,
        stdout_limit=METADATA_PROBE_JSON_LIMIT,
    )
    if proc.returncode != 0:
        raise StillError(f"cannot probe {path.name}: {proc.stderr_text.strip()[:300]}")
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
    path: Path, stream_index: int, ffprobe: str | None = None, timeout: float = 30,
    cancel: threading.Event | None = None,
) -> int:
    """Container stream index -> FFmpeg/ffprobe ``v:N`` position (measured rule)."""
    return stream_timing(path, stream_index, ffprobe, timeout, cancel).position


def source_frame_times(
    path: Path,
    stream_index: int,
    ffprobe: str | None = None,
    from_time: Fraction | None = None,
    to_time: Fraction | None = None,
    timeout: float = 60,
    cancel: threading.Event | None = None,
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
    # the metadata probe keeps its own 30 s deadline (only the token is new here)
    timing = stream_timing(path, stream_index, ffprobe, cancel=cancel)
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
    # A windowed read is bounded by the window it asked for; this one may be asked
    # without a window, so its answer is bounded by the stated ceiling below instead.
    proc = _run(
        args,
        timeout,
        f"read the frame timestamps of {path.name}",
        cancel,
        stdout_limit=PACKET_PROBE_JSON_LIMIT,
    )
    if proc.returncode != 0:
        raise StillError(
            f"cannot read frame timestamps of {path.name}: "
            f"{proc.stderr_text.strip()[:300]}"
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

#: A packet PTS is a seek hint, not proof that a picture exists (see
#: :func:`source_frame_times`). Where an answer has to be about the *pictures* a
#: file really holds - does this stream end where its header says it does? - the
#: bounded probe below decodes frames, takes integer ``best_effort_timestamp``
#: multiplied by the stream's exact ``time_base``, and reads the last frame's own
#: recorded ``duration`` rather than assuming a frame rate.


@dataclass(frozen=True)
class PictureTail:
    """What one bounded decode near a stream's end actually produced."""

    #: pictures the decode produced at all, and how many of them start inside the
    #: requested window (a probe seeks to a keyframe *before* the window, so the
    #: two differ, and the difference is how a stream that stops early is proven)
    decoded: int
    inside: int
    #: asset-local presentation time of the last picture the decode produced
    last_pts: Fraction | None
    #: that picture's own recorded duration, in asset-local seconds - never one
    #: divided by a nominal frame rate, which is a claim about a stream that may
    #: run at variable rate
    frame_duration: Fraction | None
    #: ``last_pts + frame_duration``: where the material found really ends. None
    #: when the container recorded no duration for that last picture, which leaves
    #: the end known only to within one picture
    end: Fraction | None


def tail_picture_extent(
    path: Path,
    stream_index: int,
    from_time: Fraction,
    to_time: Fraction,
    ffprobe: str | None = None,
    timeout: float = 60,
    cancel: threading.Event | None = None,
) -> PictureTail:
    """Decode a bounded window at the end of a video stream and say where it ends.

    Times are asset-local presentation seconds, the same axis the domain model
    and the render graph use; the probe reads ``[from_time, to_time)`` and stops,
    so a full-length title is never scanned. Frames the seek found before
    ``from_time`` are kept in the answer: a window that asked for the last two
    seconds and comes back with its last picture 1.8 s earlier *is* the evidence
    that the stream ends early, and throwing those frames away would turn a
    finding into a shrug.

    ``cancel`` is the caller's token: this read is a child of the render job, so
    a job that has been stopped must not sit inside one until its deadline. A
    cancelled read raises :class:`StillCancelled` - it is not a stream whose tail
    could not be measured, and must not be reported as one.
    """
    if to_time <= from_time:
        raise StillError(f"tail window [{float(from_time)}, {float(to_time)}) is empty")
    # the metadata probe keeps its own 30 s deadline (unchanged here: only the token
    # is new, no deadline value moves)
    timing = stream_timing(path, stream_index, ffprobe, cancel=cancel)
    tool = _ffprobe(ffprobe)
    proc = _run(
        [
            tool,
            "-v",
            "error",
            "-select_streams",
            f"v:{timing.position}",
            "-show_entries",
            "frame=best_effort_timestamp,pts,duration",
            "-of",
            "json",
            "-read_intervals",
            f"{decimal(from_time)}%{decimal(to_time)}",
            str(path),
        ],
        timeout,
        f"read the last pictures of {path.name}",
        cancel,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise StillError(f"unparseable ffprobe output for {path.name}: {exc}") from exc
    stamps: list[tuple[Fraction, object]] = []
    for frame in payload.get("frames", []):
        raw = frame.get("best_effort_timestamp", frame.get("pts"))
        if raw is None:
            continue
        try:
            ticks = int(raw)
        except (TypeError, ValueError):
            continue  # 'N/A': an unaddressable picture
        stamps.append((Fraction(ticks) * timing.time_base, frame.get("duration")))
    stamps.sort(key=lambda item: item[0])
    if not stamps:
        return PictureTail(decoded=0, inside=0, last_pts=None, frame_duration=None, end=None)
    last_pts, raw_duration = stamps[-1]
    duration: Fraction | None = None
    if raw_duration not in (None, "N/A"):
        try:
            duration = Fraction(int(raw_duration)) * timing.time_base
        except (TypeError, ValueError):
            duration = None
    inside = sum(1 for pts, _ in stamps if from_time <= pts < to_time)
    end = last_pts + duration if duration is not None and duration > 0 else None
    return PictureTail(
        decoded=len(stamps), inside=inside, last_pts=last_pts, frame_duration=duration, end=end
    )


#: The composition's rule ("show the last real picture at or before the sampling
#: time") needs the stream's real timestamps, which :mod:`video_editor.media.
#: frameindex` reads as bounded windows of *decoded* pictures
#: (:func:`video_editor.media.frameprobe.decoded_picture_times`), because a packet
#: is only a seek hint. This module's own bounded decode is
#: :func:`tail_picture_extent`, which additionally reads each frame's recorded
#: duration; :func:`source_frame_times` stays available as the packet-hint view that
#: the two can be cross-checked against.


# ------------------------------------------------- composition still-frame grab


def still_plan_for(
    project: Project,
    output_time: Fraction,
    geometry_scale: Fraction = ONE,
) -> tuple[RenderPlan, Fraction, int]:
    """One-frame plan for ``output_time`` plus its quantised time and index.

    Two different questions, and the planner names both helpers for them:

    * **What the playhead shows** is ``Quantisation.frame_at_playhead`` (floor):
      the frame whose picture is already covering ``output_time``. The render-range
      boundary (ceil) is deliberately *not* used here: it names the frame that
      *starts* at or after the playhead, so it would show a picture from up to one
      frame in the future, and one tick before the composition end it names a frame
      past the last one the export writes.
    * **Which frames that frame is** is the half-open range rule: the frame at
      global index ``i`` is exactly what the range ``[i/rate, (i+1)/rate)`` emits.
      So the plan is built over that frame's own lattice interval, which makes the
      still in the viewer the picture the exporter writes at index ``i`` — same
      range, same planner, no rounding agreed on by two modules.

    A playhead at or past the exact composition end has no frame to show and is
    refused: rendering black there would present a frame the export never writes at
    that index, and a viewer note cannot make that honest.
    """
    if output_time < 0:
        raise StillError("Output time must be non-negative")
    rate = project.output.frame_rate
    horizon = project.output_end()
    quant = Quantisation(rate, project.output.sample_rate)
    if output_time >= horizon:
        first_frame, last_frame = quant.render_range_frames(ZERO, horizon)
        last = quant.frame_time(max(first_frame, last_frame - 1))
        raise StillError(
            f"no output frame at {float(output_time):.6f}s: the composition ends at "
            f"{float(horizon):.6f}s; the last frame it has is at {float(last):.6f}s"
        )
    index = quant.frame_at_playhead(output_time)
    frame_time = quant.frame_time(index)
    if not frame_time <= output_time < frame_time + F(1) / rate:
        raise StillError(
            f"the frame {index} at {float(frame_time):.6f}s does not cover the playhead "
            f"{float(output_time):.6f}s; viewer and export would disagree"
        )
    plan = build_plan(project, frame_time, frame_time + F(1) / rate, geometry_scale)
    if plan.total_frames < 1:
        raise StillError(
            f"the planner produced no output frame for {float(output_time):.3f}s: "
            "nothing can be shown for that time"
        )
    first_frame, _one_past = plan.quantisation.render_range_frames(
        frame_time, frame_time + F(1) / rate
    )
    if first_frame != index or plan.total_frames != 1:
        raise StillError(
            f"the frame at {float(frame_time):.6f}s plans {plan.total_frames} frame(s) "
            f"from index {first_frame} instead of frame {index}; the still would not "
            "be the frame the export writes there"
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


def _shown_picture_stamp(
    stderr: str,
    clip: Clip,
    sampling: PictureSampling,
    container_base: Fraction | None,
    seek_text: str,
    file_seek: Fraction,
) -> Fraction | None:
    """The asset-local stamp of the picture this chain put on the output, or None.

    A ``showinfo`` tap upstream of the ``fps`` stage only proves which pictures the
    decoder handed over. It does **not** prove which one ``fps`` later chose, and the
    chain discards pictures (``select``) and relabels them (``settb``, ``setpts``)
    between the tap and that choice. So the pairing is made by carrying each arrival
    through the chain's own arithmetic, in whole ticks, exactly as
    :func:`~video_editor.render.graph.picture_filter_chain` writes it for this item:

    1. the tap's integer counters, multiplied by the time base that same tap
       reported, are the arrivals measured in the reading window, counted from the
       input seek - which is only usable when that seek is exactly what was asked
       (``seek_text`` re-reads as ``file_seek``) and lands on whole ticks of both the
       container's grid and the chain's fine ``tick``;
    2. ``settb=expr=1/<per_second>`` puts those arrivals on the plan's fine tick
       without changing their position, so an arrival's counter at the ``select``
       stage is ``offset / tick`` - a whole number, or the identity is lost here and
       nothing is reported;
    3. ``select='gte(pts,<lower>)*lt(pts,<upper>)'`` is the clip's half-open source
       slice written by :func:`~video_editor.render.graph.tick_number`; an arrival
       outside it never reaches the output, so it can never be the returned picture;
    4. the tempo stage is exact on that tick: :func:`~video_editor.render.plan.
       picture_tempo` proved the whole expression leaves no remainder, whether it is
       written factored (``setpts=PTS*<b>/<a>-<phase>``) or combined
       (``setpts=(PTS*<b>-<offset>)/<a>``). ``fps=round=up:fps=R:start_time=0`` answers
       the grid's first point - which that stage puts exactly at zero - with the kept
       arrival of greatest retimed counter, i.e. the greatest ``PTS`` with
       ``PTS*b <= offset`` (``offset`` is ``phase*a`` in the factored spelling, and the
       two are the same number): integer arithmetic, no rounding, no tolerance. A grid
       point before every kept arrival holds the first of them, which is the head hold
       the chain documents.
    5. that arrival is the first picture of the chain's output (``fps`` duplicates it
       and ``tpad=stop_mode=clone`` only clones further out), so its own stamp,
       ``seek_source + offset``, is the returned picture's timestamp.

    ``None`` - "not measured", never a near miss - whenever that chain of identity
    does not hold for this decode: the item has no exact ``tempo`` (the fallback chain
    *truncates* every stamp, so a stamp read upstream of ``fps`` belongs to the
    resampler's arithmetic rather than to a picture), the container's or the tap's time
    base is unknown, the seek text is not the seek, an arrival cannot be placed on whole
    container/fine ticks, or no arrival falls inside the slice the chain selects.
    """
    tempo = sampling.tempo
    if tempo is None or not sampling.anchored:
        return None  # the container-quantised fallback chain: identity is not carried
    if container_base is None or container_base <= 0:
        return None
    try:
        if Fraction(seek_text) != file_seek:
            return None  # the counters count from a point this call cannot name exactly
    except (ValueError, ZeroDivisionError):
        return None
    tap_base, counters = _showinfo_tap(stderr)
    if tap_base is None or tap_base <= 0 or not counters:
        return None
    tick = tempo.tick
    per_second = tempo.per_second
    b = tempo.scale  # the tempo's numerator is the divisor; see PictureTempo
    # the same bounds the chain writes, from the same numbers (graph.tick_number):
    # the slice is a statement about whole fine ticks, not about seconds
    upper = int(tick_number((clip.source_out - sampling.seek_source) * per_second))
    lower = int(tick_number((clip.source_in - sampling.seek_source) * per_second))
    kept: list[tuple[int, Fraction]] = []  # (fine-tick counter, offset from the seek)
    for counter in counters:
        if counter < 0:
            continue
        offset = counter * tap_base
        pts = offset / tick
        if pts.denominator != 1:
            return None  # not a whole number of fine ticks: select's bound is not it
        if (offset / container_base).denominator != 1:
            return None  # not a whole container tick: the stamp cannot be exact
        pts = int(pts)
        if pts >= upper or (lower > 0 and pts < lower):
            continue  # the chain discards it: it cannot be the picture shown
        kept.append((pts, offset))
    if not kept:
        return None
    threshold = tempo.offset  # PTS*b <= offset, in fine ticks: the grid's first point
    at_or_before = [o for pts, o in kept if pts * b <= threshold]
    # the chain's answer for the grid's first point: the last kept arrival at or
    # before it, or the first kept one when the grid asks before any picture
    return sampling.seek_source + (max(at_or_before) if at_or_before
                                   else min(o for _, o in kept))


def _picture_stream(
    project: Project, clip: Clip, asset_paths: dict[str, Path],
    asset_identities: dict[str, Identity] | None,
    cancel: threading.Event | None = None,
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
        position = stream_position_of(path, source.stream_index, cancel=cancel)
    return Path(path), source, position


def _run_picture_decode(
    args: list[str],
    timeout: float,
    frame_bytes: int,
    cancel: threading.Event | None = None,
) -> ProcessResult:
    """One still decode, owned by the runner, with stdout bounded to one picture.

    *frame_bytes* is :func:`rgb24_frame_bytes` of the geometry the chain was asked to
    draw — width and height already come from the plan or :func:`plan_geometry` and
    are already refused past :data:`MAX_STILL_PIXELS`, so the number of bytes this
    child may produce is known **before** it starts, and it is the capture limit.
    ``-frames:v 1`` asks for one picture; a child that answers with more than that
    picture's byte count is refused by name instead of being kept or truncated.

    stderr keeps the ``showinfo`` evidence the decoded timestamp is read from.  Its
    limit is :data:`DECODE_LOG_LIMIT`; the **dropped count** is reported, because a
    log cut short has lost its earliest tap lines and must not be paired with a
    picture as if it were complete.
    """
    try:
        return run_process(
            args,
            timeout=timeout,
            cancel=cancel,
            capture=CapturePolicy(
                stdout_limit=frame_bytes,
                stdout_keep=Keep.STRICT,
                stderr_limit=DECODE_LOG_LIMIT,
                stderr_keep=Keep.TAIL,
            ),
        )
    except ProcessCancelled as exc:
        raise StillCancelled(f"cannot decode the source frame: {exc}") from exc
    except (ProcessTimeout, ProcessLaunchError, ProcessOverflow, ProcessError) as exc:
        raise StillError(f"cannot decode the source frame: {exc}") from exc


def _window_pictures(
    path: Path, source: StreamSource, window: ReadWindow, timeout: float,
    cancel: threading.Event | None = None,
) -> tuple[Fraction, ...] | None:
    """Every real picture timestamp of a reading window, measured from the stream.

    These are the stamps an item's exact tempo is proved against: a stream whose
    container counts time more finely than its tempo can be divided on has an exact
    tick only because of where its pictures really sit
    (:func:`~video_editor.render.plan.picture_tempo`). ``None`` means "not measured" -
    a failed or unreadable probe degrades to the approximate chain and its report, it
    never becomes a guess.

    The read is the same one :class:`video_editor.media.frameindex.FrameIndex` trusts
    (decoded picture timestamps, not packet hints), asked for no more than the window
    the plan already decided to decode.

    A **cancelled** read is re-raised, not folded into "not measured": that answer
    would let a request the caller already stopped carry on building an approximate
    chain as if the stream had been asked and answered.
    """
    if not window.anchored:
        return None
    # local import: ``frameprobe`` builds on this module's own probe helpers, so
    # importing it at module scope would close a cycle
    from .frameprobe import decoded_picture_times

    try:
        read = decoded_picture_times(
            path, source.stream_index, window.seek_source,
            window.generous_source_end, timeout=timeout, cancel=cancel,
        )
    except StillCancelled:
        raise
    except (StillError, OSError, ValueError):
        return None  # reported as approximate, never as exact
    # an empty read is an answer: a window the stream has nothing in. Only a failed
    # probe is "not measured", so the empty tuple is passed on as it is.
    return read.stamps


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
    previous_frame: Fraction | None = None,
    cancel: threading.Event | None = None,
) -> tuple[bytes, Fraction | None, PictureSampling]:
    """One picture through the export path's own chain, plus what it really was.

    Returns the raw RGB24 frame, the asset-local presentation time of the source
    picture actually shown, and the sampling contract used. That time is
    ``None`` unless the returned picture's identity can be carried through the
    chain's own selection to a measured input stamp
    (:func:`_shown_picture_stamp`): the tap in front of the chain proves what was
    decoded, never what ``fps`` chose, so a stamp from upstream of ``fps`` is not
    reported as this picture's position.

    The chain, the seek and the reading bound all come from
    :func:`video_editor.render.graph.picture_filter_chain` and
    :func:`video_editor.render.plan.picture_sampling` - the same calls the exporter
    makes - because a still that sampled differently from an export would be a
    picture the project does not contain.

    ``cancel`` reaches every child this helper starts: the timing probe, the
    window's picture read (only for a stream that needs it) and the decode itself.
    None of them may answer "not measured" for a request that was stopped, and no
    picture is returned from a stopped decode.
    """
    rate = plan.quantisation.frame_rate
    base: Fraction | None = None
    try:
        base = stream_timing(path, source.stream_index, timeout=timeout,
                             cancel=cancel).time_base
    except StillCancelled:
        raise  # stopped, not unmeasurable: the decode below would refuse too
    except StillError:
        base = None  # the picture can still be decoded; exactness is reported below
    preroll = picture_preroll(clip, source, rate)
    window = picture_read_window(clip, item, rate, base, preroll, previous_frame)
    sampling = picture_sampling(
        clip,
        item,
        rate,
        time_base=base,
        preroll=preroll,
        first_frame=first_frame,
        previous_frame=previous_frame,
    )
    if sampling.anchored and sampling.tempo is None:
        # The rule that holds for *any* stamp found no tick. For a stream whose
        # container counts time far more finely than the tempo can be divided on that
        # is not the end of the road: the exact tick is a statement about the pictures
        # this window really holds, so read them. One bounded probe, and only for the
        # streams that need it - an ordinary stream is never asked.
        sampling = picture_sampling(
            clip,
            item,
            rate,
            time_base=base,
            preroll=preroll,
            first_frame=first_frame,
            previous_frame=previous_frame,
            window_pictures=_window_pictures(path, source, window, timeout, cancel),
        )
    try:
        chain = picture_filter_chain(plan, clip, item, sampling, "rgb24")
    except RenderConfigError as exc:
        # a chain that would write no picture at all is refused with its reason, in
        # the vocabulary a still-frame caller already handles
        raise StillError(str(exc)) from exc
    # input -ss is measured from the file's presentation start, not from 0
    file_seek = max(sampling.seek_source - source.input_start, ZERO)
    seek_text = decimal(file_seek)
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-ss",
        seek_text,
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
    expected = rgb24_frame_bytes(plan.width, plan.height)
    proc = _run_picture_decode(args, timeout, expected, cancel)
    if proc.returncode != 0 or len(proc.stdout) < expected:
        detail = proc.stderr_text.strip()[:300]
        raise StillError(
            f"cannot decode the frame of {path.name} at asset-local "
            f"{float(sampling.sample_start):.6f}s: "
            f"{detail or f'short read ({len(proc.stdout)} of {expected} bytes)'}"
        )
    # Not a prediction dressed up as a measurement: the stamps this decode's tap
    # saw are evidence about the pictures the decoder handed over, and only the
    # chain's own selection says which of them the returned picture is. Where that
    # association cannot be made, ``shown`` is None and the caller says so.
    # A log that reached its byte limit has lost its earliest tap lines, so the
    # counters no longer enumerate the arrivals this picture came from: that is
    # "not measured" too, never a pairing from a cut-short list.
    shown = None
    if not proc.stderr_dropped:
        shown = _shown_picture_stamp(proc.stderr_text, clip, sampling, base, seek_text,
                                     file_seek)
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
    previous_frame: Fraction | None = None,
    cancel: threading.Event | None = None,
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
    *previous_frame* is the last readable picture at or before the frame being
    shown, from the same index: where it is known the decode starts there rather
    than a nominal preroll earlier, which is what keeps a still near the end of a
    long title reading a handful of frames. It cannot change which picture is
    shown - the chain's grid does that - only how much is decoded to get it.

    ``ffprobe`` is used by the capability preflight this still goes through; the
    probes below resolve the tool themselves. It stays in the signature because
    callers pass it through to every media tool.

    ``cancel`` is this request's token: it reaches the timing probe, the window's
    picture read and the decode, and a token that is set stops the child that is
    running rather than returning a picture nobody asked for. The capability
    preflight's own probe is **not** on that token yet — it is still
    ``media/capability.py``'s own ``subprocess.run`` — so a cancellation here lands
    before the decode, not inside that preflight.
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
    # This frame is the composition's own media, so it goes through the same
    # capability preflight as export and preview: a still of picture this renderer
    # cannot draw is refused with its reason and its offending value. A black frame
    # reads no source at all, which is why the check sits after that early return.
    try:
        check_picture_capabilities(project, [plan], asset_paths, ffprobe)
    except PictureCapabilityError as exc:
        raise StillError(str(exc)) from exc
    clip = item.clip
    assert clip is not None
    path, source, position = _picture_stream(project, clip, asset_paths, asset_identities,
                                             cancel)
    rgb, shown, sampling = _decode_picture(
        encode, path, source, position, clip, plan, item, timeout, first_frame,
        previous_frame, cancel,
    )
    return StillFrame(
        width=plan.width,
        height=plan.height,
        rgb=rgb,
        output_time=frame_time,
        frame_index=index,
        black=False,
        # the plan's own sampling time: a prediction about the stream, kept so the
        # interface can show it *next to* a measured stamp, never instead of one
        source_pts=item.source_start,
        decoded_source_pts=shown,
        # "the grid was placed exactly" and "this picture's place on that grid was
        # established" are one answer here: an unmeasured position is never exact
        grid_exact=sampling.anchored and shown is not None,
        clip_id=clip.id,
        asset_id=clip.asset_id,
        stream_index=clip.stream_index,
    )


# ---------------------------------------------------- source picture decode
#
# What "inspect the source stream" must mean, from the reviewer's decision:
# the stream's decoded picture is authoritative, independently of clip rate,
# output frame rate, the composition horizon and the clip's current in/out. A
# ``showinfo`` tap placed before an ``fps`` stage, followed by a lookup of which
# picture the resampler *would have* chosen, is evidence about the resampler, not
# about a picture. So this path runs no filter that discards or reorders pictures
# at all: accurate input seek, one decoded picture, its own stamp read from that
# decode, then only geometry (fit → square pixels → pad).
#
# Measured on FFmpeg 8.1.3 (Matroska/FFV1 and Matroska/H.264 with whole-asset
# offsets, MP4/H.264 with real B-frames at ``time_base`` 1/12800, and a VFR
# Matroska whose slow section sits on 1/1000 ticks):
#
# * an input ``-ss`` seeks on the file's own content axis: a picture whose
#   asset-local presentation time is ``input_start + s`` is found with ``-ss s``,
#   both for a whole-asset offset (Matroska start 2.5) and for plain files;
# * the *reconstruction* route is a trap: without ``-copyts`` the tap stamps are
#   relative to a rebase point FFmpeg reaches by rounding the seek text through
#   microseconds and then the stream grid, and the containers do not round the
#   same way - a 9-decimal seek text landing a hair over half a tick below a
#   frame timestamp stamped that frame one tick late on the MP4 while stamping
#   it zero on the Matroska. Nothing downstream can tell; the stamps are only
#   trusted when they are the picture's own numbers, so
# * the decode runs with ``-copyts``, measured to leave every kept picture's
#   ``showinfo`` stamp equal to its true container timestamp on the asset-local
#   axis (offset Matroska: seeking 0.5195 s into a file whose presentation
#   starts at 2.5 s stamps the frame at 151/50 s; MP4 last frame: 99/25; VFR
#   last frame: 8083/1000) and to change no frame selection at all;
# * the keep rule is quantised to the stream grid, not the decimal text, and a
#   picture at exactly the quantised point is *kept*: seeking 10 µs past a
#   picture's own timestamp still returns that picture, on both containers.
# * ``-frames:v 1`` bounds the *output* to one picture, but the filtergraph may
#   still be fed more than one (measured: two ``showinfo`` lines): the bytes
#   returned are the first chain picture, which is the first stamped one, and the
#   output length is checked to be exactly one frame.

#: How far above the wanted grid point the seek is placed. No picture can sit
#: between the request and the first container-grid point at or above it, so the
#: wanted picture is the first one the seek may keep - and placing the point
#: *above* it (FFmpeg quantises the seek text to the stream grid before it
#: discards, and that rounding can land below an exact-text seek) is what keeps
#: a picture one grid step *before* an in-between request from surviving. The cap
#: keeps the offset far smaller than any real frame spacing; ``time_base / 4``
#: keeps it below a quarter tick for a container with a very coarse grid, so the
#: quantised seek point is the wanted one itself.
SEEK_AHEAD_MAX = F(1, 100000)


def _showinfo_tap(stderr: str) -> tuple[Fraction | None, list[int]]:
    """What the source tap saw: the tick size pictures arrive on, and their
    integer ``pts`` counters in the order the filtergraph received them.

    ``config in time_base`` is read from the tap's own output and the caller
    multiplies *that* base, so a decode whose filter base differs from the probe's
    is noticed below (its stamp fails the container-grid check) instead of being
    silently rescaled.
    """
    base: Fraction | None = None
    for line in stderr.splitlines():
        if "config in time_base:" in line:
            token = line.split("config in time_base:", 1)[1].split(",")[0].strip()
            base = _to_rational(token) or base
    stamps: list[int] = []
    for line in stderr.splitlines():
        if "showinfo" not in line:
            continue
        match = _PTS_TICK.search(line)
        if match is not None:
            stamps.append(int(match.group(1)))
    return base, stamps


def _source_picture_decode(
    ffmpeg: str,
    path: Path,
    position: int,
    seek_text: str,
    width: int,
    height: int,
    timeout: float,
    cancel: threading.Event | None = None,
) -> tuple[bytes, str, int]:
    """One picture of one stream, exactly as the decoder produced it.

    ``-copyts`` keeps the decoded pictures' own timestamps (the stamps are then
    the frames' true asset-local times - see the section comment above), and the
    chain is ``showinfo`` first, then only the geometry stages the composition
    uses (``picture_scale_fit`` → ``setsar=1`` → pad → format): those transform a
    picture's shape and never choose a different one, so the single output frame
    is the frame the tap stamped - and the caller checks the byte count says
    exactly one frame.

    Returns the raw bytes, the decode's log, and **how many bytes of that log fell
    out of the capture**. The caller must not read a timestamp from a log whose
    earliest ``showinfo`` lines are missing, so the count travels with it rather
    than being quietly dropped here.
    """
    chain = ",".join(
        [
            "showinfo",
            picture_scale_fit(width, height),
            "setsar=1",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
            "format=rgb24",
        ]
    )
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-ss",
        seek_text,
        "-copyts",
        "-i",
        str(path),
        "-map",
        f"0:v:{position}",
        "-frames:v",
        "1",
        "-vf",
        chain,
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    proc = _run_picture_decode(args, timeout, rgb24_frame_bytes(width, height), cancel)
    if proc.returncode != 0:
        detail = proc.stderr_text.strip()[:300]
        raise StillError(
            f"cannot decode a source picture of {path.name}: {detail or 'no detail'}"
        )
    return proc.stdout, proc.stderr_text, proc.stderr_dropped


def _measured_source_pts(
    stderr: str, requested: Fraction, container_base: Fraction
) -> Fraction | None:
    """The decoded picture's own asset-local timestamp, or None when unprovable.

    With ``-copyts`` the first ``showinfo`` stamp, multiplied by the time base
    that same tap reported, **is** the picture's container timestamp - the same
    asset-local axis the domain model uses (measured for whole-asset and
    per-stream offsets alike; ``frameprobe`` established that packet stamps
    already sit on it). It must land on the container's tick grid - within half
    a tick, which is all a time-base disagreement could be - and at or after the
    request, because a picture the seek placed above the request kept cannot
    start before it. A stamp the tap reports before the file's own zero is not
    placeable on the asset axis by this rule either. Anything that contradicts
    the measurement means the position is reported as unknown rather than
    answered with whatever arithmetic happened to produce.
    """
    base, stamps = _showinfo_tap(stderr)
    if base is None or base <= 0 or not stamps or stamps[0] < 0:
        return None
    candidate = stamps[0] * base
    if candidate < requested:
        return None
    return _snap_to_ticks(candidate, container_base)


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
    cancel: threading.Event | None = None,
) -> StillFrame:
    """Decode one *source* picture of a clip's stream, at ``source_pts``.

    This is what stepping through a source frame by frame means: the picture is
    the stream's real decoded frame at or after ``source_pts`` asset-local - never
    a picture the composition would show, never one inferred through the clip's
    tempo or the output sampling grid. A request naming a real frame gets that
    frame (timestamp and picture measured from the same decode); a request between
    two frames gets the next real one, and the reported PTS is the frame that
    actually decoded, not the one asked for.

    The clip's own in/out do **not** limit the step: cutting means moving those
    edges, so the readable range is the stream's. The stream's recorded bounds do
    limit it, because material the stream does not have cannot be decoded and a
    viewer that filled the gap with black would be showing a frame that does not
    exist. Inside the bounds but past the last decodable picture, the honest
    answer is that the decode produced nothing.

    ``output_time`` only labels the answer: ``frame_index``/``output_time`` name
    the output frame the caller is inspecting (the playhead rule, floor), whether
    or not the composition reaches that far; the picture does not depend on it.
    *geometry_scale* and the composition's output size fix the display geometry
    (the same display-ratio fit the export applies). *first_frame* is unused by
    this path - the head-preroll question belongs to the composition's sampling
    grid, which a direct source decode has none of - and stays in the signature
    because the stepping worker passes it. *asset_identities* keeps the asset
    check: a file replaced at the same path is refused, not decoded.

    ``cancel`` is the stepping request's token: the timing probe and the decode are
    both stopped by it, and a stopped request raises :class:`StillCancelled` with no
    picture, rather than reporting a decode failure for a request the caller ended.
    """
    encode = ffmpeg or shutil.which("ffmpeg")
    if encode is None:
        raise StillError("ffmpeg is not available: still frames cannot be decoded")
    del ffprobe, first_frame  # probes resolve their tool; the grid question is gone
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
    if output_time < 0:
        raise StillError("Output time must be non-negative")
    # geometry only - the composition is not asked which picture this frame shows
    width, height, _warning = plan_geometry(
        project.output.width, project.output.height, geometry_scale
    )
    if width * height > MAX_STILL_PIXELS:
        raise StillError(
            f"still frame would be {width}x{height}: refusing to allocate "
            "an oversized picture (lower the preview size)"
        )
    path, stream, position = _picture_stream(project, clip, asset_paths, asset_identities,
                                             cancel)
    timing = stream_timing(path, stream.stream_index, timeout=timeout, cancel=cancel)
    tick = timing.time_base
    # No picture of the stream can sit between the request and the next point on
    # the container's grid, so a seek placed just above that grid point - FFmpeg
    # quantises the seek text down to the grid before it discards, and a picture
    # at exactly the quantised point is kept - leaves exactly the picture the
    # request asks about, or the next real frame, which is reported as such.
    file_wanted = ceil_to_time_base(source_pts, tick) - stream.input_start
    if file_wanted < ZERO:  # recorded origin and container start contradict each other
        raise StillError(
            f"{float(source_pts):.6f}s of asset {clip.asset_id} is before this file's "
            f"presentation start ({float(stream.input_start):.6f}s): its frames cannot "
            "be addressed"
        )
    seek = file_wanted + min(SEEK_AHEAD_MAX, tick / 4)
    seek_text = decimal(seek)
    rgb, stderr, log_dropped = _source_picture_decode(
        encode, path, position, seek_text, width, height, timeout, cancel)
    expected = rgb24_frame_bytes(width, height)
    if len(rgb) == 0:
        raise StillError(
            f"stream {clip.stream_index} of asset {clip.asset_id} holds no picture at "
            f"or after {float(source_pts):.6f}s asset-local: the decodable material "
            "ends before this point inside the recorded bounds"
        )
    if len(rgb) != expected:
        raise StillError(
            f"the source decode of {path.name} returned {len(rgb)} bytes where exactly "
            f"one {width}x{height} RGB picture needs {expected}: that picture cannot be "
            "trusted as one frame"
        )
    # The tap's *first* stamp is the picture this decode returned. A log that reached
    # its capture limit has lost that line, so the position is reported as unknown
    # rather than read from whichever line survived at the end of the log.
    measured = None if log_dropped else _measured_source_pts(stderr, source_pts, tick)
    quant = Quantisation(project.output.frame_rate, project.output.sample_rate)
    index = quant.frame_at_playhead(output_time)
    return StillFrame(
        width=width,
        height=height,
        rgb=rgb,
        output_time=quant.frame_time(index),
        frame_index=index,
        black=False,
        source_pts=source_pts,
        decoded_source_pts=measured,
        grid_exact=measured is not None,
        source_only=True,
        clip_id=clip.id,
        asset_id=clip.asset_id,
        stream_index=clip.stream_index,
    )

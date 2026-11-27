"""``ffprobe`` JSON adapter.

Runs ffprobe with a subprocess argument list (never a shell string), parses
the JSON defensively, and converts the typed model in
``video_editor.media.assets``. Never raises for bad media: callers receive
``ProbeResult.error`` and may still display the file.

Since TASK_14 the child is owned by :func:`video_editor.media.process.run_process`:
an argv list, the stated :data:`PROBE_TIMEOUT_SECONDS` deadline, bounded capture
(``Keep.STRICT`` on the JSON, so an over-long answer is refused by name instead of
parsed from truncated bytes) and an optional caller-supplied cancellation
:class:`threading.Event` that reaches the process itself. Cancelling never yields
partial metadata: a cancelled probe answers with ``metadata=None`` and
:data:`CANCELLED_MESSAGE`. Nothing else about a probe - the options, the parsing,
the bounds - changes with that.

A duration that is not the stream's own is only ever an upper bound: each
container family's ``duration`` field is read with the convention that family
was measured to use (see ``ABSOLUTE_DURATION_FORMATS``), and the result stays
marked conservative so the render path still has to prove the tail.

Picture *format* facts (``field_order``, the three colour tags, the pixel format
and sample depth, and the names of any side-data records) are recorded with them
because the application has to be able to say what it cannot draw — interlaced
or HDR picture — before it renders wrong media. They are stored verbatim: a
container that says ``unspecified``, says ``unknown``, or says nothing at all is
recorded as unknown, never as a default (see ``UNKNOWN_TOKENS``). Recording them
here does not make a file renderable; the refusal decision is made where the
render happens, from the file that is about to be decoded
(:mod:`video_editor.media.capability`).
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from .assets import AssetMetadata, StreamKind, StreamMetadata
from .process import (
    DEFAULT_CAPTURE_LIMIT,
    CapturePolicy,
    Keep,
    ProcessCancelled,
    ProcessError,
    ProcessLaunchError,
    ProcessOverflow,
    ProcessTimeout,
    run_process,
)

PROBE_TIMEOUT_SECONDS = 120

#: The one error string that means "this probe was cancelled", shared with
#: :mod:`video_editor.media.importer`, which reports it as the job's outcome.
CANCELLED_MESSAGE = "cancelled"

#: Ceiling on the JSON answer.  Measured: 5 501 bytes for the three-stream
#: ``mixed.mkv`` fixture this project builds, about 1.8 kB per stream, so a real
#: film with a dozen streams is a few tens of kilobytes.  Four megabytes is
#: therefore far above anything a normal container answers, while still being a
#: number: past it the probe is **refused by name** rather than parsed from
#: truncated bytes (``Keep.STRICT``), because half an ffprobe answer would be
#: reported as metadata this application then trusts.
PROBE_JSON_LIMIT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ProbeResult:
    path: Path
    metadata: AssetMetadata | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.metadata is not None and self.error is None


def _to_fraction(value: Any) -> Fraction | None:
    """Parse ffprobe rationals/decimals exactly. 'N/A', '' and junk -> None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        # ffprobe emits strings for rationals; floats only appear from odd
        # backends. Decimal representation keeps the value exact as printed.
        return Fraction(str(value))
    text = str(value).strip()
    if not text or text in {"N/A", "none", "0/0", "inf", "-inf"}:
        return None
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_timecode(value: Any) -> Fraction | None:
    """Matroska's ``DURATION`` tag, printed as ``HH:MM:SS.nnnnnnnnn``.

    Exact: the parts are whole seconds and an exact decimal fraction, so this is
    rational arithmetic (``00:00:06.500000000`` -> ``13/2``), never a float.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.upper() in {"N/A", "UNKNOWN"}:
        return None
    total: Fraction | None = None
    for part in text.split(":"):
        piece = _to_fraction(part)
        if piece is None or piece < 0:
            return None
        total = piece if total is None else total * 60 + piece
    return total


#: How each measured container family means ``duration``.
#:
#: FFmpeg does not answer this the same way everywhere, and the difference is
#: observable, so it is recorded rather than assumed (measured on FFmpeg 8.1.3;
#: the raw JSON and the decoded frame lists are in the task record):
#:
#: * ``matroska``/``webm`` - the Duration element, both container-level and in the
#:   per-track ``DURATION`` tag, is an **end on the asset-local presentation
#:   axis**, not a span from the stream's own start. The 100-frame 25 fps
#:   Matroska written with ``-output_ts_offset 2.5`` reports ``start_time`` 2.5,
#:   ``duration`` 6.5 and a track tag ``00:00:06.500000000``, and decodes 100
#:   pictures from 5/2 to 323/50: the material ends at 13/2 = 6.5 s. Reading that
#:   same 6.5 as a span (``2.5 + 6.5 = 9``) claims 2.5 s of picture the file does
#:   not hold. Measured on five Matroska files, including one whose picture and
#:   sound start 1.5 s apart on one timeline: every per-track tag equalled or
#:   exceeded the last decoded picture plus that picture's own duration, so it is a
#:   tight upper bound, not an exact end - a file truncated by cutting its tail
#:   bytes keeps a tag that overstates its content by 1.84 s.
#: * ``mov``/``mp4``/ friends - ``duration`` is a **span from the stream's own
#:   start**, and the stream carries it exactly. The same content remuxed with
#:   ``-itsoffset 2.5`` reports stream ``start_time`` 2.5 and ``duration`` 4.0 and
#:   decodes 100 pictures from 5/2 to 323/50, ending at 13/2, so ``start +
#:   duration`` is right there and any universal subtraction would cut 2.5 s of
#:   real material.
#:
#: Families that were not measured keep the span reading: it is the looser, never
#: tighter bound. Everything derived from these fields stays marked conservative,
#: so a render still has to prove the tail before it publishes (see
#: :meth:`video_editor.render.exporter.RenderRequest.check_picture_tails`).
ABSOLUTE_DURATION_FORMATS = frozenset({"matroska", "webm"})


def _uses_absolute_duration(format_name: str | None) -> bool:
    """True for the families whose Duration element names an absolute end."""
    if not format_name:
        return False
    names = {part.strip() for part in str(format_name).split(",")}
    return bool(names & ABSOLUTE_DURATION_FORMATS)


def _stream_end_bound(
    raw: dict[str, Any], origin: Fraction, container_end: Fraction | None, absolute: bool
) -> Fraction | None:
    """Tightest defensible **upper bound** on one stream's end, or None.

    Never an exact end: a container's Duration is a claim the muxer wrote, and a
    damaged file keeps its claim while losing its data. The caller keeps the
    conservative status, and the render path measures before it publishes.
    """
    candidates: list[Fraction] = []
    if container_end is not None:
        candidates.append(container_end)
    if absolute:
        # measured: the per-track tag is absolute too, and it is per-stream, so it
        # is tighter when streams end at different times (a 6.5 s picture over a
        # 5.0 s commentary track should not inherit the picture's bound)
        tags = raw.get("tags") or {}
        tag = _to_timecode(tags.get("DURATION"))
        if tag is not None:
            candidates.append(tag)
    usable = [value for value in candidates if value > origin]
    return min(usable) if usable else None


def _language(raw: dict[str, Any]) -> str | None:
    tags = raw.get("tags") or {}
    lang = tags.get("language") or raw.get("language")
    if isinstance(lang, str) and lang.strip() and lang.strip().lower() not in {"und", "unknown"}:
        return lang.strip()
    return None


#: Tokens that mean "the container has no value here", measured on FFmpeg 8.1.3:
#: an H.264 stream whose VUI says *unspecified* simply omits ``color_transfer``
#: and ``color_primaries`` from ``-show_streams``, while a per-frame read prints
#: the word ``unknown``. Both are recorded as unknown, never as a default.
UNKNOWN_TOKENS = frozenset(
    {"", "unknown", "unspecified", "n/a", "na", "none", "null", "not set", "-"}
)


def _token(value: Any) -> str | None:
    """A verbatim container token, lower-cased, or ``None`` when it says nothing.

    Never guesses: ``"unspecified"`` is a claim that nothing was stated, so it is
    not kept as if it were a colour value, and an absent key is the same thing.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().lower()
    if text in UNKNOWN_TOKENS:
        return None
    return text


def _side_data_names(raw: dict[str, Any]) -> tuple[str, ...]:
    """The side-data record names this stream reported, in the order given.

    Only the names are kept: their payloads are codec internals this application
    does not interpret, and a payload is far too large to store in a project.
    """
    entries = raw.get("side_data_list")
    if not isinstance(entries, list):
        return ()
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("side_data_type")
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    return tuple(names)


#: ``pix_fmt`` names carry the sample depth in a marked suffix (``yuv420p10le``,
#: ``gbrp12be``, ``gray10le``). Measured against ``bits_per_raw_sample`` on the
#: 10-bit fixtures built for TASK_12l: both say 10 for the same stream, so the
#: name is used only where the container states no number. An *unmarked* name is
#: left unknown rather than called 8-bit, because names like ``nv12`` carry
#: digits that are chroma subsampling, not depth.
_PIX_FMT_DEPTH = re.compile(r"(?:p|planar|gray)(\d{2})(?:le|be)?\Z")


def bit_depth_of(pix_fmt: str | None, stated: Any) -> int | None:
    """Bits per sample as the container states it, or as its format name says.

    Public because the render-time capability check reads the same two fields out
    of its own probe and must not reach into this module's internals.
    """
    stated_depth = _to_int(stated)
    if stated_depth is not None and stated_depth > 0:
        return stated_depth
    if not pix_fmt:
        return None
    match = _PIX_FMT_DEPTH.search(pix_fmt)
    if match is None:
        return None
    return int(match.group(1))


def parse_probe_document(document: Any) -> AssetMetadata:
    """Convert a parsed ffprobe JSON document to ``AssetMetadata``.

    Raises ``ValueError`` when the document is structurally unusable (not a
    dict, streams missing). Individual stream problems skip or downgrade that
    stream instead of failing the whole asset.
    """
    if not isinstance(document, dict):
        raise ValueError("ffprobe output is not a JSON object")
    raw_streams = document.get("streams")
    if not isinstance(raw_streams, list):
        raise ValueError("ffprobe output has no 'streams' list")

    fmt = document.get("format") or {}
    container_duration = _to_fraction(fmt.get("duration"))
    container_start = _to_fraction(fmt.get("start_time"))
    format_name = fmt.get("format_name") or None
    absolute_duration_family = _uses_absolute_duration(format_name)
    # Conservative horizon: no packet can present after the container says it
    # ends. Which field that is depends on the family (see
    # ``ABSOLUTE_DURATION_FORMATS``): Matroska's Duration is already an end on the
    # asset-local axis, so adding ``start_time`` to it would invent material.
    container_end: Fraction | None = None
    if container_duration is not None and container_duration > 0:
        container_end = (
            container_duration
            if absolute_duration_family
            else (container_start or Fraction(0)) + container_duration
        )

    streams: list[StreamMetadata] = []
    for raw in raw_streams:
        if not isinstance(raw, dict):
            continue
        index = _to_int(raw.get("index"))
        if index is None:
            continue
        codec_type = (raw.get("codec_type") or "").lower()
        kind = {
            "video": StreamKind.VIDEO,
            "audio": StreamKind.AUDIO,
        }.get(codec_type, StreamKind.OTHER)

        start = _to_fraction(raw.get("start_time"))
        duration = _to_fraction(raw.get("duration"))
        duration_source = "probe" if duration is not None else None
        origin = start if start is not None else Fraction(0)

        time_base = _to_fraction(raw.get("time_base"))
        nb_frames = _to_int(raw.get("nb_frames"))
        frame_rate = _to_fraction(raw.get("avg_frame_rate"))
        nominal = _to_fraction(raw.get("r_frame_rate"))

        sample_rate = _to_int(raw.get("sample_rate"))
        nb_samples = _to_int(raw.get("nb_samples"))
        pix_fmt = _token(raw.get("pix_fmt"))

        if duration is None:
            # Derive only from reliable counts, never from the container.
            if kind is StreamKind.VIDEO and nb_frames and frame_rate and frame_rate > 0:
                duration = Fraction(nb_frames) / frame_rate
                duration_source = "derived:nb_frames/rate"
            elif kind is StreamKind.AUDIO and nb_samples and sample_rate:
                duration = Fraction(nb_samples, sample_rate)
                duration_source = "derived:nb_samples/rate"

        streams.append(
            StreamMetadata(
                stream_index=index,
                kind=kind,
                codec=raw.get("codec_name"),
                language=_language(raw),
                frame_rate=frame_rate if frame_rate and frame_rate > 0 else None,
                nominal_frame_rate=nominal if nominal and nominal > 0 else None,
                width=_to_int(raw.get("width")),
                height=_to_int(raw.get("height")),
                sample_rate=sample_rate,
                channels=_to_int(raw.get("channels")),
                channel_layout=raw.get("channel_layout"),
                start_time=start,
                duration=duration,
                time_base=time_base,
                nb_frames=nb_frames,
                duration_source=duration_source,
                end_horizon=_stream_end_bound(
                    raw, origin, container_end, absolute_duration_family
                ),
                # how the picture is made, exactly as the container says it:
                # nothing here is inferred, and an absent or "unspecified" value
                # stays unknown (see ``_token``)
                field_order=_token(raw.get("field_order")),
                color_transfer=_token(raw.get("color_transfer")),
                color_primaries=_token(raw.get("color_primaries")),
                color_space=_token(raw.get("color_space")),
                pix_fmt=pix_fmt,
                side_data=_side_data_names(raw),
                bit_depth=bit_depth_of(pix_fmt, raw.get("bits_per_raw_sample")),
            )
        )

    if not streams:
        raise ValueError("ffprobe found no streams")

    return AssetMetadata(
        format_name=format_name,
        container_duration=container_duration,
        container_start=container_start,
        streams=tuple(streams),
    )


def parse_probe_output(stdout: str) -> ProbeResult | None:
    """Parse raw ffprobe stdout. Returns None when JSON is malformed."""
    try:
        document = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        metadata = parse_probe_document(document)
    except ValueError as exc:
        return ProbeResult(path=Path(), metadata=None, error=f"Unusable probe data: {exc}")
    return ProbeResult(path=Path(), metadata=metadata, error=None)


def build_probe_command(ffprobe_path: str, path: Path) -> list[str]:
    return [
        ffprobe_path,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]


def probe_file(
    path: Path,
    ffprobe_path: str = "ffprobe",
    *,
    cancel: threading.Event | None = None,
) -> ProbeResult:
    """Probe one file. Always returns a result; never raises for bad media.

    ``cancel`` is the caller's job-level token, handed straight to the subprocess
    runner: setting it from another thread ends the child (terminate, bounded
    grace, kill, reap) and answers with :data:`CANCELLED_MESSAGE` and no metadata.
    A probe that was cancelled, timed out or over the JSON limit never reports the
    bytes it had reached as metadata.
    """
    import shutil

    exe = shutil.which(ffprobe_path)
    if exe is None:
        return ProbeResult(path=path, metadata=None, error=f"{ffprobe_path} not found on PATH")
    cmd = build_probe_command(exe, path)
    capture = CapturePolicy(
        stdout_limit=PROBE_JSON_LIMIT_BYTES,
        stdout_keep=Keep.STRICT,  # a truncated JSON answer would be a lie
        stderr_limit=DEFAULT_CAPTURE_LIMIT,
        stderr_keep=Keep.TAIL,  # the end is where ffprobe's failure message lives
    )
    try:
        completed = run_process(cmd, timeout=PROBE_TIMEOUT_SECONDS, cancel=cancel, capture=capture)
    except ProcessCancelled:
        return ProbeResult(path=path, metadata=None, error=CANCELLED_MESSAGE)
    except ProcessTimeout:
        return ProbeResult(path=path, metadata=None, error="ffprobe timed out")
    except ProcessOverflow as exc:
        return ProbeResult(
            path=path,
            metadata=None,
            error=f"ffprobe answer too large: {exc.total} bytes on {exc.stream}",
        )
    except ProcessLaunchError as exc:
        return ProbeResult(path=path, metadata=None, error=f"Could not run ffprobe: {exc}")
    except ProcessError as exc:  # unreaped child, cut-short capture: never a partial probe
        return ProbeResult(path=path, metadata=None, error=f"Could not run ffprobe: {exc}")
    if completed.returncode != 0:
        detail = completed.stderr_text.strip().splitlines()
        hint = detail[-1] if detail else f"exit status {completed.returncode}"
        return ProbeResult(path=path, metadata=None, error=f"ffprobe failed: {hint}")
    parsed = parse_probe_output(completed.stdout_text)
    if parsed is None:
        return ProbeResult(path=path, metadata=None, error="ffprobe returned malformed JSON")
    return ProbeResult(path=path, metadata=parsed.metadata, error=parsed.error)

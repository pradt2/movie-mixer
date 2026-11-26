"""What this build can actually draw, measured from the file it will decode.

The renderer ends every picture path at the delivery pixel format
(``render.graph.picture_filter_chain`` → ``format=yuv420p`` for MP4/H.264/AAC) and
performs **no** colour-space, transfer or field-order conversion. Two kinds of
source picture therefore cannot be rendered honestly here:

* **interlaced** picture, because there is no deinterlacer: the two fields of one
  source frame would be shown in separate output frames;
* **HDR** picture, because there is no tone mapper: PQ or HLG code values would be
  written straight into an SDR ``yuv420p`` frame and look wrong.

This module says so before the media is produced, not after. It refuses a
*participating* picture stream (one whose picture this render range really shows)
when the file *states* one of those formats, and warns when the file does not state
enough to know — an unknown value is never taken as proof of progressive SDR. It
never attempts a conversion, and it never treats 10-bit depth as if it proved HDR.

What was measured (FFmpeg 8.1.3; raw JSON in the task record)
------------------------------------------------------------
* The container tag is not enough on its own, and both directions of the
  disagreement are real:

  - MPEG-2 written with ``-flags +ildct+ilme -top 1`` into MPEG-PS or MPEG-TS
    reports ``field_order: "tt"``/``"bb"`` on the stream **and**
    ``interlaced_frame: 1, top_field_first: 1`` on every frame;
  - H.264 written with ``-x264-params tff=1`` reports **``field_order:
    "progressive"``** on the stream while every decoded frame reports
    ``interlaced_frame: 1``. Field-coded H.264 in Matroska/MP4 is the common
    real-world shape (broadcast rips), so a rule on the stream tag alone would wave
    it through — which is why the assessment also reads the picture structure of
    decoded frames;
  - the opposite case: ``-field_order tt`` as a *muxer* option on an ordinary
    H.264 encode in Matroska does not survive at all — that file probes as
    ``progressive`` in the stream **and** in its frames. A rule may be built only on
    what the file reports, never on what was asked of the muxer.
* Colour tags reach ``ffprobe`` only when they are written into the bitstream.
  ``-color_trc smpte2084`` on the command line leaves ``color_transfer`` absent from
  ``-show_streams`` (measured on Matroska, MP4 and MPEG-TS), while the same value
  written by ``-bsf:v h264_metadata=transfer_characteristics=16`` is printed as
  ``"smpte2084"``, and HLG is printed as ``"arib-std-b67"`` — hyphenated, where
  FFmpeg's own option name is ``arib_std_b67``. Matching therefore ignores
  punctuation, never the value.
* A value that is not stated is stated in more than one way: an H.264 VUI of
  *unspecified* omits ``color_transfer`` and ``color_primaries`` entirely, while a
  frame-level read of the same stream prints the word ``unknown``. Both mean
  unknown.
* ``x265`` given ``master-display``/``max-cll`` writes a named ``"Content light
  level metadata"`` record on frames, but its mastering-display SEI comes back as an
  unregistered user-data SEI rather than a named ``"Mastering display metadata"``
  record, so mastering metadata written that way is detectable here only through the
  MaxCLL record. Parsing SEI payloads is out of scope. DOVI and HDR10+ metadata
  cannot be authored by this FFmpeg at all (``-dovi_profile`` is not an x265 option
  here, and ``dhdr10-info`` aborts x265), so the rules for those record names are
  tested at parser level against the names this build's own ``libavutil`` prints:
  ``Dolby Vision RPU Data``, ``Dolby Vision Metadata``, ``HDR Dynamic Metadata
  SMPTE2094-40 (HDR10+)``.
* Where the picture-structure window is read from matters. ``-read_intervals
  "<t>%+#N"`` seeks and returns pictures near ``t`` for Matroska, MP4 and AVI, but
  for MPEG program/transport streams the start is ignored or starves the counted
  read entirely (measured: ``0.5%+#4`` on the DVD fixture printed pictures from
  1.42 s onward while ``2.0%+#4`` printed **none**). So two windows are sampled: the
  head of the stream, which every family answers, and — when the render range starts
  further in — the requested position. A window that yields nothing is reported as a
  gap, never as evidence of progressive coding.

All of it is one header read plus at most two bounded frame windows per stream,
cached by file identity, because still-frame inspection happens on every playhead
move and must not turn into a per-frame decode of the title.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..core.model import Project
from ..core.timeutils import ZERO
from .ffprobe import bit_depth_of

F = Fraction

#: Field-order tokens that name interlaced coding. ffprobe prints these exact short
#: tokens for the four field-coded variants (measured: the MPEG-2 fixtures print
#: ``tt`` for top-field-first and ``bb`` for bottom-field-first).
INTERLACED_FIELD_ORDERS = frozenset({"tt", "bb", "tb", "bt"})
#: The one token that claims progressive coding. Anything else — including an
#: absent value — is unknown, never progressive.
PROGRESSIVE_FIELD_ORDER = "progressive"

#: Transfer characteristics that name an HDR EOTF. Both spellings of HLG are listed
#: because ffprobe prints ``arib-std-b67`` while FFmpeg's own option name is
#: ``arib_std_b67``; matching is done on a punctuation-free form.
HDR_TRANSFERS: dict[str, str] = {
    "smpte2084": "PQ (HDR10/HDR10+) transfer",
    "aribstdb67": "HLG (ARIB STD-B67) transfer",
}

#: Side-data record names that are themselves HDR claims. Matched on
#: punctuation-free lower-case substrings because printed names move between FFmpeg
#: releases; this build's ``libavutil`` prints "Dolby Vision RPU Data", "Dolby Vision
#: Metadata", "Mastering display metadata", "Content light level metadata" and "HDR
#: Dynamic Metadata SMPTE2094-40 (HDR10+)". An unregistered user-data SEI is
#: deliberately **not** listed: x264 and x265 put their own encoder settings in one,
#: so it says nothing about HDR.
HDR_SIDE_DATA_MARKERS = (
    "dolbyvision",
    "dovi",
    "hdr10",
    "hdrdynamicmetadata",
    "smpte2094",
    "masteringdisplay",
    "contentlightlevel",
)

#: Pictures sampled per window: enough to see how a stream is coded, small enough to
#: be a probe rather than a decode.
PICTURE_STRUCTURE_FRAMES = 8

#: Sampling positions are bucketed to this many seconds so stepping the playhead
#: through one shot reuses a single picture-structure read instead of starting a
#: probe per still. The question being answered — how this stream is coded — does
#: not change within a shot.
PROBE_BUCKET_SECONDS = 1

#: How many probed streams are remembered. Bounded so that inspecting many sources
#: in a long session cannot grow without limit.
MAX_PROBED_STREAMS = 8

PROBE_TIMEOUT_SECONDS = 30.0

#: What a window that produced no picture is called in a message.
UNREADABLE_WINDOW = "no picture could be decoded in the window that was read"


class PictureCapabilityError(Exception):
    """This composition shows picture the renderer cannot produce correctly."""


def _match_form(value: str) -> str:
    """Punctuation-free lower-case form, so ``arib-std-b67`` matches ``arib_std_b67``."""
    return "".join(character for character in value.lower() if character.isalnum())


def _token(value: Any) -> str | None:
    """A printed token, or ``None`` when it states nothing.

    ``unknown`` and ``unspecified`` are claims that no value was stated (measured: an
    H.264 VUI of *unspecified* omits the key at stream level and prints ``unknown``
    at frame level), so they are never kept as if they were values.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().lower()
    if not text or text in {"unknown", "unspecified", "n/a", "none", "null", "-"}:
        return None
    return text


def _flag(record: Mapping[str, Any], key: str) -> bool | None:
    """A printed 0/1 flag, or ``None`` when the record does not carry one."""
    value = record.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return None


def interlaced_field_order(field_order: str | None) -> bool:
    """True only when a field order *names* interlaced coding."""
    return field_order is not None and field_order in INTERLACED_FIELD_ORDERS


def hdr_transfer(color_transfer: str | None) -> str | None:
    """The words that name a known HDR transfer, or ``None``.

    An unknown transfer is **not** answered here: unknown never means "refuse" and
    never means "proved SDR".
    """
    if color_transfer is None:
        return None
    return HDR_TRANSFERS.get(_match_form(color_transfer))


def hdr_side_data(names: Iterable[str]) -> tuple[str, ...]:
    """Which of these side-data record names are themselves HDR claims."""
    return tuple(
        name
        for name in names
        if any(marker in _match_form(name) for marker in HDR_SIDE_DATA_MARKERS)
    )


def plausible_hdr_without_transfer(
    color_transfer: str | None,
    color_primaries: str | None,
    color_space: str | None,
    bit_depth: int | None,
) -> bool:
    """Whether an unstated transfer sits next to other signs of an HDR master.

    Deep sample formats and BT.2020 primaries are how HDR masters are stored, so
    that combination with an unstated transfer is worth saying out loud. It only ever
    makes the application *say* something, never refuse: **10-bit on its own is not
    proof of HDR**, and no gamma or tone-map assumption is made either way.
    """
    if color_transfer is not None:
        return False
    if bit_depth is not None and bit_depth > 8:
        return True
    for tag in (color_primaries, color_space):
        if tag and _match_form(tag).startswith("bt2020"):
            return True
    return False


@dataclass(frozen=True)
class PictureFormatFacts:
    """One picture stream, as the file reports it right now.

    Every field is ``None`` or empty when the container does not state it. The
    ``frames_*`` fields come from bounded decodes of ``frames_read`` pictures: they
    describe those windows, not the whole title, and a window that could not be read
    is reported in ``probe_note`` rather than passed off as progressive SDR.
    """

    path: Path
    stream_index: int
    field_order: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    color_space: str | None = None
    pix_fmt: str | None = None
    bit_depth: int | None = None
    side_data: tuple[str, ...] = ()
    #: True when a sampled picture reported ``interlaced_frame``; False when every
    #: sampled picture reported it clear; ``None`` when nothing was sampled
    frames_interlaced: bool | None = None
    #: the sampled pictures' own ``top_field_first``, when one of them named one
    top_field_first: bool | None = None
    frames_read: int = 0
    #: colour tags and side-data records seen on the sampled pictures, which can be
    #: more specific than the stream header (measured: the MaxCLL record x265 writes
    #: appears on frames, where the stream-level read does not report it)
    frames_color_transfer: str | None = None
    frames_side_data: tuple[str, ...] = ()
    #: where the picture-structure windows were taken from
    read_from: tuple[Fraction, ...] = ()
    #: what could not be measured, if anything; ``None`` when all of it could be
    probe_note: str | None = None

    @property
    def all_side_data(self) -> tuple[str, ...]:
        """Stream-level and frame-level side-data names, de-duplicated."""
        names: list[str] = []
        for name in (*self.side_data, *self.frames_side_data):
            if name not in names:
                names.append(name)
        return tuple(names)


_STREAM_ENTRIES = (
    "stream=index,codec_type,codec_name,field_order,color_range,color_space,"
    "color_transfer,color_primaries,pix_fmt,bits_per_raw_sample,side_data_list"
)
_FRAME_ENTRIES = (
    "frame=best_effort_timestamp,interlaced_frame,top_field_first,color_space,"
    "color_transfer,color_primaries,pix_fmt,bits_per_raw_sample"
    ":side_data_list=side_data_type"
)


def _decimal(value: Fraction) -> str:
    """Fixed-point text for options that cannot take rationals.

    The same rule :func:`video_editor.media.framegrab.decimal` uses; repeated here
    because ``framegrab`` calls *this* module and the two must not form an import
    cycle.
    """
    return f"{float(value):.9f}"


def _run(args: list[str], timeout: float, what: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PictureCapabilityError(f"cannot {what}: {exc}") from exc


def _payload(stdout: str, what: str) -> dict[str, Any]:
    try:
        document = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PictureCapabilityError(f"cannot {what}: unparseable ffprobe output ({exc})") from exc
    if not isinstance(document, dict):
        raise PictureCapabilityError(f"cannot {what}: ffprobe output is not an object")
    return document


def _last(stderr: str, fallback: str) -> str:
    lines = (stderr or "").strip().splitlines()
    return lines[-1] if lines else fallback


def _side_names(records: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """The ``side_data_type`` names in these records, in the order first seen."""
    names: list[str] = []
    for record in records:
        entries = record.get("side_data_list")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("side_data_type")
            if isinstance(name, str) and name.strip() and name.strip() not in names:
                names.append(name.strip())
    return tuple(names)


def _video_records(document: dict[str, Any]) -> list[dict[str, Any]]:
    streams = document.get("streams")
    if not isinstance(streams, list):
        raise PictureCapabilityError("ffprobe reported no streams")
    return [record for record in streams if isinstance(record, dict)]


def _picture_records(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Picture records from either shape ``-show_frames`` prints.

    With ``-show_entries frame=...`` and a counted read interval, FFmpeg 8.1.3 prints
    ``packets_and_frames`` with packets interleaved between the pictures; with a
    plain ``-show_frames`` it prints ``frames``. Both are accepted and only the
    picture records are kept.
    """
    records = document.get("frames")
    if not isinstance(records, list):
        records = document.get("packets_and_frames")
    if not isinstance(records, list):
        return []
    pictures = []
    for record in records:
        if not isinstance(record, dict):
            continue
        kind = record.get("media_type") or record.get("type")
        if kind in (None, "video", "frame"):
            pictures.append(record)
    return pictures


def _window_starts(from_time: Fraction) -> tuple[Fraction, ...]:
    """Where to sample: the head of the stream, plus the requested position.

    The head read is not optional. Measured: a counted read interval starting further
    into an MPEG program or transport stream prints nothing at all, while the same
    window taken from the beginning prints pictures — so a position-only read would
    report "unknown" for exactly the DVD and broadcast files whose field coding
    matters most.
    """
    return (ZERO,) if from_time <= ZERO else (ZERO, from_time)


def probe_picture_format(
    path: Path,
    stream_index: int,
    ffprobe: str | None = None,
    from_time: Fraction = ZERO,
    frames: int = PICTURE_STRUCTURE_FRAMES,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> PictureFormatFacts:
    """Read one stream's header and the picture structure of bounded windows.

    ``from_time`` is on the file's own content axis (the axis an input ``-ss``
    addresses), so the caller subtracts the stream's container offset — the same
    convention :mod:`video_editor.render.graph` and :mod:`video_editor.media.framegrab`
    use. At most two windows are sampled, never the whole title: the head of the
    stream, which every container family answers, and the requested position.
    """
    tool = ffprobe or shutil.which("ffprobe")
    if tool is None:
        raise PictureCapabilityError("ffprobe is not available: picture formats cannot be checked")
    name = Path(path).name
    header = _run(
        [
            tool, "-v", "error", "-print_format", "json", "-show_streams",
            "-select_streams", "v", "-show_entries", _STREAM_ENTRIES, str(path),
        ],
        timeout,
        f"read the picture format of {name}",
    )
    if header.returncode != 0:
        raise PictureCapabilityError(
            f"cannot read the picture format of {name}: "
            f"{_last(header.stderr, f'exit status {header.returncode}')}"
        )
    videos = _video_records(_payload(header.stdout, f"the picture format of {name}"))
    position = next((i for i, r in enumerate(videos) if r.get("index") == stream_index), None)
    if position is None:
        raise PictureCapabilityError(
            f"cannot read the picture format of {name}: stream {stream_index} is not in it"
        )
    record = videos[position]

    windows: list[tuple[Fraction, list[dict[str, Any]]]] = []
    gaps: list[str] = []
    for start in _window_starts(from_time):
        args = [
            tool, "-v", "error", "-print_format", "json",
            "-select_streams", f"v:{position}",
            "-show_frames", "-show_entries", _FRAME_ENTRIES,
            "-read_intervals",
            f"{_decimal(start)}%+#{frames}" if start > ZERO else f"%+#{frames}",
            str(path),
        ]
        window = _run(args, timeout, f"read the picture structure of {name}")
        if window.returncode != 0:
            gaps.append(
                f"at {float(start):.3f}s: "
                f"{_last(window.stderr, f'exit status {window.returncode}')}"
            )
            continue
        try:
            pictures = _picture_records(
                _payload(window.stdout, f"the picture structure of {name}")
            )
        except PictureCapabilityError as exc:
            gaps.append(f"at {float(start):.3f}s: {exc}")
            continue
        if not pictures:
            gaps.append(f"at {float(start):.3f}s: {UNREADABLE_WINDOW}")
            continue
        windows.append((start, pictures))

    interlaced: bool | None = None
    top_first: bool | None = None
    frame_transfer: str | None = None
    frame_side: list[str] = []
    pictures_read = 0
    for _, pictures in windows:
        pictures_read += len(pictures)
        for picture in pictures:
            flag = _flag(picture, "interlaced_frame")
            if flag is not None:
                interlaced = flag if interlaced is None else interlaced or flag
            first = _flag(picture, "top_field_first")
            if first is not None:
                top_first = first if top_first is None else top_first or first
            frame_transfer = frame_transfer or _token(picture.get("color_transfer"))
        for side in _side_names(pictures):
            if side not in frame_side:
                frame_side.append(side)
    note: str | None = None
    if not windows:
        note = "; ".join(gaps) if gaps else UNREADABLE_WINDOW
    elif gaps:
        note = "partly unreadable: " + "; ".join(gaps)
    pix_fmt = _token(record.get("pix_fmt"))
    return PictureFormatFacts(
        path=Path(path),
        stream_index=stream_index,
        field_order=_token(record.get("field_order")),
        color_transfer=_token(record.get("color_transfer")),
        color_primaries=_token(record.get("color_primaries")),
        color_space=_token(record.get("color_space")),
        pix_fmt=pix_fmt,
        bit_depth=bit_depth_of(pix_fmt, record.get("bits_per_raw_sample")),
        side_data=_side_names([record]),
        frames_interlaced=interlaced,
        top_field_first=top_first,
        frames_read=pictures_read,
        frames_color_transfer=frame_transfer,
        frames_side_data=tuple(frame_side),
        read_from=tuple(start for start, _ in windows),
        probe_note=note,
    )


_PROBED: OrderedDict[tuple[str, int, int, int, int], PictureFormatFacts] = OrderedDict()


def clear_probe_cache() -> None:
    """Forget the cached reads (for tests, or a deliberate re-measurement)."""
    _PROBED.clear()


def _cached_probe(
    path: Path, stream_index: int, ffprobe: str | None, from_time: Fraction
) -> PictureFormatFacts:
    """One measurement per file identity, stream and sampling bucket.

    Size and modification time are part of the key, so a source replaced on disk is
    measured again instead of answered from the old encode — the same identity rule
    the source-availability checks use.
    """
    try:
        stat = Path(path).stat()
        identity: tuple[int, int] | None = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        identity = None
    bucket = int(from_time // PROBE_BUCKET_SECONDS) if identity is not None else 0
    key = (str(path), stream_index, bucket, *(identity or (0, 0)))
    found = _PROBED.get(key)
    if found is not None:
        _PROBED.move_to_end(key)
        return found
    facts = probe_picture_format(Path(path), stream_index, ffprobe, from_time)
    if identity is not None:  # an unreadable file is measured afresh every time
        _PROBED[key] = facts
        while len(_PROBED) > MAX_PROBED_STREAMS:
            _PROBED.popitem(last=False)
    return facts


def _describe(facts: PictureFormatFacts) -> str:
    """How a message names the stream: the file the user imported, not a hex id.

    The render layer speaks of ``asset <id> stream <n>``; the person who has to act
    on this message knows the file by the name the media bin shows.
    """
    return f"the picture in stream {facts.stream_index} of {facts.path.name}"


def _field_words(facts: PictureFormatFacts) -> str:
    if facts.top_field_first:
        return "top field first"
    if facts.top_field_first is False:
        return "bottom field first"
    return "field order not stated"


def assess_picture_format(facts: PictureFormatFacts) -> tuple[str | None, tuple[str, ...]]:
    """``(refusal, warnings)`` for one measured picture stream.

    A refusal is a finished sentence naming the stream and the value that caused it;
    ``None`` means this picture can be drawn. Warnings say what is **not known**: they
    never claim a stream is safe, and an unknown value never causes a refusal, because
    an unstated field order or transfer proves only that the file never said.
    """
    who = _describe(facts)
    refusal: str | None = None
    warnings: list[str] = []

    if interlaced_field_order(facts.field_order):
        refusal = (
            f"{who} is interlaced: ffprobe reports field_order {facts.field_order!r}, and this "
            "project has no deinterlacer, so rendering would put the two fields of one source "
            "frame into separate output frames. Deinterlace the source into a new file, or use "
            "another stream for the picture, then render again."
        )
    elif facts.frames_interlaced:
        # measured: field-coded H.264 in Matroska/MP4 prints field_order
        # "progressive" on the stream while every decoded frame reports
        # interlaced_frame=1, so the decoded pictures are the evidence here
        refusal = (
            f"{who} is interlaced: {facts.frames_read} picture(s) decoded from it report "
            f"interlaced frames ({_field_words(facts)}), even though its container says "
            f"field_order {facts.field_order or 'not stated'}, and this project has no "
            "deinterlacer, so rendering would put the two fields of one source frame into "
            "separate output frames. Deinterlace the source into a new file, or use another "
            "stream for the picture, then render again."
        )
    elif facts.field_order is None:
        warnings.append(
            f"{who} does not state a field order, so it is rendered as progressive picture. "
            "If the source is field-coded the output will show combing, because no "
            "deinterlacer is implemented"
        )

    transfer = facts.color_transfer or facts.frames_color_transfer
    named_hdr = hdr_transfer(transfer)
    hdr_records = hdr_side_data(facts.all_side_data)
    if refusal is None and named_hdr is not None:
        refusal = (
            f"{who} has an HDR colour transfer: ffprobe reports color_transfer {transfer!r}, "
            f"which is {named_hdr}. This project renders SDR yuv420p picture only and performs "
            "no colour-space or tone conversion, so those code values would be written as if "
            "they were SDR and the picture would be wrong. Tone-map the source to SDR in a new "
            "file, or use another stream for the picture, then render again."
        )
    elif refusal is None and hdr_records:
        listed = ", ".join(repr(item) for item in hdr_records)
        refusal = (
            f"{who} carries HDR metadata ({listed}). This project renders SDR yuv420p picture "
            "only and performs no colour-space or tone conversion, so it cannot produce the "
            "picture that metadata describes. Tone-map the source to SDR in a new file, or use "
            "another stream for the picture, then render again."
        )
    elif plausible_hdr_without_transfer(
        transfer, facts.color_primaries, facts.color_space, facts.bit_depth
    ):
        detail = ""
        if facts.bit_depth and facts.bit_depth > 8:
            detail = f" It is {facts.bit_depth}-bit"
        if facts.color_primaries and _match_form(facts.color_primaries) == "bt2020":
            detail += " over BT.2020 primaries" if detail else " It uses BT.2020 primaries"
        warnings.append(
            f"{who} does not state a colour transfer, and it is rendered with no colour or tone "
            "conversion, so HDR or wide-gamut source colour would come out wrong because no "
            f"tone mapping is implemented.{detail}"
        )

    if facts.probe_note:
        warnings.append(
            f"{who} could not be checked completely: {facts.probe_note}, so interlaced or HDR "
            "picture in it may not have been detected"
        )
    return refusal, tuple(warnings)


def picture_streams(project: Project, plans: Iterable[Any]) -> dict[tuple[str, int], Fraction]:
    """The picture streams these plans will actually show, and where to start reading.

    Only a plan's ``video_items`` that carry a clip count, because that is the picture
    the composition presents after stacking and range clipping: a clip on a disabled
    video track, one covered by a higher track, and one lying outside the requested
    range never appear here — so a source this render does not look at can never be
    the reason for a refusal. Audio is not examined at all: the renderer's picture
    limits say nothing about it, and unusable picture must never block a soundtrack.
    """
    wanted: dict[tuple[str, int], Fraction] = {}
    for plan in plans:
        for item in getattr(plan, "video_items", ()):
            clip = getattr(item, "clip", None)
            if clip is None:
                continue
            key = (clip.asset_id, clip.stream_index)
            current = wanted.get(key)
            if current is None or item.source_start < current:
                wanted[key] = item.source_start
    return wanted


def check_picture_capabilities(
    project: Project,
    plans: Iterable[Any],
    asset_paths: Mapping[str, Path],
    ffprobe: str | None = None,
) -> tuple[str, ...]:
    """Preflight every picture these plans show: refuse it, or warn about it.

    Returns the warnings and raises :class:`PictureCapabilityError` for the first
    stream whose picture this renderer cannot produce. Export, preview and
    still-frame inspection all call this one function, so they cannot disagree about
    what is renderable, and it measures the file that is about to be decoded rather
    than trusting what a project document recorded: a document written before these
    fields existed records nothing about them, and "nothing recorded" must never be
    read as "safe".

    A stream that cannot be measured at all — the file is gone, or ffprobe cannot read
    it as media — is **not** a refusal here. It has no picture for this renderer to get
    wrong, and the paths that resolve and decode the asset report that condition with a
    message naming the real problem; the preflight only adds that the format stayed
    unknown. Refusal is reserved for a format actually read off a stream: a stated field
    order, a decoded picture reporting fields, a stated transfer, or HDR metadata.
    """
    wanted = picture_streams(project, plans)
    warnings: list[str] = []
    for (asset_id, stream_index) in sorted(wanted):
        path = asset_paths.get(asset_id)
        if path is None:
            continue  # the source-availability checks already refuse a missing asset
        source = project.source(asset_id, stream_index)
        offset = source.input_start if source is not None else ZERO
        # measured from where this range first takes picture from that stream, on the
        # file's own content axis, the way the render graph addresses it
        from_time = max(ZERO, wanted[(asset_id, stream_index)] - offset)
        try:
            facts = _cached_probe(Path(path), stream_index, ffprobe, from_time)
        except PictureCapabilityError as exc:
            # A file that cannot be probed at all — gone, or not readable as media — is
            # not a *format* this renderer could draw wrongly: there is no picture here
            # to draw. The paths that resolve and decode the asset already report that
            # with a message naming the real problem, and this preflight must not talk
            # over them; all it can honestly say is that the format is unknown.
            warnings.append(
                f"the picture in stream {stream_index} of {Path(path).name} could not be "
                f"checked: {exc}, so interlaced or HDR picture in it may not have been "
                "detected"
            )
            continue
        refusal, stream_warnings = assess_picture_format(facts)
        if refusal is not None:
            raise PictureCapabilityError(refusal)
        warnings.extend(stream_warnings)
    return tuple(warnings)

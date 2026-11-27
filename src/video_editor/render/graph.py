"""Translate a :class:`~video_editor.render.plan.RenderPlan` into an FFmpeg job.

This module only *builds* the command line and the filter graph; executing it
(progress, cancellation, cleanup) is the exporter's job. Arguments are always
passed as lists, never through a shell.

How the plan maps onto FFmpeg
-----------------------------
* One input per distinct ``(asset, stream, seek, read)`` the plan reads, opened
  with ``-ss <seconds>`` (and ``-t <how much>``) **before** ``-i``: FFmpeg then
  does an accurate seek (decoding from the preceding keyframe and discarding) and
  reads no further than the plan asked. That seek is measured from the **file's
  own start time**, not from 0, so the plan's sources record the container start
  alongside the asset-local timestamps and the seek is
  ``source_start - container_start``. Picture seeks to a little before the frame
  its item presents (the preroll that makes the correct frame available), audio
  seeks to the **start of the clip**, not to the start of the render range,
  because the clip is then shaped as a whole (see below) and only the range's
  piece is kept. The wanted elementary stream is addressed as ``[in:v:N]`` /
  ``[in:a:N]``: FFmpeg labels filtergraph inputs by *stream type and position
  within that type*, which is not the same number as the container stream index
  kept in the domain model. The plan's sources therefore carry both, and an
  unrecorded position is a hard error rather than a guess.
* Decoded picture timestamps read **"seconds after the input ``-ss``"**: FFmpeg
  rebases them itself, and that rebase is quantised to the container's time base
  (measured: seeking to 1.041666667 s in a millisecond-based file left the next
  frame at 0.038 s, not 0.038333 s). That is harmless only when the seek is a
  whole number of container ticks, which is what the plan arranges, and the
  picture chain then puts the stamps on the composition's own axis from that
  exact origin. ``-copyts`` was tried to keep the container's own numbers
  instead: it also stops FFmpeg trimming the lead-in of an audio input, so
  decoded audio started before the clip. Measured, and refused. Every chain
  rebases timestamps itself, so the muxer still receives streams starting at 0.
* Every item is forced to an **exact output-frame / output-sample count**
  (``trim=end_frame=N``; ``apad`` + ``atrim=end_sample=N``). Those counts come
  from the plan's global indices, so joining items cannot accumulate rounding
  error and splitting a render into chunks cannot move a boundary.
* Picture per item (:func:`picture_filter_chain`, also used by the still viewer):
  ``settb`` onto a fine filter tick chosen so that every retimed timestamp is a
  whole number of ticks → ``select`` enforcing the clip's half-open **source**
  slice → ``setpts`` onto the plan's own time axis and tempo, with the sampling
  grid's phase taken off so the first grid point is the plan's first sampling time
  → ``fps`` to the output rate from that phase, choosing each frame by exact
  integer arithmetic → rebase → ``scale`` to the largest even square-pixel size the
  display aspect ratio fits inside (:func:`picture_scale_fit`) → ``setsar`` → pad →
  ``format``. Black areas use the ``color`` source filter. A stream whose exact tick
  could not be established (no measured time base, or bounds in
  :data:`~video_editor.render.plan.MAX_PICTURE_TICK`) gets the older,
  container-quantised chain and is named in :attr:`RenderJob.unanchored` or
  :attr:`RenderJob.inexact_picture`.
* Audio per contribution, one chain in this order, after which **every count is
  in output samples**:

  1. the input is opened with ``-ss``/``-t`` at the clip's slice and the read is
     additionally capped in **source** samples
     (``atrim=start_sample=0:end_sample=<source seconds x source rate>``), before
     any conversion, so material past ``source_out`` is never read and can never
     bleed into the timeline after the clip. Capping by a source-rate sample
     count rather than by a *time* is deliberate: ``atrim``'s time options proved
     unreliable in this FFmpeg (measured: 0 or 42 samples returned for a 2 s
     window), and a count written in output samples would cut the wrong amount of
     audio.
  2. ``aresample=<output sample rate>`` → ``aformat=sample_fmts=fltp:...``:
     sample-rate and channel conversion written out explicitly instead of
     trusting FFmpeg's format negotiation to insert it somewhere convenient
     (which is exactly what made an earlier chain depend on luck: the cap below
     then counted samples at whatever rate happened to be current).
  3. chained ``atempo`` stages for the pitch-preserving tempo.
  4. ``apad`` + ``atrim=start_sample=0:end_sample=<clip_samples>``: the whole
     clip at an exact output-sample length. The padding can only ever be the
     residue of tempo/resampler edges — the plan refuses a clip that would need
     material the stream does not contain, rather than filling it with silence.
  5. ``afade=start_sample=…:nb_samples=…`` per fade, then ``volume`` for the
     constant clip gain. ``afade`` addressed by **sample** start and length is
     sample-accurate and reproduces ``core.model.envelope_gain`` exactly (the
     k-th sample of a ramp is scaled by k/N — measured, see
     ``tests/test_render_audio_timing.py``); ``volume=…:eval=frame`` does not,
     because it re-evaluates once per filter *frame*, which is a whole buffer of
     samples. Fades are applied to the whole clip and cut to the range afterwards, so
     a chunk or preview window that begins mid-fade continues the ramp.
  6. ``atrim=start_sample=<pre>:end_sample=<pre+samples>`` — this range's piece
     of that clip.
  7. positioned at its exact global sample offset by concatenating leading
     silence, then padded to the whole range so the mix stays anchored.

  Contributions are summed with
  ``amix=normalize=0`` — a plain sum, never per-clip normalisation.
* Picture and audio are assembled as **two independent timelines** (picture by
  ``concat``, audio by globally anchored summation). Doing both inside one
  ``concat`` would let per-segment rounding of one stream drag the other.
* VFR input goes through the same picture stages: the container's time base bounds
  the timestamps and the fine tick is derived from it, while
  :mod:`video_editor.media.frameindex` supplies the real picture times the reading
  window needs. ``fps=round=up`` then chooses the last picture at or before each
  sampling time. Measured against decoded PTS evidence on variable-frame-rate
  fixtures (the ``vfr-*`` cases of the picture-phase diagnostic), a whole render
  and independently sought ranges then agree with each other and with that rule.
  It is not a claim to reproduce some other tool's VFR handling.
* Colour: geometry and pixel format are normalised, but no colour
  space/transfer-characteristic conversion is performed and no HDR handling is
  claimed.
"""

from __future__ import annotations

import math
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from ..core.model import Clip, StreamSource, TrackKind
from .plan import (
    AudioItem,
    PictureSampling,
    ReadWindow,
    RenderPlan,
    VideoItem,
    picture_chain_reaches_its_first_grid_point,
    picture_preroll,
    picture_read_window,
    picture_sampling,
    picture_window_holds_a_picture,
)

F = Fraction


class RenderConfigError(Exception):
    """The plan cannot be expressed as a correct FFmpeg job."""


#: atempo accepts a limited factor range per stage; other rates are chained.
ATEMPO_MIN = F(1, 2)
ATEMPO_MAX = F(2)

#: How far past its own end a sound item's input is allowed to keep reading.
#: An input ``-t`` stops at a packet boundary and a bound landing exactly on the
#: clip's end decides that packet out, so a bound equal to the slice loses real
#: material: measured on an AAC stream, a 0.04 s slice with ``-t 0.04`` decoded
#: one sample instead of 1920. Audio packet sizes here are 1024 samples (AAC) and
#: 3840 samples (PCM in Matroska, 80 ms at 48 kHz), so a quarter second covers
#: any of them with room to spare. The exact amount kept is the plan's
#: ``atrim`` sample count, never this bound.
AUDIO_READ_OVERREAD = F(1, 4)

#: Channel layouts we are willing to claim support for. Anything else is
#: refused rather than silently down-mixed into a different layout.
SUPPORTED_LAYOUTS = frozenset(
    {"mono", "stereo", "2.1", "3.0", "5.0", "5.1", "7.0", "7.1"}
)

#: Spellings FFmpeg accepts that name a speaker position we do not claim to map.
SIDE_LAYOUTS = frozenset({"quad(side)", "5.1(side)", "6.1(side)", "7.1(wide-side)"})


def normalise_layout(layout: str) -> str:
    """Map ffprobe spellings onto FFmpeg layout names, or refuse.

    Deliberately *not* helpful about side speakers: an earlier version stripped
    ``(side)``, which quietly promised that side and rear speakers are the same
    position. They are not, and folding one into the other moves sound around,
    so an output layout that names side speakers is refused with the reason.
    """
    if not layout:
        return "stereo"
    if layout in SUPPORTED_LAYOUTS:
        return layout
    if layout in SIDE_LAYOUTS or "(side)" in layout:
        raise RenderConfigError(
            f"Channel layout {layout!r} names side speakers, which this renderer "
            "does not map onto rear speakers; choose one of: "
            + ", ".join(sorted(SUPPORTED_LAYOUTS))
        )
    raise RenderConfigError(
        f"Channel layout {layout!r} is not supported for export; supported: "
        + ", ".join(sorted(SUPPORTED_LAYOUTS))
    )


@dataclass(frozen=True)
class CodecPreset:
    """Output container + codecs."""

    name: str
    extension: str
    video_codec: str
    audio_codec: str
    pixel_format: str = "yuv420p"
    muxer: str = "mp4"
    extra_video_args: tuple[str, ...] = ()
    extra_audio_args: tuple[str, ...] = ()
    #: deliberately no "samples per audio packet" field: what a packet holds is a
    #: container detail, measured here as 3840 samples per packet for PCM in
    #: Matroska and 1024 for AAC, so the produced file's sound is counted by
    #: *decoding it*
    #: instead of inferred from packet counts (``exporter.measure_decoded_span``)

    @property
    def display(self) -> str:
        return f"{self.name} ({self.video_codec} + {self.audio_codec})"


#: Initial (and only claimed) delivery preset.
MP4_H264_AAC = CodecPreset(
    name="MP4 H.264/AAC",
    extension="mp4",
    video_codec="libx264",
    audio_codec="aac",
    extra_video_args=("-preset", "veryfast", "-crf", "20"),
    extra_audio_args=("-b:a", "192k"),
)

#: Preview variant of the delivery preset: same container, codecs and
#: composition semantics, but a coarser picture and a short GOP so a preview
#: renders quickly and seeking inside the file lands near any time. Quality is a
#: presentation choice; it never changes what is rendered (the planner keeps the
#: same cuts, stacking, gaps and mix, only the picture size differs).
PREVIEW_H264_FAST = CodecPreset(
    name="Preview MP4 H.264/AAC",
    extension="mp4",
    video_codec="libx264",
    audio_codec="aac",
    extra_video_args=("-preset", "veryfast", "-crf", "30", "-g", "15"),
    extra_audio_args=("-b:a", "96k"),
)

#: Intermediate used when a plan is rendered in chunks: PCM/FFV1 have no
#: encoder delay or tail padding, so chunk joins stay sample/frame exact while
#: the lossy encoders are initialised exactly once, at the final encode.
LOSSLESS_INTERMEDIATE = CodecPreset(
    name="Lossless intermediate",
    extension="mkv",
    video_codec="ffv1",
    audio_codec="pcm_s16le",
    pixel_format="yuv420p",
    muxer="matroska",
)


@dataclass
class RenderJob:
    """A runnable FFmpeg invocation plus what it represents."""

    args: list[str]
    duration: Fraction
    video_items: int
    audio_items: int
    warnings: tuple[str, ...] = ()
    temp_files: list[Path] = field(default_factory=list)
    #: ``(asset id, stream index)`` for picture streams whose sampling grid could
    #: not be placed on an exactly representable container timestamp (no time base
    #: could be read for them). The render is still the composition the plan
    #: describes, but the picture chosen for a sampling time that falls exactly on
    #: a frame time is then not guaranteed: this is recorded so a caller can say
    #: so rather than presenting the frame as exact.
    unanchored: frozenset[tuple[str, int]] = frozenset()
    #: ``(asset id, stream index)`` for picture streams whose time base was read
    #: but for which no usable fine tick could be established (an unrepresentable
    #: tick, or a timeline long enough that the exact expression would leave the
    #: exactly-representable range). Those items are rendered by the older
    #: container-quantised chain: named here rather than left implicit.
    inexact_picture: frozenset[tuple[str, int]] = frozenset()


def decimal(value: Fraction, digits: int = 9) -> str:
    """Fixed-point text for the few FFmpeg options that cannot take rationals.

    Error is below one nanosecond for the values used here; everywhere exactness
    matters an integer frame/sample count is used instead.
    """
    return f"{float(value):.{digits}f}"


def rational(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def tick_number(value: Fraction) -> str:
    """The whole-tick bound that states ``value`` exactly for an integer ``pts``.

    ``av_expr`` parses every number as a ``double``, so a bound written as a decimal
    fraction is only as good as that rounding - and a long film leaves the double no
    room at all. It does not need to be: ``pts`` at that point in the chain is a
    whole number of fine ticks, and for an integer the statements "``pts < v``" and
    "``pts >= v``" are both stated exactly by the ceiling of ``v``. An edge that is
    already a whole tick - the usual case, since the seek and most clip edges are -
    is unchanged by this, and the ceiling is an integer the plan has kept below the
    range a ``double`` holds exactly (:data:`~video_editor.render.plan.MAX_PICTURE_TICK_PRODUCT`).
    """
    return str(math.ceil(value))


def atempo_chain(rate: Fraction) -> list[str]:
    """Pitch-preserving tempo as chained atempo stages within [1/2, 2]."""
    if rate <= 0:
        raise RenderConfigError("Clip rate must be positive")
    factors: list[Fraction] = []
    remaining = rate
    while remaining > ATEMPO_MAX:
        factors.append(ATEMPO_MAX)
        remaining /= ATEMPO_MAX
    while remaining < ATEMPO_MIN:
        factors.append(ATEMPO_MIN)
        remaining /= ATEMPO_MIN
    if remaining != 1:
        factors.append(remaining)
    return [f"atempo={decimal(f)}" for f in factors]


#: How a graph finds a stream's container time base (``media.framegrab`` probes
#: it; knowing it is what lets a picture seek land on an exactly representable
#: timestamp instead of one FFmpeg will round).
TimeBaseLookup = Callable[[str, int], Fraction | None]
#: How a graph finds the first real picture at or after a source time
#: (``media.frameindex`` reads bounded windows of it; this is what turns a clip
#: head that asks for material the source does not have into a held picture
#: instead of a one-frame shift of everything after it).
FirstFrameLookup = Callable[[str, int, Fraction], Fraction | None]
#: How a graph finds the last real picture at or before a source time. The first
#: grid point of an item shows exactly that picture, so where it is measured the
#: reading window can start there instead of at a nominal preroll - and a
#: variable-frame-rate stream is not guessed at.
FrameLookup = Callable[[str, int, Fraction], Fraction | None]
#: How a graph finds every real picture timestamp of the source interval
#: ``[start, end)``, or ``None`` when the interval was not read all the way across.
#: These are the stamps an item's exact tempo is *proved* against (see
#: :func:`~video_editor.render.plan.picture_tempo`), so a partly read answer has to
#: come back as ``None`` rather than as whatever pictures the read happened to reach.
WindowPicturesLookup = Callable[
    [str, int, Fraction, Fraction], tuple[Fraction, ...] | None
]


def picture_scale_fit(width: int, height: int) -> str:
    """``scale`` stage that fits a picture into ``width``x``height`` by its **display** ratio.

    The output box is square-pixel (every chain ends in ``setsar=1``), so the size
    the picture is scaled to has to come from the display aspect ratio, not from the
    coded width and height. ``dar`` in a ``scale`` expression is the input display
    aspect — measured on FFmpeg 8.1.3: for 720x576 at SAR 64:45 it evaluates to
    16/9, where the coded ratio (the ``a`` variable) is 5/4, and for a stream whose
    sample aspect ratio the container never stated it falls back to the coded ratio
    (SAR read as 1:1: measured ``dar`` 4/3 for 640x480 with no SAR in the file, and
    still 4/3 for a file explicitly written with ``setsar=0/1``).

    ``trunc(./2)*2`` keeps both numbers even (4:2:0 cannot address odd dimensions)
    and rounding *down* is what guarantees the fit stays inside the box; ``min``
    against the box is what stops it overflowing. Measured on FFmpeg 8.1.3 for the
    exact-fit cases, where the fitted size is the box itself and the double
    arithmetic has to land on it rather than 2 px short: 720x576 SAR 64:45, 1440x1080
    SAR 4:3 and 1920x1080 SAR 1:1 into their matching 16:9 boxes, 720x480 SAR 8:9,
    704x576 SAR 12:11 and 720x576 SAR 16:15 into 960x720, and 720x576 SAR 32:27 into
    800x540 all come out at the box, not inside it.

    ``max(2, ...)``, and nothing else, guards the case where the display ratio is so
    extreme that the even fit rounds to zero. That is not hypothetical: without it
    ``scale`` reads a computed ``0`` as *"use the input size"*, so the shape is then
    decided by the source instead of by the box — measured on a 3x2000 source into
    1280x720, the unguarded expression gives 3x720 (the input width) while the
    clamped one gives 2x720. No even square-pixel size inside a 1280x720 box can
    hold a 3:2000 ratio honestly, so the clamp states the choice: the smallest legal
    strip, centred by the ``pad`` that follows, rather than a size inherited from the
    file. The box itself cannot be smaller than this: :func:`~.plan.plan_geometry`
    makes the plan's picture even and at least 16 px per side.
    """
    return (
        f"scale=w='max(2,trunc(min({width},{height}*dar)/2)*2)'"
        f":h='max(2,trunc(min({height},{width}/dar)/2)*2)'"
    )


def picture_filter_chain(
    plan: RenderPlan,
    clip: Clip,
    item: VideoItem,
    sampling: PictureSampling,
    pixel_format: str,
) -> str:
    """The picture filter chain for one item, shared by export, preview and stills.

    Written once so a still frame cannot be a different picture from the exported
    frame at the same global output index.

    With an exact ``sampling.tempo`` - the ordinary case, because the container's time
    base was measured at import - the order is:

    1. ``settb=expr=1/<fine ticks per second>`` — move the item onto the filter
       tick :func:`~video_editor.render.plan.picture_tempo` proved exact for this
       tempo and slice. ``setpts`` below truncates to *that* grid rather than the
       container's, so a tempo like 25/24 no longer quantises the timestamps
       before a picture is chosen. The tick is a whole multiple of the container's,
       so the pictures arrive on it with their timestamps unchanged.
    2. ``select='gte(pts,<in>)*lt(pts,<out>)'`` — the clip's **half-open source
       slice**, written on the source axis in whole ticks. ``-ss``/``-t`` bound the
       *reading* in floating-point seconds; this bounds the *composition*, so the
       reading window can be generous and material outside the slice still cannot
       reach the timeline.
    3. the clip's tempo (``a/b`` source seconds per output second) onto the
       composition's clock, with the grid's own phase taken off, so the first point
       of the ``fps`` grid is exactly this item's first sampling time. Two exact
       spellings, chosen by :func:`~video_editor.render.plan.picture_tempo`:

       * ``setpts=PTS*<b>/<a>-<phase>`` when every incoming stamp is already a whole
         multiple of ``a`` on this tick, which is the condition provable *without*
         knowing the stream's stamps, and the one this chain has always written;
       * ``setpts=(PTS*<b>-<offset>)/<a>`` when only the difference between a picture
         and the grid point divides. That weaker condition is what lets a stream whose
         container counts time in twenty-nanosecond ticks keep an exact tempo at all:
         measured, ``-video_track_timescale 50000000`` at tempo 1000/1001 needs the
         container's own tick and no expansion, while the stamp-free rule demands
         50,000,000,000 filter ticks per second, which no bound and no ``AVRational``
         denominator allows.

       Either way every term is a whole number of fine ticks by that proof (``PTS`` is
       counted in time-base ticks, not seconds — measured: subtracting ``1`` moves a
       frame by one tick), and the phase does not depend on where the seek started,
       which is what makes a sub-render choose the same pictures as the whole render.
    4. ``fps=round=up:fps=<output rate>:start_time=0`` — choose the picture for
       each output frame. ``fps`` converts every stamp to a whole output-frame
       count and walks the grid in those counts, both in integer arithmetic, so
       with exact stamps the choice is exact and independent of the seek.
       ``round=up`` is what makes the rule "the last decoded frame at or before the
       sampling time": measured on a 2410-frame matrix of tempos, slice positions
       and output rates (FFmpeg 8.1.3), the default ``round=near`` resolved each
       grid point to the *nearest* frame and so showed a later picture in 777 of
       them. A grid point before the first readable frame holds that frame, so no
       head padding is needed on this path; pictures before the first grid point
       are dropped, which is what the preroll makes room for.
       **At a tie** — a sampling time exactly equal to a picture's own retimed
       stamp, the ordinary case at tempo 1 — the exact chain has no conversion
       error to resolve: grid point and stamp are the same integer, the picture
       whose stamp equals the sampling time is the one shown, and every seek and
       every range agrees. The fallback has the opposite property: ``setpts``
       *truncates* the value of its expression, so a tempo that is not exact on the
       container's grid shifts each stamp by up to a whole container tick — 1 ms on
       a 1/1000 base, a quarter of the 40 ms spacing of 25 fps material — enough to
       give the grid point to the neighbouring picture at exactly those ties. That
       is the worst error the fallback accepts, which is why it is reported.
    5. ``tpad=stop_mode=clone`` and ``trim`` — the tail the slice cannot supply
       holds the last legal picture, and the item comes out exactly as long as the
       plan promises whether ``fps`` stopped early or ran over.
    6. ``setpts=PTS-STARTPTS`` — the item starts at 0 again, so concatenating
       items cannot accumulate the offsets of steps 1-5.
    7. ``scale`` fitted by **display** aspect ratio (see :func:`picture_scale_fit`)
       → ``setsar=1`` → pad to the output frame → pixel format → ``trim`` to the
       exact frame count the plan promises. That order is the geometry: the fit is
       written in the square pixels the output is made of, so anamorphic material
       is neither pillarboxed by mistake nor stretched. ``scale`` with
       ``force_original_aspect_ratio`` in the box and ``setsar=1`` after it — the
       previous chain — fits the *coded* size by display ratio (measured:
       720x576 SAR 64:45 into 900x720 still carrying SAR 64:45) and then flattens
       those anamorphic pixels, so the same picture arrives 900 px inside a 1280 px
       frame, which ``pad`` fills with 190 px bars.
    """
    rate = plan.quantisation.frame_rate
    # A window the stream leaves empty is decisive for either spelling: no decoded frame
    # means no output frame, whatever the time base. Named rather than left to an empty
    # render that the frame-count check would only notice afterwards.
    if picture_window_holds_a_picture(sampling) is False:
        raise RenderConfigError(
            f"Clip {clip.id} cannot be sampled on asset {clip.asset_id} stream "
            f"{clip.stream_index}: its reading window "
            f"[{float(sampling.seek_source):.6f}, "
            f"{float(sampling.read_source_end):.6f})s holds no picture of that stream "
            f"at all, so the {float(sampling.sample_start):.6f}s the plan asks for "
            "cannot be shown by any chain"
        )
    if sampling.tempo is None:
        # No exact tick: the older, container-quantised chain (see above), with
        # padding at the head because ``fps`` also starts late on that path. That
        # chain is only usable when its own first grid point lies within reach of the
        # pictures its window decodes - a rule measured, not assumed, and it belongs to
        # this spelling alone: the exact chain places its grid from the phase, so a
        # sampling time past the last picture is the ordinary tail hold, while this one
        # places its grid from a decimal ``start_time`` and can fall off the end of its
        # own arrivals. Measured on the fine-timescale fixture (container tick
        # 1/50000000, tempo 1000/1001, 25 fps out), one item and six arrivals: with the
        # grid 0.024 s past the last arrival the chain wrote 126 pictures, and 0.004 s
        # further out it wrote **none** - same decoded pictures, same ``tpad`` request,
        # an empty stream. An item provably in that shape is refused here, by name, for
        # the export and the still path alike.
        if picture_chain_reaches_its_first_grid_point(sampling) is False:
            raise RenderConfigError(
                f"Clip {clip.id} cannot be sampled on asset {clip.asset_id} stream "
                f"{clip.stream_index}: {sampling.tick_reason}; and its approximate "
                "picture chain would write no picture at all for this item, because its "
                f"first sampling time {float(sampling.sample_start):.6f}s lies past the "
                "last picture its reading window "
                f"[{float(sampling.seek_source):.6f}, "
                f"{float(sampling.read_source_end):.6f})s can put on the grid"
            )
        stages = [
            f"setpts=PTS/{decimal(clip.rate)}",
            f"fps=round=up:fps={rational(rate)}:start_time={decimal(sampling.grid_start)}",
        ]
        if sampling.head_gap:
            stages.append(f"tpad=start_mode=clone:start={sampling.head_gap}")
    else:
        tempo = sampling.tempo
        per_second = tempo.per_second
        lower = (clip.source_in - sampling.seek_source) * per_second
        upper = (clip.source_out - sampling.seek_source) * per_second
        # the seek already starts inside the slice when the lower bound has fallen
        # to or below the first readable frame, so only the upper bound can cut
        bounds = f"lt(pts,{tick_number(upper)})"
        if lower > 0:
            bounds = f"gte(pts,{tick_number(lower)})*{bounds}"
        if tempo.factored:
            # every stamp is a whole multiple of the tempo's numerator on this tick,
            # so the tempo and the grid's phase can be written as separate terms
            phase = "" if not tempo.phase else (
                f"-{tempo.phase}" if tempo.phase > 0 else f"+{-tempo.phase}"
            )
            retime = f"setpts=PTS*{tempo.scale}/{tempo.divisor}{phase}"
        else:
            # only the difference between a picture and the grid point divides, so the
            # tempo and the phase go inside one division: the whole expression is the
            # one value that has to come out whole
            constant = (
                f"-{tempo.offset}" if tempo.offset > 0
                else f"+{-tempo.offset}" if tempo.offset < 0 else ""
            )
            retime = f"setpts=(PTS*{tempo.scale}{constant})/{tempo.divisor}"
        stages = [
            f"settb=expr=1/{int(per_second)}",
            f"select='{bounds}'",
            retime,
            f"fps=round=up:fps={rational(rate)}:start_time=0",
        ]
    # generous at the tail: the clones sit past the last real picture, and the
    # trim below only reaches them when the lattice really asked for them
    stages.append(f"tpad=stop_mode=clone:stop={max(item.frames, 1)}")
    stages += [
        "setpts=PTS-STARTPTS",
        picture_scale_fit(plan.width, plan.height),
        "setsar=1",
        f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2",
        f"format={pixel_format}",
        f"trim=start_frame=0:end_frame={item.frames}",
    ]
    return ",".join(stages)


def encoder_available(ffmpeg: str, codec: str) -> bool:
    """True when this FFmpeg build can encode with ``codec``."""
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) > 1 and fields[1] == codec:
            return True
    return False


class GraphBuilder:
    """Builds one FFmpeg job for one plan."""

    def __init__(
        self,
        plan: RenderPlan,
        asset_paths: dict[str, Path],
        sources: dict[tuple[str, int], StreamSource],
        ffmpeg: str,
        time_base_of: TimeBaseLookup | None = None,
        first_frame_of: FirstFrameLookup | None = None,
        previous_frame_of: FrameLookup | None = None,
        window_pictures_of: WindowPicturesLookup | None = None,
    ) -> None:
        self.plan = plan
        self.asset_paths = asset_paths
        self.sources = sources
        self.ffmpeg = ffmpeg
        self.time_base_of = time_base_of
        self.first_frame_of = first_frame_of
        self.previous_frame_of = previous_frame_of
        self.window_pictures_of = window_pictures_of
        #: streams whose picture grid could not be anchored to an exact container
        #: timestamp, collected so the job can say so instead of hiding it
        self.unanchored: set[tuple[str, int]] = set()
        #: streams drawn with the container-quantised picture chain (see above)
        self.inexact_picture: set[tuple[str, int]] = set()
        self.layout = normalise_layout(plan.channel_layout)
        self.inputs: list[list[str]] = []
        self._input_ids: dict[tuple[str, int, Fraction, Fraction | None], int] = {}
        self.filter_parts: list[str] = []
        self._counter = 0

    # ---------------------------------------------------------------- shared
    def _label(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _stream_label(
        self, asset_id: str, stream_index: int, seek: Fraction, read: Fraction | None = None
    ) -> str:
        """One input per item slice, opened exactly and only as long as needed.

        Four facts about FFmpeg 8 input handling drive this, all measured (see
        ``tests/test_render_ffmpeg_features.py``,
        ``tests/test_render_audio_timing.py`` and
        ``tests/test_render_picture_timing.py``):

        * input ``-ss`` is measured from the **presentation start** of the file
          (``format.start_time``), not from 0, so the container start recorded in
          the source is subtracted. Seeking a file whose timeline begins at 2.5 s
          to ``-ss 0`` yields its first frame.
        * input ``-ss`` is frame accurate: it seeks to the preceding keyframe and
          decodes-and-discards, so no extra frame skipping is needed (and none
          should be added, which would drop real frames). What it discards is
          everything *before* the requested time, so a picture item has to be
          told where its sampling grid starts (``fps=start_time``) rather than
          left to anchor on whatever frame the seek happened to find.
        * input ``-t`` bounds the *reading* to that much material, on the file's
          own time axis, so nothing past the slice the plan asked for is ever
          decoded — which is what keeps material that belongs to nobody from
          leaking into the mix or the picture after the clip ends.
        * a picture input's ``-ss`` value is chosen to be a whole number of the
          container's time-base ticks, because FFmpeg quantises its own timestamp
          rebase to that base: measured on an MP4 with ``time_base`` 1/12800,
          asking for 0.333333333 s rebases by 4267/12800 s, which leaves the
          whole sampling grid up to half a tick out of phase.
        * filtergraph inputs are addressed as ``[in:v:N]`` / ``[in:a:N]``: type
          plus position within that type, which is *not* the container stream
          index kept in the domain model. An unrecorded position is refused
          rather than guessed.
        """
        path = self.asset_paths.get(asset_id)
        if path is None:
            raise RenderConfigError(
                f"Asset {asset_id} is not available for rendering (renamed or moved? "
                "relink it before exporting)"
            )
        source = self._source(asset_id, stream_index)
        input_seek = max(seek - source.input_start, F(0))
        key = (asset_id, stream_index, input_seek, read)
        index = self._input_ids.get(key)
        if index is None:
            index = len(self.inputs)
            self._input_ids[key] = index
            group = ["-ss", decimal(input_seek)]
            if read is not None:
                group += ["-t", decimal(read)]
            group += ["-i", str(path)]
            self.inputs.append(group)
        kind = "v" if source.kind is TrackKind.VIDEO else "a"
        return f"[{index}:{kind}:{source.stream_position}]"

    def _time_base(self, asset_id: str, stream_index: int) -> Fraction | None:
        """Container time base of a stream, when a lookup was provided.

        ``None`` (no lookup, or the probe failed) is not fatal: the seek then goes
        to the sampling time itself, and FFmpeg rounds its own timestamp rebase to
        whatever the container's time base is, so the sampling grid can be up to
        half a tick out of phase. Every affected stream is recorded in
        :attr:`unanchored` so the job says so instead of hiding it.
        """
        if self.time_base_of is None:
            return None
        try:
            base = self.time_base_of(asset_id, stream_index)
        except Exception:  # a probe failure must not lose the render
            return None
        return base if base and base > 0 else None

    def _first_frame(self, clip: Clip) -> Fraction | None:
        """First real picture at or after this clip's start, when it can be known.

        Only ``picture_sampling``'s head-hold count needs it, and only to be
        *exact*: without it the count is inferred from the lattice (never more
        than one frame) instead of measured from the stream.
        """
        if self.first_frame_of is None:
            return None
        try:
            found = self.first_frame_of(clip.asset_id, clip.stream_index, clip.source_in)
        except Exception:  # as above: a failed probe degrades, it does not fail
            return None
        if found is None or found > clip.source_out:
            return None  # nothing readable in the slice; the plan's bounds decide
        return found

    def _previous_frame(self, clip: Clip, source_time: Fraction) -> Fraction | None:
        """Last real picture at or before ``source_time``, when it can be known.

        Measured evidence, so the reading window starts where the first grid point
        actually needs it rather than at an estimate of frame spacing; a stream the
        index has not read, or a lookup that failed, simply falls back to the
        nominal preroll.
        """
        if self.previous_frame_of is None:
            return None
        try:
            found = self.previous_frame_of(clip.asset_id, clip.stream_index, source_time)
        except Exception:  # as above: a failed probe degrades, it does not fail
            return None
        if found is None or found < clip.source_in:
            return None  # outside the slice: reading still starts at the edge
        return found

    def _window_pictures(
        self, clip: Clip, window: ReadWindow
    ) -> tuple[Fraction, ...] | None:
        """Every real picture the reading window can hand over, when all of it was read.

        The exact tempo of an item whose container counts time more finely than its
        tempo can be divided on is a statement about *these* timestamps
        (:func:`~video_editor.render.plan.picture_tempo`), so it is only made when they
        were actually measured and the read went all the way across the window. A
        lookup that is absent, failed, or only partly read answers ``None``: partial
        evidence is not evidence about the pictures that were not read.
        """
        if self.window_pictures_of is None:
            return None
        try:
            found = self.window_pictures_of(
                clip.asset_id, clip.stream_index,
                window.seek_source, window.generous_source_end,
            )
        except Exception:  # as above: a failed probe degrades, it does not fail
            return None
        # an empty answer is still an answer: the chain read the window and the stream
        # has nothing in it. Only a missing or failed lookup is "not measured".
        return found

    def _source(self, asset_id: str, stream_index: int) -> StreamSource:
        """The registered stream, with the addressing data rendering requires."""
        source = self.sources.get((asset_id, stream_index))
        if source is None:
            raise RenderConfigError(
                f"Stream {stream_index} of asset {asset_id} is not registered in the "
                "project; it cannot be addressed for rendering"
            )
        if source.stream_position is None or source.stream_position < 0:
            raise RenderConfigError(
                f"Stream {stream_index} of asset {asset_id} has no recorded stream "
                "position; reimport the asset so rendering can address it exactly"
            )
        return source

    def _geometry(self) -> str:
        return f"{self.plan.width}x{self.plan.height}"

    # --------------------------------------------------------------- picture
    def build_video(self) -> str:
        labels = [
            self._black_item(item) if item.is_black else self._picture_item(item)
            for item in self.plan.video_items
        ]
        if not labels:
            return self._black_item(VideoItem(start_frame=0, frames=self.plan.total_frames))
        if len(labels) == 1:
            return labels[0]
        out = self._label("v")
        chain = "".join(f"[{label}]" for label in labels)
        self.filter_parts.append(
            f"{chain}concat=n={len(labels)}:v=1:a=0,format=yuv420p[{out}]"
        )
        return out

    def _black_item(self, item: VideoItem) -> str:
        out = self._label("black")
        rate = self.plan.quantisation.frame_rate
        duration = F(item.frames) / rate
        self.filter_parts.append(
            f"color=c=black:s={self._geometry()}:r={rational(rate)}:d={decimal(duration)},"
            f"trim=start_frame=0:end_frame={item.frames},"
            f"setsar=1,format=yuv420p[{out}]"
        )
        return out

    def _picture_item(self, item: VideoItem) -> str:
        clip = item.clip
        assert clip is not None
        source = self.sources.get((clip.asset_id, clip.stream_index))
        if source is None:
            raise RenderConfigError(
                f"Stream {clip.stream_index} of asset {clip.asset_id} is unknown"
            )
        if source.kind.value != "video":
            raise RenderConfigError(
                f"Clip {clip.id} uses a {source.kind.value} stream as picture"
            )
        rate = self.plan.quantisation.frame_rate
        base = self._time_base(clip.asset_id, clip.stream_index)
        preroll = picture_preroll(clip, source, rate)
        previous = self._previous_frame(clip, item.source_start)
        # where this item reads, decided by the plan's own arithmetic, so the probe
        # below is asked for exactly the window the chain will decode
        window = picture_read_window(clip, item, rate, base, preroll, previous)
        sampling = picture_sampling(
            clip,
            item,
            rate,
            time_base=base,
            preroll=preroll,
            first_frame=self._first_frame(clip),
            previous_frame=previous,
            window_pictures=self._window_pictures(clip, window),
        )
        if not sampling.anchored:
            self.unanchored.add((clip.asset_id, clip.stream_index))
        elif sampling.tempo is None:
            self.inexact_picture.add((clip.asset_id, clip.stream_index))
        label_in = self._stream_label(
            clip.asset_id,
            clip.stream_index,
            sampling.seek_source,
            read=sampling.read_duration,
        )
        out = self._label("v")
        chain = picture_filter_chain(self.plan, clip, item, sampling, "yuv420p")
        self.filter_parts.append(f"{label_in}{chain}[{out}]")
        return out

    # ----------------------------------------------------------------- audio
    def build_audio(self) -> str:
        total = self.plan.total_samples
        if total <= 0:
            total = 1  # containers want *some* audio track; one sample is silent
        if not self.plan.audio_items:
            return self._pure_silence(total)
        labels = [self._audio_item(item) for item in self.plan.audio_items]
        if len(labels) == 1:
            out = self._label("a")
            self.filter_parts.append(
                f"[{labels[0]}]atrim=start_sample=0:end_sample={total},"
                f"{self._aformat()}[{out}]"
            )
            return out
        out = self._label("a")
        chain = "".join(f"[{label}]" for label in labels)
        self.filter_parts.append(
            f"{chain}amix=inputs={len(labels)}:normalize=0:duration=longest,"
            f"atrim=start_sample=0:end_sample={total},{self._aformat()}[{out}]"
        )
        return out

    def _aformat(self) -> str:
        """Pin rate, layout and sample format, so later counts are unambiguous.

        ``fltp`` is not a cosmetic choice: the fades and the gain are applied in
        float, so a ramp is not rounded to the source's integer format.
        """
        q = self.plan.quantisation
        return (
            f"aformat=sample_fmts=fltp:sample_rates={q.sample_rate}"
            f":channel_layouts={self.layout}"
        )

    def _pure_silence(self, total: int) -> str:
        q = self.plan.quantisation
        out = self._label("silence")
        self.filter_parts.append(
            f"anullsrc=r={q.sample_rate}:cl={self.layout},"
            f"atrim=start_sample=0:end_sample={total},{self._aformat()}[{out}]"
        )
        return out

    def _audio_item(self, item: AudioItem) -> str:
        clip = item.clip
        source = self.sources.get((clip.asset_id, clip.stream_index))
        if source is None:
            raise RenderConfigError(
                f"Stream {clip.stream_index} of asset {clip.asset_id} is unknown"
            )
        if source.kind.value != "audio":
            raise RenderConfigError(
                f"Clip {clip.id} uses a {source.kind.value} stream as sound"
            )
        # The clip is built in full, from its own start, however much of it this
        # render range actually uses: that is what makes fades, tempo phase and
        # sample counts come out the same whether the composition is rendered in
        # one pass, in chunks, or as a preview of a few seconds.
        #
        # The reading bound deliberately overshoots the slice. An input ``-t`` is
        # applied to whole packets, and a bound that lands exactly on the clip's
        # end decides the last packet out (measured: a 0.04 s slice of an AAC
        # stream decoded *one* sample with ``-t 0.04``, all 1920 with a generous
        # bound). Exactness does not come from the input bound at all: it comes
        # from the ``atrim`` sample counts below, which are taken from the plan.
        label_in = self._stream_label(
            clip.asset_id,
            clip.stream_index,
            clip.source_in,
            read=clip.source_duration + AUDIO_READ_OVERREAD,
        )
        q = self.plan.quantisation
        chain = ["asetpts=PTS-STARTPTS"]
        # Bound the read on the source's own sample axis. ``end_sample`` is the
        # only unambiguous way to do this here: at this point in the chain the
        # samples are still the *source's* samples, and a count taken from the
        # plan (which counts output samples) would keep the wrong amount of
        # audio across a sample-rate change. Rounded up: input bounds may be
        # generous, the output count below is what is exact.
        cap = self._source_sample_cap(item, source)
        if cap is not None:
            chain.append(f"atrim=start_sample=0:end_sample={cap}")
        # Convert rate and layout *before* any sample counting, so that every
        # sample count from here on means output samples.
        chain.append(f"aresample={q.sample_rate}")
        chain.append(self._aformat())
        chain.extend(atempo_chain(clip.rate))
        # exactly the clip's output length, whatever tempo/resample edges produced
        chain.append("apad")
        chain.append(f"atrim=start_sample=0:end_sample={item.clip_samples}")
        chain.extend(self._fades(item))
        if clip.gain != 1:
            chain.append(f"volume=volume={decimal(clip.gain)}")
        if item.pre_samples or item.samples < item.clip_samples:
            chain.append(
                f"atrim=start_sample={item.pre_samples}"
                f":end_sample={item.pre_samples + item.samples}"
            )
            # ``atrim`` keeps the timestamps of what it kept, and this cut throws
            # away the part of the clip before the render range: without rebasing,
            # the muxer is handed a stream that starts seconds in, and the
            # container comes out longer than the composition.
            chain.append("asetpts=PTS-STARTPTS")
        sliced = self._label("as")
        self.filter_parts.append(f"{label_in}{','.join(chain)}[{sliced}]")
        if item.start_sample <= 0:
            placed = sliced
        else:
            silence = self._label("sil")
            self.filter_parts.append(
                f"anullsrc=r={q.sample_rate}:cl={self.layout},"
                f"atrim=start_sample=0:end_sample={item.start_sample},{self._aformat()}"
                f"[{silence}]"
            )
            placed = self._label("ap")
            self.filter_parts.append(
                f"[{silence}][{sliced}]concat=n=2:v=0:a=1[{placed}]"
            )
        # Padded to the whole range before mixing so every contribution is
        # anchored to the same output sample 0; the mix is then cut once.
        return self._pad_tail(placed)

    def _source_sample_cap(self, item: AudioItem, source: StreamSource) -> int | None:
        """Source-rate sample count covering this clip's whole source slice.

        ``None`` when the stream's sample rate was never recorded: then the
        input-side ``-t`` bound is all that can be promised, and that is stated
        rather than assumed.
        """
        if not source.sample_rate or source.sample_rate <= 0:
            return None
        samples = item.clip.source_duration * source.sample_rate
        return -((-samples.numerator) // samples.denominator)  # ceil

    def _fades(self, item: AudioItem) -> list[str]:
        """Sample-accurate fades over the whole clip, matching the domain envelope.

        ``afade`` addressed by start *sample* and *length* scales the k-th
        sample of the ramp by exactly k/N, which is what
        ``core.model.envelope_gain`` computes; ``volume`` with ``eval=frame``
        re-evaluates once per filter frame (a whole buffer of samples), so it
        cannot follow a short fade honestly.
        """
        parts: list[str] = []
        if item.fade_in_samples > 0:
            parts.append(
                f"afade=t=in:curve=tri:start_sample=0:nb_samples={item.fade_in_samples}"
            )
        if item.fade_out_samples > 0:
            parts.append(
                f"afade=t=out:curve=tri:start_sample={item.fade_out_start_sample}"
                f":nb_samples={item.fade_out_samples}"
            )
        return parts

    def _pad_tail(self, label: str) -> str:
        """Extend a contribution to the full range so the mix stays anchored."""
        out = self._label("at")
        self.filter_parts.append(
            f"[{label}]apad,atrim=start_sample=0:end_sample={self.plan.total_samples}[{out}]"
        )
        return out


def build_job(
    plan: RenderPlan,
    asset_paths: dict[str, Path],
    sources: dict[tuple[str, int], StreamSource],
    destination: Path,
    ffmpeg: str,
    preset: CodecPreset = MP4_H264_AAC,
    time_base_of: TimeBaseLookup | None = None,
    first_frame_of: FirstFrameLookup | None = None,
    previous_frame_of: FrameLookup | None = None,
    window_pictures_of: WindowPicturesLookup | None = None,
) -> RenderJob:
    """Full FFmpeg argv for one plan (single process, one encoder pass).

    ``time_base_of``, ``first_frame_of``, ``previous_frame_of`` and
    ``window_pictures_of`` are the stream measurements the picture chain needs to place
    its sampling grid exactly, to prove its tempo against the real picture stamps of
    the reading window, and to start reading where the first grid point actually needs
    material (see :func:`picture_filter_chain`); they are optional, and a graph that
    could not get them says so in :attr:`RenderJob.unanchored` /
    :attr:`RenderJob.inexact_picture` rather than pretending the picture is exact.
    """
    builder = GraphBuilder(
        plan, asset_paths, sources, ffmpeg, time_base_of, first_frame_of,
        previous_frame_of, window_pictures_of,
    )
    video_label = builder.build_video()
    audio_label = builder.build_audio()
    q = plan.quantisation
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    for group in builder.inputs:
        args.extend(group)
    args.extend(["-filter_complex", ";".join(builder.filter_parts)])
    args.extend(["-map", f"[{video_label}]", "-map", f"[{audio_label}]"])
    args.extend(["-f", preset.muxer])
    args.extend(["-c:v", preset.video_codec, "-pix_fmt", preset.pixel_format])
    args.extend(preset.extra_video_args)
    args.extend(["-r", rational(q.frame_rate)])
    args.extend(["-c:a", preset.audio_codec, "-ar", str(q.sample_rate)])
    args.extend(preset.extra_audio_args)
    if preset.muxer == "mp4":
        args.extend(["-movflags", "+faststart"])
    args.extend(["-progress", "pipe:1", "-nostats", str(destination)])
    return RenderJob(
        args=args,
        duration=plan.duration,
        video_items=len(plan.video_items),
        audio_items=len(plan.audio_items),
        warnings=plan.warnings,
        unanchored=frozenset(builder.unanchored),
        inexact_picture=frozenset(builder.inexact_picture),
    )

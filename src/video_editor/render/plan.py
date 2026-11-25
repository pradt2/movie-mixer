"""Pure render-plan construction from an immutable ``Project`` snapshot.

No FFmpeg, no filesystem, no Qt: the plan is plain data shared by the preview
and export paths, so what you see and what you export come from the same
composition semantics.

Quantisation policy (defined once here, used by every backend)
--------------------------------------------------------------
* Output positions are addressed by a **global index over the whole render
  range**: ``frame_index(t) = round_half_up(t * frame_rate)``,
  ``frame_time(i) = i / frame_rate`` (same for samples). Item durations are
  differences of those indices, never independently rounded durations, so
  rounding error cannot accumulate: every boundary lies within **½ output
  frame** (or ½ sample) of the exact composition boundary, at any point of a
  multi-hour film.
* Exact ``Fraction`` times are kept next to the quantised indices: source
  slices are computed from the quantised *output* frame/sample times (that is
  what is actually presented), and fade anchors stay at the clip's absolute
  output edges, so splitting a render into chunks cannot restart or shorten a
  fade.
* Container encoder padding (AAC priming/delay) is outside this module and is
  checked by the exporter against the rendered file's duration.

The per-item audio contract
---------------------------
``AudioItem`` states everything the audio graph has to satisfy, so the graph
never has to work out a duration itself (guessing where a *sample* count is
counted is how a sample-rate change used to steal or drop material):

* which source interval to read — ``clip.source_in``/``clip.source_out`` in
  seconds on the **stream's own** time axis, always inside the recorded stream
  bounds (``_check_source_slice`` refuses a clip that reaches past them, because
  the only way to fill the plan would then be invented silence);
* the tempo (``clip.rate``) and the exact **output** sample counts: the whole
  clip (``clip_samples``) and the range-clipped contribution inside it
  (``start_sample``/``samples``, with ``pre_samples`` saying how many clip
  samples to discard after shaping);
* the clip's fades in output samples (``fade_in_samples``/``fade_out_samples``),
  anchored to the whole clip rather than to the render range, so a chunk or
  preview window that starts mid-fade continues the ramp instead of restarting
  it. A fade is rounded half-up to whole samples, and a positive fade is never
  rounded down to zero — the domain envelope says the clip's first sample is
  silent, and that stays true.

Composition rules (restated for rendering)
------------------------------------------
* Picture: topmost *enabled* video track wins per output frame; uncovered
  areas become explicit black items.
* Audio: every enabled, unmuted track contributes; contributions are summed
  with the clip gain and the domain envelope (``core.model.envelope_gain``).
  Gaps are explicit silence — ``total_samples`` does not depend on what plays.
* Muted/disabled tracks never shorten the output: the range is the requested
  interval (default ``[0, project.output_end())``).
* The mix is a plain sum. No per-clip normalisation; possible overload is
  reported as a plan warning and saturated at the sample-format boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise

from ..core.model import Clip, Project, StreamSource, TrackKind, envelope_gain
from ..core.timeutils import ONE, ZERO, overlaps

F = Fraction


class RenderPlanError(Exception):
    """The project (or requested range) cannot be rendered correctly."""


# --------------------------------------------------------------------- time


def round_half_up(value: Fraction) -> int:
    """Deterministic, sign-safe half-up rounding for all quantisation."""
    return int((value * 2 + 1) // 2)


#: Smallest picture the render path will produce. Below this a preview is both
#: useless to look at and outside what scalers/encoders handle sanely.
MIN_DIMENSION = 16


def plan_geometry(
    width: int, height: int, scale: Fraction = ONE
) -> tuple[int, int, str | None]:
    """Resolve the picture size to render at, always even.

    ``scale`` shrinks (or grows) the picture **only**: it never touches output
    time, so a half-size preview has exactly the same cuts, stacking, gaps and
    audio as the full-size export. Dimensions are rounded half up and then to an
    even number because ``yuv420p`` cannot address odd dimensions and x264 refuses
    them; the returned warning documents any such adjustment instead of letting it
    happen silently.
    """
    if scale <= 0:
        raise RenderPlanError(f"Geometry scale {scale} must be positive")
    if width <= 0 or height <= 0:
        raise RenderPlanError(f"Output size {width}x{height} must be positive")
    scaled_w = max(MIN_DIMENSION, round_half_up(width * scale))
    scaled_h = max(MIN_DIMENSION, round_half_up(height * scale))
    warning: str | None = None
    if scaled_w % 2 or scaled_h % 2:
        even_w = max(MIN_DIMENSION, scaled_w - scaled_w % 2)
        even_h = max(MIN_DIMENSION, scaled_h - scaled_h % 2)
        warning = (
            f"Output size {width}x{height} renders at {even_w}x{even_h}: 4:2:0 picture "
            "needs even dimensions"
        )
        scaled_w, scaled_h = even_w, even_h
    return scaled_w, scaled_h, warning


@dataclass(frozen=True)
class Quantisation:
    """Exact output-time ↔ global output index mapping for one render range."""

    frame_rate: Fraction
    sample_rate: int

    def frame_index(self, t: Fraction) -> int:
        return round_half_up(t * self.frame_rate)

    def frame_time(self, index: int) -> Fraction:
        return F(index) / self.frame_rate

    def frame_count(self, seconds: Fraction) -> int:
        """How many frames a length of ``seconds`` yields: ``ceil(seconds·rate)``.

        This is deliberately *not* :meth:`frame_index`. ``frame_index`` names the
        global frame at a position (rounded, because a boundary belongs to exactly
        one frame); counting a length must not round half up, or a 0.02 s length at
        25 fps would claim a whole frame and a preview would promise media that the
        render path cannot produce. Rounding up is also what keeps the count equal
        to the number of indices a range spans in every other case.
        """
        if seconds <= 0:
            return 0
        value = seconds * self.frame_rate
        return -((-value.numerator) // value.denominator)

    def sample_index(self, t: Fraction) -> int:
        return round_half_up(t * self.sample_rate)

    def sample_time(self, index: int) -> Fraction:
        return F(index) / self.sample_rate


# ---------------------------------------------------------------------- items


@dataclass(frozen=True)
class VideoItem:
    """Exactly ``frames`` output frames; black when ``clip`` is None."""

    start_frame: int  # relative to the render range start
    frames: int
    clip: Clip | None = None
    #: exact asset-local source slice presented during this item
    source_start: Fraction = ZERO
    source_end: Fraction = ZERO
    #: exact (quantised) output interval this item covers
    output_start: Fraction = ZERO
    output_end: Fraction = ZERO

    @property
    def is_black(self) -> bool:
        return self.clip is None

    @property
    def end_frame(self) -> int:
        return self.start_frame + self.frames

    @property
    def duration_frames(self) -> int:
        return self.frames


@dataclass(frozen=True)
class AudioItem:
    """One contribution: ``samples`` samples starting at ``start_sample``.

    The fields here are the whole contract the audio graph has to satisfy, in
    **output samples**: the source slice to read (in exact source seconds, on the
    stream's own time axis), the tempo to apply, the output sample count to
    produce, and where the clip's own fades fall inside it. The graph does no
    arithmetic of its own — that is what made the old chain depend on where
    FFmpeg happened to insert a resampler.
    """

    clip: Clip
    start_sample: int  # relative to the render range start
    samples: int
    source_start: Fraction
    source_end: Fraction
    #: exact output interval this contribution covers (range-clipped)
    output_start: Fraction
    output_end: Fraction
    #: Whole-clip context, in **output samples**. Every count the audio graph
    #: uses is one of these, so the graph never has to convert a sample count
    #: into source time (that conversion is where the old chain lost or stole
    #: material at a sample-rate change). ``clip_start_sample`` is negative when
    #: the render range begins inside the clip: the clip is still built in full,
    #: its fades are applied over the whole clip, and only then is the range cut
    #: out — which is what lets a preview chunk that starts mid-fade continue
    #: the ramp instead of restarting it.
    clip_start_sample: int = 0
    clip_samples: int = 0
    #: fade lengths quantised to whole output samples against ``clip_samples``
    fade_in_samples: int = 0
    fade_out_samples: int = 0

    @property
    def end_sample(self) -> int:
        return self.start_sample + self.samples

    @property
    def pre_samples(self) -> int:
        """Clip samples before the render range, to be discarded after shaping."""
        return self.start_sample - self.clip_start_sample

    @property
    def fade_out_start_sample(self) -> int:
        """Where the fade-out ramp begins, counted from the start of the clip."""
        return self.clip_samples - self.fade_out_samples

    def level_at(self, output_time: Fraction) -> Fraction:
        """Clip gain times the domain envelope at an exact output time."""
        return self.clip.gain * envelope_gain(self.clip, output_time)


@dataclass(frozen=True)
class RenderPlan:
    """Complete description of one render range."""

    output_start: Fraction
    output_end: Fraction
    quantisation: Quantisation
    total_frames: int
    total_samples: int
    video_items: tuple[VideoItem, ...]
    audio_items: tuple[AudioItem, ...]
    #: output geometry / layout taken from the project's output settings
    width: int = 1920
    height: int = 1080
    channel_layout: str = "stereo"
    warnings: tuple[str, ...] = ()

    @property
    def duration(self) -> Fraction:
        return self.output_end - self.output_start

    def picture_at(self, output_time: Fraction) -> VideoItem | None:
        frame = self.quantisation.frame_index(output_time) - self.quantisation.frame_index(
            self.output_start
        )
        for item in self.video_items:
            if item.start_frame <= frame < item.end_frame:
                return item
        return None

    def audio_at(self, output_time: Fraction) -> tuple[AudioItem, ...]:
        sample = self.quantisation.sample_index(output_time) - self.quantisation.sample_index(
            self.output_start
        )
        return tuple(a for a in self.audio_items if a.start_sample <= sample < a.end_sample)

    def peak_audio_level(self, output_time: Fraction) -> Fraction:
        return sum((item.level_at(output_time) for item in self.audio_at(output_time)), ZERO)


# ------------------------------------------------------------------ building


def build_plan(
    project: Project,
    out_start: Fraction = ZERO,
    out_end: Fraction | None = None,
    geometry_scale: Fraction = ONE,
) -> RenderPlan:
    """Resolve ``project`` for the half-open range ``[out_start, out_end)``.

    ``out_end`` defaults to ``project.output_end()``, which counts clips on
    muted and disabled tracks, so toggling a track never shortens the output.

    ``geometry_scale`` multiplies the project's picture size (see
    :func:`plan_geometry`); timing, stacking, gaps and audio are unaffected, which
    is what lets the preview path reuse these semantics instead of inventing a
    second set of rules.
    """
    if out_start < 0:
        raise RenderPlanError("Render range start must be non-negative")
    range_end = out_end if out_end is not None else project.output_end()
    if range_end < out_start:
        raise RenderPlanError("Render range end is before its start")

    quant = Quantisation(project.output.frame_rate, project.output.sample_rate)
    base_frame = quant.frame_index(out_start)
    base_sample = quant.sample_index(out_start)
    total_frames = quant.frame_index(range_end) - base_frame
    total_samples = quant.sample_index(range_end) - base_sample

    warnings: list[str] = []
    width, height, geometry_warning = plan_geometry(
        project.output.width, project.output.height, geometry_scale
    )
    if geometry_warning:
        warnings.append(geometry_warning)
    _check_sources(project, out_start, range_end, warnings)

    video_items = _plan_video(project, out_start, range_end, quant, base_frame)
    audio_items = _plan_audio(project, out_start, range_end, quant, base_sample)
    peak = _peak_level(audio_items)
    if peak > 1:
        warnings.append(
            f"Overlapping audio can sum to {float(peak):.2f}x full scale; the mix is a "
            "plain sum, so samples above full scale are saturated (no normalisation)"
        )

    return RenderPlan(
        output_start=out_start,
        output_end=range_end,
        quantisation=quant,
        total_frames=total_frames,
        total_samples=total_samples,
        video_items=video_items,
        audio_items=audio_items,
        width=width,
        height=height,
        channel_layout=project.output.channel_layout,
        warnings=tuple(warnings),
    )


def _check_sources(
    project: Project, out_start: Fraction, out_end: Fraction, warnings: list[str]
) -> None:
    """Refuse material we could not render honestly; warn on soft guarantees."""
    for clip in project.clips_in_interval(out_start, out_end):
        source = project.source(clip.asset_id, clip.stream_index)
        if source is None:
            raise RenderPlanError(
                f"Clip {clip.id} references stream {clip.stream_index} of asset "
                f"{clip.asset_id}, which is not registered in the project"
            )
        if not source.placeable:
            raise RenderPlanError(
                f"Clip {clip.id} uses stream {clip.stream_index}, whose bounds are "
                "unknown; it cannot be rendered"
            )
        track = project.track(clip.track_id)
        if track is not None and source.kind is not track.kind:
            raise RenderPlanError(
                f"Clip {clip.id} puts a {source.kind.value} stream on a "
                f"{track.kind.value} track"
            )
        _check_source_slice(clip, source)
        if source.conservative_end and clip.source_out >= source.end:
            warnings.append(
                f"Clip {clip.id} runs to the container-derived end of asset "
                f"{clip.asset_id} stream {clip.stream_index}; that end is an upper "
                "bound, so the final frames may run short"
            )
        _check_layout(clip, source, project, warnings)


def _check_source_slice(clip: Clip, source: StreamSource) -> None:
    """Refuse a clip that asks for material past the end of its stream.

    This is the difference between *padding* and *inventing*: the graph always
    produces the exact number of samples the plan promises, so material the
    stream does not contain would silently arrive as silence. Anything beyond a
    single source sample (the largest quantum a boundary artefact can be) is
    treated as a real gap in the source and refused.
    """
    assert source.end is not None  # placeable, checked by the caller
    tolerance = F(1) / source.sample_rate if source.sample_rate else ZERO
    missing = clip.source_out - source.end
    if missing > tolerance:
        raise RenderPlanError(
            f"Clip {clip.id} needs {float(missing):.6f}s past the end of asset "
            f"{clip.asset_id} stream {clip.stream_index} (which runs to "
            f"{float(source.end)}s on its own time axis). Rendering it would replace "
            "that much of the clip with silence; shorten the clip or use another source."
        )
    before = source.origin - clip.source_in
    if before > tolerance:
        raise RenderPlanError(
            f"Clip {clip.id} starts {float(before):.6f}s before the beginning of "
            f"asset {clip.asset_id} stream {clip.stream_index} (which starts at "
            f"{float(source.origin)}s); rendering would start the clip with silence."
        )


def _check_layout(
    clip: Clip, source: StreamSource, project: Project, warnings: list[str]
) -> None:
    """Warn only where the channel remap really is a guess.

    Changing the number of channels is normal (a mono commentary into a stereo
    mix), and FFmpeg's down/up-mix for the layouts we support is documented in
    ``docs/architecture.md``, so this stays quiet about it. What would be a
    silent invention is a source whose layout was never recorded, or one that
    names *side* speakers: FFmpeg distinguishes side from rear, and folding a
    ``5.1(side)`` stream into a ``5.1`` mix (or into stereo) reinterprets those
    speakers, so the plan says so instead of pretending they match.
    """
    have = source.channel_layout
    want = project.output.channel_layout
    if source.kind is not TrackKind.AUDIO or have == want:
        return
    if not have:
        # a missing *name* is harmless between mono and stereo: the channel count
        # is still in the container and FFmpeg's duplicate/average is documented.
        # Into a surround mix it is a guess about where sound belongs, so that
        # case is said out loud.
        if want not in ("mono", "stereo"):
            warnings.append(
                f"Clip {clip.id} plays an audio stream with no recorded channel layout "
                f"into a {want!r} output: its channels may be remapped in a way this "
                "project cannot describe; reimport the asset if the mix matters"
            )
    elif "side" in have and want not in ("mono", "stereo"):
        warnings.append(
            f"Clip {clip.id} plays a {have} stream into a {want} output. Side and "
            "rear speakers are different positions: the mix may place that material "
            "where you did not intend it"
        )


def _plan_video(
    project: Project,
    out_start: Fraction,
    out_end: Fraction,
    quant: Quantisation,
    base_frame: int,
) -> tuple[VideoItem, ...]:
    """Partition on *video* clip boundaries only, then resolve stacking."""
    enabled_video = [
        c
        for c in project.clips
        if (t := project.track(c.track_id)) is not None
        and t.kind is TrackKind.VIDEO
        and t.enabled
        and overlaps(c.output_start, c.output_end, out_start, out_end)
    ]
    boundaries: set[Fraction] = {out_start, out_end}
    for clip in enabled_video:
        for edge in (clip.output_start, clip.output_end):
            if out_start < edge < out_end:
                boundaries.add(edge)

    items: list[VideoItem] = []
    for begin, finish in pairwise(sorted(boundaries)):
        start_frame = quant.frame_index(begin) - base_frame
        end_frame = quant.frame_index(finish) - base_frame
        if end_frame <= start_frame:
            continue  # boundary pair inside half a frame: no output frame at all
        exact_start = quant.frame_time(start_frame + base_frame)
        exact_end = quant.frame_time(end_frame + base_frame)
        winner = project.video_clip_at((begin + finish) / 2)
        if winner is None:
            items.append(
                VideoItem(
                    start_frame=start_frame,
                    frames=end_frame - start_frame,
                    output_start=exact_start,
                    output_end=exact_end,
                )
            )
            continue
        items.append(
            VideoItem(
                start_frame=start_frame,
                frames=end_frame - start_frame,
                clip=winner,
                source_start=winner.source_time_at(exact_start),
                source_end=winner.source_time_at(exact_end),
                output_start=exact_start,
                output_end=exact_end,
            )
        )
    return _merge_video_items(items)


def _merge_video_items(items: list[VideoItem]) -> tuple[VideoItem, ...]:
    """Join consecutive frames that come from the same slice (smaller graphs)."""
    merged: list[VideoItem] = []
    for item in items:
        last = merged[-1] if merged else None
        if last is None or last.end_frame != item.start_frame:
            merged.append(item)
            continue
        if last.is_black and item.is_black:
            merged[-1] = VideoItem(
                start_frame=last.start_frame,
                frames=last.frames + item.frames,
                output_start=last.output_start,
                output_end=item.output_end,
            )
        elif (
            not (last.is_black or item.is_black)
            and last.clip is item.clip
            and last.source_end == item.source_start
        ):
            merged[-1] = VideoItem(
                start_frame=last.start_frame,
                frames=last.frames + item.frames,
                clip=last.clip,
                source_start=last.source_start,
                source_end=item.source_end,
                output_start=last.output_start,
                output_end=item.output_end,
            )
        else:
            merged.append(item)
    return tuple(merged)


def _fade_samples(duration: Fraction, sample_rate: int) -> int:
    """Fade length in whole output samples.

    Half-up, and never zero for a positive fade: the domain envelope says the
    clip's first sample is silent, so rounding a very short fade away would
    disagree with ``core.model.envelope_gain`` about what the first sample is.
    """
    if duration <= 0:
        return 0
    return max(1, round_half_up(duration * sample_rate))


def _plan_audio(
    project: Project,
    out_start: Fraction,
    out_end: Fraction,
    quant: Quantisation,
    base_sample: int,
) -> tuple[AudioItem, ...]:
    items: list[AudioItem] = []
    rate_hz = quant.sample_rate
    for clip in project.clips:
        track = project.track(clip.track_id)
        if (
            track is None
            or track.kind is not TrackKind.AUDIO
            or not track.enabled
            or track.muted
            or not overlaps(clip.output_start, clip.output_end, out_start, out_end)
        ):
            continue
        begin = max(clip.output_start, out_start)
        finish = min(clip.output_end, out_end)
        clip_start_sample = quant.sample_index(clip.output_start) - base_sample
        clip_samples = quant.sample_index(clip.output_end) - quant.sample_index(
            clip.output_start
        )
        start_sample = quant.sample_index(begin) - base_sample
        end_sample = quant.sample_index(finish) - base_sample
        if end_sample <= start_sample:
            continue
        fade_in = _fade_samples(clip.fade_in, rate_hz)
        fade_out = _fade_samples(clip.fade_out, rate_hz)
        # quantisation can add up to one sample to each ramp; the fades still
        # have to fit inside the clip, so the shorter of the two gives way
        if fade_in + fade_out > clip_samples:
            fade_in = min(fade_in, clip_samples)
            fade_out = min(fade_out, clip_samples - fade_in)
        items.append(
            AudioItem(
                clip=clip,
                start_sample=start_sample,
                samples=end_sample - start_sample,
                source_start=clip.source_time_at(quant.sample_time(start_sample + base_sample)),
                source_end=clip.source_time_at(quant.sample_time(end_sample + base_sample)),
                output_start=begin,
                output_end=finish,
                clip_start_sample=clip_start_sample,
                clip_samples=clip_samples,
                fade_in_samples=fade_in,
                fade_out_samples=fade_out,
            )
        )
    return tuple(sorted(items, key=lambda a: (a.start_sample, a.clip.id)))


def _peak_level(audio_items: tuple[AudioItem, ...]) -> Fraction:
    """Maximum summed level, evaluated at every level-change point."""
    points: set[Fraction] = set()
    for item in audio_items:
        clip = item.clip
        points.add(item.output_start)
        if clip.fade_in > 0:
            points.add(clip.output_start + clip.fade_in)
        if clip.fade_out > 0:
            points.add(clip.output_end - clip.fade_out)
        points.add(min(clip.output_end, item.output_start + F(1, 1000)))
    peak = ZERO
    for point in points:
        peak = max(peak, sum((i.level_at(point) for i in audio_items if _covers(i, point)), ZERO))
    return peak


def _covers(item: AudioItem, output_time: Fraction) -> bool:
    return item.output_start <= output_time < item.output_end

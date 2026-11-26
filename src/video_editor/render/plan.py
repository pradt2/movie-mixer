"""Pure render-plan construction from an immutable ``Project`` snapshot.

No FFmpeg, no filesystem, no Qt: the plan is plain data shared by the preview
and export paths, so what you see and what you export come from the same
composition semantics.

Two questions about output time (defined once here, used by every backend)
--------------------------------------------------------------------------
Asking "which frames does this **render range** contain?" and asking "which frame
does this **playhead** show?" are different questions and get different roundings.
They are named separately here so no caller can answer one with the other:

* **Quantising a render range** — ``Quantisation.render_range_frames(a, b)``, half
  open, rounding **up**: the range ``[a, b)`` at output rate ``R`` emits the global
  frame indices ``ceil(a·R) … ceil(b·R) - 1``, and frame ``i`` is sampled at
  ``frame_time(i) = i / R`` (same rule for samples). Both boundaries snap
  **forward**, to the first frame time at or after them. Item durations are
  differences of those indices, never independently rounded durations, so rounding
  error cannot accumulate: every boundary lies within **one output frame** of the
  exact composition boundary, at any point of a multi-hour film, and it always
  lies the same way — forward.
* **Inspecting a playhead** — ``Quantisation.frame_at_playhead(t)``, rounding
  **down**: a still at playhead ``t`` shows the frame whose picture is *already*
  covering ``t``, i.e. ``floor(t·R)``. The range boundary must not be used here:
  rounding a playhead up shows the next frame early — a picture from up to one
  frame in the future — and a playhead one tick before the composition end would
  name a frame the composition never writes.

Within the range question, naming a boundary and counting a length are
deliberately the *same* rounding (``frame_count`` = ``frame_index`` = up). They
used to differ — indices rounded half up, lengths rounded up — and the difference
was observable rather than academic: a 0.01 s composition had no frames at all for
the exporter while the preview handed out a one-frame range for it, and a length
of 1.005 s at 25 fps made the preview ask for a frame the export would not write.
One rounding for that one question, and a positive-length composition always has
at least one frame.
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

The one output-frame lattice
----------------------------
Frame ``i`` of the project's output represents output time ``i / frame_rate`` and
is the picture the export writes at that index. A render range ``[a, b)`` produces
the frames with index ``ceil(a·rate) … ceil(b·rate) - 1`` — each boundary snaps
**forward**, to the first frame time at or after it, and nothing else decides how
many frames there are. The preview window and the export both use those indices,
so "output frame 4123" means the same picture everywhere:

* Consequence: any composition of positive length has at least one frame, and the
  final partial frame (the material after the last frame time, up to one frame of
  it) is emitted as that last frame rather than dropped. A composition of length
  zero has no frames and nothing is rendered.
* The rendered file is exactly ``total_frames / frame_rate`` long — never longer
  than the composition by more than the partial frame it covers, and never
  shorter than it by more than one frame.
* Consequence: two clip edges within one frame of each other can snap to the
  same frame; the topmost enabled track wins that frame.

Because frame ``i`` is what the range ``[i/rate, (i+1)/rate)`` emits, the two
questions meet in one place: asking for a **playhead** ``t`` answers
``floor(t·rate)`` (see the section above), and the frame it names is then handed
to the range rule as that frame's own lattice interval. ``media.framegrab`` builds
a still exactly that way, which is what makes an inspected frame a frame the
export really writes — and never a frame past the end of the composition.

Which source picture an output frame shows
------------------------------------------
``picture_sampling`` states this once, for every picture item, and both the
renderer and the still-frame viewer follow it (see ``render/graph.py``):

* the frame at global index ``i`` samples source time
  ``clip.source_in + (frame_time(i) - clip.output_start) · clip.rate``;
* the source picture shown is the **last real frame at or before** that sampling
  time. ``render/graph.py`` makes the sampling stage honour it (``fps=round=up``;
  FFmpeg's default ``round=near`` resolves a grid point to the *nearest* frame
  instead, which measures as a different picture in a third of a retime matrix);
* the item may only read inside ``[clip.source_in, clip.source_out)``. If the
  lattice asks for material outside that slice (a boundary snapped past an edge),
  the nearest legal picture is held instead; excluded footage is never sampled;
* a range that starts mid-clip gets the same sampling times as a full render of
  that clip, because the grid is anchored to the plan's sampling time and not to
  the first picture the seek happens to find (``grid_start`` says how far into
  the reading window the first frame lies). Measured: joining two such ranges
  reproduces a single render exactly — see ``tests/test_picture_identity.py``.

One measured caveat, stated rather than hidden: FFmpeg quantises the retimed
source timestamps to the container's own time base before it chooses a frame, so
wherever a tempo sends them between ticks (25/24 → 1001/1000 style mixes are the
common case) the picture can be one frame away from what the exact rational
arithmetic above names. 26 of the 2410 frames in the matrix in
``docs/architecture.md`` are like that. The plan does not guess about it: the
viewer reports the source position *measured from the decode*, so the time shown
next to a picture is always the one that picture really has.

Composition rules (restated for rendering)
------------------------------------------
* Picture: topmost *enabled* video track wins per output frame; uncovered
  areas become explicit black items.
* Audio: every enabled, unmuted track contributes; contributions are summed
  with the clip gain and the domain envelope (``core.model.envelope_gain``).
  Gaps are explicit silence — ``total_samples`` does not depend on what plays.
* Muted/disabled tracks never shorten the output: the range is the requested
  interval (default ``[0, project.output_end())``).
* Horizon: a positive composition starts at frame 0 and its final partial frame is
  emitted. A render range deliberately extended past the composition end yields
  explicit black and silence there; a *playhead inspection* at or past the exact
  end has no frame to show and is refused by the still path, and a range holding
  no whole frame is refused by the preview — neither is padded with an invented
  frame.
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
    """Exact output-time ↔ global output index mapping for one render range.

    Two questions, two named helpers, and they are not interchangeable:

    * :meth:`render_range_frames` — which frames a half-open range ``[a, b)``
      emits: ``ceil(a·rate) … ceil(b·rate) - 1``, i.e. :meth:`frame_index` at each
      boundary (and :meth:`frame_count` for a length).
    * :meth:`frame_at_playhead` — which frame a still at playhead ``t`` shows:
      ``floor(t·rate)``, the frame already covering ``t``.

    :meth:`frame_index` and :meth:`frame_count` are the *range* rounding. Using
    them to ask what a playhead shows names the next frame early.
    """

    frame_rate: Fraction
    sample_rate: int

    def frame_index(self, t: Fraction) -> int:
        """The output-frame **boundary** at output time ``t``: ceil, never down.

        This answers the render-range question — where a range edge beginning at
        ``t`` falls — and is the same rounding as :meth:`frame_count`. A boundary
        snaps **up**, to the first frame time at or after it, which is what makes
        every positive length produce at least one frame; see the module docstring
        for why the boundary rule and the counting rule had to become one rule.
        For "what does a playhead at ``t`` show?" use :meth:`frame_at_playhead`.
        """
        return self.frame_count(t)

    def render_range_frames(self, start: Fraction, end: Fraction) -> tuple[int, int]:
        """The **render range** question: first (inclusive) and last (exclusive)
        global frame index of the half-open interval ``[start, end)``.

        Empty (``first == last``) when the interval holds no frame time — the
        caller must refuse it rather than invent a frame.
        """
        if end <= start:
            return 0, 0
        first = max(self.frame_index(start), 0)
        return first, max(first, self.frame_index(end))

    def frame_at_playhead(self, t: Fraction) -> int:
        """The **playhead inspection** question: the frame whose picture covers ``t``.

        Frame ``i`` is presented from ``frame_time(i)`` until the next frame takes
        over, so a playhead at ``t`` is looking at ``floor(t·rate)``. Rounding this
        up (the range boundary, :meth:`frame_index`) would show the *next* frame
        early — a picture from up to one frame in the future — and at a playhead
        just below the end of the composition it names a frame one past the last
        one the export writes. Negative only for a negative ``t``, which has no
        frame: the caller refuses that before asking.
        """
        value = t * self.frame_rate
        return value.numerator // value.denominator  # floor, for any sign

    def frame_time(self, index: int) -> Fraction:
        return F(index) / self.frame_rate

    def frame_count(self, seconds: Fraction) -> int:
        """How many frames a length of ``seconds`` yields: ``ceil(seconds·rate)``.

        This is the same rounding as :meth:`frame_index`, so the *range* question
        agrees with itself: the number of frames a length needs never disagrees
        with the boundary indices that length spans, and a preview range cannot
        promise a frame the export would not write. A length below one frame still
        needs one frame; a length of zero needs none. It is not the rule for what a
        playhead shows — see :meth:`frame_at_playhead`.
        """
        if seconds <= 0:
            return 0
        value = seconds * self.frame_rate
        return -((-value.numerator) // value.denominator)

    def sample_index(self, t: Fraction) -> int:
        """Index of the output sample that covers output time ``t``."""
        return self.sample_count(t)

    def sample_span(self, start: Fraction, end: Fraction) -> tuple[int, int]:
        """First (inclusive) and last (exclusive) global sample index of a range."""
        if end <= start:
            return 0, 0
        first = max(self.sample_index(start), 0)
        return first, max(first, self.sample_index(end))

    def sample_time(self, index: int) -> Fraction:
        return F(index) / self.sample_rate

    def sample_count(self, seconds: Fraction) -> int:
        """How many output samples a length of ``seconds`` yields (rounded up)."""
        if seconds <= 0:
            return 0
        value = seconds * self.sample_rate
        return -((-value.numerator) // value.denominator)


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
class PictureSampling:
    """Where one picture item reads, and how its output frames are sampled.

    This is the whole picture contract, in **asset-local source seconds** (the
    same axis as ``Clip.source_in``, which is also the container's own timestamp
    axis — see :class:`StreamSource`). Written here rather than in the graph so
    the still viewer and the exporter cannot drift apart.

    The reading window is ``[seek_source, read_source_end)`` and the sampling
    grid starts at ``sample_start``, stepping by ``sample_step``. With an exact
    ``tick`` the renderer puts the decoded pictures on that grid's own axis - the
    fine tick, whole numbers only, with the grid's first point taken off every
    timestamp - and lets ``fps`` start its grid at zero: the choice of picture is
    then exact integer arithmetic in FFmpeg, and it cannot depend on where the
    seek happened to start. Without an exact tick the renderer falls back to
    ``fps=start_time=grid_start`` on container-tick timestamps, which is the
    older behaviour and is quantised to the container's grid. Either way the grid
    is anchored to the *plan*, not to the first picture the seek happened to find,
    so a range that begins mid-clip samples at exactly the same source times as a
    full render of that clip does.

    ``seek_source`` is placed on an **exactly representable container timestamp**
    (:func:`floor_to_time_base`). That matters because input ``-ss`` quantises
    its own timestamp rebase to the container's time base: measured on an MP4
    with ``time_base`` 1/12800, asking for 0.333333333 s rebases by 4267/12800 s.
    A fraction of a millisecond of phase error is enough to choose the
    neighbouring picture whenever a sampling time falls exactly on a frame time,
    which is the ordinary case for a clip at rate 1.0.
    """

    #: source time the input seek is placed at: an exact multiple of the
    #: container's time base when one was known (``anchored``), so FFmpeg's own
    #: timestamp rebase after ``-ss`` is exact; otherwise the sampling time itself
    seek_source: Fraction
    #: source time the reading stops at. With an exact ``tick`` it sits a whole
    #: container tick outside the slice on purpose: the chain's ``select`` decides
    #: the half-open slice on the source axis in whole ticks, while this bound is
    #: written in floating-point seconds - and a bound that lands exactly on a
    #: picture's own timestamp would let two ranges of the same clip, which reach
    #: it by different sums, disagree about whether that picture is read at all.
    read_source_end: Fraction
    #: source time presented by the item's first output frame
    sample_start: Fraction
    #: source seconds between consecutive output frames: ``clip.rate / frame_rate``
    sample_step: Fraction
    #: ``fps`` ``start_time`` in the fallback chain, and the grid's placement in
    #: output seconds when an exact ``tick`` is used: the first grid point on the
    #: tempo-scaled axis. Negative when the seek has to start past the first
    #: sampling time, because the clip's slice begins between container ticks.
    grid_start: Fraction
    #: True when ``seek_source`` is an exact multiple of the stream's time base,
    #: so the grid lands where the plan says; False means no time base could be
    #: read and the grid is anchored at whatever picture the seek found — the
    #: pre-frame-index behaviour, exact only when the sampling time is itself a
    #: frame time. Reported, never presented as a guarantee.
    anchored: bool
    #: True when the lattice asked for material before ``clip.source_in``, so the
    #: item's first frames hold the clip's first legal picture
    head_held: bool
    #: output frames at the item's head that must hold its first legal picture,
    #: because the first grid points fall before any readable frame. At most one
    #: under this lattice (a clip edge can only snap forward by one frame), and
    #: what makes the head honest rather than a one-frame shift of everything.
    head_gap: int
    #: True when the last grid point lies past ``clip.source_out``, so the item's
    #: last frames hold the clip's last legal picture
    tail_held: bool
    #: The filter tick the picture stages put this item's timestamps on, or
    #: ``None`` when no exact one could be established (see :func:`picture_tick`).
    #: With a tick, every retimed stamp is a whole number of ticks, so ``fps``
    #: decides each output frame by exact integer arithmetic and cannot be moved
    #: by the container's coarser grid. Without it the picture chain falls back to
    #: quantising the tempo to the container's timestamps, which is the older,
    #: approximate behaviour, and the stream is reported as unanchored.
    tick: Fraction | None = None
    #: The item's first sampling time in ``tick`` units: the grid's absolute phase.
    #: Negative when the seek had to start past that sampling time, because the
    #: clip's slice begins between container ticks. The chain subtracts it from
    #: every stamp so that the first point of the ``fps`` grid is exactly this
    #: item's first sampling time, whatever the seek happened to deliver first.
    phase: int | None = None

    @property
    def read_duration(self) -> Fraction:
        return self.read_source_end - self.seek_source

    def sample_time(self, frame_in_item: int) -> Fraction:
        """Source time presented by the ``frame_in_item``-th frame of this item."""
        return self.sample_start + frame_in_item * self.sample_step


def floor_to_time_base(value: Fraction, time_base: Fraction) -> Fraction:
    """Largest representable timestamp at or below ``value``."""
    ticks = value / time_base
    whole = ticks.numerator // ticks.denominator  # floor, for any sign
    return whole * time_base


def ceil_to_time_base(value: Fraction, time_base: Fraction) -> Fraction:
    """Smallest representable timestamp at or above ``value``."""
    ticks = value / time_base
    whole = -((-ticks.numerator) // ticks.denominator)  # ceil
    return whole * time_base


#: Largest fine-tick denominator the picture chain accepts, and the largest
#: intermediate product its expression may form. ``ffmpeg``'s ``setpts`` evaluates
#: its expression in ``double`` and truncates the result, so a stamp is only exact
#: if the product it computes is a whole number the double can hold exactly:
#: every integer below 2**53 can be. The tick cap alone would already keep a
#: feature-length timeline far inside that, so both bounds are stated.
MAX_PICTURE_TICK = 10**9
MAX_PICTURE_TICK_PRODUCT = 1 << 53


def picture_tick(
    rate: Fraction,
    time_base: Fraction | None,
    sample_start: Fraction,
    seek_source: Fraction,
    read_source_end: Fraction,
) -> Fraction | None:
    """The filter tick that makes one item's retimed stamps whole numbers.

    The picture chain puts every decoded picture on the composition's own clock
    (``source seconds`` → ``source seconds / rate``) and then lets ``fps`` choose
    the picture for each output frame. ``fps`` does that choice in exact integer
    arithmetic, but only on whatever timestamps it is given: after ``setpts`` those
    timestamps live in the *container's* time base, so a tempo like 25/24 quantises
    them to container ticks first and the phase error picks the neighbouring
    picture whenever a sampling time falls near a frame time. A tick fine enough
    that each stamp is a whole number of ticks removes the quantisation entirely.

    Writing ``rate`` as ``a/b`` source seconds per output second, a picture ``p``
    container ticks after the (tick-aligned) seek carries ``p * J * b / a`` fine
    ticks once the time base has been expanded by ``J`` per container tick, so
    ``J`` has to be a multiple of ``a``. The grid's own phase
    ``(sample_start - seek) / rate`` has to be a whole number of fine ticks too,
    which fixes the rest: ``J = a * denominator((sample_start - seek) * b /
    time_base)``.

    ``None`` means no such tick is available or usable: no measured time base, a
    time base that will not divide an integer tick, a tick finer than
    :data:`MAX_PICTURE_TICK`, or a timeline so long that the expression's product
    would leave the exactly-representable range. The caller then says so instead of
    pretending the sampling is exact.
    """
    if time_base is None or time_base <= 0 or rate <= 0:
        return None
    a, b = rate.numerator, rate.denominator
    phase_ticks = (sample_start - seek_source) * b / time_base
    expansion = a * phase_ticks.denominator  # fine ticks per container tick
    per_second = Fraction(expansion, 1) / time_base  # fine ticks per second
    if per_second.denominator != 1 or per_second > MAX_PICTURE_TICK:
        return None
    per_second = int(per_second)
    # the largest ``PTS * b`` the expression can form over this reading window,
    # in fine ticks: a double holds every integer below 2**53 exactly, and
    # ``av_expr`` stops being exact past that
    if ceil_to_time_base(read_source_end - seek_source, time_base) * per_second * b >= (
        MAX_PICTURE_TICK_PRODUCT
    ):
        return None
    return Fraction(1, per_second)


#: How far before the first sampling time a picture item starts reading. The
#: picture shown for a sampling time is the last one at or before it, so the
#: reading window has to reach back at least one source frame; "one frame" is not
#: known exactly for variable frame rates, so this is generous — the cost is a
#: few decoded frames per item, and the window never passes the clip's own edge.
MIN_PICTURE_PREROLL = Fraction(1, 4)


def picture_preroll(clip: Clip, source: StreamSource, frame_rate: Fraction) -> Fraction:
    """How far before the first sampling time a picture item starts reading."""
    step = clip.rate / frame_rate
    period = ZERO
    if source.frame_rate and source.frame_rate > 0:
        period = 1 / source.frame_rate
    return max(MIN_PICTURE_PREROLL, 2 * period, step)


def picture_sampling(
    clip: Clip,
    item: VideoItem,
    frame_rate: Fraction,
    time_base: Fraction | None = None,
    preroll: Fraction = MIN_PICTURE_PREROLL,
    first_frame: Fraction | None = None,
    previous_frame: Fraction | None = None,
) -> PictureSampling:
    """The sampling contract for one picture item (see :class:`PictureSampling`).

    ``item.source_start`` is what the composition says this item's first frame
    presents; it can fall slightly outside the clip's own slice when a clip edge
    snapped to a neighbouring frame. Clamping the *start* of the grid (rather
    than shifting the whole grid, which would move every later frame too) keeps
    the clip's timing intact and turns those first frames into a held picture.

    With ``time_base`` (the container's, probed by ``media.framegrab``) the seek
    is placed on an exactly representable timestamp, which is what makes the grid
    exact: FFmpeg rounds its own ``-ss`` timestamp rebase to the container's time
    base (measured: seeking to 0.333333333 s in an MP4 with ``time_base``
    1/12800 rebases by 4267/12800 s, not by the requested amount), and a fraction
    of a millisecond of phase error is enough to choose the neighbouring picture
    when a sampling time falls exactly on a frame time — the ordinary case for a
    clip at rate 1.0. Without it the seek goes to the sampling time and the grid
    starts at 0: how this worked before, correct only when the sampling time is
    itself a frame time.

    ``previous_frame`` (the last real picture at or before the first sampling
    time, from :mod:`video_editor.media.frameindex`) replaces the nominal preroll
    where it is known: that picture is exactly what the first grid point needs, so
    reading starts one container tick before it rather than a nominal quarter of a
    second earlier. Less decoded material per item, and no reliance on an estimate
    of frame spacing for variable-frame-rate streams.

    The reading bound is pulled back by half a time-base tick, and pushed a whole
    tick past the slice whenever the item has an exact ``tick``. Both are about the
    same hazard from opposite sides: a bound that lands exactly on a picture's
    timestamp is decided in floating point, so two ranges of the same clip - which
    reach that bound by different sums - can disagree about whether the picture
    just outside the clip's slice is read. With the exact tick the chain itself
    enforces the half-open slice on the source axis, so the bound can afford to be
    generous and still read nothing that the slice forbids.

    ``first_frame`` is the first *real* picture the slice can read (from
    :mod:`video_editor.media.frameindex`). It decides ``head_gap``: grid points
    before that picture have nothing to show and hold it instead. Without it the
    head gap is inferred from the lattice alone - at most one frame, because a
    clip edge can only snap forward by one frame - which is exact whenever the
    preroll really does reach a readable picture, and reported by ``head_held``.
    """
    step = clip.rate / frame_rate
    wanted = item.source_start
    start = max(wanted, clip.source_in)
    last = start + (max(item.frames, 1) - 1) * step
    allowed = min(last + step, clip.source_out)
    if time_base is None or time_base <= 0:
        seek, read_end, anchored = start, allowed, False
    else:
        # reach back far enough to have the picture the grid will want, but never
        # past the clip's own slice: material outside it must not be sampled
        earliest = ceil_to_time_base(clip.source_in, time_base)
        reach = floor_to_time_base(start - preroll, time_base)
        if previous_frame is not None and previous_frame >= earliest:
            # measured, so exact, and it replaces the estimate: this picture is
            # what the first grid point shows, and nothing earlier can be chosen.
            # One tick of margin keeps an accurate seek that lands on this
            # picture's own timestamp from cutting it off.
            reach = previous_frame - time_base
        seek = max(earliest, reach)
        read_end = allowed - time_base / 2
        if read_end <= seek:  # a slice no wider than the rounding of the bound
            read_end = seek + time_base
        anchored = True
    head_held = wanted < clip.source_in
    if first_frame is not None and first_frame > start:
        gap = (first_frame - start) / step
        head_gap = -((-gap.numerator) // gap.denominator)  # ceil: frames to hold
    else:
        head_gap = 1 if head_held else 0
    # one tick of slack for the generous reading bound decided below; the bound the
    # chain writes for the slice edge is measured to ``clip.source_out``, which a
    # short render range of a long clip leaves outside the reading window, so the
    # arithmetic limit is checked against whichever of the two is farther out
    bound = max(allowed + (time_base or ZERO), clip.source_out)
    tick = picture_tick(clip.rate, time_base, start, seek, bound)
    phase: int | None = None
    if tick is not None:
        # the grid's first point, in fine ticks: what the chain takes off every
        # stamp so the ``fps`` grid starts on the plan's sampling time
        exact = ((start - seek) * clip.rate.denominator / tick) / clip.rate.numerator
        if exact.denominator != 1:  # the grid cannot be placed on this tick
            tick = None
        else:
            phase = int(exact)
    if tick is not None and anchored:
        # the slice itself is enforced in the chain, so the reading bound can sit
        # a whole tick outside it instead of being rounded to land just inside
        generous = ceil_to_time_base(allowed, time_base) + time_base
        if generous > read_end:
            read_end = generous
    return PictureSampling(
        seek_source=seek,
        read_source_end=read_end,
        sample_start=start,
        sample_step=step,
        grid_start=(start - seek) / clip.rate,
        anchored=anchored,
        head_held=head_held,
        head_gap=min(head_gap, max(item.frames, 0)),
        tail_held=last + step > clip.source_out,
        tick=tick,
        phase=phase,
    )


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
        """The picture item owning the frame that ``output_time`` bounds.

        Items are addressed by the plan's own **render-range** indices, so this
        answers "which item owns the frame a range starting here would use" —
        exact at a frame time, which is how the still path calls it. A caller
        asking what a playhead shows passes the time of
        :meth:`Quantisation.frame_at_playhead` instead of the raw playhead.
        """
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
    # the range question, in global frame indices: [first, last)
    base_frame, last_frame = quant.render_range_frames(out_start, range_end)
    base_sample = quant.sample_index(out_start)
    total_frames = last_frame - base_frame
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
    stream does not contain would arrive as invented silence in the sound, or as
    the last picture held across the rest of the clip in the picture - and either
    would look like an ending the user chose.

    The slack allowed is the largest quantum a boundary artefact can be: **one
    sample of the sound being read**. Picture gets none, because ``source_out`` is
    a number the editor chose rather than something the plan rounds; the output
    grid's own boundary rounding is handled where it happens (the plan holds the
    nearest legal picture inside the slice) and must not be paid for by widening
    what a clip may ask of the source. A stream whose end is only a container
    bound gets no extra slack either - a bound that may be too generous is
    measured by the exporter before anything is published.
    """
    assert source.end is not None  # placeable, checked by the caller
    tolerance = F(1) / source.sample_rate if source.sample_rate else ZERO
    audio = source.kind is TrackKind.AUDIO
    missing = clip.source_out - source.end
    if missing > tolerance:
        fills = (
            "replace that much of the clip with silence"
            if audio
            else "hold the stream's last picture across that much of the clip"
        )
        raise RenderPlanError(
            f"Clip {clip.id} needs {float(missing):.6f}s past the end of asset "
            f"{clip.asset_id} stream {clip.stream_index} (which runs to "
            f"{float(source.end)}s on its own time axis). Rendering it would {fills}; "
            "shorten the clip or use another source."
        )
    before = source.origin - clip.source_in
    if before > tolerance:
        starts = (
            "start the clip with silence" if audio else "start the clip with no picture"
        )
        raise RenderPlanError(
            f"Clip {clip.id} starts {float(before):.6f}s before the beginning of "
            f"asset {clip.asset_id} stream {clip.stream_index} (which starts at "
            f"{float(source.origin)}s); rendering would {starts}."
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
            continue  # both edges snap to the same frame boundary: no frame of its own
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

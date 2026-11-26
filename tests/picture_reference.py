"""A slow, independent picture reference: which picture each output frame must show.

This is TASK_12i's fixed contract written as code that shares nothing with the
thing it checks. It does **not** import ``render.plan.picture_sampling``,
``render.graph``, or ask FFmpeg's ``fps`` filter what it intends; it does not
know what a frame rate is. Everything it knows comes from two places:

* the composition, as it was placed (a source slice, an output start, a tempo);
* one short fixture's **decoded presentation times** (measured once by
  ``media_fixtures.picture_facts``), which is what says where the real pictures
  actually are — so variable frame rate and a nonzero stream start need no
  special case, and a fixture that quietly lost a picture changes the answer
  instead of validating a wrong one.

The contract, for one global output frame index ``i``
-----------------------------------------------------
1. ``t = i / R`` — output frame ``i`` represents that instant, always counted
   from output 0, never from the start of a render range.
2. The winning clip is the one whose half-open output interval
   ``[output_start, output_end)`` contains ``t``, where
   ``output_end = output_start + (source_out - source_in) / rate``.
3. ``wanted = source_in + (t - output_start) * rate`` — the source position that
   frame asks for.
4. Inside the legal slice ``[source_in, source_out)`` the picture shown is the
   **last decoded PTS at or before** ``wanted`` — the preceding-frame rule, never
   the nearest one.
5. If ``wanted`` precedes the timestamp of the first legal picture, hold that
   first picture. If ``wanted`` is strictly after the timestamp of the last legal
   picture, hold the last one. Either hold is only legitimate because the grid
   asked past material that does not exist; it is never a way to fill footage the
   file has. A sampling time exactly on a picture's own timestamp is that picture,
   not a hold of it.
6. A slice containing no legal picture is an error, not a black frame and not a
   hold.

The output-frame lattice (how many frames a length needs, and which indices a
render range holds) is re-derived here with exact rational ceiling, so a frame
count below is comparable with what a plan claims without asking that plan.

Deliberately absent: any tolerance, any "one frame is close enough", and any
count of acceptable failures. What this module can say about a borderline case
is a *measured distance* — :meth:`PictureReference.tie` — between ``wanted`` and
the real pictures on both sides of it. A backend timestamp-quantisation tie is
only a tie if it is described by such a distance, and it never applies to exact
source inspection at all.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from fractions import Fraction as F
from itertools import pairwise

ZERO = F(0)

#: what an output frame got, as far as this reference is concerned
BLACK = "black"  #: no clip covers that output time
PICK = "pick"  #: a real picture sits at or before the sampling time
HEAD_HOLD = "head-hold"  #: the grid asked before the slice's first real picture
TAIL_HOLD = "tail-hold"  #: the grid asked past the slice's last real picture


class EmptySliceError(Exception):
    """The clip slices a region of the stream that holds no picture at all."""


class OverlapError(Exception):
    """Two clips claim the same output frame, so the reference cannot answer."""


def ceil_exact(value: F) -> int:
    """Smallest integer at or above an exact ``Fraction`` (no floating point)."""
    value = F(value)
    return -((-value.numerator) // value.denominator)


def floor_exact(value: F) -> int:
    """Largest integer at or below an exact ``Fraction`` (no floating point)."""
    value = F(value)
    return value.numerator // value.denominator


def frame_time(index: int, frame_rate: F) -> F:
    """The output instant global frame ``index`` presents."""
    return F(index) / frame_rate


def output_frames(seconds: F, frame_rate: F) -> int:
    """How many output frames a length of ``seconds`` needs: ``ceil``, never down."""
    if seconds <= 0:
        return 0
    return ceil_exact(F(seconds) * frame_rate)


def range_frame_indices(start: F, end: F, frame_rate: F) -> tuple[int, int]:
    """First (inclusive) and last (exclusive) global index of range ``[start, end)``.

    Both edges snap **forward**, to the first frame time at or after them, so a
    positive length always holds at least one frame and two ranges that meet
    share no index and lose none.
    """
    if end <= start:
        return 0, 0
    first = max(ceil_exact(F(start) * frame_rate), 0)
    return first, max(first, ceil_exact(F(end) * frame_rate))


@dataclass(frozen=True)
class PlacedClip:
    """One clip exactly as the editor placed it: slice, output start, tempo."""

    clip_id: str
    source_in: F
    source_out: F
    output_start: F = ZERO
    rate: F = F(1)  # source seconds per output second

    def __post_init__(self) -> None:
        if self.source_out <= self.source_in:
            raise ValueError(f"clip {self.clip_id}: source slice must have length > 0")
        if self.rate <= 0:
            raise ValueError(f"clip {self.clip_id}: rate must be positive")
        if self.output_start < 0:
            raise ValueError(f"clip {self.clip_id}: output start must be non-negative")

    @property
    def output_end(self) -> F:
        return self.output_start + (self.source_out - self.source_in) / self.rate

    def source_at(self, output_time: F) -> F:
        """The source position this clip presents at output instant ``output_time``."""
        return self.source_in + (output_time - self.output_start) * self.rate


@dataclass(frozen=True)
class DecodedStream:
    """What a fixture really presents: decoded PTS, ascending, plus each identity.

    Built from ``media_fixtures.picture_facts`` (one decode of a short file), not
    from a frame rate: ``picture_facts`` reads ``best_effort_timestamp`` times as
    exact integer ticks of the stream's own ``time_base``.
    """

    pts: tuple[F, ...]
    identities: tuple[int, ...] | None = None
    time_base: F | None = None
    #: identity -> decoded ordinal, built once (identity fixtures make this 1:1)
    _by_identity: dict[int, int] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.pts:
            raise ValueError("a decoded stream with no pictures cannot be a reference")
        for a, b in pairwise(self.pts):
            if b <= a:
                raise ValueError(f"decoded PTS must strictly ascend: {a} then {b}")
        if self.identities is not None and len(self.identities) != len(self.pts):
            raise ValueError("identities must say one thing per decoded picture")
        if self.identities is not None:
            self._by_identity.update({i: n for n, i in enumerate(self.identities)})

    @property
    def count(self) -> int:
        return len(self.pts)

    @property
    def first(self) -> F:
        return self.pts[0]

    @property
    def last(self) -> F:
        return self.pts[-1]

    def identity(self, ordinal: int) -> int:
        """The number the fixture's ``ordinal``-th picture carries."""
        if self.identities is None:
            return ordinal
        return self.identities[ordinal]

    def ordinal_of(self, identity: int) -> int | None:
        """Which decoded picture carries ``identity`` (``None`` if none does)."""
        return self._by_identity.get(identity, None)


@dataclass(frozen=True)
class LegalSlice:
    """The pictures a clip is allowed to show, as ordinals into the decode."""

    first: int
    last: int
    first_pts: F
    last_pts: F
    #: is the slice's last picture also the stream's last real picture? A tail
    #: hold is "the film ended here" when this is true, and "the edit ended here"
    #: when it is false — different sentences, and the contract forbids reading
    #: past either one.
    ends_at_stream_end: bool

    @property
    def count(self) -> int:
        return self.last - self.first + 1


@dataclass(frozen=True)
class Pick:
    """The picture global output frame ``index`` must show, and why."""

    index: int
    output_time: F
    clip_id: str
    wanted: F
    ordinal: int
    identity: int
    pts: F
    kind: str
    #: the two real pictures either side of ``wanted``, whether or not the slice
    #: allows them: these are the candidates a backend weighs
    prev_pts: F | None
    next_pts: F | None

    @property
    def held(self) -> bool:
        return self.kind in (HEAD_HOLD, TAIL_HOLD)


@dataclass(frozen=True)
class Tie:
    """How far a sampling time sits from the real picture boundary beside it."""

    index: int
    wanted: F
    prev_pts: F | None
    next_pts: F | None
    #: distance to the nearer real PTS (``None`` only if the stream has one picture)
    distance: F | None

    def within(self, tolerance: F) -> bool:
        return self.distance is not None and self.distance <= tolerance


class PictureReference:
    """Answer "which picture does global output frame ``i`` show?" for one composition.

    Every answer is arithmetic over the measured decode; nothing here consults a
    plan, a filter chain or a backend.
    """

    def __init__(self, stream: DecodedStream, frame_rate: F, clips: tuple[PlacedClip, ...]):
        if frame_rate <= 0:
            raise ValueError("output frame rate must be positive")
        self.stream = stream
        self.frame_rate = F(frame_rate)
        self.clips = tuple(clips)
        self._by_id = {c.clip_id: c for c in self.clips}
        if len(self._by_id) != len(self.clips):
            raise ValueError("clip ids must be unique in one reference")
        self._legal: dict[str, LegalSlice] = {}

    # --------------------------------------------------------------- the slice
    def legal(self, clip: PlacedClip) -> LegalSlice:
        """The pictures inside ``[source_in, source_out)`` — half-open, both ways."""
        found = self._legal.get(clip.clip_id)
        if found is not None:
            return found
        first = bisect_left(self.stream.pts, clip.source_in)
        last = bisect_left(self.stream.pts, clip.source_out) - 1
        if last < first:
            raise EmptySliceError(
                f"clip {clip.clip_id} slices [{clip.source_in}, {clip.source_out}) of a "
                f"stream whose real pictures run {self.stream.first} .. {self.stream.last} "
                f"({self.stream.count} pictures): nothing legal to show there"
            )
        found = LegalSlice(
            first=first,
            last=last,
            first_pts=self.stream.pts[first],
            last_pts=self.stream.pts[last],
            ends_at_stream_end=last == self.stream.count - 1,
        )
        self._legal[clip.clip_id] = found
        return found

    def legal_id(self, clip_id: str) -> LegalSlice:
        """:meth:`legal` addressed by clip id (a :class:`Pick` names the clip, not the object)."""
        return self.legal(self._by_id[clip_id])

    # ----------------------------------------------------------- the grid step
    def owner(self, output_time: F) -> PlacedClip | None:
        """The clip presenting output instant ``output_time`` (``None`` is black)."""
        owners = [c for c in self.clips if c.output_start <= output_time < c.output_end]
        if not owners:
            return None
        if len(owners) > 1:
            raise OverlapError(
                f"output {output_time} is inside {len(owners)} clips "
                f"({[c.clip_id for c in owners]}); one track cannot show two pictures"
            )
        return owners[0]

    def wanted_source(self, index: int) -> tuple[PlacedClip, F] | None:
        """The clip that owns global frame ``index`` and the source time it asks for."""
        moment = frame_time(index, self.frame_rate)
        clip = self.owner(moment)
        if clip is None:
            return None
        return clip, clip.source_at(moment)

    # ------------------------------------------------------------- the answer
    def pick(self, index: int) -> Pick | None:
        """The picture global output frame ``index`` must show (``None`` is black)."""
        asked = self.wanted_source(index)
        if asked is None:
            return None
        clip, wanted = asked
        legal = self.legal(clip)
        # the preceding-frame rule: the last real picture at or before wanted ...
        at_or_before = bisect_right(self.stream.pts, wanted) - 1
        prev_pts = self.stream.pts[at_or_before] if at_or_before >= 0 else None
        next_ordinal = bisect_right(self.stream.pts, wanted)
        next_pts = self.stream.pts[next_ordinal] if next_ordinal < self.stream.count else None
        # ... held to the slice, which is what makes an edge a hold rather than a
        # picture from footage this clip does not contain. A frame is a *hold* when
        # the sampling time is past the timestamp of the newest picture the slice
        # can show (or before the oldest), whether or not the slice still runs on:
        # between the last picture and ``source_out`` there is nothing newer, so the
        # grid is being answered with a picture that is already older there.
        if wanted < legal.first_pts:
            ordinal, kind = legal.first, HEAD_HOLD
        elif wanted > legal.last_pts:
            ordinal, kind = legal.last, TAIL_HOLD
        else:
            ordinal, kind = at_or_before, PICK
        return Pick(
            index=index,
            output_time=frame_time(index, self.frame_rate),
            clip_id=clip.clip_id,
            wanted=wanted,
            ordinal=ordinal,
            identity=self.stream.identity(ordinal),
            pts=self.stream.pts[ordinal],
            kind=kind,
            prev_pts=prev_pts,
            next_pts=next_pts,
        )

    def picks(self, first_index: int, count: int) -> list[Pick | None]:
        return [self.pick(first_index + k) for k in range(count)]

    def expected_identities(
        self, first_index: int = 0, count: int | None = None
    ) -> list[int | None]:
        """Expected identity per global index; ``count`` defaults to the composition's end."""
        if count is None:
            count = output_frames(max(c.output_end for c in self.clips), self.frame_rate)
        return [
            None if pick is None else pick.identity
            for pick in self.picks(first_index, count)
        ]

    # ------------------------------------------------------- ties and holds
    def tie(self, index: int) -> Tie | None:
        """How close frame ``index``'s sampling time comes to a real picture boundary."""
        pick = self.pick(index)
        if pick is None:
            return None
        distances = [
            pick.wanted - pick.prev_pts if pick.prev_pts is not None else None,
            pick.next_pts - pick.wanted if pick.next_pts is not None else None,
        ]
        real = [d for d in distances if d is not None and d >= 0]
        return Tie(
            index=index,
            wanted=pick.wanted,
            prev_pts=pick.prev_pts,
            next_pts=pick.next_pts,
            distance=min(real) if real else None,
        )

    def ties(self, first_index: int, count: int, within: F) -> list[Tie]:
        """Every frame whose sampling time is within ``within`` of a real boundary."""
        return [
            t for t in (self.tie(first_index + k) for k in range(count))
            if t and t.within(within)
        ]

    def holds(self, first_index: int, count: int) -> list[tuple[str, int, int]]:
        """Runs of held pictures as ``(kind, first_index, length)``.

        A hold is legitimate only for the grid points that genuinely ask past a
        real picture, so its length is evidence: a run of a thousand is the
        "long accidental hold" the contract forbids, and a run of one or two at
        an edge is the required edge behaviour.
        """
        runs: list[tuple[str, int, int]] = []
        for pick in self.picks(first_index, count):
            if pick is None or pick.kind == PICK:
                continue
            if runs and runs[-1][0] == pick.kind and runs[-1][1] + runs[-1][2] == pick.index:
                runs[-1] = (pick.kind, runs[-1][1], runs[-1][2] + 1)
            else:
                runs.append((pick.kind, pick.index, 1))
        return runs


# --------------------------------------------------------------- mismatches

#: the mismatch classes TASK_12i distinguishes. The first two are the grid
#: legitimately asking for material that is not there; the third is the renderer
#: answering a question it was asked with a different picture.
TAIL_HOLD_MISMATCH = "tail-hold"
HEAD_HOLD_MISMATCH = "head-hold"
RETIMING_MISMATCH = "retiming"
PHASE_MISMATCH = "phase/join"
BLACK_MISMATCH = "black-vs-picture"
OUTSIDE_SLICE_MISMATCH = "outside-slice"
COUNT_MISMATCH = "frame-count"


@dataclass(frozen=True)
class Mismatch:
    """One frame where what was observed is not what the reference names."""

    index: int
    #: which comparison this is, e.g. "reference vs whole render"
    label: str
    wanted: F | None
    expected: int | None
    observed: int | None
    #: the decoded ordinals behind those identities, when they are real pictures
    expected_ordinal: int | None
    observed_ordinal: int | None
    #: how far the observed picture is from the required one, in decoded pictures
    offset: int | None
    kind: str
    #: distance from ``wanted`` to the nearer real picture boundary
    tie_distance: F | None

    def row(self) -> tuple[object, ...]:
        """A printable row: index, wanted, expected, observed, class, tie distance."""
        return (
            self.index,
            str(self.wanted) if self.wanted is not None else "-",
            self.expected,
            self.observed,
            self.kind,
            str(self.tie_distance) if self.tie_distance is not None else "-",
        )


def classify(
    reference: PictureReference,
    index: int,
    observed: int | None,
    label: str,
    stream: DecodedStream | None = None,
) -> Mismatch:
    """Name the class of one disagreement between a render and the reference.

    * ``tail-hold`` / ``head-hold`` — the grid asked past a real picture and the
      reference answered by holding the nearest legal one. Expected behaviour;
      what a backend does there is its own business, and this class says the
      mismatch is not a retiming fault.
    * ``retiming`` — the grid asked for real material, there *is* real material
      at that sampling time, and a different picture arrived.
    * ``outside-slice`` — a picture from footage the clip does not contain.
    * ``black-vs-picture`` — black where a picture was required (or the reverse).
    """
    pick = reference.pick(index)
    stream = stream or reference.stream
    tie = reference.tie(index)
    expected_ordinal = pick.ordinal if pick is not None else None
    observed_ordinal = stream.ordinal_of(observed) if observed is not None else None
    offset = (
        observed_ordinal - expected_ordinal
        if observed_ordinal is not None and expected_ordinal is not None
        else None
    )
    if pick is None or observed is None:
        # one side shows the composition's own black, the other shows a picture
        kind = BLACK_MISMATCH
    elif observed_ordinal is None:
        # an identity this fixture never presents: not a shift, a picture from elsewhere
        kind = "unknown-identity"
    else:
        legal = reference.legal_id(pick.clip_id)
        if not legal.first <= observed_ordinal <= legal.last:
            kind = OUTSIDE_SLICE_MISMATCH
        elif pick.kind == TAIL_HOLD:
            kind = TAIL_HOLD_MISMATCH
        elif pick.kind == HEAD_HOLD:
            kind = HEAD_HOLD_MISMATCH
        else:
            kind = RETIMING_MISMATCH
    return Mismatch(
        index=index,
        label=label,
        wanted=pick.wanted if pick is not None else None,
        expected=pick.identity if pick is not None else None,
        observed=observed,
        expected_ordinal=expected_ordinal,
        observed_ordinal=observed_ordinal,
        offset=offset,
        kind=kind,
        tie_distance=tie.distance if tie is not None else None,
    )


def compare(
    reference: PictureReference,
    observed: list[int | None],
    label: str,
    first_index: int = 0,
) -> list[Mismatch]:
    """Every frame of ``observed`` that is not what the reference names.

    ``observed[i]`` is the identity measured at global index ``first_index + i``;
    ``None`` is the composition's own black. No tolerance is applied anywhere:
    the caller decides what to do with the classes this returns.
    """
    out: list[Mismatch] = []
    for k, got in enumerate(observed):
        index = first_index + k
        wanted = reference.pick(index)
        if (wanted is None) == (got is None) and (wanted is None or wanted.identity == got):
            continue
        out.append(classify(reference, index, got, label))
    return out


@dataclass(frozen=True)
class PathDisagreement:
    """The third mismatch class: two production paths, one global index, two pictures."""

    index: int
    reference: int | None  #: what the fixed-origin render (trimmed) shows
    sought: int | None  #: what an independently sought range shows


def path_disagreements(
    reference_render: list[int | None],
    first_index: int,
    sought_render: list[int | None],
    sought_first_index: int,
) -> tuple[list[PathDisagreement], list[str]]:
    """Compare two renders by **global output index**, not by position in the file.

    Returns the disagreements and a list of structural notes: an index one side
    does not hold at all is a different fault from a picture that moved, so it is
    said separately rather than being lined up by eye.
    """
    problems: list[str] = []
    a_end = first_index + len(reference_render)
    b_end = sought_first_index + len(sought_render)
    shared_start, shared_end = max(first_index, sought_first_index), min(a_end, b_end)
    if shared_end <= shared_start:
        problems.append(
            f"no global index in common: {first_index}..{a_end - 1} against "
            f"{sought_first_index}..{b_end - 1}"
        )
        return [], problems
    if first_index != sought_first_index or a_end != b_end:
        problems.append(
            f"index coverage differs: {first_index}..{a_end - 1} against "
            f"{sought_first_index}..{b_end - 1}"
        )
    gaps = [
        index
        for index in range(shared_start, shared_end)
        if (reference_render[index - first_index] is None)
        != (sought_render[index - sought_first_index] is None)
    ]
    if gaps:
        problems.append(f"one side is black where the other has a picture: {gaps[:10]}")
    out = [
        PathDisagreement(
            index=index,
            reference=reference_render[index - first_index],
            sought=sought_render[index - sought_first_index],
        )
        for index in range(shared_start, shared_end)
        if reference_render[index - first_index] != sought_render[index - sought_first_index]
    ]
    return out, problems

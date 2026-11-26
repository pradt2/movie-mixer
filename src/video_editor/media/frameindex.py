"""Decoded frame index: where this stream's real pictures actually are, and what
of that is proven.

The composition works in exact rational times, and the frame shown for a
composition time is *the last real picture at or before it*. Answering that
honestly needs the stream's actual presentation timestamps. Nominal frame rate
and "duration / frame rate" invent a grid that variable-frame-rate material,
dropped frames, presentation offsets and truncated files do not have.

Packet timestamps are not enough either: a packet's ``pts`` is a seek hint, not
evidence that a picture exists. This index therefore asks
:func:`video_editor.media.frameprobe.decoded_picture_times`, which decodes one
bounded window of the chosen video stream and reports the pictures it really
produced, as ``best_effort_timestamp`` times the stream's exact ``time_base``
(never ffprobe's printed decimals), sorted and deduplicated, on the asset-local
axis.

A whole film is never scanned. Each probe reads one window around the time being
asked about, and the answers say what they can support:

* a **proven span** is a stretch of the stream a read enumerated: from the first
  picture it decoded to the last, because everything the decode produced is reported
  and nothing between those two is missing. A read is not trusted as far as the end
  it was asked for - measured behaviour is that it can stop short of it, and can also
  run past it - and successive reads therefore overlap (see :data:`PROBE_OVERLAP`) so
  that their spans join up instead of leaving an unclaimed strip between them;
* neighbouring frames are only ever answered from the span reachable from the
  question - never across an unread gap, because a nearer picture could be hiding in
  it;
* an answer is :attr:`AnswerStatus.NO_PICTURE` ("this stream has no picture that
  way") only when the span reaches a limit beyond which nothing can exist: the origin
  of the asset axis when looking earlier, or a read that was asked past the end the
  container declares for its own content when looking later. A read that merely ran
  out of material answers with the stream's tail and proves nothing of the kind -
  measured behaviour at a long presentation break is the same shape of answer - and a
  container that declares no end gives no licence, so the answer stays UNKNOWN;
* anything else is :attr:`AnswerStatus.UNKNOWN`: the index ran out of probe budget
  before it read far enough. That is a bounded, honest "not known", and it is
  never reported as the end of the stream.

A search does not spend its whole budget finding out that it cannot answer. Each
read either enumerates ground farther in the direction being looked at, or it brings
back only pictures the previous read already enumerated - and then the search stops,
because a third read of that neighbourhood would prove the same thing again. An
unanswered question is remembered until the next read, so asking the same thing twice
does not pay for the same two reads twice.

Measured with FFmpeg 8.1.3 on a 15-minute 25 fps Matroska title (4.7 MB, 22 500
frames, 10 s GOP), n=3 per measurement at two positions of the title, and repeat run
to run on the same machine: one decoded window costs 0.23-0.29 s, whatever its width
(2 s wide measures 0.23-0.27 s, 20 s wide 0.25-0.29 s), so the width of a read is
nearly free and :data:`DEFAULT_WINDOW` is chosen for what one read *buys* - 10 s holds
250 frames at 25 fps, which is what makes :data:`PROBE_OVERLAP` negligible - while
:data:`MAX_PROBES_PER_QUERY` caps what one question may cost. A decoded read is
somewhat dearer than the packet read this index replaced (0.20-0.22 s for the same
window), and it answers a random single-frame question from a cold index in 0.36-0.44 s
against 0.33 s; from a warm index it is quicker (0.15-0.18 s median against 0.22 s,
because the window is smaller). An export's anchoring question costs the same as
before - three or four reads, about 0.46 s of probing for a four-clip project - and
returns the same first frames. That is the price of being able to say what it proved.

This module reads media; it knows nothing about the timeline, and nothing about
Qt: callers that want it off the GUI thread run it in their own worker.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from pathlib import Path

from ..fs.identity import Identity
from .framegrab import StillError, StreamTiming, stream_timing
from .frameprobe import (
    DeclaredEnd,
    PictureWindow,
    declared_end,
    decoded_picture_times,
)

F = Fraction

#: Source time read by one probe. Wide enough that stepping through a long run of
#: frames costs one read (10 s is 250 frames at 25 fps) and wide enough to reach
#: across a presentation break of a few seconds in a single read, which is what lets
#: a question inside the break be answered rather than left unknown. A decoded read
#: costs about the same whatever its width (measured on the 15-minute title: 0.23 s
#: for 2 s, 0.25 s for 20 s), so width buys coverage rather than time - and a window
#: this size is what an export's first anchoring question reads, so it is not made
#: bigger than it has to be.
DEFAULT_WINDOW = F(10)

#: How far the next read reaches back into ground already enumerated. A read is only
#: trusted as far as the last picture it decoded, which measured behaviour puts short
#: of the end it was asked for (up to the stream's reorder distance: 0.08 s on these
#: fixtures, unbounded in principle), so consecutive reads placed edge to edge would
#: leave an unclaimed strip between them and the ground would never join up. One
#: second is far more than any measured gap and costs nothing, because a decoded
#: window costs about the same whatever its width.
PROBE_OVERLAP = F(1)

#: How many windows one stream keeps. Six 10 s windows hold up to 1500 timestamps
#: (250 per window at 25 fps), which is a few tens of kilobytes, and cover the
#: several places a scrubbing user may jump back to. When a seventh is read, the one
#: farthest from the read that was just paid for is dropped, so the neighbourhood
#: being worked in survives (:meth:`FrameIndex._evict`).
MAX_WINDOWS = 6

#: How many probes one question may trigger when the answer lies outside every
#: cached window. Bounded so a request near a gap cannot turn into a full scan;
#: running out does not invent an answer, it produces UNKNOWN. At the measured cost
#: of a read this caps one question at about a second; in practice a question with no
#: answer stops in two reads, because the second enumerates nothing the first did
#: not (:meth:`FrameIndex._answer`).
MAX_PROBES_PER_QUERY = 4

#: How many "not known" answers one stream remembers so that asking the same thing
#: again costs nothing. Bounded because a user scrubbing through a gap asks a new
#: question per pixel: the cache is dropped when it fills up, and any read clears it,
#: so a remembered answer can never outlive the ground it was based on.
MAX_UNKNOWN_ANSWERS = 32


class FrameIndexError(StillError):
    """The stream's timestamps cannot be read (missing file, probe failure)."""


class AnswerStatus(Enum):
    """How much an answer is worth."""

    #: a real picture, found inside a span this index enumerated
    FOUND = "found"
    #: proven that no picture lies that way, within the span reached
    NO_PICTURE = "no-picture"
    #: the span that would answer the question was never read: cost or coverage,
    #: not the end of the stream
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FrameAnswer:
    """One neighbour question, with the status of the answer and of the reading."""

    #: the real picture's timestamp, None when there is none that way or it is unknown
    pts: Fraction | None
    status: AnswerStatus
    #: the proven span the answer was taken from (None when nothing was read
    #: around the question at all)
    coverage: tuple[Fraction, Fraction] | None = None

    @property
    def known(self) -> bool:
        """True when the index can say what is there, as opposed to not having read it."""
        return self.status is not AnswerStatus.UNKNOWN

    def describe(self) -> str:
        """Short honest wording for reports and the interface."""
        if self.status is AnswerStatus.UNKNOWN:
            if self.coverage is None:
                return "unknown: no picture data was readable around that time"
            return (
                "unknown: only "
                f"{float(self.coverage[0]):.3f}-{float(self.coverage[1]):.3f}s was read"
            )
        if self.pts is None:
            return "no picture that way"
        return f"{float(self.pts):.6f}s"


@dataclass(frozen=True)
class SpanAnswer:
    """The pictures in a span, and whether the span was read all the way across."""

    pts: tuple[Fraction, ...]
    #: False when part of the span was never read: the frames listed are all the
    #: index can vouch for, and there may be more beyond what it read
    complete: bool
    coverage: tuple[Fraction, Fraction] | None = None


@dataclass(frozen=True)
class _Region:
    """A stretch of the stream a set of reads enumerated, and what it can close.

    ``pts`` are the picture starts proven inside it. The two flags say whether the
    span runs up to a limit beyond which nothing can exist, which is what licenses
    saying "there is no picture that way" instead of "I did not read that far":
    the axis origin when looking earlier, and a read that ran past the end the
    container claims for its own content when looking later.
    """

    start: Fraction
    end: Fraction
    #: every picture start proven inside it, ascending
    pts: tuple[Fraction, ...]
    #: the span reaches the origin of the asset axis
    reaches_floor: bool = False
    #: the span reaches past the container's own claim about where content stops
    reaches_end: bool = False


class FrameIndex:
    """Real picture timestamps of one video stream, read in bounded windows.

    Use from one thread at a time: it is a cache of subprocess results, not a
    concurrent structure. Every method answers in *asset-local* time, the same
    axis as ``StreamSource``/``Clip.source_in``.

    The plain lookups return the timestamp, or ``None`` for "no picture that way
    **or I could not read far enough to know**". A caller that needs to tell those
    apart — because it is about to claim a stream ended, or to refuse a cut — uses
    :meth:`answer_at_or_before`, :meth:`answer_at_or_after`, :meth:`answer_step`
    or :meth:`answer_frames_in` and looks at :attr:`FrameAnswer.status`. ``None``
    from a plain lookup is never a promise that a boundary was reached.

    The window cache is scoped to the file's identity (path plus the size and
    ``mtime_ns`` of :mod:`video_editor.fs.identity`): every lookup stats the file
    and drops its reads when the file is not the one it read, so a re-encode left
    at the same path cannot be navigated with another file's timestamps.
    """

    def __init__(
        self,
        path: Path,
        stream_index: int,
        ffprobe: str | None = None,
        window: Fraction = DEFAULT_WINDOW,
        max_windows: int = MAX_WINDOWS,
        timeout: float = 60,
        probe: Callable[[Fraction, Fraction], PictureWindow] | None = None,
    ) -> None:
        """*probe* replaces the read itself.

        Production never passes it. Tests do, to feed fabricated probe answers —
        a seek that lands earlier than asked, a read that covers only part of a
        window, a stream that ends mid-span — so the coverage and termination
        rules can be checked exactly, and with an upper bound on the probes a
        question may cost.
        """
        if window <= 0:
            raise FrameIndexError(f"probe window {window} must be positive")
        self.path = Path(path)
        self.stream_index = stream_index
        self.ffprobe = ffprobe
        self.window = window
        # reads must overlap to make the enumerated ground contiguous, and never by
        # more than half the window, or a narrow window would gain nothing per read
        self._overlap = min(PROBE_OVERLAP, self.window / 2)
        self.max_windows = max(1, max_windows)
        self.timeout = timeout
        self._probe_read = probe
        self._timing: StreamTiming | None = None
        self._declared: DeclaredEnd = DeclaredEnd(None, "not probed")
        #: earliest addressable time on this axis; see :meth:`_revalidate`
        self._floor = F(0)
        self._identity: Identity | None = None
        self._windows: list[PictureWindow] = []
        self._regions_cache: tuple[_Region, ...] | None = None
        # questions already read to the end of what is known, so that asking the same
        # thing again costs nothing; any read that adds ground clears this, so a
        # remembered "not known" can never hide a picture the index has since found
        self._unknown: dict[tuple[Fraction, int, bool], FrameAnswer] = {}
        self.probes = 0  #: reads actually run, for tests and for honest reports
        self.invalidations = 0  #: times the file changed under the index
        self._revalidate()

    # ------------------------------------------------------------------ identity

    @property
    def time_base(self) -> Fraction:
        """Container time base of this stream (exact, not an estimate)."""
        return self._timing.time_base

    @property
    def stream_position(self) -> int:
        """Position among video streams, which is what FFmpeg addresses by."""
        return self._timing.position

    @property
    def declared_end(self) -> DeclaredEnd:
        """What the container claims about where this file stops — a claim, not a measurement.

        It is used for one thing only: a read *asked past it* had to demux the rest of
        the stream, so its silence after its last decoded picture may be reported as
        "no picture later than here". No read reaches it, no licence, and the answer
        stays UNKNOWN. It never invents a picture, and it never bounds a clip.
        """
        return self._declared

    @property
    def file_identity(self) -> Identity | None:
        """The size and ``mtime_ns`` this index's reads belong to (None if unreadable)."""
        return self._identity

    # -------------------------------------------------------------------- lookups

    def at_or_before(self, t: Fraction) -> Fraction | None:
        """PTS of the last real picture at or before ``t`` (the frame the
        composition shows for that time), or None if there is none or it is unknown."""
        return self.answer_at_or_before(t).pts

    def at_or_after(self, t: Fraction) -> Fraction | None:
        """PTS of the first real picture at or after ``t`` (what an input seek to
        ``t`` decodes first), or None if there is none or it is unknown."""
        return self.answer_at_or_after(t).pts

    def step(self, t: Fraction, direction: int) -> Fraction | None:
        """Neighbouring real picture strictly either side of ``t``.

        ``direction`` is +1 (later) or -1 (earlier). None means the index found no
        picture that way — which covers both a genuine edge of the stream and a
        stretch it was not allowed to read; only :meth:`answer_step` says which,
        and a caller that reports an edge to a user should use it.
        """
        return self.answer_step(t, direction).pts

    def answer_at_or_before(self, t: Fraction) -> FrameAnswer:
        return self._answer(t, -1, strict=False)

    def answer_at_or_after(self, t: Fraction) -> FrameAnswer:
        return self._answer(t, +1, strict=False)

    def answer_step(self, t: Fraction, direction: int) -> FrameAnswer:
        """Neighbouring real picture strictly either side of ``t``, with its status."""
        if direction == 0:
            return self.answer_at_or_before(t)
        return self._answer(t, 1 if direction > 0 else -1, strict=True)

    def frames_in(self, start: Fraction, end: Fraction) -> tuple[Fraction, ...]:
        """Every proven picture timestamp in ``[start, end)``, ascending.

        Part of a span wider than the probe budget may go unread: the frames
        listed are then all the index can vouch for, and :meth:`answer_frames_in`
        says so.
        """
        return self.answer_frames_in(start, end).pts

    def answer_frames_in(self, start: Fraction, end: Fraction) -> SpanAnswer:
        """Pictures in ``[start, end)``, plus whether the span was read across.

        The frames come from the ground enumerated **from ``start`` rightwards**: each
        read continues from where the last one got to, so a span wider than the probe
        budget stops and reports ``complete=False`` with the frames it reached. A read
        that landed beyond an unread gap is deliberately left out of the answer: it
        would make the list look like a continuous read of the span. A span that runs
        off the end of the stream is complete instead - a read asked past the end the
        container declares enumerated the rest of the stream and found nothing, so
        there is nothing left to read across.
        """
        if end <= start:
            return SpanAnswer((), True, None)
        self._revalidate()
        reached: tuple[Fraction, Fraction] | None = None
        requests = {(w.read_from, w.read_to) for w in self._windows}
        planned_start = F(0)
        for attempt in range(MAX_PROBES_PER_QUERY + 1):
            reached = self._span_reach(start)
            span = self._region_at(start)
            if reached is not None and reached[0] <= start and (
                reached[1] >= end or (span is not None and span.reaches_end)
            ):
                # either the ground up to ``end`` has been enumerated, or a read that
                # ran past the end this container claims already established that
                # there is nothing more to find anywhere later than it
                break
            if attempt >= MAX_PROBES_PER_QUERY:
                break
            if reached is None or reached[0] > start:
                next_start = start
            else:
                # walk rightwards from where the reading got to, but never from the
                # same place twice: a read whose pictures stop short of its window
                # moves the covered span by less than its width
                advance = max(self.window - self._overlap, self.window / 2)
                next_start = max(reached[1] - self._overlap, planned_start + advance)
            next_start = max(F(0), next_start)
            planned_start = next_start
            planned = (next_start, next_start + self.window)
            if planned in requests:
                break
            requests.add(planned)
            self._probe(next_start)
            grown = self._span_reach(start)
            if reached is not None and grown is not None and grown[1] <= reached[1]:
                break  # this read enumerated nothing beyond the last one: stop here
        region = self._region_at(start)
        reached = self._span_reach(start)  # fresh: the last probe may have extended it
        found: list[Fraction] = []
        if region is not None:
            # only the ground read across from ``start``: a second read that landed
            # beyond an unread gap proves nothing about this span, and listing its
            # pictures would hide the hole in the middle of the answer
            lo = bisect_left(region.pts, start)
            hi = bisect_left(region.pts, end)
            found = list(region.pts[lo:hi])
        complete = (
            reached is not None
            and region is not None
            and reached[0] <= start
            and (reached[1] >= end or region.reaches_end)
        )
        return SpanAnswer(tuple(found), complete, reached)

    # -------------------------------------------------------------------- caching

    def cached_spans(self) -> tuple[tuple[Fraction, Fraction], ...]:
        """The windows that were *asked for* (ordered), for reports and tests."""
        return tuple(
            (w.read_from, w.read_to) for w in sorted(self._windows, key=lambda w: w.read_from)
        )

    def proven_spans(self) -> tuple[tuple[Fraction, Fraction], ...]:
        """What those reads actually proved, merged: spans a question can rely on."""
        return tuple((r.start, r.end) for r in self._regions())

    def forget(self) -> None:
        """Drop every cached window (a changed file invalidates all of them)."""
        self._windows.clear()
        self._regions_cache = None
        self._unknown.clear()

    # ------------------------------------------------------------------ internals

    def _revalidate(self) -> None:
        """Re-read the metadata if the file is no longer the one being indexed.

        The metadata probe (stream time base, position, declared end) happens once
        per index and is only repeated when the file's identity changed: the point
        is that no question pays for it, and no question is answered from a
        different file's timestamps.
        """
        try:
            identity = Identity.of(self.path)
        except OSError as exc:
            raise FrameIndexError(f"cannot index {self.path.name}: {exc}") from exc
        if identity == self._identity:
            return
        if self._identity is not None:
            self.forget()
            self.invalidations += 1
        self._identity = identity
        try:
            self._timing = stream_timing(self.path, self.stream_index, self.ffprobe, self.timeout)
        except StillError as exc:
            raise FrameIndexError(str(exc)) from exc
        # the axis origin, not the stream's declared start: a container that
        # misstates where its pictures begin must not be able to buy a "there is
        # nothing earlier" answer, and every read is clamped to this origin, so a
        # span that reaches it has enumerated everything this axis can address
        self._floor = F(0)
        # the container's own claim about where its content stops, used for one
        # thing only: licensing a read that ran past it to say "no picture later
        # than here". A read that stops short of it answers UNKNOWN instead.
        self._declared = declared_end(self.path, self._timing.start_time, self.ffprobe,
                                      self.timeout)

    def _probe(self, start: Fraction) -> tuple[Fraction, Fraction]:
        """Read one bounded window beginning at ``start``; returns the window asked for.

        The window is what the probe is *asked* for; whatever comes back is kept as
        it was reported, because the read starts at a keyframe at or before the
        window (so it brings preroll pictures) and a window asked past the end of a
        stream answers with that stream's tail. What the read proved is worked out
        where questions are answered, from :meth:`PictureWindow.enumerated`, not by
        throwing evidence away here.
        """
        start = max(F(0), start)
        end = start + self.window
        read = (
            self._probe_read(start, end)
            if self._probe_read is not None
            else decoded_picture_times(
                self.path, self.stream_index, start, end,
                ffprobe=self.ffprobe, timeout=self.timeout, timing=self._timing,
            )
        )
        self.probes += 1
        self._windows.append(read)
        self._regions_cache = None
        # a remembered "not known" is only true of the ground known when it was
        # written: this read may be the one that answers that question
        self._unknown.clear()
        self._evict()
        return (start, end)

    def _evict(self) -> None:
        """Keep the cache bounded: the window farthest from the latest read goes first.

        That keeps the neighbourhood the user is working in, which is what makes
        stepping and scrubbing cheap; a jump elsewhere costs one read, not a scan.
        (Sorting the list *descending* and popping its end, as an earlier revision
        did, removed the window nearest the read that had just been paid for: the
        cache then threw away exactly what it had used.)
        """
        if len(self._windows) <= self.max_windows:
            return
        centre = (self._windows[-1].read_from + self._windows[-1].read_to) / 2
        self._windows.sort(
            key=lambda w: abs(((w.read_from + w.read_to) / 2) - centre)
        )
        self._windows.pop()
        self._regions_cache = None
        self._unknown.clear()  # spans just got smaller: their edges moved

    def _regions(self) -> tuple[_Region, ...]:
        """Merge the reads into contiguous enumerated spans (rebuilt only when a read is added).

        What one read enumerated is decided by :meth:`PictureWindow.enumerated`, not
        by what it was asked for: measured behaviour is that it can stop short of its
        window and can run past it. Two reads count as one span when they touch or
        overlap: each enumerated its own span completely, so together they enumerate
        the union. Reads that do not touch say nothing about the ground between them,
        and stamps found there must not be used to answer a question on this side of
        the gap.
        """
        if self._regions_cache is not None:
            return self._regions_cache
        claimed = self._declared.end
        pieces: list[tuple[Fraction, Fraction, bool]] = []
        for window in self._windows:
            lo, hi = window.enumerated(claimed)
            pieces.append((lo, hi, claimed is not None and window.read_to >= claimed))
        pieces.sort(key=lambda piece: (piece[0], piece[1]))
        merged: list[list[object]] = []
        for lo, hi, licensed in pieces:
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
                merged[-1][2] = bool(merged[-1][2]) or licensed
            else:
                merged.append([lo, hi, licensed])
        regions: list[_Region] = []
        for lo, hi, licensed in merged:
            stamps: set[Fraction] = set()
            for window in self._windows:
                first = window.stamps[0] if window.stamps else window.read_from
                last = window.stamps[-1] if window.stamps else window.read_to
                if lo <= first and last <= hi:
                    stamps.update(window.stamps)
            regions.append(
                _Region(
                    lo, hi, tuple(sorted(stamps)),
                    reaches_floor=lo <= self._floor, reaches_end=bool(licensed),
                )
            )
        self._regions_cache = tuple(regions)
        return self._regions_cache

    def _region_at(self, t: Fraction) -> _Region | None:
        for region in self._regions():
            if region.start <= t <= region.end:
                return region
        return None

    def _span_reach(self, start: Fraction) -> tuple[Fraction, Fraction] | None:
        """How far the proven span starting at or before ``start`` reaches."""
        region = self._region_at(start)
        return None if region is None else (region.start, region.end)

    def _answer(self, t: Fraction, direction: int, strict: bool) -> FrameAnswer:
        """Nearest real picture in ``direction`` from ``t``, or an honest non-answer.

        Every pass either answers from an enumerated span, or reads one window: centred
        on the question if nothing has been read near it (a scrub jumps, it does not
        walk), otherwise continuing from the edge of the ground already enumerated, so
        the probe budget - not a repeated cursor - is the only thing that can end the
        loop, and a window is never read twice. An earlier revision moved the cursor to
        the edge of a window *before* consulting it, and when the answer was inside that
        window it re-asked a question the same window could never answer, forever
        (``tests/test_frame_index.py``).

        A candidate outside the span reachable from ``t`` is never used: a nearer
        picture could lie in the unread ground between, and answering with the
        farther one would step the user past a frame they never saw.
        """
        self._revalidate()
        if t < 0 and direction < 0:
            return FrameAnswer(None, AnswerStatus.NO_PICTURE, (F(0), F(0)))
        asked = max(F(0), t)
        key = (asked, direction, strict)
        remembered = self._unknown.get(key)
        if remembered is not None:
            # this exact question was already read to the end of what is known: the
            # answer has not changed, because any new picture would have been found
            # by a read that also clears this cache
            return remembered
        region: _Region | None = None
        # windows this index has already paid for count as asked: a second question
        # must not re-run a read the first one already rejected
        requests = {(w.read_from, w.read_to) for w in self._windows}
        cursor: Fraction | None = None
        for attempt in range(MAX_PROBES_PER_QUERY + 1):
            region = self._region_at(asked)
            if region is not None:
                answer = self._pick(region, asked, direction, strict)
                if answer is not None:
                    return answer
            near = region if region is not None else self._frontier_region(asked, direction)
            region = near if near is not None else region
            if attempt >= MAX_PROBES_PER_QUERY:
                break
            for _skip in range(MAX_PROBES_PER_QUERY):
                start = max(F(0), self._placement(near, asked, direction, cursor))
                planned = (start, start + self.window)
                if planned not in requests:
                    break
                cursor = start  # already read, and it did not answer: look farther out
            else:
                break
            requests.add(planned)
            cursor = start
            before = self._extent(direction)
            self._probe(start)
            after = self._extent(direction)
            if (
                before is not None
                and after is not None
                and (after <= before if direction > 0 else after >= before)
            ):
                # the decode enumerated nothing beyond what the previous read had
                # already enumerated: another read of this neighbourhood would prove
                # the same thing again, so the search stops here rather than spending
                # its whole budget on repeats of a fact it now knows
                break
        final = self._region_at(asked) or self._frontier_region(asked, direction)
        region = final if final is not None else region
        coverage = (region.start, region.end) if region is not None else None
        answer = FrameAnswer(None, AnswerStatus.UNKNOWN, coverage)
        if len(self._unknown) >= MAX_UNKNOWN_ANSWERS:
            self._unknown.clear()  # bounded: scrubbing asks a new question per pixel
        self._unknown[key] = answer
        return answer

    def _extent(self, direction: int) -> Fraction | None:
        """How far the enumerated ground reaches in one direction, over every span."""
        edges = [region.end if direction > 0 else region.start for region in self._regions()]
        if not edges:
            return None
        return max(edges) if direction > 0 else min(edges)

    def _placement(
        self,
        near: _Region | None,
        asked: Fraction,
        direction: int,
        cursor: Fraction | None,
    ) -> Fraction:
        """Where the next read of a search begins.

        The first read is centred on the question, and so is a read for a question
        more than a window away from anything already read - a scrub jumps, it does
        not walk. Otherwise the search continues from the edge of the ground it has
        enumerated, overlapping it so the spans join up, and never at a place it has
        already stood: a search that re-reads a window it just read is the bug this
        whole method exists to prevent.
        """
        advance = max(self.window - self._overlap, self.window / 2)
        if direction > 0:
            if cursor is None and near is not None and asked - near.end > self.window:
                return asked - self.window / 2  # a jump: read around the question
            start = near.end - self._overlap if near is not None else asked - self.window / 2
            return start if cursor is None else max(start, cursor + advance)
        if cursor is None and near is not None and near.start - asked > self.window:
            return asked - self.window / 2
        start = (
            near.start - self.window + self._overlap
            if near is not None
            else asked - self.window / 2
        )
        return start if cursor is None else min(start, cursor - advance)

    def _frontier_region(self, t: Fraction, direction: int) -> _Region | None:
        """The span closest to ``t`` on the side the question is looking at.

        Used only to decide where to read next, never to answer from: a span that
        does not contain the question has unread ground between it and the question,
        so its stamps cannot be the nearest ones.
        """
        best: _Region | None = None
        for region in self._regions():
            if direction > 0:
                if region.end <= t and (best is None or region.end > best.end):
                    best = region
            elif region.start >= t and (best is None or region.start < best.start):
                best = region
        return best

    def _pick(
        self, region: _Region, t: Fraction, direction: int, strict: bool
    ) -> FrameAnswer | None:
        """Answer from one enumerated span, or None when that span is not enough.

        ``None`` here means "keep reading": the span does not reach far enough in the
        requested direction to say that nothing is there. Saying ``NO_PICTURE`` needs
        a span that runs to a limit beyond which nothing can exist — see
        :attr:`_Region.reaches_floor` and :attr:`_Region.reaches_end` — and a span
        that merely stopped because the reading stopped says nothing.
        """
        pts = region.pts
        span = (region.start, region.end)
        if direction > 0:
            index = bisect_right(pts, t) if strict else bisect_left(pts, t)
            if index < len(pts):
                return FrameAnswer(pts[index], AnswerStatus.FOUND, span)
            if region.reaches_end:
                return FrameAnswer(None, AnswerStatus.NO_PICTURE, span)
            return None
        index = bisect_left(pts, t) if strict else bisect_right(pts, t)
        if index > 0:
            return FrameAnswer(pts[index - 1], AnswerStatus.FOUND, span)
        if region.reaches_floor:
            return FrameAnswer(None, AnswerStatus.NO_PICTURE, span)
        return None

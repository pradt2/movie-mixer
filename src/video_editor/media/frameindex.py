"""Decoded frame index: where this stream's real pictures actually are.

The composition works in exact rational times, and the frame shown for a
composition time is *the last real picture at or before it*. Answering that
question honestly needs the stream's actual presentation timestamps, which a
container records per packet: nominal frame rate and "duration / frame rate"
invent a grid that variable-frame-rate material, dropped frames, and streams
whose first picture does not sit at the start do not have.

A whole film is never scanned. Each probe reads one **bounded window** of packets
around the time being asked about. Measured with FFmpeg 8.1.3 on a one-hour,
25 fps H.264 fixture (58 MiB, both Matroska and MP4):

* a 20 s window of packets: 0.12 s (500 timestamps);
* a 2 s window of decoded frames: 0.16-0.18 s, a 20 s window of frames: 0.54 s;
* the whole file's packets: 90 000 timestamps in 0.56-0.64 s.

So packet timestamps are the cheap, correct choice (a packet's ``pts`` is its
presentation time, so B-frame reordering does not disturb it), and a 20 s window
is wide enough that stepping through hundreds of frames costs one probe. Windows
are kept in a bounded cache and reused, which is what makes repeated navigation
on a long source cheap after the first step.

This module reads media; it knows nothing about the timeline, and nothing about
Qt: callers that want it off the GUI thread run it in their own worker.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .framegrab import StillError, source_frame_times, stream_timing

F = Fraction

#: Source time read by one probe. Wide enough that stepping through a long run of
#: frames needs one probe (20 s is 500 frames at 25 fps), narrow enough to cost a
#: fraction of a second (measured above).
DEFAULT_WINDOW = F(20)

#: How many windows one stream keeps. Six 20 s windows hold 6000 timestamps (a few
#: hundred kilobytes) and cover the several places a scrubbing user may jump to.
MAX_WINDOWS = 6

#: How many extra probes one query may trigger when the answer lies outside every
#: cached window. Bounded so a request near a gap cannot turn into a full scan.
MAX_PROBES_PER_QUERY = 4


class FrameIndexError(StillError):
    """The stream's timestamps cannot be read (missing file, probe failure)."""


@dataclass(frozen=True)
class _Window:
    """One probed span of a stream, ascending, half-open like the rest here."""

    start: Fraction
    end: Fraction
    pts: tuple[Fraction, ...]

    def covers(self, t: Fraction) -> bool:
        return self.start <= t < self.end


class FrameIndex:
    """Real picture timestamps of one video stream, read in bounded windows.

    Use from one thread at a time: it is a cache of subprocess results, not a
    concurrent structure. Every method answers in *asset-local* time, the same
    axis as ``StreamSource``/``Clip.source_in``.

    ``None`` from a lookup means "this stream has no picture that way" - the start
    or end of the stream was reached - and is how callers detect a cut boundary.
    A stream with no readable packets at all behaves the same way, so a caller
    that needs certainty asks for :meth:`time_base` and reports what it found.
    """

    def __init__(
        self,
        path: Path,
        stream_index: int,
        ffprobe: str | None = None,
        window: Fraction = DEFAULT_WINDOW,
        max_windows: int = MAX_WINDOWS,
        timeout: float = 60,
    ) -> None:
        if window <= 0:
            raise FrameIndexError(f"probe window {window} must be positive")
        self.path = Path(path)
        self.stream_index = stream_index
        self.ffprobe = ffprobe
        self.window = window
        self.max_windows = max(1, max_windows)
        self.timeout = timeout
        self._timing = stream_timing(path, stream_index, ffprobe, timeout)
        self._windows: list[_Window] = []
        self.probes = 0  #: probes actually run, for tests and for honest reports

    # ------------------------------------------------------------------ identity

    @property
    def time_base(self) -> Fraction:
        """Container time base of this stream (exact, not an estimate)."""
        return self._timing.time_base

    @property
    def stream_position(self) -> int:
        """Position among video streams, which is what FFmpeg addresses by."""
        return self._timing.position

    # -------------------------------------------------------------------- lookups

    def at_or_before(self, t: Fraction) -> Fraction | None:
        """PTS of the last real picture at or before ``t`` (the frame the
        composition shows for that time), or None before the stream's first one."""
        return self._search(t, -1)

    def at_or_after(self, t: Fraction) -> Fraction | None:
        """PTS of the first real picture at or after ``t`` (what an input seek to
        ``t`` decodes first), or None past the stream's last one."""
        return self._search(t, +1)

    def step(self, t: Fraction, direction: int) -> Fraction | None:
        """Neighbouring real picture strictly either side of ``t``.

        ``direction`` is +1 (later) or -1 (earlier); None means the edge of the
        stream has been reached, which is what tells the user a source edge cannot
        be moved further.
        """
        if direction == 0:
            return self.at_or_before(t)
        if direction > 0:
            later = [w for w in self._covering(t)]
            for window in later:
                tail = [p for p in window.pts if p > t]
                if tail:
                    return tail[0]
            return self._search(t, +1, strict=True)
        earlier = [p for w in self._covering(t) for p in w.pts if p < t]
        if earlier:
            return max(earlier)
        return self._search(t, -1, strict=True)

    def frames_in(self, start: Fraction, end: Fraction) -> tuple[Fraction, ...]:
        """Every real picture timestamp in ``[start, end)``, ascending.

        Reads only the windows needed; a span wider than the cache probes just
        enough windows to cover it, stopping at :data:`MAX_PROBES_PER_QUERY` so a
        silly request cannot turn into a scan of a film.
        """
        if end <= start:
            return ()
        for _ in range(MAX_PROBES_PER_QUERY):
            if self._covered(start, end):
                break
            self._probe((start + end) / 2)
        found: list[Fraction] = []
        for window in self._windows:
            # half-open on purpose: the frame exactly at ``start`` belongs to the
            # span, the one exactly at ``end`` does not
            lo = bisect_left(window.pts, start)
            hi = bisect_left(window.pts, end)
            found.extend(window.pts[lo:hi])
        return tuple(sorted(set(found)))

    # -------------------------------------------------------------------- caching

    def cached_spans(self) -> tuple[tuple[Fraction, Fraction], ...]:
        """What this index has already read (ordered), for reports and tests."""
        return tuple((w.start, w.end) for w in sorted(self._windows, key=lambda w: w.start))

    def forget(self) -> None:
        """Drop every cached window (a changed file invalidates all of them)."""
        self._windows.clear()

    # ------------------------------------------------------------------ internals

    def _covering(self, t: Fraction) -> list[_Window]:
        hit = [w for w in self._windows if w.covers(t)]
        if hit:
            return hit
        for _ in range(MAX_PROBES_PER_QUERY):
            self._probe(t)
            hit = [w for w in self._windows if w.covers(t)]
            if hit:
                return hit
            # the probe found nothing at all: the stream has no packets there
            if not self._windows:
                break
        return []

    def _covered(self, start: Fraction, end: Fraction) -> bool:
        return any(w.start <= start and end <= w.end for w in self._windows)

    def _search(self, t: Fraction, direction: int, strict: bool = False) -> Fraction | None:
        """Nearest real picture in ``direction`` from ``t``, probing as needed.

        Each pass either answers from what has already been read, or reads one
        window strictly further out than anything read before, so the bounded
        probe budget is always the thing that ends the loop. An earlier version
        moved the search position to the edge of the window it had just read
        *before* asking that window the question; when the answer was inside that
        window it looped forever without spending a probe — measured as a hang on
        ``at_or_after`` for a six-second clip (``tests/test_frame_index.py``).

        A probe that comes back with no packets is the edge of the stream, and a
        window that already reaches the start of the asset axis means nothing
        exists earlier: both answer ``None`` instead of spending the remaining
        probes.
        """
        centre = t
        probed: set[Fraction] = set()
        for _ in range(MAX_PROBES_PER_QUERY):
            lo, hi = self._reach(t)
            found = self._inside_reach(t, direction, strict, lo, hi)
            if found is not None:
                return found
            if any(not window.pts for window in self._windows):
                return None  # a window with no packets: the edge, or a hole, is here
            if hi > lo:  # something has been read around ``t``: reach past its edge
                centre = hi + self.window / 2 if direction > 0 else max(F(0), lo - self.window / 2)
            else:  # nothing is known at ``t`` yet: read its neighbourhood first
                centre = t
            if centre in probed:
                return None  # nothing new left to read within the budget
            probed.add(centre)
            self._probe(centre)
        return None

    def _reach(self, t: Fraction) -> tuple[Fraction, Fraction]:
        """The span read in one piece around ``t``, or ``[t, t]`` if nothing is.

        Windows are separate reads. A window that does not touch the one holding
        ``t`` says nothing about what lies between ``t`` and its own contents, so
        its stamps cannot be used to answer "which frame comes next" - and an
        earlier revision used them anyway, answering ``at_or_after(0)`` with a
        frame at 7 s because a window around 8 s happened to be cached.
        """
        lo = hi = t
        while True:
            grew = False
            for window in self._windows:
                if (window.start <= hi and window.end >= lo) and (
                    window.start < lo or window.end > hi
                ):
                    lo, hi = min(lo, window.start), max(hi, window.end)
                    grew = True
            if not grew:
                return lo, hi

    def _inside_reach(
        self, t: Fraction, direction: int, strict: bool, lo: Fraction, hi: Fraction
    ) -> Fraction | None:
        """Best answer among the windows that are actually reachable from ``t``.

        Candidates outside the contiguous span that was read are ignored: they may
        be real frames, but there could be a nearer one in the gap that has not
        been read, and answering with the farther one would move the user past a
        frame they never saw.
        """
        best: Fraction | None = None
        for window in self._windows:
            found = self._pick(window, t, direction, strict)
            if found is None or not lo <= found <= hi:
                continue
            if best is None or (found < best if direction > 0 else found > best):
                best = found
        return best

    @staticmethod
    def _touches(window: _Window, t: Fraction) -> bool:
        return window.start <= t <= window.end

    @staticmethod
    def _pick(
        window: _Window, t: Fraction, direction: int, strict: bool
    ) -> Fraction | None:
        pts = window.pts
        if direction > 0:
            i = bisect_right(pts, t) if strict else bisect_left(pts, t)
            return pts[i] if i < len(pts) else None
        i = bisect_left(pts, t) if strict else bisect_right(pts, t)
        return pts[i - 1] if i > 0 else None

    def _probe(self, t: Fraction) -> None:
        """Read one bounded window of packet timestamps around ``t``.

        The stamps are filtered to the window that was asked for, because
        ffprobe's interval read does not honour the bounds the way the word
        "interval" suggests: measured on FFmpeg 8.1.3, asking a 6 s Matroska clip
        for ``20%40`` - a range past its end - answered with the last 25 packets,
        from 5.00 s to 5.96 s, rather than with nothing. Kept as they came, those
        tail stamps would say "there are frames at 20 s" about frames that are
        nowhere near it, and the stream's end would never be recognised.
        """
        half = self.window / 2
        start = max(F(0), t - half)
        end = start + self.window
        frames = source_frame_times(
            self.path,
            self.stream_index,
            ffprobe=self.ffprobe,
            from_time=start,
            to_time=end,
            timeout=self.timeout,
        )
        self.probes += 1
        self._windows.append(
            _Window(
                start=start,
                end=end,
                pts=tuple(f.pts for f in frames if start <= f.pts < end),
            )
        )
        self._evict()

    def _evict(self) -> None:
        """Keep the cache bounded: the least recently useful window goes first.

        "Least useful" is the one whose centre is farthest from the most recent
        query, so navigation keeps the neighbourhood the user is working in.
        """
        if len(self._windows) <= self.max_windows:
            return
        newest = self._windows[-1]
        centre = (newest.start + newest.end) / 2
        self._windows.sort(
            key=lambda w: abs(((w.start + w.end) / 2) - centre), reverse=True
        )
        self._windows.pop()
        self._windows.sort(key=lambda w: w.start)

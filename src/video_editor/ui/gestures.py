"""Pure drag/trim/snapping helpers — the widgets contain no edit math.

Snapping tolerances are defined in **screen pixels**, converted to time via
the current rational zoom, so behaviour is predictable at every scale.
Precision entry (inspector, Go-to-Time) never snaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from ..core.model import Clip, Project

F = Fraction
SNAP_TOLERANCE_PX = 8


@dataclass(frozen=True)
class SnapResult:
    time: Fraction
    #: the boundary we snapped to (for drawing a marker), None when free
    marker: Fraction | None


def snap_candidates(
    project: Project,
    exclude_clip_ids: frozenset[str],
    playhead: Fraction,
) -> tuple[Fraction, ...]:
    """Snap targets: playhead plus every output boundary of other clips."""
    points: list[Fraction] = [playhead]
    for clip in project.clips:
        if clip.id in exclude_clip_ids:
            continue
        points.append(clip.output_start)
        points.append(clip.output_end)
    return tuple(sorted(set(points)))


def snap_time(
    t: Fraction,
    candidates: tuple[Fraction, ...],
    pps: Fraction,
    enabled: bool = True,
) -> SnapResult:
    """Snap ``t`` to the nearest candidate within the pixel tolerance."""
    if not enabled or not candidates:
        return SnapResult(t, None)
    tolerance = F(SNAP_TOLERANCE_PX) / pps
    best: Fraction | None = None
    best_dist: Fraction | None = None
    for cand in candidates:
        dist = abs(cand - t)
        if dist <= tolerance and (best_dist is None or dist < best_dist):
            best, best_dist = cand, dist
    if best is None:
        return SnapResult(t, None)
    return SnapResult(best, best)


def drag_delta_from_pixels(dx: int, pps: Fraction) -> Fraction:
    """Exact output-time delta for a horizontal pixel drag."""
    return F(dx) / pps


def trim_source_time(
    clip: Clip, edge: str, boundary_output_time: Fraction
) -> Fraction:
    """Asset-local source time at an output boundary (used by trim gestures)."""
    return clip.source_time_at(boundary_output_time)

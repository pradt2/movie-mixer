"""Pure tests for drag/snapping helpers (no Qt widgets involved)."""

from fractions import Fraction

from video_editor.core.model import Clip, Project, Track, TrackKind
from video_editor.ui.gestures import (
    SNAP_TOLERANCE_PX,
    drag_delta_from_pixels,
    snap_candidates,
    snap_time,
    trim_source_time,
)

F = Fraction


def project_for_snaps() -> Project:
    t = Track("v1", TrackKind.VIDEO, "V1")
    c1 = Clip("c1", "a", 0, "v1", F(0), F(10), F(0))
    c2 = Clip("c2", "a", 0, "v1", F(0), F(10), F(20))
    return Project(tracks=(t,), clips=(c1, c2))


def test_snap_candidates_exclude_dragged():
    p = project_for_snaps()
    points = snap_candidates(p, frozenset({"c1"}), F(17))
    assert F(17) in points  # playhead
    assert F(20) in points and F(30) in points  # other clip boundaries
    assert F(10) not in points  # excluded clip's boundary
    assert points == tuple(sorted(points))


def test_snap_within_pixel_tolerance():
    pps = F(25)  # 25 px per second -> tolerance 8/25 s
    cand = (F(4),)
    res = snap_time(F(4) + F(3, 25), cand, pps, True)  # 3 px away
    assert res.time == F(4) and res.marker == F(4)
    res = snap_time(F(4) + F(20, 25), cand, pps, True)  # 20 px away
    assert res.time == F(4) + F(20, 25) and res.marker is None


def test_snap_tolerance_is_pixel_defined_across_zooms():
    # same 8 px tolerance yields proportionally different time windows
    far_zoom = F(1, 10)  # 0.1 px/s -> 8 px = 80 s
    res = snap_time(F(100), (F(150),), far_zoom, True)
    assert res.time == F(150)
    near_zoom = F(1000)  # 8 px = 0.008 s
    res = snap_time(F(100), (F(150),), near_zoom, True)
    assert res.time == F(100)


def test_snap_disabled_returns_free_time():
    res = snap_time(F(5), (F(5),), F(100), enabled=False)
    assert res.time == F(5) and res.marker is None


def test_snap_nearest_candidate_wins():
    res = snap_time(F(10), (F(9), F(10.5)), F(2), True)  # tolerance 4 s
    assert res.time == F(10.5)


def test_drag_delta_exact():
    assert drag_delta_from_pixels(50, F(25)) == F(2)
    assert drag_delta_from_pixels(-1, F(24000, 1001)) == F(-1001, 24000)


def test_trim_source_time_matches_clip_math():
    c = Clip("c", "a", 0, "t", F(100), F(200), F(5), rate=F(2))
    # at output time 10: source = 100 + (10-5)*2 = 110
    assert trim_source_time(c, "left", F(10)) == F(110)
    assert trim_source_time(c, "right", F(10)) == F(110)


def test_tolerance_constant_public():
    assert SNAP_TOLERANCE_PX == 8

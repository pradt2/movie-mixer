"""Timeline widget tests: pixel<->time math, selection, playhead, culling."""

from fractions import Fraction

from PySide6.QtWidgets import QApplication

from video_editor.core.model import Clip, Project, Track, TrackKind
from video_editor.ui.timeline import HEADER_WIDTH, ROW_HEIGHT, RULER_HEIGHT, Timeline

F = Fraction


def sample_project() -> Project:
    t1 = Track(id="v1", kind=TrackKind.VIDEO, name="V1")
    t2 = Track(id="a1", kind=TrackKind.AUDIO, name="A1")
    c1 = Clip("clipv", "assetV", 0, "v1", F(10), F(20), F(0), rate=F(1))
    c2 = Clip("clipa", "assetA", 1, "a1", F(0), F(10), F(2))
    return Project(tracks=(t1, t2), clips=(c1, c2))


def make_timeline(app: QApplication) -> Timeline:
    tl = Timeline()
    tl.resize(1000, 400)
    tl.show()
    app.processEvents()
    return tl


def test_pixel_time_roundtrip_exact(qapp_instance: QApplication):
    tl = make_timeline(qapp_instance)
    tl.set_view(F(3600), F(25))  # 1 h offset, 25 px per second
    assert tl.time_at_pixel(HEADER_WIDTH) == F(3600)
    assert tl.time_at_pixel(HEADER_WIDTH + 250) == F(3610)
    assert tl.time_at_pixel(HEADER_WIDTH + 1) == F(3600) + F(1, 25)
    assert tl.pixel_at_time(F(3610)) == HEADER_WIDTH + 250
    # exact even at fractional pps
    tl.set_view(F(0), F(24000, 1001) / 4)
    t = tl.time_at_pixel(HEADER_WIDTH + 12)
    assert t == F(12) / (F(24000, 1001) / 4)
    assert tl.pixel_at_time(t) == HEADER_WIDTH + 12


def test_zoom_centred_on_time_keeps_it_in_place(qapp_instance: QApplication):
    tl = make_timeline(qapp_instance)
    tl.set_view(F(100), F(10))
    center = F(130)
    before = tl.pixel_at_time(center)
    tl.zoom_at(2.0, center)
    after = tl.pixel_at_time(center)
    assert abs(after - before) < 1e-6
    assert tl.pps == F(20)
    tl.zoom_at(0.5, center)
    assert tl.pps == F(10)


def test_zoom_clamps(qapp_instance: QApplication):
    tl = make_timeline(qapp_instance)
    for _ in range(60):
        tl.zoom_in()
    assert tl.pps <= 4000
    for _ in range(80):
        tl.zoom_out()
    assert tl.pps >= F(1, 400)


def test_view_range_and_fit(qapp_instance: QApplication):
    tl = make_timeline(qapp_instance)
    tl.set_project(sample_project())
    tl.fit_whole()
    start, end = tl.view_range()
    assert start == F(0)
    assert end >= F(12)  # output end of sample project


def test_long_project_scales_without_drift(qapp_instance: QApplication):
    two_hours = F(7200)
    big_clip = Clip("late", "assetV", 0, "v1", F(0), F(60), two_hours)
    project = Project(
        tracks=(Track("v1", TrackKind.VIDEO, "V1"),),
        clips=(big_clip,),
    )
    tl = make_timeline(qapp_instance)
    tl.set_project(project)
    tl.fit_whole()
    _start, end = tl.view_range()
    assert end >= two_hours + F(60)
    # zoom to seconds around the late clip: exact positioning preserved
    tl.set_view(two_hours - F(10), F(50))
    assert tl.time_at_pixel(HEADER_WIDTH + 500) == two_hours
    assert tl.pixel_at_time(two_hours) == HEADER_WIDTH + 500


def test_playhead_set_and_signal(qapp_instance: QApplication):
    tl = make_timeline(qapp_instance)
    seen = []
    tl.playheadChanged.connect(seen.append)
    tl.set_playhead(F(5025), from_user=True)  # 1:23:45 in exact seconds
    assert tl.playhead == F(5025)
    assert seen == [F(5025)]
    tl.set_playhead(F(-5), from_user=True)
    assert tl.playhead == F(0)  # never negative


def test_clip_hit_testing_and_selection(qapp_instance: QApplication):
    tl = make_timeline(qapp_instance)
    tl.set_project(sample_project())
    tl.set_view(F(0), F(20))
    # video clip [0,10) on row 0
    x = int(tl.pixel_at_time(F(5)))
    y = RULER_HEIGHT + ROW_HEIGHT // 2
    hit = tl.clip_at_pixel(x, y)
    assert hit is not None and hit.id == "clipv"
    # audio clip [2,12) on row 1
    y2 = RULER_HEIGHT + ROW_HEIGHT + ROW_HEIGHT // 2
    hit2 = tl.clip_at_pixel(int(tl.pixel_at_time(F(6))), y2)
    assert hit2 is not None and hit2.id == "clipa"
    # gap on row 0 at t=15
    assert tl.clip_at_pixel(int(tl.pixel_at_time(F(15))), y) is None

    selections = []
    tl.clipSelectionChanged.connect(selections.append)
    tl.set_clip_selection(("clipv",))
    tl.set_clip_selection(("clipv", "clipa"))
    assert selections[-1] == ("clipv", "clipa")
    assert tl.selected_clips == ("clipv", "clipa")


def test_click_in_ruler_moves_playhead(qapp_instance: QApplication):
    tl = make_timeline(qapp_instance)
    tl.set_view(F(0), F(20))
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QMouseEvent

    pos = QPoint(HEADER_WIDTH + 40, 10)
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, pos, tl.viewport().mapToGlobal(pos),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    tl.mousePressEvent(event)
    assert tl.playhead == F(2)


def test_viewport_culling_is_positional_only(qapp_instance: QApplication):
    # many clips across a long project: drawing must not walk all of them per
    # frame. We assert the public geometry contract holds at extreme zoom.
    clips = tuple(
        Clip(f"c{i}", "a", 0, "v1", F(i * 10), F(i * 10 + 5), F(i * 10))
        for i in range(300)
    )
    project = Project(tracks=(Track("v1", TrackKind.VIDEO, "V1"),), clips=clips)
    tl = make_timeline(qapp_instance)
    tl.set_project(project)
    tl.set_view(F(1500), F(1))  # 1 px per second: window is ~1000 s
    start, end = tl.view_range()
    assert start == F(1500) and end - start >= 800
    hit = tl.clip_at_pixel(int(tl.pixel_at_time(F(1502))), RULER_HEIGHT + ROW_HEIGHT // 2)
    assert hit is not None and hit.id == "c150"

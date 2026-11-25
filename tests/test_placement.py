"""Placement + navigation acceptance at the GUI level (offscreen)."""

import time
from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QApplication

from media_fixtures import (
    HAS_FFMPEG,
    make_audio_only,
    make_mixed,
    make_stream_offset,
    make_video_only,
)
from video_editor.core.commands import AddTrackCommand
from video_editor.core.model import TrackKind
from video_editor.ui.main_window import MainWindow
from video_editor.ui.media_bin import ROLE_ASSET_ID, ROLE_STREAM_INDEX

F = Fraction

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")


def import_wait(app: QApplication, window: MainWindow, paths) -> None:
    window.bin_controller.request_import(list(paths))
    deadline = time.monotonic() + 60
    while window.bin_controller.pending_imports and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window.bin_controller.pending_imports == 0
    for _ in range(100):
        app.processEvents()
        time.sleep(0.01)


def stream_item(window: MainWindow, asset_id: str, stream_index: int):
    for i in range(window.media_bin.topLevelItemCount()):
        top = window.media_bin.topLevelItem(i)
        if top.data(0, ROLE_ASSET_ID) != asset_id:
            continue
        for j in range(top.childCount()):
            child = top.child(j)
            if int(child.data(0, ROLE_STREAM_INDEX)) == stream_index:
                return child
    raise AssertionError(f"stream {asset_id}#{stream_index} not in bin tree")


def select_only(window: MainWindow, items) -> None:
    sel = window.media_bin.selectionModel()
    sel.clearSelection()
    for item in items:
        index = window.media_bin.indexFromItem(item)
        sel.select(index, QItemSelectionModel.Select | QItemSelectionModel.Rows)


def asset_by_path(window: MainWindow, path) -> object:
    """Import completion order is thread-dependent, so look assets up by path."""
    for asset in window.bin_controller.assets:
        if Path(asset.path) == Path(path):
            return asset
    raise AssertionError(f"asset {path} not imported")


def test_place_two_assets_land_on_separate_tracks(qapp_instance: QApplication, tmp_path):
    window = MainWindow()
    mixed = make_mixed(tmp_path)
    wav = make_audio_only(tmp_path)
    import_wait(qapp_instance, window, [mixed, wav])
    asset_mixed = asset_by_path(window, mixed)
    asset_wav = asset_by_path(window, wav)

    # video from mixed at playhead 0
    window.timeline.set_playhead(F(0))
    select_only(window, [stream_item(window, asset_mixed.id, 0)])
    window._place_selected_streams()
    assert len(window.session.project.clips) == 1
    clip_v = window.session.project.clips[0]
    assert window.session.project.track(clip_v.track_id).kind is TrackKind.VIDEO

    # audio-only asset at playhead 5 -> new audio track (no video overlap issue)
    window.timeline.set_playhead(F(5))
    select_only(window, [stream_item(window, asset_wav.id, 0)])
    window._place_selected_streams()
    assert len(window.session.project.clips) == 2
    clip_a = next(c for c in window.session.project.clips if c.id != clip_v.id)
    assert clip_a.track_id != clip_v.track_id
    assert clip_a.output_start == F(5)
    assert window.session.project.output_end() > 0

    # undo both placements (placement may also register sources: undo the
    # whole sequence and confirm the clips vanish, in history order)
    for _ in range(6):
        window._undo()
    assert len(window.session.project.clips) == 0
    window.close()


def test_grouped_placement_preserves_stream_offsets(qapp_instance: QApplication, tmp_path):
    window = MainWindow()
    offset_file = make_stream_offset(tmp_path)
    import_wait(qapp_instance, window, [offset_file])
    (asset,) = window.bin_controller.assets
    video_index = next(s.stream_index for s in asset.metadata.streams if s.kind.value == "video")
    audio_index = next(s.stream_index for s in asset.metadata.streams if s.kind.value == "audio")
    select_only(window, [stream_item(window, asset.id, video_index),
                         stream_item(window, asset.id, audio_index)])
    window.timeline.set_playhead(F(0))
    window._place_selected_streams()
    clips = {c.stream_index: c for c in window.session.project.clips}
    assert clips[video_index].output_start == F(0)
    # audio stream starts at ~1.0 s on the asset axis -> placed ~1 s later
    audio_start = clips[audio_index].output_start
    assert abs(audio_start - F(1)) <= Fraction(1, 10)
    assert abs(clips[audio_index].source_in - F(1)) <= Fraction(1, 10)
    window.close()


def test_zoom_whole_film_to_seconds_keeps_playhead(qapp_instance: QApplication, tmp_path):
    window = MainWindow()
    big = make_video_only(tmp_path)
    import_wait(qapp_instance, window, [big])
    window.timeline.fit_whole()
    whole_pps = window.timeline.pps
    window.timeline.set_playhead(F(1), from_user=True)
    for _ in range(12):
        window.timeline.zoom_in()
    start, end = window.timeline.view_range()
    assert start <= F(1) <= end
    assert window.timeline.pps > whole_pps * 100
    window.close()


def test_track_mute_toggle_is_undoable(qapp_instance: QApplication, tmp_path):
    window = MainWindow()
    window._apply(AddTrackCommand(TrackKind.AUDIO, "A1"))
    track_id = window.session.project.tracks[0].id
    window._on_track_toggle(track_id, "mute")
    assert window.session.project.track(track_id).muted
    window._undo()
    assert not window.session.project.track(track_id).muted
    window.close()


def test_overlapping_placement_creates_extra_track(qapp_instance: QApplication, tmp_path):
    window = MainWindow()
    mixed = make_mixed(tmp_path)
    import_wait(qapp_instance, window, [mixed])
    (asset,) = window.bin_controller.assets
    select_only(window, [stream_item(window, asset.id, 0)])
    window.timeline.set_playhead(F(0))
    window._place_selected_streams()
    window.timeline.set_playhead(F(1))
    window._place_selected_streams()  # overlaps: goes to a new track, no overlap
    clips = window.session.project.clips
    assert len(clips) == 2
    assert clips[0].track_id != clips[1].track_id
    video_tracks = [t for t in window.session.project.tracks if t.kind is TrackKind.VIDEO]
    assert len(video_tracks) == 2
    from video_editor.core.commands import validate_project

    assert validate_project(window.session.project) == []
    window.close()

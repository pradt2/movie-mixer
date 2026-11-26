"""Offscreen GUI tests: media bin displays assets/streams; window survives imports."""

import time

import pytest
from PySide6.QtWidgets import QApplication

from media_fixtures import (
    HAS_FFMPEG,
    HAS_MPEG2,
    make_garbage,
    make_hdr10,
    make_interlaced_mpeg2,
    make_mixed,
    make_progressive_sdr,
)
from video_editor.ui.media_bin import MediaBinController

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffprobe on PATH")


def wait_imports(app: QApplication, controller: MediaBinController, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while controller.pending_imports and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert controller.pending_imports == 0
    # completion signals arrive through queued connections: drain them
    drain_deadline = time.monotonic() + 2.0
    while time.monotonic() < drain_deadline:
        app.processEvents()
        time.sleep(0.01)


def test_window_with_media_bin_launches(qapp_instance: QApplication):
    from video_editor.ui.main_window import MainWindow

    window = MainWindow()
    window.show()
    assert window.media_bin is not None
    assert window.import_action.isEnabled()
    window.close()


def test_import_populates_tree_and_handles_failure(qapp_instance: QApplication, tmp_path):
    from video_editor.ui.main_window import MainWindow

    window = MainWindow()
    good = make_mixed(tmp_path)
    bad = make_garbage(tmp_path)
    messages: list[str] = []
    window.bin_controller.status_message.connect(messages.append)
    window.bin_controller.request_import([good, bad])
    wait_imports(qapp_instance, window.bin_controller)

    assert len(window.bin_controller.assets) == 1
    asset = window.bin_controller.assets[0]
    assert asset.metadata.streams and asset.editable

    tree = window.media_bin
    assert tree.topLevelItemCount() == 1
    top = tree.topLevelItem(0)
    assert top.childCount() == 3  # video + two audio streams
    details = [top.child(i).text(1) for i in range(3)]
    assert any("eng" in details[i] for i in range(3)) or any("[eng]" in d for d in details)
    assert any("fre" in d for d in details)
    assert any("failed" in m for m in messages)

    # duplicate import rejected with a message, no new assets
    window.bin_controller.request_import([good])
    qapp_instance.processEvents()
    assert len(window.bin_controller.assets) == 1
    assert any("Already imported" in m for m in messages)
    window.close()


def test_a_picture_this_build_cannot_draw_is_listed_and_says_so(qapp_instance, tmp_path):
    """Importing is not the gate: the bin says what cannot be drawn and why.

    The file stays in the project — inspectable, and usable for its sound — so the note
    has to be readable where the file is seen instead of arriving later in an export
    error. The ordinary file next to it must say nothing of the kind.
    """
    if not HAS_MPEG2:
        pytest.skip("this FFmpeg has no mpeg2video encoder")
    from video_editor.ui.main_window import MainWindow

    interlaced = make_interlaced_mpeg2(tmp_path, "interlaced.ts")
    hdr = make_hdr10(tmp_path, "hdr.mkv")
    ordinary = make_progressive_sdr(tmp_path, "ordinary.mkv")

    window = MainWindow()
    messages: list[str] = []
    window.bin_controller.status_message.connect(messages.append)
    window.bin_controller.request_import([interlaced, hdr, ordinary])
    wait_imports(qapp_instance, window.bin_controller)

    # all three are in: a picture format this build cannot draw never blocks an import
    assert len(window.bin_controller.assets) == 3
    assert all(asset.editable for asset in window.bin_controller.assets)

    tree = window.media_bin
    rows = {
        tree.topLevelItem(i).text(0): tree.topLevelItem(i)
        for i in range(tree.topLevelItemCount())
    }
    assert set(rows) == {"interlaced.ts", "hdr.mkv", "ordinary.mkv"}
    for path in (interlaced, hdr, ordinary):
        assert rows[path.name].toolTip(0) == str(path)  # unchanged: the row says the path

    details = {name: [rows[name].child(r).text(1) for r in range(rows[name].childCount())]
               for name in rows}
    assert any("interlaced (field_order tt)" in text for text in details["interlaced.ts"])
    assert any("HDR (color_transfer smpte2084)" in text for text in details["hdr.mkv"])
    for text in details["ordinary.mkv"]:
        assert "interlaced" not in text and "HDR" not in text

    tooltips = {name: [rows[name].child(r).toolTip(1) for r in range(rows[name].childCount())]
                for name in rows}
    interlaced_notes = "\n".join(tooltips["interlaced.ts"])
    assert "no deinterlacer" in interlaced_notes
    assert "cannot be rendered, previewed or stepped frame by frame" in interlaced_notes
    assert "can still be imported and inspected" in interlaced_notes
    hdr_notes = "\n".join(tooltips["hdr.mkv"])
    assert "no colour or tone conversion" in hdr_notes
    assert "can still be imported, inspected and used for sound" in hdr_notes
    assert "\n".join(tooltips["ordinary.mkv"]) == ""

    # and the same is said when the file is imported, in the status line
    assert any("picture in stream 0 cannot be rendered" in m and "interlaced, field_order tt" in m
               and "inspection and sound still work" in m for m in messages), messages
    assert any("HDR, color_transfer smpte2084" in m for m in messages), messages
    window.close()


def test_the_new_note_does_not_replace_the_unplaceable_one(qapp_instance, tmp_path):
    """Two different kinds of note, and only one of them dims the row.

    A stream whose bounds are unknown cannot be placed at all and stays dimmed; a stream
    whose *picture* this build cannot draw is still placeable material — for its sound,
    and for inspection — so it is annotated without being dimmed.
    """
    from media_fixtures import make_identity_video
    from video_editor.media.assets import StreamKind
    from video_editor.media.ffprobe import probe_file
    from video_editor.ui.format import processing_caveat, stream_details

    drawable = make_identity_video(tmp_path, "identity.mkv", count=25)
    picture = probe_file(drawable).metadata.stream(0)
    assert picture.kind is StreamKind.VIDEO
    assert picture.editability_note is None and processing_caveat(picture) is None
    assert "field order" not in stream_details(picture)

    if not HAS_MPEG2:
        pytest.skip("this FFmpeg has no mpeg2video encoder")
    interlaced = make_interlaced_mpeg2(tmp_path, "interlaced.ts")
    stream = probe_file(interlaced).metadata.stream(0)
    assert stream.editability_note is None  # placeable material, still
    caveat = processing_caveat(stream)
    assert caveat and "no deinterlacer" in caveat
    assert "interlaced (field_order tt)" in stream_details(stream)

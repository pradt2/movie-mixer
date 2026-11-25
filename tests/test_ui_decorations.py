"""Timeline decorations: bounded planning, caching and background extraction.

The rules under test are the ones that keep a long timeline honest and usable: a
decoration is only ever a picture of what the clip already means, the amount of
decoding is bounded whatever the user asks for, work is reused between edits and
paints, and a decoration that cannot be made leaves the clip drawn as a shape
with a stated reason instead of a guess.
"""

import time
from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from core_helpers import audio_source
from media_fixtures import (
    HAS_FFMPEG,
    make_colour_steps,
    make_garbage,
    make_mixed,
    source_from_probe,
)
from video_editor.core.model import Clip, Project, StreamSource, Track, TrackKind
from video_editor.ui import decorations as module
from video_editor.ui.decorations import (
    BUCKET_LADDER,
    MAX_QUEUED_JOBS,
    ClipDecorations,
    Decorations,
    ladder_step,
    thumb_box,
)

F = Fraction

_LIVE: list[ClipDecorations] = []


def make_layer() -> ClipDecorations:
    layer = ClipDecorations()
    _LIVE.append(layer)
    return layer


@pytest.fixture(autouse=True)
def _stop_workers():
    """No extraction thread may outlive a test."""
    yield
    while _LIVE:
        _LIVE.pop().shutdown()


def fake_decoding(monkeypatch, delay: float = 0.0):
    """Replace the extractors so planning can be tested without FFmpeg."""

    class Fake:
        nbytes = 4096

    def envelope(*args, **kwargs):
        time.sleep(delay)
        return Fake()

    monkeypatch.setattr(module, "audio_envelope", envelope)
    monkeypatch.setattr(module, "video_strip", envelope)
    return Fake


def wait_for(app: QApplication, predicate, seconds: float = 30.0) -> bool:
    """Pump the event loop until the background layer has delivered."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def material_for(path: Path, *sources: StreamSource) -> dict[tuple[str, int], Decorations]:
    stat = path.stat()
    return {
        (source.asset_id, source.stream_index): Decorations(
            source=source, path=path, identity=(stat.st_size, stat.st_mtime_ns)
        )
        for source in sources
    }


def audio_project(clips: tuple[Clip, ...]) -> Project:
    return Project(
        tracks=(Track(id="at", kind=TrackKind.AUDIO, name="A"),), clips=clips
    )


# --------------------------------------------------------------------- planning
def test_the_ladder_never_goes_finer_than_asked():
    assert ladder_step(F(1, 3)) == F(1)  # the next finer rung would be more work
    assert ladder_step(F(3)) == F(5)
    assert ladder_step(F(1)) == F(1)
    assert ladder_step(F(10 ** 6)) == BUCKET_LADDER[-1]
    assert ladder_step(F(0)) == BUCKET_LADDER[0]
    for step in BUCKET_LADDER:
        assert ladder_step(step) == step


def test_thumbnail_boxes_stay_small():
    assert thumb_box(4000, 900) == (240, 135)
    assert thumb_box(2, 2) == (16, 12)
    assert thumb_box(64, 40) == (64, 40)


def test_an_audio_clip_is_planned_on_the_material_it_actually_shows():
    layer = make_layer()
    source = audio_source("aa", end=F(100), origin=F(0))
    clip = Clip("a1", "aa", 1, "at", source_in=F(10), source_out=F(50), output_start=F(5))
    planned = layer.plan(
        clip, Decorations(source, Path("aa.wav"), (1, 1)),
        out_from=F(0), out_to=F(100), pixels_per_second=F(20), box=(0, 0),
    )
    assert planned is not None and planned.kind == "envelope"
    # 20 px per second, 4 px per sample: one measurement every fifth of a second
    assert planned.step == F(1, 5)
    # the clip shows source 10..50; the request starts there, not at the file start
    assert planned.start == F(10)
    assert F(39) < planned.duration <= F(40)


def test_a_retimed_clip_is_measured_at_its_own_speed():
    """2x output means twice the source under the same output range."""
    layer = make_layer()
    source = audio_source("aa", end=F(200))
    matter = Decorations(source, Path("aa.wav"), (1, 1))
    fast = layer.plan(
        Clip("a1", "aa", 1, "at", F(0), F(200), F(0), rate=F(2)), matter,
        F(0), F(50), F(20), (0, 0),
    )
    assert fast is not None
    assert fast.duration <= F(100)  # 50 output seconds at 2x = 100 source seconds
    slow = layer.plan(
        Clip("a2", "aa", 1, "at", F(0), F(200), F(0), rate=F(1, 2)), matter,
        F(0), F(50), F(20), (0, 0),
    )
    assert slow is not None
    assert slow.duration <= F(25)  # 50 output seconds at half speed = 25 source
    # and a slow clip needs finer source sampling for the same pixel density
    assert slow.step <= fast.step


def test_a_decoration_never_leaves_the_registered_material():
    layer = make_layer()
    source = audio_source("aa", end=F(20), origin=F(5))
    clip = Clip("a1", "aa", 1, "at", source_in=F(0), source_out=F(90), output_start=F(0))
    planned = layer.plan(
        clip, Decorations(source, Path("a"), (1, 1)), F(0), F(1000), F(20), (0, 0),
    )
    assert planned is not None
    assert planned.start >= source.origin
    assert planned.start + planned.duration <= source.end + planned.step


def test_a_clip_with_no_registered_material_has_no_decoration():
    layer = make_layer()
    clip = Clip("a1", "nope", 1, "at", F(0), F(10), F(0))
    assert layer.request(audio_project((clip,)), {}, F(0), F(10), F(20), (0, 0)) == 0
    assert layer.pending == 0


def test_a_stream_of_unknown_position_is_not_decoded_from_a_guess():
    layer = make_layer()
    source = audio_source("aa", end=F(100))
    unknown = StreamSource(**{
        **{name: getattr(source, name) for name in source.__dataclass_fields__},
        "stream_position": None,  # the container position was never recorded
    })
    clip = Clip("a1", "aa", 1, "at", F(0), F(60), F(0))
    assert layer.request(
        audio_project((clip,)), material_for(Path(__file__), unknown),
        F(0), F(60), F(20), (0, 0),
    ) == 0


def test_only_clips_inside_the_view_are_consulted(monkeypatch):
    """The scan is bounded by the viewport, not by the length of the timeline.

    A two-hour film has hundreds of clips and a handful on screen. Reaching the
    file-system check and the plan arithmetic for every clip in the project cost
    ~9 ms per view change at 843 clips; the cheap interval test now decides that
    first, so nothing off screen is even looked at.
    """
    fake_decoding(monkeypatch)
    layer = make_layer()
    source = audio_source("aa", end=F(10 ** 6))

    class Counted:
        """Stand-in for the material file, so reads can be counted."""

        def __init__(self, target: Path):
            self.target = target

        def is_file(self) -> bool:
            self.reads += 1
            return True

        reads = 0

    file = Counted(Path(__file__))
    material = {("aa", 1): Decorations(source=source, path=file, identity=(1, 1))}
    clips = tuple(
        Clip(f"c{i}", "aa", 1, "at", F(i * 10), F(i * 10 + 5), F(i * 10))
        for i in range(400)
    )
    queued = layer.request(audio_project(clips), material, F(100), F(160), F(20), (0, 0))
    assert queued == 6  # c10..c15 are the only clips touching [100,160)
    assert file.reads == 6, f"{file.reads} reads: clips outside the view were consulted"


def test_the_request_queue_is_capped(monkeypatch):
    """Two hundred visible clips must not mean two hundred queued decodes."""
    fake_decoding(monkeypatch, delay=0.05)  # keep the queue from draining instantly
    layer = make_layer()
    source = audio_source("aa", end=F(10 ** 6))
    clips = tuple(
        Clip(f"c{i}", "aa", 1, "at", F(i * 10), F(i * 10 + 5), F(i * 10))
        for i in range(200)
    )
    queued = layer.request(
        audio_project(clips), material_for(Path(__file__), source),
        F(0), F(2 * 10 ** 6), F(20), (0, 0),
    )
    assert 0 < queued <= MAX_QUEUED_JOBS
    assert layer.pending <= MAX_QUEUED_JOBS


def test_a_warm_cache_asks_for_nothing(monkeypatch):
    fake_decoding(monkeypatch)
    layer = make_layer()
    source = audio_source("aa", end=F(100))
    material = material_for(Path(__file__), source)
    clip = Clip("a1", "aa", 1, "at", F(0), F(60), F(0))
    project = audio_project((clip,))
    assert layer.request(project, material, F(0), F(60), F(20), (0, 0)) == 1
    assert layer.request(project, material, F(0), F(60), F(20), (0, 0)) == 0
    # a different zoom lands on a different ladder rung: that is a new request
    assert layer.request(project, material, F(0), F(60), F(80), (0, 0)) == 1


def test_a_replaced_file_is_not_drawn_with_the_old_file_s_pictures(monkeypatch):
    fake_decoding(monkeypatch)
    layer = make_layer()
    source = audio_source("aa", end=F(100))
    clip = Clip("a1", "aa", 1, "at", F(0), F(60), F(0))
    project = audio_project((clip,))
    before = material_for(Path(__file__), source)
    assert layer.request(project, before, F(0), F(60), F(20), (0, 0)) == 1
    after = {
        key: Decorations(source=value.source, path=value.path, identity=(99, 99))
        for key, value in before.items()
    }
    assert layer.request(project, after, F(0), F(60), F(20), (0, 0)) == 1


def test_invalidate_forgets_everything(monkeypatch):
    fake_decoding(monkeypatch)
    layer = make_layer()
    source = audio_source("aa", end=F(100))
    clip = Clip("a1", "aa", 1, "at", F(0), F(60), F(0))
    layer.request(audio_project((clip,)), material_for(Path(__file__), source),
                  F(0), F(60), F(20), (0, 0))
    assert layer.pending == 1
    layer.invalidate()
    assert layer.pending == 0
    assert layer.cached_bytes == 0
    assert layer.reason_for(clip.id) is None


def test_the_cache_holds_a_bounded_amount(monkeypatch):
    """Sixteen 1 MB waveforms may not all be kept."""

    class Fake:
        def __init__(self, nbytes: int) -> None:
            self.nbytes = nbytes

    monkeypatch.setattr(module, "ENVELOPE_BUDGET", 4 << 20)
    layer = make_layer()
    for index in range(16):
        layer._envelopes.put(("envelope", index), Fake(1 << 20))
    assert layer.cached_bytes <= 4 << 20


def test_shutdown_is_safe_twice():
    layer = make_layer()
    layer.shutdown()
    layer.shutdown()


# ------------------------------------------------------------------- real media
@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg required")
def test_a_waveform_arrives_from_the_background(qapp_instance: QApplication, tmp_path):
    path = make_mixed(tmp_path)
    source = source_from_probe(path, stream_index=1, asset_id="mixed")
    matter = material_for(path, source)[("mixed", 1)]
    clip = Clip("a1", "mixed", 1, "at", F(0), F(6), F(0))
    layer = make_layer()
    layer.request(audio_project((clip,)), material_for(path, source), F(0), F(6), F(40), (0, 0))

    def delivered() -> bool:
        return layer.envelope_for(clip, matter, F(0), F(6), F(40)) is not None

    assert wait_for(qapp_instance, delivered), "no waveform was delivered"
    envelope = layer.envelope_for(clip, matter, F(0), F(6), F(40))
    assert envelope.count > 4
    assert max(envelope.peaks) > 0.05  # the fixture's tone is measurable
    assert layer.cached_bytes > 0
    # the envelope is placed where the stream really is, not where it was asked for
    assert envelope.start >= source.origin


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg required")
def test_thumbnails_arrive_from_the_background(qapp_instance: QApplication, tmp_path):
    path = make_colour_steps(tmp_path)
    source = source_from_probe(path, stream_index=0, asset_id="steps")
    matter = material_for(path, source)[("steps", 0)]
    clip = Clip("v1", "steps", 0, "vt", F(0), F(6), F(0))
    project = Project(
        tracks=(Track(id="vt", kind=TrackKind.VIDEO, name="V"),), clips=(clip,)
    )
    layer = make_layer()
    layer.request(project, material_for(path, source), F(0), F(6), F(40), (48, 36))

    def delivered() -> bool:
        return layer.strip_for(clip, matter, F(0), F(6), F(40), (48, 36)) is not None

    assert wait_for(qapp_instance, delivered), "no thumbnails were delivered"
    strip = layer.strip_for(clip, matter, F(0), F(6), F(40), (48, 36))
    assert strip.count >= 3
    assert all(frame.startswith(b"\xff\xd8") for frame in strip.frames)
    assert not QImage.fromData(strip.frames[0], "JPEG").isNull()


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg required")
def test_a_file_that_cannot_be_decoded_says_so_and_stops_trying(
    qapp_instance: QApplication, tmp_path
):
    path = make_garbage(tmp_path)
    source = StreamSource("broken", 0, TrackKind.VIDEO, F(0), F(10), stream_position=0)
    matter = material_for(path, source)[("broken", 0)]
    clip = Clip("v1", "broken", 0, "vt", F(0), F(6), F(0))
    project = Project(
        tracks=(Track(id="vt", kind=TrackKind.VIDEO, name="V"),), clips=(clip,)
    )
    layer = make_layer()
    reasons: list[str] = []
    layer.complained.connect(reasons.append)
    layer.request(project, material_for(path, source), F(0), F(6), F(40), (48, 36))

    assert wait_for(qapp_instance, lambda: bool(reasons)), "no reason was reported"
    assert layer.reason_for(clip.id)
    assert layer.strip_for(clip, matter, F(0), F(6), F(40), (48, 36)) is None
    # asked again, it is not retried on every repaint
    assert layer.request(project, material_for(path, source), F(0), F(6), F(40), (48, 36)) == 0


def test_an_unreadable_asset_requests_nothing_at_all(tmp_path):
    """Offline material is not retried: the clip is drawn without a decoration."""
    layer = make_layer()
    source = audio_source("gone", end=F(100))
    matter = Decorations(source, tmp_path / "gone.wav", (0, 0))
    clip = Clip("a1", "gone", 1, "at", F(0), F(60), F(0))
    layer.request(audio_project((clip,)), {("gone", 1): matter}, F(0), F(60), F(20), (0, 0))
    assert layer.pending == 0


# ---------------------------------------------------------------------- painting
def ink_of(widget) -> int:
    """How much of the widget painting covered: decorations must not erase shape."""
    image = widget.grab().toImage()
    return sum(
        1 for y in range(0, image.height(), 3) for x in range(0, image.width(), 3)
        if image.pixel(x, y) != 0xFF000000
    )


def test_a_decoration_never_replaces_the_clip_s_own_geometry(qapp_instance: QApplication):
    from video_editor.ui.timeline import Timeline

    timeline = Timeline()
    timeline.resize(900, 300)
    timeline.show()
    qapp_instance.processEvents()
    project = audio_project((Clip("a1", "aa", 1, "at", F(0), F(60), F(0)),))
    timeline.set_project(project)
    timeline.set_view(F(0), F(10))
    plain = ink_of(timeline)
    assert plain > 100, "the clip shape itself disappeared"

    # a decoration layer with nothing in it yet: the picture must not change
    timeline.set_decoration_source(make_layer(), {})
    qapp_instance.processEvents()
    assert ink_of(timeline) == plain

    # a decoration that failed: the shape is still exactly the shape
    source = audio_source("aa", end=F(100))
    material = material_for(Path(__file__), source)
    timeline.set_decoration_source(make_layer(), material)
    timeline._decorations._failures["a1"] = "ffmpeg could not decode stream 1"
    qapp_instance.processEvents()
    assert ink_of(timeline) >= plain
    timeline.hide()


def test_a_timeline_without_a_decoration_layer_is_complete(qapp_instance: QApplication):
    from video_editor.ui.timeline import Timeline

    timeline = Timeline()
    timeline.resize(600, 200)
    timeline.show()
    timeline.set_project(audio_project((Clip("a1", "aa", 1, "at", F(0), F(20), F(0)),)))
    qapp_instance.processEvents()
    assert ink_of(timeline) > 100
    timeline.set_decoration_source(None, {})
    qapp_instance.processEvents()
    assert ink_of(timeline) > 100
    timeline.hide()


def test_a_layer_dropped_without_shutdown_does_not_take_the_process_with_it(qapp_instance):
    """Forgetting to shut a decoration layer down may be careless, not fatal.

    Destroying a running `QThread` aborts the process, which is the one way this
    component can damage the application. The destructor therefore stops the
    thread itself. This test deliberately opts the layer out of this module's
    fixture — nothing else will clean it up — and drops it: if the guard were
    missing, the suite would die here instead of reporting anything.
    """
    import gc

    layer = ClipDecorations()
    module._LIVE.discard(layer)  # the fixture must not be what stops this one
    assert layer._thread.isRunning()
    del layer
    gc.collect()
    qapp_instance.processEvents()

    survivor = make_layer()  # still able to start a worker: the process is healthy
    assert survivor._thread.isRunning()

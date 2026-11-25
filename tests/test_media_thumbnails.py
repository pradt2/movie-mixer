"""Integration tests: real FFmpeg thumbnail strips (no user films)."""

import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from media_fixtures import (
    HAS_FFMPEG,
    make_colour_steps,
    make_garbage,
    make_long_video,
    source_from_probe,
)
from video_editor.core.model import StreamSource, TrackKind
from video_editor.media.thumbnails import (
    MAX_FRAMES,
    MAX_HEIGHT,
    MAX_WIDTH,
    Cancelled,
    ThumbnailError,
    video_strip,
)

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg required")

F = Fraction

#: the fixture's own order, one solid colour per second
ORDER = ("red", "green", "blue", "magenta", "yellow", "cyan")


def average_colour(frame: bytes) -> tuple[int, int, int]:
    """What the thumbnail really shows: one RGB sample averaged from the frame."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-vf", "scale=1:1", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        input=frame, capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()[-300:]
    assert len(proc.stdout) >= 3, proc.stderr.decode()[-300:]
    return (proc.stdout[0], proc.stdout[1], proc.stdout[2])


#: the colours the fixture is built from, as FFmpeg renders them
PALETTE = {
    "red": (255, 0, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "magenta": (255, 0, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
}


def name_colour(rgb: tuple[int, int, int]) -> str:
    """The palette colour a thumbnail is nearest to (lossy JPEG drifts a little)."""
    return min(PALETTE, key=lambda name: sum(
        (a - b) ** 2 for a, b in zip(rgb, PALETTE[name], strict=True)
    ))


def frame_size(frame: bytes, tmp_path: Path) -> tuple[int, int]:
    path = tmp_path / "frame.jpg"
    path.write_bytes(frame)
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.strip()
    width, height = proc.stdout.strip().split("x")
    return int(width), int(height)


def test_a_strip_shows_the_pictures_at_their_asset_local_times(tmp_path):
    path = make_colour_steps(tmp_path)
    source = source_from_probe(path, stream_index=0)
    assert source.kind is TrackKind.VIDEO

    strip = video_strip(path, source, start=F(0), duration=F(6), interval=F(1),
                        width=64, height=48)
    assert strip.start == F(0)
    assert strip.interval == F(1)
    assert strip.count == 6, f"{strip.count} frames, truncated={strip.truncated}"
    assert not strip.truncated
    assert [name_colour(average_colour(frame)) for frame in strip.frames] == list(ORDER)


def test_a_strip_that_starts_inside_the_stream_starts_there(tmp_path):
    """The grid is the requested one, so frame 0 is the picture at ``start``."""
    path = make_colour_steps(tmp_path)
    source = source_from_probe(path, stream_index=0)
    strip = video_strip(path, source, start=F(2), duration=F(3), interval=F(1),
                        width=64, height=48)
    assert strip.start == F(2)
    assert strip.time_at(0) == F(2) and strip.time_at(2) == F(4)
    assert strip.count == 3
    assert [name_colour(average_colour(frame)) for frame in strip.frames] == [
        "blue", "magenta", "yellow",
    ]
    assert name_colour(average_colour(strip.frame_at(F(4)))) == "yellow"
    assert strip.frame_at(F(1)) is None  # outside the strip, not guessed
    assert strip.frame_at(strip.end) is None


def test_the_grid_follows_the_zoom_not_the_frame_rate(tmp_path):
    path = make_colour_steps(tmp_path)
    source = source_from_probe(path, stream_index=0)
    strip = video_strip(path, source, start=F(0), duration=F(1), interval=F(1, 5),
                        width=48, height=36)
    assert strip.interval == F(1, 5)
    assert strip.count == 5, f"{strip.count} frames at 5 per second"
    # 25 source frames, five samples: a strip is a picture of the shot, not a frame dump
    assert {name_colour(average_colour(frame)) for frame in strip.frames} == {"red"}


def test_thumbnails_stay_small_whatever_the_request(tmp_path):
    path = make_colour_steps(tmp_path)
    source = source_from_probe(path, stream_index=0)
    strip = video_strip(path, source, start=F(0), duration=F(2), interval=F(1),
                        width=4000, height=3000)
    assert (strip.width, strip.height) == (MAX_WIDTH, MAX_HEIGHT)
    assert frame_size(strip.frames[0], tmp_path) == (MAX_WIDTH, MAX_HEIGHT)
    assert strip.nbytes < 6 << 20


def test_every_frame_is_a_real_picture(tmp_path):
    path = make_colour_steps(tmp_path)
    source = source_from_probe(path, stream_index=0)
    strip = video_strip(path, source, start=F(0), duration=F(2), interval=F(1),
                        width=32, height=24)
    for frame in strip.frames:
        assert frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9")


def test_an_audio_stream_is_refused_rather_than_guessed(tmp_path):
    path = make_colour_steps(tmp_path)
    source = StreamSource("steps", 0, TrackKind.AUDIO, F(0), F(6), stream_position=0)
    with pytest.raises(ThumbnailError, match="not a video stream"):
        video_strip(path, source, start=F(0), duration=F(1), interval=F(1),
                    width=32, height=24)


def test_an_unrecorded_stream_position_is_refused(tmp_path):
    path = make_colour_steps(tmp_path)
    source = source_from_probe(path, stream_index=0)
    unknown = StreamSource(**{
        **{name: getattr(source, name) for name in source.__dataclass_fields__},
        "stream_position": None,
    })
    with pytest.raises(ThumbnailError, match="no recorded position"):
        video_strip(path, unknown, start=F(0), duration=F(1), interval=F(1),
                    width=32, height=24)


def test_a_request_beyond_the_material_is_refused(tmp_path):
    path = make_colour_steps(tmp_path)
    source = source_from_probe(path, stream_index=0)
    with pytest.raises(ThumbnailError, match="outside"):
        video_strip(path, source, start=source.end + F(1), duration=F(1), interval=F(1),
                    width=32, height=24)


def test_the_request_size_is_bounded(tmp_path):
    path = make_colour_steps(tmp_path)
    source = source_from_probe(path, stream_index=0)
    tiny = Fraction(1, 500)  # 3 000 thumbnails for six seconds
    assert -(-F(6) // tiny) > MAX_FRAMES
    with pytest.raises(ThumbnailError, match="thumbnails exceeds the limit"):
        video_strip(path, source, start=F(0), duration=F(6), interval=tiny,
                    width=32, height=24)


def test_a_file_that_is_not_media_produces_no_pictures(tmp_path):
    path = make_garbage(tmp_path)
    source = StreamSource("broken", 0, TrackKind.VIDEO, F(0), F(10), stream_position=0)
    with pytest.raises(ThumbnailError, match=r"could not decode|no pictures"):
        video_strip(path, source, start=F(0), duration=F(1), interval=F(1),
                    width=32, height=24)


def test_cancelling_stops_the_extraction(tmp_path):
    path = make_long_video(tmp_path, seconds=12)
    source = source_from_probe(path, stream_index=0)
    calls: list[int] = []

    def cancel() -> bool:
        calls.append(1)
        return True

    with pytest.raises(Cancelled):
        video_strip(path, source, start=F(0), duration=F(12), interval=F(1),
                    width=64, height=48, cancel=cancel)
    assert calls, "the cancel predicate was never consulted"

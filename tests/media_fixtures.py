"""Generate short synthetic media fixtures with FFmpeg (never user films).

All fixtures use distinguishable solid-colour video sections and sine tones
at distinct frequencies so picture and sound correctness is measurable.
Fixtures live in pytest ``tmp_path`` directories and are removed with them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

FFMPEG = shutil.which("ffmpeg")
HAS_FFMPEG = FFMPEG is not None

_COMMON = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"] if FFMPEG else []


def _run(args: list[str]) -> None:
    proc = subprocess.run([*_COMMON, *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg fixture generation failed: {proc.stderr.strip()}")


def make_mixed(tmp: Path, name: str = "mixed.mkv") -> Path:
    """6 s: 3 solid colour sections (red/green/blue @25fps) + two audio
    streams (440 Hz eng, 880 Hz fre, 44.1 kHz)."""
    out = tmp / name
    _run(
        [
            "-f", "lavfi", "-i", "color=c=red:s=128x96:r=25:d=2",
            "-f", "lavfi", "-i", "color=c=green:s=128x96:r=25:d=2",
            "-f", "lavfi", "-i", "color=c=blue:s=128x96:r=25:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6:sample_rate=44100",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=6:sample_rate=44100",
            "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0[v]",
            "-map", "[v]", "-map", "3", "-map", "4",
            "-metadata:s:a:0", "language=eng",
            "-metadata:s:a:1", "language=fre",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25",
            "-c:a", "aac",
            str(out),
        ]
    )
    return out


def make_audio_only(tmp: Path, name: str = "tone.wav") -> Path:
    """3 s of a 330 Hz tone, WAV (no container start_time metadata)."""
    out = tmp / name
    _run(["-f", "lavfi", "-i", "sine=frequency=330:duration=3:sample_rate=48000", str(out)])
    return out


def make_video_only(tmp: Path, name: str = "pictures.mp4") -> Path:
    """4 s green-only video @ 24000/1001 fps, no audio stream."""
    out = tmp / name
    _run(
        [
            "-f", "lavfi", "-i", "color=c=green:s=160x96:r=24000/1001:d=4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(out),
        ]
    )
    return out


def make_colour_steps(tmp: Path, name: str = "steps.mkv") -> Path:
    """6 s silent video: six 1 s solid colours at 25 fps, 128x96.

    Order: red, green, blue, magenta, yellow, cyan. Distinct second-long blocks
    make retime/trim/stacking correctness measurable frame by frame.
    """
    out = tmp / name
    colours = ["red", "green", "blue", "magenta", "yellow", "cyan"]
    args: list[str] = []
    for colour in colours:
        args += ["-f", "lavfi", "-i", f"color=c={colour}:s=128x96:r=25:d=1"]
    labels = "".join(f"[{i}:v]" for i in range(len(colours)))
    args += [
        "-filter_complex",
        f"{labels}concat=n={len(colours)}:v=1:a=0[v]",
        "-map",
        "[v]",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "25",
        str(out),
    ]
    _run(args)
    return out


def make_tone(tmp: Path, name: str, frequency: int, seconds: int = 6) -> Path:
    """Audio-only WAV with one sine tone (distinct frequency per asset)."""
    out = tmp / name
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration={seconds}:sample_rate=48000",
            "-c:a",
            "pcm_s16le",
            str(out),
        ]
    )
    return out


def make_long_video(tmp: Path, name: str = "long.mkv", seconds: int = 12) -> Path:
    """Silent 25 fps video of ``seconds`` seconds (cancellation tests)."""
    out = tmp / name
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=gray:s=320x240:r=25:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:duration={seconds}:sample_rate=48000",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(out),
        ]
    )
    return out


def make_whole_asset_offset(tmp: Path, name: str = "offset.mkv") -> Path:
    """2 s video+audio where the entire presentation timeline starts at 2.5 s."""
    out = tmp / name
    _run(
        [
            "-f", "lavfi", "-i", "color=c=blue:s=128x96:r=25:d=2",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=2:sample_rate=44100",
            "-output_ts_offset", "2.5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(out),
        ]
    )
    return out


def make_stream_offset(tmp: Path, name: str = "stream_offset.mkv") -> Path:
    """Video starts at 0, audio stream starts 1.0 s later on the asset axis."""
    out = tmp / name
    _run(
        [
            "-f", "lavfi", "-i", "color=c=red:s=128x96:r=25:d=3",
            "-itsoffset", "1.0",
            "-f", "lavfi", "-i", "sine=frequency=660:duration=2:sample_rate=44100",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(out),
        ]
    )
    return out


def make_vfr(tmp: Path, name: str = "vfr.mkv") -> Path:
    """12 s of *variable* frame rate picture: 8 s at 25 fps, then 4 s at 12 fps.

    Concatenating differently-timed sections keeps real, irregular packet
    timestamps (ffprobe reports average ≠ nominal), which is what makes source
    frame stepping testable: a constant-rate assumption lands on the wrong frames
    after the change. Colours alternate so a decoded frame identifies its section.
    """
    out = tmp / name
    _run(
        [
            "-f", "lavfi", "-i", "color=c=red:s=128x96:r=25:d=4",
            "-f", "lavfi", "-i", "color=c=blue:s=128x96:r=25:d=4",
            "-f", "lavfi", "-i", "color=c=green:s=128x96:r=12:d=4",
            "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0[v]",
            "-map", "[v]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "24",
            str(out),
        ]
    )
    return out


# ------------------------------------------------------- identity-colour picture

#: Six brightness levels, 51 apart. Measured on FFmpeg 8.1.3: an 8-bit 4:2:0 round
#: trip moves a level by only a few units, so reading a decoded pixel as the
#: *nearest* level recovers the encoded number exactly for every frame these
#: fixtures use - while dividing the byte by 32 aliased neighbouring frames onto the
#: same value and made a passing test look correct for the wrong reason.
IDENTITY_LEVELS = (0, 51, 102, 153, 204, 255)

#: Every channel of every frame is lifted by this much. Frame 0 of the sequence is
#: otherwise (0,0,0) — exactly what the composition writes where there is *no*
#: picture — and the test could not tell "the first frame of the clip" from "black".
#: The lift is far from every level boundary, so classification is unaffected.
IDENTITY_LIFT = 8


def identity_rgb(index: int) -> tuple[int, int, int]:
    """One frame number as three base-6 digits, one per channel (unique to 216)."""
    return tuple(
        min(255, IDENTITY_LEVELS[(index // place) % 6] + IDENTITY_LIFT)
        for place in (1, 6, 36)
    )


def identity_at(rgb: bytes, width: int, height: int) -> int:
    """The frame number carried by the centre pixel of a raw RGB24 picture."""
    offset = (height // 2) * width * 3 + (width // 2) * 3
    digits = [
        min(range(6), key=lambda k: abs(IDENTITY_LEVELS[k] - value))
        for value in rgb[offset : offset + 3]
    ]
    return digits[0] + 6 * digits[1] + 36 * digits[2]


def identity_sequence(
    tmp: Path, count: int, size: tuple[int, int] = (64, 48), first: int = 0
) -> Path:
    """Write ``count`` PPM frames, each carrying its own number as a colour."""
    directory = tmp / f"identity_seq_{first}_{count}"
    directory.mkdir(parents=True, exist_ok=True)
    width, height = size
    header = f"P6\n{width} {height}\n255\n".encode()
    for n in range(count):
        body = bytes(identity_rgb(first + n)) * (width * height)
        (directory / f"{n:04d}.ppm").write_bytes(header + body)
    return directory


def _encode_sequence(directory: Path, out: Path, framerate: str, extra: list[str]) -> Path:
    _run(["-framerate", framerate, "-i", str(directory / "%04d.ppm"), "-map_metadata", "-1",
          "-an", *extra, str(out)])
    return out


def make_identity_video(
    tmp: Path,
    name: str = "identity.mkv",
    count: int = 150,
    framerate: str = "25",
    size: tuple[int, int] = (64, 48),
) -> Path:
    """Lossless FFV1 picture where frame *N* carries the number *N*, at a constant rate.

    A solid colour per frame makes "which picture is this" answerable with no
    tolerance at all: comparing rendered frames to planned ones is then exact
    arithmetic, not a judgement about similarity.
    """
    directory = identity_sequence(tmp, count, size)
    return _encode_sequence(directory, tmp / name, framerate, ["-c:v", "ffv1", "-level", "3"])


def make_identity_bframes(
    tmp: Path, name: str = "identity_bframes.mp4", count: int = 100
) -> Path:
    """The same identity pictures in MP4/H.264 with B-frames.

    ``-bf 2`` reorders *decode* order against *presentation* order, which is where a
    stream-index or seek assumption shows up as the wrong picture; ``-qp 0`` keeps
    the colours exact so the identity is still readable through 4:2:0.
    """
    directory = identity_sequence(tmp, count)
    return _encode_sequence(
        directory,
        tmp / name,
        "25",
        ["-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", "-g", "5", "-bf", "2"],
    )


def make_identity_offset(
    tmp: Path, name: str = "identity_offset.mkv", count: int = 100
) -> Path:
    """Identity pictures whose whole presentation starts 2.5 s into the asset axis."""
    directory = identity_sequence(tmp, count)
    return _encode_sequence(
        directory, tmp / name, "25",
        ["-output_ts_offset", "2.5", "-c:v", "ffv1", "-level", "3"],
    )


def make_identity_vfr(
    tmp: Path, name: str = "identity_vfr.mkv", fast: int = 100, slow: int = 50
) -> Path:
    """Identity pictures at two frame rates in one file: 100 @ 25 fps, then 50 @ 12 fps.

    The frame numbers keep counting, so a decoded picture says both which frame it
    is and which timing section it came from: the timestamps are irregular across
    the join (and the average rate is not the nominal one), which is what a
    constant-frame-duration assumption gets wrong.
    """
    fast_dir = identity_sequence(tmp, fast, first=0)
    slow_dir = identity_sequence(tmp, slow, first=fast)
    first_part = _encode_sequence(fast_dir, tmp / f"{name}.part0.mkv", "25",
                                  ["-c:v", "ffv1", "-level", "3"])
    second_part = _encode_sequence(slow_dir, tmp / f"{name}.part1.mkv", "12",
                                   ["-c:v", "ffv1", "-level", "3"])
    listing = tmp / f"{name}.concat.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in (first_part, second_part)))
    out = tmp / name
    _run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)])
    return out


def make_identity_anamorphic(
    tmp: Path, name: str = "identity_720x576.mkv", count: int = 50
) -> Path:
    """720x576 pictures with a 64:45 sample aspect ratio: 5:4 in pixels, 16:9 on screen.

    Anamorphic DVD material is the case where pixel count and picture shape
    disagree: honouring the recorded sample aspect ratio decides whether the output
    is the whole frame or a pillarboxed one, and the still and the export must
    agree about it.
    """
    directory = identity_sequence(tmp, count, size=(720, 576))
    return _encode_sequence(
        directory,
        tmp / name,
        "25",
        # ``setsar=64/45``, never ``setsar=64:45``: a colon separates *options* in
        # an FFmpeg filter argument, so the colon form silently produced sample
        # aspect 45/1 (display aspect 225/4) instead of 64/45 - measured with
        # ffprobe on the file this function writes.
        ["-vf", "setsar=64/45", "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", "-g", "5"],
    )


def make_garbage(tmp: Path, name: str = "broken.mp4") -> Path:
    """A file that is not readable media."""
    out = tmp / name
    out.write_bytes(b"this is definitely not a video file" * 10)
    return out


def make_truncated_audio(tmp: Path, name: str = "truncated.mkv", seconds: int = 6) -> Path:
    """A lossless-sound Matroska whose container still claims its full length.

    Written whole, then cut short. Matroska keeps the duration it was given, so
    the file *says* ``seconds`` and holds a good deal less: exactly the situation
    a renderer has to refuse instead of quietly padding with silence.
    """
    out = tmp / name
    _run(
        [
            "-f", "lavfi",
            "-i", f"sine=frequency=660:duration={seconds}:sample_rate=48000",
            "-c:a", "pcm_s24le",
            str(out),
        ]
    )
    data = out.read_bytes()
    out.write_bytes(data[: max(64, int(len(data) * 0.7))])
    return out


def make_tone_window(tmp: Path, name: str, frequency: int, *, start: int, end: int,
                     total: int = 6) -> Path:
    """Video plus one audio track: a tone only inside ``[start, end)``.

    Silent before and after, so a waveform's edges are measurable against known
    asset-local times instead of being assumed.
    """
    out = tmp / name
    _run(
        [
            "-f", "lavfi", "-i", f"color=c=gray:s=128x96:r=25:d={total}",
            "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={start}",
            "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:d={end - start}",
            "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={total - end}",
            "-filter_complex", "[1:a][2:a][3:a]concat=n=3:v=0:a=1[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25",
            "-c:a", "pcm_s16le",
            str(out),
        ]
    )
    return out


def make_silent_and_tone_streams(tmp: Path, name: str, *, frequency: int,
                                 seconds: int = 4) -> Path:
    """One video and two audio streams: the first carries a tone, the second silence.

    Only one of them makes a sound, so a measurement that addressed the wrong
    stream is visible rather than plausible.
    """
    out = tmp / name
    _run(
        [
            "-f", "lavfi", "-i", f"color=c=gray:s=128x96:r=25:d={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:d={seconds}",
            "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={seconds}",
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25",
            "-c:a", "pcm_s16le",
            str(out),
        ]
    )
    return out


def source_from_probe(path: Path, stream_index: int, asset_id: str | None = None):
    """A core ``StreamSource`` for one real stream, built from its own probe.

    Tests measure or decode real material; this is the same translation the
    application does at import (container position, offsets and bounds included),
    so a stream addressing bug cannot hide behind a hand-written record.
    """
    from video_editor.core.model import StreamSource, TrackKind
    from video_editor.core.timeutils import ZERO
    from video_editor.media.ffprobe import probe_file

    result = probe_file(path)
    if not result.ok:
        raise RuntimeError(f"probe failed: {result.error}")
    meta = result.metadata
    stream = meta.stream(stream_index)
    if stream is None:
        raise RuntimeError(f"no stream {stream_index} in {path}")
    return StreamSource(
        asset_id=asset_id or path.name,
        stream_index=stream.stream_index,
        kind=TrackKind.VIDEO if stream.kind.value == "video" else TrackKind.AUDIO,
        origin=stream.origin,
        end=stream.end_time,
        conservative_end=stream.bounds_are_conservative,
        frame_rate=stream.frame_rate,
        sample_rate=stream.sample_rate,
        channel_layout=stream.channel_layout,
        width=stream.width,
        height=stream.height,
        stream_position=meta.position_of(stream.stream_index),
        input_start=meta.container_start or ZERO,
    )

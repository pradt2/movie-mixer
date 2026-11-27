"""Generate short synthetic media fixtures with FFmpeg (never user films).

All fixtures use distinguishable solid-colour video sections and sine tones
at distinct frequencies so picture and sound correctness is measurable.
Fixtures live in pytest ``tmp_path`` directories and are removed with them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise
from pathlib import Path

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
HAS_FFMPEG = FFMPEG is not None


def _encoders() -> frozenset[str]:
    """Which encoders this FFmpeg actually has, read from the tool itself.

    A fixture that cannot be built must be skipped by name with the reason, never
    quietly written in some other format and asserted as if it were the shape under
    test.
    """
    if not FFMPEG:
        return frozenset()
    proc = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True,
                          text=True, timeout=60)
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAXS":
            names.add(parts[1])
    return frozenset(names)


ENCODERS = _encoders()
HAS_H264 = "libx264" in ENCODERS
HAS_HEVC = "libx265" in ENCODERS
HAS_MPEG2 = "mpeg2video" in ENCODERS

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


def make_presentation_hole(tmp: Path, name: str = "hole.mkv", seconds: int = 12,
                           hole_start: float = 4.0, hole_end: float = 7.9) -> Path:
    """Picture that stops and starts again: a real hole on the presentation axis.

    Measured on FFmpeg 8.1.3: 202 pictures, 0.00-3.96 s then 7.92-11.96 s, so the
    only spacings are 1/25 and 99/25 — nothing about this file is evenly spaced
    across the break. A bounded read inside the break answers with the pictures
    *before* it, exactly as a read past the end of a stream does, which is the
    whole point of the fixture: "this window held no picture" is not "the stream
    ended", and the true next picture after 5 s is 7.92 s.
    """
    out = tmp / name
    _run(
        [
            "-f", "lavfi", "-i", f"testsrc=s=128x96:r=25:d={seconds}",
            "-vf", f"select='lt(t,{hole_start})+gt(t,{hole_end})'",
            "-fps_mode", "passthrough",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25", "-an",
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


def make_identity_fine_timescale(
    tmp: Path,
    name: str = "identity_fine.mp4",
    count: int = 150,
    timescale: int = 50_000_000,
) -> Path:
    """Identity pictures in an MP4 whose track timescale is finer than any tempo divides.

    ``-video_track_timescale 50000000`` puts the container's timestamps on twenty-
    nanosecond ticks while the pictures stay on 1/25 s. The picture chain's rule for an
    exact filter tick that holds for *any* timestamp then asks for a tick finer than
    :data:`~video_editor.render.plan.MAX_PICTURE_TICK` - and finer than an FFmpeg time
    base can state at all - so this is the shape where an item's exact tempo has to be
    proved against the stream's own measured picture stamps instead of guessed from the
    container. Measured with :func:`picture_facts`: 150 pictures at ``n/25`` s, tick
    ``1/50000000``, and the declared 6 s duration overshoots the last picture
    (``149/25``) by ``4/25`` s, which is the material a tail hold is made of.
    """
    directory = identity_sequence(tmp, count)
    return _encode_sequence(
        directory, tmp / name, "25",
        ["-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", "-g", "5",
         "-video_track_timescale", str(timescale)],
    )


def make_identity_bframes(
    tmp: Path, name: str = "identity_bframes.mp4", count: int = 100
) -> Path:
    """The same identity pictures in MP4/H.264 that really does contain B-frames.

    B-frames are what makes this fixture worth having: they put *decode* order
    against *presentation* order, which is where a stream-index or seek assumption
    shows up as the wrong picture. They also have to be **measured**, not assumed
    from an option: ``-qp 0 -bf 2`` is x264's lossless mode and it silently writes
    no B-frames at all (measured census on FFmpeg 8.1.3: 33 I + 67 P, zero B), so a
    test that believed ``-bf`` was testing nothing. ``-crf 4`` with
    ``bframes=3:b-adapt=0`` measures 22 I + 40 B + 38 P, and ``picture_facts`` reads
    every identity back exactly through 4:2:0 (worst centre-channel error 3 against
    a level step of 51), so the picture numbers stay unambiguous.
    """
    directory = identity_sequence(tmp, count)
    return _encode_sequence(
        directory,
        tmp / name,
        "25",
        ["-c:v", "libx264", "-crf", "4", "-pix_fmt", "yuv420p", "-g", "5",
         "-x264-params", "bframes=3:b-adapt=0"],
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
    is and which timing section it came from. The irregularity is only visible in
    the real timestamps: measured on FFmpeg 8.1.3 the container still calls this
    stream 25/1 both nominal *and* average, while the decoded presentation times
    step by 1/25 and then by 83/1000 / 21/250 (the ``-c copy`` concat lands 1/12 on
    a 1/1000 tick). That is exactly what a constant-frame-duration assumption gets
    wrong, and why the tests read the measured PTS list instead of the rate.
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


def make_identity_mp4(
    tmp: Path,
    name: str = "identity.mp4",
    count: int = 100,
    offset: str | None = None,
) -> Path:
    """The identity pictures in an ordinary MP4, optionally starting off zero.

    Measured on FFmpeg 8.1.3, and the reason this fixture exists rather than a
    guess about MP4 offsets: the MOV/MP4 muxer normalises ``-output_ts_offset``
    away (the file it writes starts at 0 with a 4.0 s duration), while
    ``-itsoffset 2.5`` **before** the input really does move the presentation.
    That file reports container and stream ``start_time`` 2.52 and ``duration``
    4.0, and decodes 100 pictures from 63/25 to 162/25, the last 1/25 s long: its
    material runs 63/25 .. 163/25 s. MP4 is the family where a duration is a span
    from the stream's own start and is recorded exactly, which is why no universal
    subtraction is allowed anywhere. (A whole-asset offset applied as a copy from
    Matroska measures 2.5 .. 6.5 instead - the amount of the shift is the
    muxer's, the *convention* is what matters here.)
    """
    directory = identity_sequence(tmp, count)
    extra = ["-itsoffset", offset] if offset else []
    _run([
        *extra, "-framerate", "25", "-i", str(directory / "%04d.ppm"),
        "-map_metadata", "-1", "-an",
        "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", "-g", "5",
        tmp / name,
    ])
    return tmp / name


def make_per_stream_offset(
    tmp: Path, name: str = "per_stream_offset.mkv"
) -> Path:
    """One asset whose picture and sound are offset by different amounts.

    Measured on FFmpeg 8.1.3 (``-itsoffset 2.5`` on the video input, ``1.0`` on
    the audio input): the container says start 1.0, duration 6.52; the video
    stream starts at 63/25 and its last picture is 162/25 with a 1/25 duration
    (material ends at 163/25 = 6.52 s); the audio stream starts at 1.0 and decodes
    to 4989/1000 + 1/100 = 4999/1000 s. So the two streams genuinely end about
    1.5 s apart on one timeline: a bound that gives both of them the container's
    6.52 s is an upper bound for the sound, not its length.
    """
    out = tmp / name
    _run(
        [
            "-itsoffset", "2.5", "-f", "lavfi", "-i", "color=c=green:s=64x48:r=25:d=4",
            "-itsoffset", "1.0", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=4:sample_rate=48000",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "ffv1", "-level", "3", "-c:a", "pcm_s16le",
            str(out),
        ]
    )
    return out


def make_truncated_identity_offset(
    tmp: Path, name: str = "truncated_identity_offset.mkv", count: int = 100, keep: float = 0.55
) -> Path:
    """The offset identity file with its tail bytes cut off, header and all.

    Measured on FFmpeg 8.1.3: after cutting 45% of the file, ffprobe still reports
    start 2.5, duration 6.5 and a per-track ``DURATION`` tag of 6.5, while only 54
    pictures decode, the last at 231/50 with a 1/25 duration - the material ends
    at 233/50 s, 1.84 s short of what the file claims. This is why a
    container-derived bound stays marked conservative, and why a render that reads
    that far decodes the tail before it publishes: the missing picture would
    otherwise arrive as the last frame held for almost two seconds.
    """
    out = make_identity_offset(tmp, name=name, count=count)
    data = out.read_bytes()
    out.write_bytes(data[: max(64, int(len(data) * keep))])
    return out


# ------------------------------------------------- measuring a fixture, once

def _ffprobe(args: list[str], timeout: int) -> str:
    """``ffprobe`` with an explicit failure: a measurement never answers ''."""
    proc = subprocess.run([FFPROBE, "-hide_banner", "-v", "error", *args],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {args[-1]}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _parse_rational(text: object) -> Fraction | None:
    """Exact rational from ffprobe text: ``64:45``, ``64/45``, ``25`` or ``N/A``."""
    if text is None or isinstance(text, bool):
        return None
    value = str(text).strip()
    if not value or value in {"N/A", "0:0", "0/0", "undefined"}:
        return None
    try:
        return Fraction(value.replace(":", "/"))
    except (ValueError, ZeroDivisionError):
        return None


@dataclass(frozen=True)
class PictureFacts:
    """What a picture fixture *actually* holds, measured from the file itself.

    Nothing here is inferred from what the fixture was asked for: ``pictures`` are
    the decoded presentation times read from ``ffprobe -show_frames``, ``pict_types``
    is the census from that same pass, and ``identities``/``centres`` are read back
    from one decode of the file. A test treats these as measured ground truth and
    gets them from :func:`picture_facts`, once per fixture, never per frame.
    """

    path: Path
    codec: str
    pix_fmt: str
    width: int
    height: int
    #: as stored in the container (``sample_aspect_ratio``/``display_aspect_ratio``)
    sample_aspect_ratio: Fraction | None
    display_aspect_ratio: Fraction | None
    #: exact tick size of this stream in its container
    time_base: Fraction
    #: what the container *claims*, kept apart from what decodes: on the offset
    #: fixture the two disagree, and that difference is a production finding
    stream_start: Fraction | None
    stream_duration: Fraction | None
    container_duration: Fraction | None
    nominal_frame_rate: Fraction | None
    average_frame_rate: Fraction | None
    declared_frames: int | None
    #: decoded presentation times, ascending, on the asset-local axis
    pictures: tuple[Fraction, ...]
    #: distinct gaps between neighbouring pictures: more than one means VFR
    spacings: tuple[Fraction, ...]
    #: measured ``pict_type`` census, e.g. ``{"I": 22, "B": 40, "P": 38}``
    pict_types: dict[str, int]
    #: the frame number each decoded picture carries, in presentation order
    identities: tuple[int, ...]
    #: centre pixel of each decoded picture (the identity survives, or it does not)
    centres: tuple[tuple[int, int, int], ...]

    @property
    def count(self) -> int:
        return len(self.pictures)

    @property
    def last(self) -> Fraction:
        """Asset-local time of the last real picture."""
        return self.pictures[-1]

    @property
    def uniform_spacing(self) -> Fraction | None:
        """The single frame gap, when the stream really is constant rate."""
        return self.spacings[0] if len(self.spacings) == 1 else None

    def of_type(self, kind: str) -> int:
        """How many pictures the decode reported as this ``pict_type``."""
        return self.pict_types.get(kind, 0)


#: keyed by the file's identity (resolved path, size, mtime), so an unchanged
#: fixture is probed and decoded exactly once however many questions are asked
_FACTS: dict[tuple[str, int, int], PictureFacts] = {}


def picture_facts(path: Path, timeout: int = 300) -> PictureFacts:
    """Measure one fixture: PTS list, picture types, geometry, identity colours.

    Presentation times are the integer ``best_effort_timestamp`` multiplied by the
    stream's exact ``time_base`` - never ffprobe's printed decimals, which render a
    Matroska frame at 8.0833... as 8.083 and would put a VFR source step on the
    wrong picture.

    The picture-type census comes from that same pass, because an encoder option is
    not evidence about a file: measured on FFmpeg 8.1.3, ``-qp 0 -bf 2`` (x264
    lossless) writes **no** B-frames at all, while this content at ``-crf 4`` with
    ``b-adapt=0`` writes 40 of them.

    The decode uses ``-fps_mode passthrough``: ``rawvideo``'s own default rate of
    25 fps makes FFmpeg duplicate frames, measured as 204 pictures out of a
    150-picture VFR fixture.
    """
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    cached = _FACTS.get(key)
    if cached is not None:
        return cached
    if FFPROBE is None:
        raise RuntimeError("ffprobe is required to measure a fixture")
    stream = json.loads(_ffprobe(
        [
            "-select_streams", "v:0",
            "-show_entries",
            "stream=codec_name,pix_fmt,width,height,sample_aspect_ratio,"
            "display_aspect_ratio,nb_frames,avg_frame_rate,r_frame_rate,duration,"
            "start_time,time_base",
            "-of", "json", str(path),
        ],
        timeout,
    ))["streams"][0]
    time_base = _parse_rational(stream.get("time_base"))
    if time_base is None or time_base <= 0:
        raise RuntimeError(f"{path.name}: no usable stream time base, PTS cannot be exact")
    frames = [
        frame
        for frame in json.loads(_ffprobe(
            [
                "-select_streams", "v:0",
                "-show_entries", "frame=media_type,best_effort_timestamp,pict_type",
                "-of", "json", str(path),
            ],
            timeout,
        ))["frames"]
        if frame.get("media_type") == "video"
    ]
    if not frames:
        raise RuntimeError(f"{path.name}: ffprobe reported no pictures")
    pictures = tuple(
        sorted(Fraction(int(frame["best_effort_timestamp"])) * time_base for frame in frames)
    )
    width, height = int(stream["width"]), int(stream["height"])
    stride = width * height * 3
    proc = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-map", "0:v:0", "-fps_mode", "passthrough",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{path.name}: decode failed: {proc.stderr.decode()[:300]}")
    decoded = len(proc.stdout) // stride
    if decoded != len(pictures):
        raise RuntimeError(
            f"{path.name}: ffprobe counted {len(pictures)} pictures, decoding gave {decoded}"
        )
    identities: list[int] = []
    centres: list[tuple[int, int, int]] = []
    centre = (height // 2) * (width * 3) + (width // 2) * 3
    for index in range(decoded):
        frame = proc.stdout[index * stride : (index + 1) * stride]
        identities.append(identity_at(frame, width, height))
        centres.append(tuple(frame[centre : centre + 3]))
    declared = stream.get("nb_frames")
    facts = PictureFacts(
        path=path,
        codec=str(stream.get("codec_name", "?")),
        pix_fmt=str(stream.get("pix_fmt", "?")),
        width=width,
        height=height,
        sample_aspect_ratio=_parse_rational(stream.get("sample_aspect_ratio")),
        display_aspect_ratio=_parse_rational(stream.get("display_aspect_ratio")),
        time_base=time_base,
        stream_start=_parse_rational(stream.get("start_time")),
        stream_duration=_parse_rational(stream.get("duration")),
        container_duration=_parse_rational(json.loads(
            _ffprobe(["-show_entries", "format=duration", "-of", "json", str(path)], timeout)
        )["format"].get("duration")),
        nominal_frame_rate=_parse_rational(stream.get("r_frame_rate")),
        average_frame_rate=_parse_rational(stream.get("avg_frame_rate")),
        declared_frames=int(declared) if declared not in (None, "N/A") else None,
        pictures=pictures,
        spacings=tuple(sorted({b - a for a, b in pairwise(pictures)})),
        pict_types=dict(Counter(str(frame.get("pict_type", "?")) for frame in frames)),
        identities=tuple(identities),
        centres=tuple(centres),
    )
    _FACTS[key] = facts
    return facts


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


# ------------------------------------------------- picture-format fixtures (TASK_12l)
#
# Files whose *picture format* is stated in the container, so the application can
# be shown refusing what it cannot draw and rendering what it can. Every shape
# below was measured with raw ``ffprobe`` before any expectation was written (the
# commands and JSON are in the task record), because the obvious way to ask
# FFmpeg for a shape is often not the way that survives into the file:
#
# * ``-color_trc smpte2084`` on the command line does **not** reach ``ffprobe``
#   for H.264 in Matroska/MP4/TS; writing the value into the bitstream with
#   ``-bsf:v h264_metadata=transfer_characteristics=16`` does;
# * ``-field_order tt`` on an ordinary H.264 Matroska does not survive at all
#   (that file probes ``progressive``), while ``-x264-params tff=1`` gives the
#   opposite split: ``progressive`` at stream level, ``interlaced_frame=1`` on
#   every decoded frame;
# * MPEG-2 with ``-flags +ildct+ilme`` states its field order at stream level in
#   both program and transport streams, whichever token the muxer settles on.


def _encode_h264_sequence(
    tmp: Path,
    name: str,
    count: int,
    pix_fmt: str,
    extra: list[str],
    bsf: str | None = None,
) -> Path:
    """Identity pictures in H.264, optionally re-marked by a bitstream filter.

    ``-crf 4`` keeps the identity colours readable through 4:2:0 (measured the same
    way as :func:`make_identity_bframes`: worst centre-channel error far below the
    level step), so a file that must render can still be checked frame by frame.
    The bitstream-filter pass is ``-c copy``: the pictures are untouched, only the
    colour description in the bitstream is written, which is the only way measured
    to make a colour tag visible to ``ffprobe``.
    """
    directory = identity_sequence(tmp, count)
    out = tmp / name
    if bsf is None:
        _run([
            "-framerate", "25", "-i", str(directory / "%04d.ppm"), "-map_metadata", "-1", "-an",
            "-c:v", "libx264", "-crf", "4", "-pix_fmt", pix_fmt, "-g", "12", *extra, str(out),
        ])
        return out
    marked = tmp / f"marked_{name}"
    _run([
        "-framerate", "25", "-i", str(directory / "%04d.ppm"), "-map_metadata", "-1", "-an",
        "-c:v", "libx264", "-crf", "4", "-pix_fmt", pix_fmt, "-g", "12", *extra, str(marked),
    ])
    _run(["-i", str(marked), "-c", "copy", "-bsf:v", bsf, str(out)])
    marked.unlink(missing_ok=True)
    return out


def make_progressive_sdr(tmp: Path, name: str = "progressive_sdr.mkv", count: int = 50) -> Path:
    """Ordinary progressive 8-bit H.264 whose bitstream states BT.709 in full.

    The colour values have to be written by the encoder (``-x264-params``), not by
    the command-line ``-color_trc`` option: measured on FFmpeg 8.1.3, ``-color_trc``
    leaves ``color_transfer`` absent from ``-show_streams`` while ``-colorspace``
    survives as ``color_space`` — a half-tagged file is exactly the kind of thing a
    colour rule must not silently guess about.
    """
    return _encode_h264_sequence(
        tmp, name, count, "yuv420p",
        ["-x264-params", "colormatrix=bt709:transfer=bt709:colprim=bt709"],
    )


def make_field_coded_h264(tmp: Path, name: str = "field_coded.mkv", count: int = 50) -> Path:
    """H.264 that is really field-coded while its container says ``progressive``.

    ``-x264-params tff=1`` is the shape of a broadcast rip, and the measured reason
    a rule on ``field_order`` alone is not enough: the stream record says
    ``progressive`` and every decoded frame says ``interlaced_frame=1``.
    """
    return _encode_h264_sequence(tmp, name, count, "yuv420p", ["-x264-params", "tff=1"])


def make_container_flagged_progressive(
    tmp: Path, name: str = "container_flagged.mkv", count: int = 50
) -> Path:
    """Progressive H.264 asked for with ``-field_order tt``, which does not survive.

    Measured: this file probes ``progressive`` at stream level *and* in its frames.
    It exists so the refusal rule cannot be built on what was *asked* of the muxer —
    a file whose pictures are progressive must still render.
    """
    return _encode_h264_sequence(tmp, name, count, "yuv420p", ["-field_order", "tt"])


def make_interlaced_mpeg2(
    tmp: Path, name: str = "interlaced_mpeg2.ts", count: int = 50, with_sound: bool = False
) -> Path:
    """MPEG-2 written field-coded (``-flags +ildct+ilme -top 1``) into MPEG-TS.

    The classic broadcast/DVD shape. Unlike field-coded H.264 it *does* state its
    field order at stream level, and its decoded frames report ``interlaced_frame=1``;
    which of ``tt``/``bb`` the muxer settles on is measured, not assumed.

    ``with_sound`` adds a 660 Hz MP2 track, because "unusable picture must not block a
    soundtrack" has to be tested against a file that carries both.
    """
    directory = identity_sequence(tmp, count)
    out = tmp / name
    args = ["-framerate", "25", "-i", str(directory / "%04d.ppm")]
    if with_sound:
        args += ["-f", "lavfi", "-i",
                 f"sine=frequency=660:sample_rate=48000:duration={count // 25}",
                 "-c:a", "mp2"]
    else:
        args += ["-an"]
    args += [
        "-map_metadata", "-1", "-c:v", "mpeg2video", "-pix_fmt", "yuv420p", "-q:v", "8",
        "-g", "12", "-bf", "2", "-flags", "+ildct+ilme", "-top", "1", str(out),
    ]
    _run(args)
    return out


def make_hdr10(tmp: Path, name: str = "hdr10.mkv", count: int = 50) -> Path:
    """10-bit H.264 whose bitstream says BT.2020 primaries and the PQ transfer.

    Written by re-marking an ordinary 10-bit encode with
    ``-bsf:v h264_metadata=colour_primaries=9:transfer_characteristics=16:``
    ``matrix_coefficients=9``; measured to report ``color_transfer: smpte2084``.
    """
    return _encode_h264_sequence(
        tmp, name, count, "yuv420p10le", [],
        bsf="h264_metadata=colour_primaries=9:transfer_characteristics=16:"
            "matrix_coefficients=9",
    )


def make_hlg(tmp: Path, name: str = "hlg.mkv", count: int = 50) -> Path:
    """The same file with the HLG transfer (``transfer_characteristics=18``).

    Measured to print ``color_transfer: arib-std-b67`` — hyphenated, where FFmpeg's
    own option name is ``arib_std_b67``, which is why matching ignores punctuation.
    """
    return _encode_h264_sequence(
        tmp, name, count, "yuv420p10le", [],
        bsf="h264_metadata=colour_primaries=9:transfer_characteristics=18:"
            "matrix_coefficients=9",
    )


def make_sdr_10bit(tmp: Path, name: str = "sdr_10bit.mkv", count: int = 50) -> Path:
    """10-bit H.264 that states nothing about its transfer characteristics.

    Sample depth is not an HDR indicator: this is ordinary deep-bit SDR, and it must
    still render. Its unstated transfer is the case the application has to *say*
    something about without refusing anything.
    """
    return _encode_h264_sequence(tmp, name, count, "yuv420p10le", [])


def make_hdr_metadata_h265(tmp: Path, name: str = "hdr_metadata.mkv", count: int = 50) -> Path:
    """HEVC carrying HDR static metadata (MaxCLL) with no transfer tag to read.

    ``-x265-params max-cll=1000,400`` is measured on FFmpeg 8.1.3 to put a named
    ``Content light level metadata`` record on the decoded frames — the only of
    x265's HDR metadata records this FFmpeg prints by name: its mastering-display
    SEI comes back as an unregistered user-data SEI instead of a
    ``Mastering display metadata`` record. The file states no transfer at all, so it
    is the case where a *metadata record*, and not a colour tag, is what shows the
    picture is not SDR.
    """
    directory = identity_sequence(tmp, count)
    out = tmp / name
    _run([
        "-framerate", "25", "-i", str(directory / "%04d.ppm"), "-map_metadata", "-1", "-an",
        "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-preset", "ultrafast",
        "-x265-params", "keyint=12:max-cll=1000,400", str(out),
    ])
    return out


def make_untagged_avi(tmp: Path, name: str = "untagged.avi", count: int = 25) -> Path:
    """Motion JPEG in AVI: a picture that states no field order at all.

    Measured: no ``field_order`` key in the stream record, while a decoded-frame
    read reports ``interlaced_frame=0``. Unknown field order is warned and rendered,
    never refused.
    """
    directory = identity_sequence(tmp, count)
    out = tmp / name
    _run([
        "-framerate", "25", "-i", str(directory / "%04d.ppm"), "-map_metadata", "-1", "-an",
        "-c:v", "mjpeg", "-pix_fmt", "yuvj422p", "-q:v", "2", str(out),
    ])
    return out


def raw_picture_tags(path: Path, timeout: int = 60) -> dict:
    """The picture-format tags ``ffprobe`` prints for a file's first video stream.

    Read straight from the tool, never through the application's own parser, so a
    fixture that does not carry what its builder claims is caught as a fixture bug
    instead of being quietly asserted.
    """
    document = json.loads(_ffprobe([
        "-select_streams", "v:0", "-show_streams", "-show_entries",
        "stream=field_order,color_space,color_transfer,color_primaries,pix_fmt,"
        "bits_per_raw_sample,side_data_list",
        "-print_format", "json", str(path),
    ], timeout))
    streams = document.get("streams") or [{}]
    tags = dict(streams[0])
    frames = json.loads(_ffprobe([
        "-select_streams", "v:0", "-read_intervals", "%+#8", "-show_frames",
        "-show_entries",
        "frame=interlaced_frame,top_field_first,color_transfer"
        ":side_data_list=side_data_type",
        "-print_format", "json", str(path),
    ], timeout))
    records = frames.get("frames") or frames.get("packets_and_frames") or []
    pictures = [r for r in records if r.get("type") in (None, "frame", "video")]
    tags["frame_interlaced"] = any(r.get("interlaced_frame") for r in pictures)
    tags["frame_top_field_first"] = any(r.get("top_field_first") for r in pictures)
    tags["frame_side_data"] = sorted({
        entry.get("side_data_type")
        for r in pictures
        for entry in (r.get("side_data_list") or [])
        if entry.get("side_data_type")
    })
    tags["pictures_read"] = len(pictures)
    return tags

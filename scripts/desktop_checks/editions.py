"""Two editions of one film: synthetic fixtures and the manual A/B acceptance.

The motivating workflow of this application, written down as something that can
be run and measured: edition A and edition B are different cuts of the same
material — different frame rate, different shot order, different shot lengths,
one shot that exists only in B, one only in A. Neither file is the reference.
The editor builds one output that takes some pictures from A and some from B,
some sound from A and some from B, sums two sound tracks in one place, and uses
a fade, a half-gain clip, a gap and a rate change.

Two things are verified, and they are deliberately kept apart:

* what the **interface** can do — select a stream of one edition, place it under
  the picture of the other, align it by numbers, split and trim it;
* what the **composition** means — the output described below is rendered, then
  decoded, and every claim about it is measured from the decoded file: which
  colour is on screen in which output second, which tone is sounding, how loud,
  and where the silence is.

The layout and the claims about it come from the same table (``PICTURE``,
``UNDERNEATH``, ``SOUND``), so the check cannot drift away from what it built.

Run it unattended::

    python scripts/ab_workflow_check.py

Fixtures are generated in a scratch directory; no real film is needed or used.
"""

from __future__ import annotations

import array
import math
import subprocess
import sys
from dataclasses import dataclass, replace
from fractions import Fraction as F
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import driver as G

from video_editor.core.commands import (
    AddClipCommand,
    RegisterSourceCommand,
    SetFadesCommand,
    SetGainCommand,
    TrimClipCommand,
    TrimEdge,
)
from video_editor.core.model import (
    OutputSettings,
    Project,
    StreamSource,
    Track,
    TrackKind,
)

SR = 48000
SIZE = (128, 96)
A_RATE, B_RATE = 24, 25
OUT_RATE = F(A_RATE)


# --------------------------------------------------------------- the fixtures
@dataclass(frozen=True)
class Shot:
    """One shot of the film as it appears in each edition."""

    name: str
    colour: str
    hz: int
    a_start: F
    a_end: F
    b_start: F | None  # None: this shot is not in edition B
    b_end: F | None


#: the shot table. Edition A tells six shots in order, two seconds each at 24 fps.
#: Edition B tells five of them in a different order, three seconds each at 25
#: fps, adds a white shot A does not have, and leaves A's yellow shot out.
SHOTS: tuple[Shot, ...] = (
    Shot("red", "red", 220, F(0), F(2), F(9), F(12)),
    Shot("green", "green", 330, F(2), F(4), F(12), F(15)),
    Shot("blue", "blue", 440, F(4), F(6), F(15), F(18)),
    Shot("magenta", "magenta", 550, F(6), F(8), F(3), F(6)),
    Shot("yellow", "yellow", 660, F(8), F(10), None, None),  # only in A
    Shot("cyan", "cyan", 770, F(10), F(12), F(0), F(3)),
)
#: B-only material, present so that "ignore what you do not want" is testable
B_ONLY = ("white", 880, F(6), F(9))


@dataclass(frozen=True)
class Editions:
    """Paths of the two editions. Each container holds stream 0 (video) and
    stream 1 (audio): neither file is privileged and numbering is per file."""

    a: Path
    b: Path

    a_video = 0
    a_audio = 1
    b_video = 0
    b_audio = 1


def _segments_for(edition: str) -> tuple[tuple[str, int, int], ...]:
    if edition == "a":
        return tuple((shot.colour, shot.hz, 2) for shot in SHOTS)
    order = [(B_ONLY[0], B_ONLY[1], 3)]  # built in B's own running order
    for shot in SHOTS:
        if shot.b_start is None:
            continue
        order.append((shot.colour, shot.hz, 3))
    # B's running order: cyan, magenta, white, red, green, blue
    wanted = {"cyan": 0, "magenta": 1, "white": 2, "red": 3, "green": 4, "blue": 5}
    return tuple(sorted(order, key=lambda item: wanted[item[0]]))


def _ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                          capture_output=True, timeout=600)


def _build(path: Path, fps: int, segments: tuple[tuple[str, int, int], ...]) -> None:
    args: list[str] = []
    for colour, hz, seconds in segments:
        args += ["-f", "lavfi", "-i", f"color=c={colour}:s={SIZE[0]}x{SIZE[1]}:r={fps}:d={seconds}"]
        args += ["-f", "lavfi", "-i", f"sine=frequency={hz}:duration={seconds}:sample_rate={SR}"]
    parts = "".join(f"[{2 * i}:v][{2 * i + 1}:a]" for i in range(len(segments)))
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args,
         "-filter_complex", f"{parts}concat=n={len(segments)}:v=1:a=1[v][a]",
         "-map", "[v]", "-map", "[a]", "-r", str(fps),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", str(fps),
         "-c:a", "pcm_s16le", str(path)],
        check=True, capture_output=True, timeout=600,
    )


def make_editions(directory: Path) -> Editions:
    """Generate the two editions (each has one video and one audio stream)."""
    directory.mkdir(parents=True, exist_ok=True)
    a, b = directory / "edition_a.mkv", directory / "edition_b.mkv"
    _build(a, A_RATE, _segments_for("a"))
    _build(b, B_RATE, _segments_for("b"))
    return Editions(a=a, b=b)


# ------------------------------------------------------------- the output plan
@dataclass(frozen=True)
class PictureItem:
    clip_id: str
    edition: str
    stream: int
    track: str
    out_start: F
    src_in: F
    src_out: F
    rate: F
    colour: str

    @property
    def out_end(self) -> F:
        return self.out_start + (self.src_out - self.src_in) / self.rate


@dataclass(frozen=True)
class SoundItem:
    clip_id: str
    edition: str
    stream: int
    track: str
    out_start: F
    src_in: F
    src_out: F
    hz: int
    gain: F = F(1)
    fade_out: F = F(0)

    @property
    def out_end(self) -> F:
        return self.out_start + (self.src_out - self.src_in)


#: The top picture track: red/green/magenta/yellow from edition A, blue from
#: edition B, and edition B's cyan at rate 3/2 (three source seconds occupying
#: two output seconds). Six items, twelve output seconds.
PICTURE: tuple[PictureItem, ...] = (
    PictureItem("p-red", "a", 0, "picture", F(0), F(0), F(2), F(1), "red"),
    PictureItem("p-green", "a", 0, "picture", F(2), F(2), F(4), F(1), "green"),
    PictureItem("p-blue", "b", 0, "picture", F(4), F(15), F(17), F(1), "blue"),
    PictureItem("p-magenta", "a", 0, "picture", F(6), F(6), F(8), F(1), "magenta"),
    PictureItem("p-yellow", "a", 0, "picture", F(8), F(8), F(10), F(1), "yellow"),
    PictureItem("p-cyan", "b", 0, "picture", F(10), F(0), F(3), F(3, 2), "cyan"),
)

#: A second picture track, deliberately covered by the one above: where both
#: have material, only the top track may be seen.
UNDERNEATH: tuple[PictureItem, ...] = (
    PictureItem("p-under", "a", 0, "under", F(4), F(0), F(2), F(1), "red"),
)

#: Two independent sound tracks. "dub" carries edition B, "orig" carries edition
#: A; they overlap for one output second (a real sum of two editions), one clip
#: fades out, one sits at half gain, and A's yellow sound stops halfway, leaving
#: silence under a continuing picture.
SOUND: tuple[SoundItem, ...] = (
    SoundItem("s-red", "b", 1, "dub", F(0), F(9), F(11), 220),
    SoundItem("s-green", "b", 1, "dub", F(2), F(12), F(14), 330, fade_out=F(1)),
    SoundItem("s-blue", "a", 1, "orig", F(4), F(4), F(6), 440),
    SoundItem("s-magenta", "b", 1, "dub", F(6), F(3), F(5), 550),
    SoundItem("s-overlap", "a", 1, "orig", F(6), F(10), F(12), 770),
    SoundItem("s-yellow", "a", 1, "orig", F(8), F(8), F(9), 660, gain=F(1, 2)),
    SoundItem("s-cyan", "b", 1, "dub", F(10), F(0), F(2), 770),
)

ALL_ITEMS: tuple[PictureItem | SoundItem, ...] = (*PICTURE, *UNDERNEATH, *SOUND)


def output_end() -> F:
    return max(item.out_end for item in ALL_ITEMS)


def expected_picture(when: F) -> PictureItem | None:
    """Which picture item wins at output time *when* (tracks[0] is the top)."""
    for item in (*PICTURE, *UNDERNEATH):
        if item.out_start <= when < item.out_end:
            return item
    return None


def expected_sounds(when: F) -> tuple[SoundItem, ...]:
    return tuple(item for item in SOUND if item.out_start <= when < item.out_end)


# ------------------------------------------------------------ building it
def register_streams(project: Project, pairs: list[tuple[str, Path]]) -> Project:
    """Register every stream of every file, read from their own probes.

    No file is the reference: all streams are peers, and their bounds, rates and
    offsets come from what the container says, not from what a check hoped the
    fixtures would be. ``pairs`` maps asset id -> path, so the ids stay the ones
    the window's own bin gave these files.
    """
    from video_editor.media.ffprobe import probe_file

    for asset_id, path in pairs:
        result = probe_file(path)
        if not result.ok:
            raise RuntimeError(f"{path}: {result.error}")
        metadata = result.metadata
        position = {TrackKind.VIDEO: 0, TrackKind.AUDIO: 0}
        for stream in metadata.streams:
            kind = TrackKind.VIDEO if stream.kind.value == "video" else TrackKind.AUDIO
            if not stream.bounds_known:
                raise RuntimeError(f"{path}: stream {stream.stream_index} has no bounds")
            project = RegisterSourceCommand(StreamSource(
                asset_id=asset_id, stream_index=stream.stream_index, kind=kind,
                origin=stream.origin, end=stream.end_time,
                conservative_end=stream.bounds_are_conservative,
                frame_rate=stream.frame_rate, sample_rate=stream.sample_rate,
                channel_layout=stream.channel_layout,
                width=stream.width, height=stream.height,
                stream_position=position[kind],
                input_start=metadata.container_start or F(0),
            )).apply(project)
            position[kind] += 1
    return project


def register(project: Project, editions: Editions, asset_ids: dict[str, str]) -> Project:
    """Register both editions under the ids the window gave them at import."""
    return register_streams(project, [(asset_ids["a"], editions.a),
                                      (asset_ids["b"], editions.b)])


def composition(editions: Editions, asset_ids: dict[str, str]) -> Project:
    """The layout described by the tables above, built by ordinary commands."""
    project = Project(output=OutputSettings(SIZE[0], SIZE[1], OUT_RATE, SR, "stereo"))
    project = register(project, editions, asset_ids)
    project = project.with_tracks((
        Track("picture", TrackKind.VIDEO, "Picture"),
        Track("under", TrackKind.VIDEO, "Picture (kept underneath)"),
        Track("dub", TrackKind.AUDIO, "Sound (edition B)"),
        Track("orig", TrackKind.AUDIO, "Sound (edition A)"),
    ))
    for item in ALL_ITEMS:
        project = AddClipCommand(
            asset_ids[item.edition], item.stream, item.track, item.out_start,
            source_in=item.src_in, source_out=item.src_out,
            rate=getattr(item, "rate", F(1)), clip_id=item.clip_id,
        ).apply(project)
        if isinstance(item, SoundItem):
            if item.gain != 1:
                project = SetGainCommand(item.clip_id, item.gain).apply(project)
            if item.fade_out:
                project = SetFadesCommand(item.clip_id, F(0), item.fade_out).apply(project)
    return project


def mixed_session(editions: Editions | None = None) -> tuple[object, Editions, Project,
                                                            dict[str, Path]]:
    """A window that imported both editions, holding the mixed composition.

    Everything a real session has: the bin's own asset ids, real probe data and
    the timeline showing the result. Passing ``editions`` lets a later leg reuse
    fixtures it has already renamed instead of generating another pair.
    """
    editions = editions or make_editions(G.WORKDIR / "media")
    win = G.window()
    G.import_files([editions.a, editions.b])
    by_path = {Path(asset.path): asset.id for asset in win.bin_controller.assets}
    ids = {"a": by_path[editions.a], "b": by_path[editions.b]}
    paths = {ids["a"]: editions.a, ids["b"]: editions.b}
    project = composition(editions, ids)
    win.session.replace_project(project)
    win._refresh_timeline()
    G.pump(300)
    return win, editions, project, paths


# ------------------------------------------------------------- decoding it
PALETTE = {
    "red": (255, 0, 0), "green": (0, 128, 0), "blue": (0, 0, 255),
    "magenta": (255, 0, 255), "yellow": (255, 255, 0), "cyan": (0, 255, 255),
    "white": (255, 255, 255), "black": (16, 16, 18),
}


def nearest_colour(rgb: tuple[int, int, int]) -> str:
    return min(PALETTE, key=lambda name: sum(
        (a - b) ** 2 for a, b in zip(rgb, PALETTE[name], strict=True)))


def decode_picture(path: Path) -> list[tuple[int, int, int]]:
    """Centre pixel of every decoded frame, in one decode pass.

    Taken in the decoder's own output order, never by seeking: seeking can land
    on a different frame than the one a viewer would show.
    """
    x, y = SIZE[0] // 2 - 1, SIZE[1] // 2 - 1
    proc = _ffmpeg(["-i", str(path), "-vf", f"crop=2:2:{x}:{y}", "-f", "rawvideo",
                    "-pix_fmt", "rgb24", "-"])
    if proc.returncode != 0 or len(proc.stdout) % 12:
        raise RuntimeError(proc.stderr.decode()[-300:])
    return [
        (
            (proc.stdout[i * 12] + proc.stdout[i * 12 + 3] + proc.stdout[i * 12 + 6]) // 3,
            (proc.stdout[i * 12 + 1] + proc.stdout[i * 12 + 4] + proc.stdout[i * 12 + 7]) // 3,
            (proc.stdout[i * 12 + 2] + proc.stdout[i * 12 + 5] + proc.stdout[i * 12 + 8]) // 3,
        )
        for i in range(len(proc.stdout) // 12)
    ]


def decode_audio(path: Path) -> list[float]:
    """The whole soundtrack, decoded once, mono, at the output sample rate."""
    proc = _ffmpeg(["-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode()[-300:])
    return list(array.array("f", proc.stdout))


def colour_at(frames: list[tuple[int, int, int]], when: F) -> tuple[str, tuple[int, int, int]]:
    index = max(0, min(len(frames) - 1, int(when * OUT_RATE)))
    rgb = frames[index]
    return nearest_colour(rgb), rgb


def window_of(samples: list[float], start: F, seconds: F) -> list[float]:
    first, last = int(start * SR), int((start + seconds) * SR)
    return samples[max(0, first):min(len(samples), last)]


def rms(samples: list[float]) -> float:
    return math.sqrt(sum(x * x for x in samples) / len(samples)) if samples else 0.0


def _goertzel(samples: list[float], freq: int) -> float:
    """Magnitude of one frequency component (single-bin DFT: no FFT needed)."""
    if not samples:
        return 0.0
    coeff = 2 * math.cos(2 * math.pi * freq / SR)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s1, s2 = s0, s1
    return math.sqrt(max(0.0, s1 * s1 + s2 * s2 - coeff * s1 * s2)) / len(samples)


def tone_amplitude(samples: list[float], start: F, freq: int, seconds=F(1, 4)) -> float:
    return _goertzel(window_of(samples, start, seconds), freq)


def tone_share(samples: list[float], start: F, freq: int, seconds=F(1, 4)) -> float:
    """How much of this window's energy is at *freq* (1.0 would be a pure tone)."""
    chunk = window_of(samples, start, seconds)
    noise = rms(chunk)
    return _goertzel(chunk, freq) / noise if noise else 0.0


def decibels(quiet: float, loud: float) -> float:
    if quiet <= 0 or loud <= 0:
        return math.inf
    return -20 * math.log10(quiet / loud)


# ------------------------------------------------------------- measuring it
class Report:
    """Prints each measured claim as it is decided; counts failures."""

    def __init__(self) -> None:
        self.failures = 0

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""),
              flush=True)
        if not ok:
            self.failures += 1


def verify_rendered(path: Path, report: Report, tag: str, start: F = F(0),
                    end: F | None = None) -> None:
    """Every claim below is measured from the decoded file, not from the plan.

    ``start``/``end`` say which output interval was rendered, so the same
    measurements apply to a whole composition and to a selected review window:
    file-local time is composition time minus ``start``.
    """
    frames = decode_picture(path)
    samples = decode_audio(path)
    stop = end if end is not None else output_end()
    span = stop - start

    report.check(f"{tag}: the decoded picture has exactly the planned frame count",
                 len(frames) == int(span * OUT_RATE),
                 f"{len(frames)} frames @ {OUT_RATE} fps, planned {int(span * OUT_RATE)}")
    report.check(f"{tag}: the decoded soundtrack is the planned length at 48 kHz",
                 abs(len(samples) / SR - float(span)) < 0.05,
                 f"{len(samples)} samples = {len(samples) / SR:.3f} s, planned "
                 f"{float(span):.3f} s")

    for item in PICTURE:
        if item.out_end <= start or item.out_start >= stop:
            continue
        for when, where in ((item.out_start + F(1, 10), "just after the cut"),
                            (item.out_start + (item.out_end - item.out_start) / 2, "mid-shot"),
                            (item.out_end - F(1, 10), "just before the cut")):
            if not start <= when < stop:
                continue
            colour, rgb = colour_at(frames, when - start)
            report.check(
                f"{tag}: output {float(item.out_start)}-{float(item.out_end)} s shows edition "
                f"{item.edition.upper()}'s {item.colour} picture ({where})",
                colour == item.colour, f"measured {colour} {rgb} at t={float(when):.2f}")

    covered = F(5)
    if start <= covered < stop:
        colour, rgb = colour_at(frames, covered - start)
        report.check(
            f"{tag}: the top picture track wins where a lower track also has material",
            colour == "blue",
            f"measured {colour} {rgb} at t={float(covered)}; the lower track holds "
            "edition A's red picture there")

    for item in SOUND:
        if item.out_end <= start or item.out_start >= stop:
            continue
        mid = item.out_start + (item.out_end - item.out_start) / 2
        if not start <= mid < stop:
            continue
        at = max(F(0), mid - start - F(1, 8))
        share = tone_share(samples, at, item.hz)
        mixed = len(expected_sounds(mid)) > 1
        report.check(
            f"{tag}: output {float(item.out_start)}-{float(item.out_end)} s sounds like "
            f"edition {item.edition.upper()}'s {item.hz} Hz tone"
            + (" (summed with another track)" if mixed else ""),
            share > (0.35 if mixed else 0.5), f"share {share:.2f} at t={float(mid):.2f}")
        for intruder_hz in (660, 880):
            if intruder_hz == item.hz or any(s.hz == intruder_hz
                                             for s in expected_sounds(mid)):
                continue
            level = tone_share(samples, at, intruder_hz)
            report.check(f"{tag}: no {intruder_hz} Hz tone where none was placed "
                         f"(t={float(mid):.2f})", level < 0.25, f"share {level:.2f}")

    overlap = F(13, 2)
    if start <= overlap < stop:
        first = tone_share(samples, overlap - start - F(1, 8), 550)
        second = tone_share(samples, overlap - start - F(1, 8), 770)
        report.check(
            f"{tag}: at t={float(overlap)} s both editions' sound really play together",
            first > 0.35 and second > 0.35, f"550 Hz share {first:.2f}, 770 Hz {second:.2f}")

    if start <= F(91, 10) and stop >= F(98, 10):
        level = rms(window_of(samples, F(91, 10) - start, F(7, 10)))
        colour, rgb = colour_at(frames, F(19, 2) - start)
        report.check(
            f"{tag}: the gap in the soundtrack is silence while the picture continues",
            level < 0.002 and colour == "yellow",
            f"rms {level:.6f} at t=9.1-9.8 s, picture {colour} {rgb}")

    if start <= F(0) and stop >= output_end():
        faded = next(item for item in SOUND if item.fade_out)
        loud = tone_amplitude(samples, faded.out_start + F(1, 20), faded.hz)
        quiet = tone_amplitude(samples, faded.out_end - F(7, 20), faded.hz)
        report.check(
            f"{tag}: the {faded.hz} Hz tone fades away over the last "
            f"{float(faded.fade_out)} s of its clip",
            decibels(quiet, loud) >= 8.0,
            f"{decibels(quiet, loud):.1f} dB down inside the ramp")

        halved = next(item for item in SOUND if item.gain != 1)
        full = SOUND[0]
        ratio = (tone_amplitude(samples, halved.out_start + F(1, 20), halved.hz)
                 / tone_amplitude(samples, full.out_start + F(1, 20), full.hz))
        report.check(f"{tag}: the half-gain clip really contributes half the level",
                     0.3 < ratio < 0.7, f"measured ratio {ratio:.3f}")

        seen = {nearest_colour(rgb) for rgb in frames}
        report.check(f"{tag}: edition B's white shot was never used, so it never leaks in",
                     "white" not in seen, f"colours seen: {sorted(seen)}")
        leaked = max(tone_share(samples, item.out_start + F(1, 2), B_ONLY[1])
                     for item in PICTURE)
        report.check(f"{tag}: edition B's {B_ONLY[1]} Hz tone never appears in the output",
                     leaked < 0.25, f"largest share anywhere: {leaked:.2f}")


# ---------------------------------------------------------------- interface
def stream_rows(win, asset_name: str) -> list[tuple[int, str]]:
    """The stream rows the bin offers for one asset: (stream index, label)."""
    from PySide6.QtCore import Qt

    tree = win.media_bin
    for row in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(row)
        if asset_name in top.text(0):
            return [
                (int(top.child(i).data(0, Qt.UserRole + 2)), top.child(i).text(0))
                for i in range(top.childCount())
            ]
    return []


def probed_streams(win) -> dict[str, dict[int, dict[str, object]]]:
    """What the window actually recorded about each imported file's streams.

    Read back from the bin rather than from what this check hoped the fixtures
    would be: the whole A/B workflow depends on each edition keeping its own
    rate and bounds, so those numbers are verified, not assumed.
    """
    rows: dict[str, dict[int, dict[str, object]]] = {}
    for asset in win.bin_controller.assets:
        metadata = asset.metadata
        name = Path(asset.path).name
        if metadata is None:
            rows[name] = {}
            continue
        rows[name] = {
            stream.stream_index: {
                "kind": stream.kind.value,
                "frame_rate": stream.frame_rate,
                "sample_rate": stream.sample_rate,
                "size": (stream.width, stream.height),
                "origin": stream.origin,
                "end": stream.end_time,
                "position": metadata.position_of(stream.stream_index),
            }
            for stream in metadata.streams
        }
    return rows


def interface_leg(win, editions: Editions, report: Report, answers) -> None:
    """What the interface must allow for this workflow, driven through widgets."""
    G.import_files([editions.a, editions.b])
    names = sorted(Path(asset.path).name for asset in win.bin_controller.assets)
    report.check("both editions imported as peers, neither marked as a reference",
                 names == ["edition_a.mkv", "edition_b.mkv"], str(names))
    answers.project_answers["Media status"] = "OK"
    win._show_media_status()
    G.pump(300)
    listing = (answers.project_dialogs.get("Media status") or [""])[-1]
    report.check("the window reports both editions as online, by their own paths",
                 listing.count("[online]") == 2 and "edition_a.mkv" in listing
                 and "edition_b.mkv" in listing, listing.replace("\n", " · ")[:200])
    for name in names:
        rows = stream_rows(win, name)
        kinds = [label.split()[0].lower() for _index, label in rows]
        report.check(f"{name} offers its own video and audio stream to choose from",
                     kinds.count("video") == 1 and kinds.count("audio") == 1, str(rows))

    probed = probed_streams(win)
    a_video = probed.get("edition_a.mkv", {}).get(editions.a_video, {})
    b_video = probed.get("edition_b.mkv", {}).get(editions.b_video, {})
    report.check("edition A keeps its 24 fps and edition B its 25 fps: the rate difference is "
                 "recorded per file, not normalised away",
                 a_video.get("frame_rate") == F(A_RATE)
                 and b_video.get("frame_rate") == F(B_RATE),
                 f"A {a_video.get('frame_rate')} / B {b_video.get('frame_rate')}")
    report.check("B's extra footage shows as a longer stream, and A's shorter one is not "
                 "stretched to match",
                 a_video.get("end") is not None and b_video.get("end") is not None
                 and a_video["end"] < b_video["end"],
                 f"A ends {a_video.get('end')} s, B ends {b_video.get('end')} s")
    report.check("each edition's audio is addressable as audio, by its own stream index",
                 probed.get("edition_a.mkv", {}).get(editions.a_audio, {}).get("kind") == "audio"
                 and probed.get("edition_b.mkv", {}).get(editions.b_audio, {}).get("kind")
                 == "audio",
                 str({name: sorted(rows) for name, rows in probed.items()}))

    ids = {Path(asset.path).name: asset.id for asset in win.bin_controller.assets}
    a_id, b_id = ids["edition_a.mkv"], ids["edition_b.mkv"]

    G.select_streams("edition_a.mkv", (editions.a_video,))
    G.set_playhead(F(0))
    G.place_at_playhead()
    picture = [c for c in win.session.project.clips if c.asset_id == a_id]
    report.check("edition A's video stream can be placed on its own",
                 len(picture) == 1 and picture[0].stream_index == editions.a_video,
                 f"{len(picture)} clip(s)")
    report.check("placing picture registers no sound the editor did not choose",
                 not any(c.asset_id == b_id for c in win.session.project.clips))

    G.select_streams("edition_b.mkv", (editions.b_audio,))
    G.set_playhead(F(3))
    G.place_at_playhead()
    dub = [c for c in win.session.project.clips if c.asset_id == b_id]
    report.check("edition B's audio can be placed under edition A's picture",
                 len(dub) == 1 and dub[0].stream_index == editions.b_audio,
                 str([(str(c.stream_index), str(c.output_start)) for c in dub]))
    picture_tracks = {c.track_id for c in win.session.project.clips if c.asset_id == a_id}
    dub_tracks = {c.track_id for c in dub}
    report.check("the two editions occupy separate tracks, so they do not compete",
                 bool(picture_tracks) and bool(dub_tracks)
                 and not (picture_tracks & dub_tracks),
                 f"{picture_tracks} / {dub_tracks}")

    clip = dub[0]
    win.timeline.set_clip_selection((clip.id,))
    win.inspector.show_clip(win.session.project, clip)
    win.inspector.output_start.setText("2")
    win.inspector.source_in.setText("9")
    win.inspector.source_out.setText("11")
    win.inspector.apply_button.click()
    G.pump(200)
    moved = next(c for c in win.session.project.clips if c.id == clip.id)
    report.check("exact numbers align the other edition's sound under the picture",
                 (moved.output_start, moved.source_in, moved.source_out)
                 == (F(2), F(9), F(11)),
                 f"{moved.output_start} {moved.source_in} {moved.source_out}")
    still = next(c for c in win.session.project.clips if c.asset_id == a_id)
    report.check("aligning the sound did not move the picture",
                 still.output_start == F(0) and still.source_in == F(0),
                 f"{still.output_start} {still.source_in}")
    report.check("picture and sound stay unlinked: each was chosen separately",
                 all(not c.link_group for c in win.session.project.clips))

    before = {(c.id, c.output_start, c.source_in, c.source_out)
              for c in win.session.project.clips}
    win._undo()
    G.pump(150)
    after = {(c.id, c.output_start, c.source_in, c.source_out)
             for c in win.session.project.clips}
    report.check("one undo restores both clips exactly",
                 before != after and len(after) == len(before), f"{len(before)} clips")
    win._redo()
    G.pump(150)
    report.check("redo puts the alignment back exactly",
                 {(c.id, c.output_start, c.source_in, c.source_out)
                  for c in win.session.project.clips} == before)


def edit_leg(win, report: Report) -> None:
    """Split and trim across an edition boundary, through the real actions."""
    kinds = {track.id: track.kind for track in win.session.project.tracks}
    audio = [c for c in win.session.project.clips if kinds.get(c.track_id) is TrackKind.AUDIO]
    report.check("the two placements produced exactly one sound clip to work on",
                 len(audio) == 1, f"{len(audio)} audio clip(s)")
    dub = audio[0]

    win.timeline.set_clip_selection((dub.id,))
    win.timeline.set_playhead(F(3), from_user=True)
    G.pump(200)
    win.split_action.trigger()
    G.pump(250)
    pieces = [c for c in win.session.project.clips if c.track_id == dub.track_id]
    report.check("splitting the other edition's sound makes two clips and one keeps the "
                 "original id",
                 len(pieces) == 2 and dub.id in {c.id for c in pieces},
                 str([(c.id[:6], str(c.output_start), str(c.output_end), str(c.source_in),
                       str(c.source_out)) for c in pieces]))
    if len(pieces) != 2:
        return
    left = next(c for c in pieces if c.id == dub.id)
    right = next(c for c in pieces if c.id != dub.id)
    report.check("the split lands exactly where the playhead was, in output and in source",
                 left.output_end == F(3) == right.output_start
                 and left.source_out == right.source_in
                 == dub.source_in + (F(3) - dub.output_start) * dub.rate,
                 f"left {left.output_start}-{left.output_end} src ...-{left.source_out}, "
                 f"right {right.output_start}-{right.output_end} src {right.source_in}-...")

    shortened = win._apply(TrimClipCommand(right.id, TrimEdge.RIGHT,
                                           right.source_out - F(1, 2)))
    G.pump(200)
    trimmed = next(c for c in win.session.project.clips if c.id == right.id)
    report.check("trimming the right edge shortens that clip without moving the rest",
                 shortened and trimmed.source_out == right.source_out - F(1, 2)
                 and trimmed.output_start == right.output_start
                 and trimmed.output_end == right.output_end - F(1, 2),
                 f"{trimmed.output_start}-{trimmed.output_end} from "
                 f"{trimmed.source_in}-{trimmed.source_out}")
    win._undo()
    G.pump(200)
    report.check("one undo brings the trimmed material back",
                 next(c for c in win.session.project.clips if c.id == right.id).source_out
                 == right.source_out)


# --------------------------------------------------------------------- legs
def render_leg(win, project, asset_paths: dict[str, Path], target: Path, report: Report,
               out_start: F = F(0), out_end: F | None = None,
               tag: str = "the mixed composition") -> None:
    """Render through the window's own controller, then measure the file."""
    from video_editor.render.exporter import RenderRequest

    if target.exists():
        target.unlink()
    request = RenderRequest(
        project=project, asset_paths=asset_paths, destination=target,
        out_start=out_start, out_end=out_end, overwrite=True,
    )
    started = win.render_controller.start(request)
    report.check(f"{tag}: the window accepted the render", started,
                 "" if started else "the controller refused the request")
    spy = G.SignalSpy(win.render_controller)
    deadline = 9000
    while win.render_controller.running and deadline:
        G.pump(100)
        deadline -= 1
    if spy.failed_messages:
        report.check(f"{tag}: the render finished", False, spy.failed_messages[0][:200])
        return
    report.check(f"{tag}: the render finished and wrote its file", bool(spy.finished)
                 and target.is_file(), str(target))
    if target.is_file():
        try:
            verify_rendered(target, report, tag, out_start, out_end)
        except (RuntimeError, OSError) as exc:  # the measuring itself failed
            report.check(f"{tag}: the rendered file could be decoded for measuring", False,
                         str(exc)[:200])


def export_dialog_leg(win, project, asset_paths: dict[str, Path], report: Report) -> None:
    """The export dialog's stated choices are the ones that actually get rendered."""
    from PySide6.QtWidgets import QDialogButtonBox

    from video_editor.ui.export_dialog import RANGE_CUSTOM, RANGE_WHOLE, ExportDialog

    whole = G.WORKDIR / "dialog-whole.mp4"
    dialog = ExportDialog(win, project, whole, F(0))
    dialog.range_box.setCurrentIndex(dialog.range_box.findData(RANGE_WHOLE))
    G.pump(100)
    summary = dialog.summary.toPlainText()
    report.check("the export dialog accepts the whole mixed composition",
                 dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled(),
                 dialog._error_label.text() or summary.splitlines()[0])
    report.check("the dialog counts the picture items and finds no black fill",
                 f"{len(PICTURE)} item(s)" in summary and "0 black" in summary,
                 next((line for line in summary.splitlines() if "Picture:" in line), ""))
    report.check("the dialog counts every sound contribution, summed rather than exclusive",
                 f"{len(SOUND)} contribution(s)" in summary,
                 next((line for line in summary.splitlines() if "Audio:" in line), ""))
    dialog.reject()

    interval = G.WORKDIR / "interval.mp4"
    picker = ExportDialog(win, project, interval, F(5))
    picker.range_box.setCurrentIndex(picker.range_box.findData(RANGE_CUSTOM))
    picker.start_edit.setText("5")
    picker.end_edit.setText("7")
    G.pump(100)
    choices = picker.choices()
    report.check("an explicit output interval can be chosen for review",
                 (choices.out_start, choices.out_end) == (F(5), F(7)),
                 f"{choices.out_start} .. {choices.out_end}")
    report.check("the summary states exactly what that interval will contain",
                 "48 frames" in picker.summary.toPlainText(),
                 picker.summary.toPlainText().splitlines()[0])
    picker.reject()
    render_leg(win, project, asset_paths, choices.destination, report, choices.out_start,
               choices.out_end, "the selected 5-7 s interval")


def persistence_leg(win, editions: Editions, project: Project, report: Report,
                    asset_paths: dict[str, Path]) -> None:
    """Save the mix, reopen it in another window, render from the reopened copy."""
    folder = G.WORKDIR / "mix"
    folder.mkdir(parents=True, exist_ok=True)
    document = folder / "mixed.vep"
    if document.exists():
        document.unlink()

    report.check("the mixed composition of two editions saves",
                 win._save_project(path=document) and document.is_file(), str(document))
    report.check("saving a project writes nothing beside the media themselves",
                 not list((G.WORKDIR / "media").glob("*.vep")),
                 str(sorted(p.name for p in (G.WORKDIR / "media").iterdir())))
    G.close_window()

    second = G.window()
    reopened = second._open_project(document)
    G.pump(400)
    report.check("the saved mix reopens in a fresh window, identical down to ids and exact "
                 "rational times",
                 reopened and second.session.project == project,
                 G.status_text()[:140])
    records = {record.asset.id: record for record in second._media_records}
    report.check("both editions come back online, resolved from the saved document",
                 set(asset_paths) <= set(records)
                 and all(record.state.value == "online" for record in records.values()),
                 str([(key[:8], record.state.value, str(record.resolved_path))
                      for key, record in records.items()]))
    resolved = {key: record.resolved_path for key, record in records.items()
                if key in asset_paths}
    render_leg(second, second.session.project, resolved, G.WORKDIR / "reopened.mp4", report,
               tag="the reopened mix")
    G.close_window()


def cancellation_leg(win, project: Project, asset_paths: dict[str, Path],
                     report: Report) -> None:
    """A cancelled render leaves no output and no partial file."""
    target = G.WORKDIR / "cancelled.mp4"
    for stale in (target, *target.parent.glob(f"{target.stem}.*.partial.*")):
        if stale.exists():
            stale.unlink()

    from video_editor.render.exporter import RenderRequest

    request = RenderRequest(project=project, asset_paths=asset_paths, destination=target,
                            overwrite=True)
    started = win.render_controller.start(request)
    report.check("a mixed render starts", started,
                 "" if started else "the controller refused the request")
    spy = G.SignalSpy(win.render_controller)
    G.pump(400)
    win.render_controller.cancel()
    deadline = 600
    while win.render_controller.running and deadline:
        G.pump(100)
        deadline -= 1
    report.check("cancelling it stops the render",
                 not win.render_controller.running and spy.cancelled == 1,
                 f"running={win.render_controller.running} cancelled={spy.cancelled} "
                 f"finished={len(spy.finished)} failed={spy.failed_messages}")
    report.check("a cancelled render leaves no output file", not target.exists(), str(target))
    debris = sorted(p.name for p in target.parent.glob(f"{target.stem}.*.partial.*"))
    report.check("and leaves no partial file behind", not debris, str(debris))


def missing_source_leg(project: Project, editions: Editions, asset_paths: dict[str, Path],
                       report: Report) -> Editions:
    """A moved edition is named as missing; pointing at the new name works again."""
    from video_editor.render.exporter import RenderError, RenderRequest, render

    folder = G.WORKDIR / "media"
    moved = folder / "edition_b_moved.mkv"
    editions.b.rename(moved)
    fixed = replace(editions, b=moved)
    target = G.WORKDIR / "after-move.mp4"
    if target.exists():
        target.unlink()

    request = RenderRequest(project=project, asset_paths=asset_paths, destination=target,
                            out_start=F(4), out_end=F(6), overwrite=True)
    try:
        render(request)
        report.check("a render whose source has moved away is refused", False,
                     "it unexpectedly produced a file")
    except RenderError as exc:
        message = str(exc)
        report.check("a render whose source has moved away is refused, naming the file",
                     "missing" in message.lower() and str(editions.b) in message,
                     message[:180])
    report.check("the refused render wrote nothing", not target.exists(), str(target))

    fixed_paths = {key: (fixed.b if path == editions.b else path)
                   for key, path in asset_paths.items()}
    retry = replace(request, asset_paths=fixed_paths)
    try:
        result = render(retry)
        report.check("once the source is pointed out again, the same range renders",
                     target.is_file() and result.produced_duration > 0, str(target))
    except RenderError as exc:
        report.check("once the source is pointed out again, the same range renders", False,
                     str(exc)[:200])
    return fixed


# -------------------------------------------------------------------- runner
def run() -> int:
    report = Report()
    media = G.WORKDIR / "media"
    editions = make_editions(media)
    print(f"edition A: {editions.a}  ({A_RATE} fps, 12 s, 6 shots)")
    print(f"edition B: {editions.b}  ({B_RATE} fps, 18 s, 6 shots, one extra)")
    print(f"planned output: {float(output_end())} s @ {OUT_RATE} fps, {SIZE[0]}x{SIZE[1]}, "
          f"{len(ALL_ITEMS)} clips on 4 tracks")

    answerer = G.ModalAnswerer()
    answerer.start()
    try:
        print("\ninterface: choosing picture and sound independently")
        win = G.window()
        interface_leg(win, editions, report, answerer)
        print("\nedits: splitting and trimming across the edition boundary")
        edit_leg(win, report)
        G.close_window()

        print("\ncomposition: two editions mixed, rendered and measured")
        win, editions, project, paths = mixed_session()
        report.check("the mixed composition is built from independent clips on four tracks",
                     len(project.clips) == len(ALL_ITEMS) and len(project.tracks) == 4
                     and project.output_end() == output_end(),
                     f"{len(project.clips)} clips, {len(project.tracks)} tracks, end "
                     f"{project.output_end()}")
        render_leg(win, project, paths, G.WORKDIR / "mixed.mp4", report)
        export_dialog_leg(win, project, paths, report)

        print("\ncancellation: a mixed render stopped halfway")
        cancellation_leg(win, project, paths, report)
        G.close_window()

        print("\nre-render: the same mix after edition B was renamed")
        editions = missing_source_leg(project, editions, paths, report)
        win, _ed, project, paths = mixed_session(editions)
        render_leg(win, project, paths, G.WORKDIR / "after-move-mix.mp4", report,
                   tag="the mix with edition B at its new path")
        G.close_window()

        print("\npersistence: save the mix, reopen it, render from the reopened copy")
        win, editions, project, paths = mixed_session()
        persistence_leg(win, editions, project, report, paths)
    finally:
        G.close_window()
        answerer.stop()
        from video_editor.ui.decorations import shutdown_all

        shutdown_all()

    print(f"\n{'FAILED' if report.failures else 'OK'}: {report.failures} failed check(s)")
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(run())

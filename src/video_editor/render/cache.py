"""Bounded caches for preview media: rendered ranges on disk, frames in RAM.

Both caches are explicitly bounded and own only what they create:

* The disk cache keeps rendered preview ranges in one directory it owns. Files
  are named ``preview-<key>.<ext>``; the key identifies the exact render inputs
  (see :func:`render_fingerprint`), so a cached file is reused only for a
  request that would produce the same media. Eviction deletes the least recently
  used entries until both the byte budget and the entry-count budget hold.
* Nothing outside that directory is ever deleted, and even inside it only files
  matching the cache's own naming scheme (including the render layer's staging
  files, which a cancelled process can leave behind) are removed. A user's export
  sitting in the same directory is untouched.
* The frame cache is bounded by total bytes of decoded picture, not by a frame
  count alone, because one 4K frame can outweigh a hundred preview frames.

Nothing here knows about Qt: the viewer drives it through ordinary calls.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ..core.model import Project
from .graph import CodecPreset

F = Fraction

#: Default disk budget for cached preview ranges.
DEFAULT_DISK_BUDGET_BYTES = 1024 * 1024 * 1024
#: How many ranges are kept regardless of size (each is seekable independently).
DEFAULT_MAX_ENTRIES = 12
#: Default RAM budget for decoded frames used by still-frame inspection.
DEFAULT_FRAME_BUDGET_BYTES = 64 * 1024 * 1024

ENTRY_PREFIX = "preview-"
_STAGED_INFIX = ".partial."
_CHUNK_INFIX = ".chunk"
_ENTRY_NAME = re.compile(rf"^{re.escape(ENTRY_PREFIX)}[0-9a-f]{{16,64}}\.[A-Za-z0-9]+$")

#: What the renderer guarantees about the media it produces. Cached media is only
#: valid for the renderer that made it, so this goes into every fingerprint and
#: must be raised whenever the *meaning* of a project changes — for example when
#: fade anchors were fixed to follow output time (v2), or when audio slices were
#: redefined to bound the source read on the stream's own axis and fades became
#: sample-accurate instead of per-frame (v3). Version 4 covers the picture change:
#: every output-frame boundary now snaps forward to the frame that covers it (one
#: rule for naming a frame and counting a length), and a picture item's frames are
#: sampled from a grid the plan places — anchored on an exactly representable
#: container timestamp — instead of wherever the decoder's seek happened to land.
#: Previews written under v3 pick different pictures at retime boundaries, so they
#: must not be served as if they were current. Version 5 is the display-geometry
#: fix: a picture is now scaled to the largest even square-pixel size its **display**
#: aspect ratio fits into, instead of to a coded size that ``setsar=1`` then flattens.
#: Previews rendered under v4 show anamorphic material inset by black bars (measured:
#: 720x576 SAR 64:45 lit only columns 190..1089 of a 1280x720 frame) and stretched in
#: the other cases where the coded and display ratios differ, so they must not be
#: served for a v5 composition. Version 6 separates the two questions the output lattice
#: answers: quantising a **render range** still emits ``ceil(a·R) … ceil(b·R)-1``, but
#: **inspecting a playhead** at ``t`` now names ``floor(t·R)`` — the frame already
#: covering it — and the still is planned as that frame's own lattice interval
#: ``[i/R, (i+1)/R)`` instead of the range ``[t, t+1/R)``. The windows the preview path
#: chooses did not move (measured: ``preview_range`` output is byte-identical over 896
#: playhead/duration/rate probes), so this bump discards cached previews that would
#: still be valid media; it is raised because the render layer's contract about which
#: frame an output position presents changed, and every cached file is labelled by that
#: contract.
#:
#: 7 (TASK_12j): the picture chain samples on a fine filter tick with the grid's
#: phase taken off every timestamp, so a whole render and an independently sought
#: sub-render now choose the same pictures; previews rendered under "6" can hold
#: pictures that a range render of the same project would not write.
#:
#: 8 (TASK_12n): an item's exact filter tick may now be proved from the picture
#: timestamps the reading window really holds, so a stream whose container counts time
#: more finely than its tempo divides on (measured: MP4 with
#: ``-video_track_timescale 50000000`` at tempo 1000/1001) samples on its own exact
#: grid instead of the container-quantised fallback. That fallback shifts every stamp
#: by up to a whole container tick, which decides a tie - a sampling time equal to a
#: picture's own stamp - in favour of the neighbouring picture, so a preview cached
#: under "7" for such a stream can be showing the picture either side of the one this
#: composition names.
SEMANTICS_VERSION = "8"


# ------------------------------------------------------------------ identity


def render_fingerprint(
    project: Project,
    asset_paths: dict[str, Path],
    out_start: Fraction,
    out_end: Fraction,
    geometry_scale: Fraction,
    preset: CodecPreset,
) -> str:
    """Stable key for "which media would this request produce?".

    Everything that can change the produced media is included, and nothing that
    cannot: output settings, every track's enabled/muted state and stacking
    order, every registered stream's bounds and addressing, every clip's source
    range, placement, rate, gain, fades and link group, the requested range, the
    picture scale, and the encoder preset. Per-asset identity is the resolved
    path plus its size and modification time, so re-importing a replaced file
    produces a different key rather than serving a stale preview.

    View state (playhead, zoom, selection) is absent by design: it is not part of
    the project and must never invalidate a render.
    """
    out = project.output
    parts: list[str] = [
        f"semantics={SEMANTICS_VERSION}",
        f"out={out.width}x{out.height}@{out.frame_rate.numerator}/{out.frame_rate.denominator}"
        f"/{out.sample_rate}Hz/{out.channel_layout}",
        f"range={_frac(out_start)},{_frac(out_end)}",
        f"scale={_frac(geometry_scale)}",
        f"preset={preset.name}|{preset.extension}|{preset.video_codec}|{preset.audio_codec}"
        f"|{preset.pixel_format}|{preset.muxer}|{','.join(preset.extra_video_args)}"
        f"|{','.join(preset.extra_audio_args)}",
        "tracks=" + ";".join(
            f"{t.id}:{t.kind.value}:{'e' if t.enabled else 'E'}"
            f"{':m' if t.muted else ':M'}"
            for t in project.tracks
        ),
        "sources=" + ";".join(
            f"{s.asset_id}#{s.stream_index}:{s.kind.value}:origin={_frac(s.origin)}"
            f":end={_frac(s.end) if s.end is not None else '?'}"
            f"{'~' if s.conservative_end else ''}"
            f":fps={_frac(s.frame_rate) if s.frame_rate is not None else '?'}"
            f":sr={s.sample_rate if s.sample_rate is not None else '?'}"
            f":{s.width}x{s.height}/{s.channel_layout or '?'}"
            f":pos={s.stream_position if s.stream_position is not None else '?'}"
            f":in={_frac(s.input_start) if s.input_start is not None else '?'}"
            for s in sorted(project.sources, key=lambda s: (s.asset_id, s.stream_index))
        ),
        "clips=" + ";".join(
            f"{c.id}@{c.track_id}:{c.asset_id}#{c.stream_index}"
            f":src={_frac(c.source_in)},{_frac(c.source_out)}"
            f":out={_frac(c.output_start)},{_frac(c.output_end)}"
            f":rate={_frac(c.rate)}:gain={_frac(c.gain)}"
            f":fade={_frac(c.fade_in)},{_frac(c.fade_out)}"
            f":link={c.link_group or '-'}"
            for c in sorted(project.clips, key=lambda c: c.id)
        ),
    ]
    used = {clip.asset_id for clip in project.clips}
    present = sorted(a for a in used if a in asset_paths)
    parts.append("assets=" + ";".join(_asset_identity(asset_paths[a]) for a in present))
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:24]


def _frac(value: Fraction | int) -> str:
    value = Fraction(value)
    return f"{value.numerator}/{value.denominator}"


def _asset_identity(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:  # missing/unreadable: a distinct identity, never a reused cache
        return f"{path}!<unavailable>"
    return f"{path}|{stat.st_size}|{stat.st_mtime_ns}"


# ------------------------------------------------------------------ disk cache


@dataclass(frozen=True)
class CacheEntry:
    key: str
    path: Path
    size: int


class PreviewCache:
    """LRU-bounded directory of rendered preview ranges owned by this cache."""

    def __init__(
        self,
        root: Path,
        max_bytes: int = DEFAULT_DISK_BUDGET_BYTES,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        if max_bytes <= 0 or max_entries <= 0:
            raise ValueError("cache budgets must be positive")
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._adopt_existing()

    # ---------------------------------------------------------------- lookups
    def lookup(self, key: str) -> Path | None:
        """Return a usable cached file for *key*, or None (and adopt a stray one)."""
        entry = self._entries.get(key)
        if entry is not None and entry.path.is_file():
            self._entries.move_to_end(key)
            return entry.path
        if entry is not None:  # disappeared underneath us
            self._entries.pop(key, None)
        candidate = self.path_for(key)
        if candidate.is_file():
            self.commit(candidate, key)
            return candidate
        return None

    def path_for(self, key: str) -> Path:
        """Where a render for *key* should be written (parent directory exists)."""
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / f"{ENTRY_PREFIX}{key}.mp4"

    def commit(self, path: Path, key: str | None = None) -> CacheEntry:
        """Register a finished file and evict until the budgets hold again."""
        path = Path(path)
        if not self.owns(path):
            raise ValueError(f"{path} is not a cache-owned file")
        key = key or _key_of(path)
        entry = CacheEntry(key=key, path=path, size=path.stat().st_size)
        self._entries.pop(key, None)
        self._entries[key] = entry
        self.evict_to_budget()
        return entry

    def owns(self, path: Path) -> bool:
        """True for files this cache created (and may therefore delete)."""
        try:
            parent = Path(path).resolve().parent
        except OSError:
            return False
        return parent == self.root.resolve() and _name_is_ours(Path(path).name)

    def discard(self, path: Path) -> bool:
        """Delete one owned file (a failed or stale render). Never touches others."""
        path = Path(path)
        if not self.owns(path):
            return False
        self._entries.pop(_key_of(path), None)
        with contextlib.suppress(FileNotFoundError):  # already gone
            path.unlink()
        return True
        return True

    # ----------------------------------------------------------------- budgets
    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self._entries.values())

    def entries(self) -> tuple[CacheEntry, ...]:
        """Oldest-touched first."""
        return tuple(self._entries.values())

    def evict_to_budget(self) -> list[Path]:
        """Drop least-recently-used entries until both budgets hold."""
        removed: list[Path] = []
        while self._entries and (
            len(self._entries) > self.max_entries or self.total_bytes > self.max_bytes
        ):
            _, entry = next(iter(self._entries.items()))
            self._entries.pop(entry.key, None)
            with contextlib.suppress(FileNotFoundError):  # external removal
                entry.path.unlink()
            removed.append(entry.path)
        return removed

    def clear(self) -> list[Path]:
        """Delete every owned file (shutdown, or an explicit "free the cache")."""
        removed = [entry.path for entry in self._entries.values()]
        self._entries.clear()
        for path in list(self.root.glob(f"{ENTRY_PREFIX}*")):
            if _name_is_ours(path.name) and path.is_file():
                removed.append(path)
                with contextlib.suppress(FileNotFoundError):  # external removal
                    path.unlink()
        return removed

    def prune_orphans(self) -> list[Path]:
        """Delete leftovers (staging/chunk files) from interrupted renders."""
        removed: list[Path] = []
        for path in _leftovers(self.root):
            with contextlib.suppress(FileNotFoundError):  # external removal
                path.unlink()
            removed.append(path)
        return removed

    def close(self) -> list[Path]:
        """Release everything this cache owns. The root directory is left in place."""
        return self.clear()

    # ------------------------------------------------------------------- init
    def _adopt_existing(self) -> None:
        """Adopt files from a previous run so the budgets apply to them too."""
        if not self.root.is_dir():
            return
        for path in sorted(self.root.iterdir(), key=lambda p: p.stat().st_atime_ns):
            if path.is_file() and path.name.startswith(ENTRY_PREFIX):
                name = path.name
                key = _key_of(path)
                if _ENTRY_NAME.match(name):
                    self._entries[key] = CacheEntry(key=key, path=path, size=path.stat().st_size)
        self.evict_to_budget()


def _key_of(path: Path) -> str:
    return path.name[len(ENTRY_PREFIX):].rsplit(".", 1)[0]


def _name_is_ours(name: str) -> bool:
    if name.startswith(ENTRY_PREFIX):
        return True
    return _STAGED_INFIX in name or _CHUNK_INFIX in name


def _leftovers(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return ()
    return (
        path
        for path in root.iterdir()
        if path.is_file()
        and (_STAGED_INFIX in path.name or _CHUNK_INFIX in path.name)
    )


# ------------------------------------------------------------------ frame cache


@dataclass(frozen=True)
class CachedFrame:
    """One decoded picture, plus the caller's own object for it.

    The cache layer stays media-agnostic: ``rgb`` is what bounds memory, and
    ``payload`` is whatever the caller wants back (the UI puts its ``StillFrame``
    there, PTS and all), so no render-layer module needs to know about it.
    """

    key: tuple[object, ...]
    width: int
    height: int
    rgb: bytes
    payload: object = None

    @property
    def nbytes(self) -> int:
        return len(self.rgb)


#: Hard count cap as well as a byte cap: many tiny frames still cost lookups.
MAX_CACHED_FRAMES = 512


class FrameCache:
    """LRU cache of decoded still frames, bounded by total picture bytes."""

    def __init__(self, max_bytes: int = DEFAULT_FRAME_BUDGET_BYTES) -> None:
        if max_bytes <= 0:
            raise ValueError("frame budget must be positive")
        self.max_bytes = max_bytes
        self._frames: OrderedDict[tuple[object, ...], CachedFrame] = OrderedDict()
        self._bytes = 0

    def get(self, key: tuple[object, ...]) -> CachedFrame | None:
        frame = self._frames.get(key)
        if frame is not None:
            self._frames.move_to_end(key)
        return frame

    def put(self, frame: CachedFrame) -> int:
        """Store *frame* and return how many frames had to be dropped."""
        existing = self._frames.pop(frame.key, None)
        if existing is not None:
            self._bytes -= existing.nbytes
        self._frames[frame.key] = frame
        self._bytes += frame.nbytes
        dropped = 0
        while self._frames and (
            self._bytes > self.max_bytes or len(self._frames) > MAX_CACHED_FRAMES
        ):
            _, oldest = next(iter(self._frames.items()))
            self._frames.pop(oldest.key, None)
            self._bytes -= oldest.nbytes
            dropped += 1
        return dropped

    def forget(self, prefix: tuple[object, ...]) -> int:
        """Drop every frame whose key starts with *prefix* (e.g. one preview file)."""
        doomed = [k for k in self._frames if k[: len(prefix)] == prefix]
        for key in doomed:
            self._bytes -= self._frames.pop(key).nbytes
        return len(doomed)

    def clear(self) -> int:
        """Drop everything (the pictures no longer match the composition)."""
        count = len(self._frames)
        self._frames.clear()
        self._bytes = 0
        return count

    @property
    def total_bytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._frames)


#: A hard count cap as well as a byte cap: many tiny frames still cost lookups.
_MAX_FRAMES = MAX_CACHED_FRAMES  # kept as a private alias for readability

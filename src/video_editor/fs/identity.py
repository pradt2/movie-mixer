"""Is the file behind an asset still the file this project was built from?

One policy, used everywhere media is about to be read: at load, before a
render, before a still frame, before thumbnails or waveforms, and after any
relink. A path that exists is not enough — the case this exists for is a
*different encode written to the same name*, which would otherwise be decoded
with the old stream indexes, bounds and frame rates.

Statuses are explicit, because the user has to do different things about them:

``ONLINE``
    the file is readable and still matches what was recorded;
``MISSING``
    nothing is at that path (moved, renamed, unmounted volume, deleted);
``CHANGED``
    a file is there, but it is not the one recorded — the composition's source
    bounds and stream indexes may no longer describe it;
``UNREADABLE``
    something is at that path but cannot be used: permissions, a directory, a
    socket, or an I/O error while inspecting it.

Identity is **size + mtime_ns**, the same cheap policy the project file has
always recorded. It is not a content hash and no cryptographic claim is made:
a file rewritten with an identical size *and* mtime is indistinguishable, and
that limitation is stated rather than hidden. What the check does catch is the
realistic accident — a re-encode, a replacement, a truncated copy.

Nothing here touches the network, and nothing reads media contents: one
``stat()`` per asset.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from stat import S_ISREG


class AssetStatus(Enum):
    """Whether an asset's file can be used, and why not if it cannot."""

    ONLINE = "online"
    MISSING = "missing"
    #: the file at the recorded path is not the file that was recorded
    CHANGED = "changed"
    #: something is there, but it cannot be inspected or read
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Identity:
    """A cheap, comparable fingerprint of a filesystem entry."""

    size_bytes: int
    mtime_ns: int

    @classmethod
    def of(cls, path: Path) -> Identity:
        """Identity of *path*; raises ``OSError`` if it cannot be inspected."""
        stat = Path(path).stat()
        return cls(size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)

    def __str__(self) -> str:
        return f"{self.size_bytes} bytes, mtime_ns {self.mtime_ns}"


@dataclass(frozen=True)
class Availability:
    """Result of checking one path against one recorded identity."""

    path: Path
    status: AssetStatus
    #: what is on disk now (None when there is nothing to stat)
    found: Identity | None = None
    #: what was recorded (None when the record cannot prove anything)
    recorded: Identity | None = None
    #: actionable explanation, shown to the user; None when online
    note: str | None = None

    @property
    def usable(self) -> bool:
        return self.status is AssetStatus.ONLINE

    def describe(self) -> str:
        """Short label for status bars and render errors."""
        if self.usable:
            return f"available ({self.path})"
        return self.note or f"{self.status.value}: {self.path}"


def check(
    path: Path, recorded: Identity | None = None, label: str | None = None
) -> Availability:
    """Stat *path* and compare it with *recorded*.

    A record with no identity (an asset imported before identity was captured,
    or a hand-written project file) cannot prove anything, so it is reported
    ``ONLINE`` rather than inventing a mismatch — the same convention project
    loading has always used.
    """
    path = Path(path)
    who = f"{label} ({path})" if label else str(path)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return Availability(
            path=path,
            status=AssetStatus.MISSING,
            recorded=recorded,
            note=(
                f"{who} is not there: it was moved, renamed or is on a volume that "
                "is not mounted"
            ),
        )
    except OSError as exc:
        return Availability(
            path=path,
            status=AssetStatus.UNREADABLE,
            recorded=recorded,
            note=f"{who} cannot be inspected: {exc.strerror or exc}",
        )
    if not S_ISREG(stat.st_mode):
        return Availability(
            path=path,
            status=AssetStatus.UNREADABLE,
            recorded=recorded,
            note=f"{who} is not a regular file (it is a directory or a special file)",
        )
    found = Identity(size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
    if recorded is not None and (
        found.size_bytes != recorded.size_bytes or found.mtime_ns != recorded.mtime_ns
    ):
        return Availability(
            path=path,
            status=AssetStatus.CHANGED,
            found=found,
            recorded=recorded,
            note=(
                f"{who} is not the file recorded here: it was {recorded}, it is now "
                f"{found}. This may be a different encode of the same film, so the "
                "recorded stream bounds and frame rate cannot be trusted; relink or "
                "reimport it."
            ),
        )
    return Availability(path=path, status=AssetStatus.ONLINE, found=found, recorded=recorded)


def refusal(
    report: dict[str, Availability], asset_id: str, action: str = "use this source"
) -> str:
    """A refusal sentence naming the file and what to do about it.

    Keyed by whatever the caller identifies sources with (an asset id); this
    layer knows nothing about assets.
    """
    entry = report.get(asset_id)
    if entry is None:
        return (
            f"cannot {action}: no source {asset_id!r} has a file recorded for it. "
            "Relink or reimport it."
        )
    if entry.status in (AssetStatus.CHANGED, AssetStatus.MISSING):
        return f"cannot {action}: {entry.describe()} Relink this asset to continue."
    return f"cannot {action}: {entry.describe()}"

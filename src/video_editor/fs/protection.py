"""Never write onto media, and never clobber a file the user did not approve.

Two separate guarantees, both enforced outside Qt so every caller gets them —
export, preview, project save, and any future writer:

**Source protection.** A destination that *is* one of this project's media files
is refused outright, whatever the user clicked. Confirmation is for replacing an
output; it is not consent to destroy a source. Comparison is by resolved path
**and** by device/inode, so a relative alias, a symlink to the film, and a hard
link to the same inode are all caught, not just an identical spelling.

**Publication safety.** A render is built in a temporary sibling file and then
*published*:

* if nothing is at the destination, publication is a same-filesystem hard link
  that fails with ``EEXIST`` rather than overwriting — so a file created while
  the render was running survives untouched;
* if the user approved replacing a file, the approval is tied to the bytes that
  existed when they were asked (``Consent.capture``). Immediately before
  publishing, the destination is inspected again: if it appeared, disappeared
  or changed since that moment, publication is refused;
* the previous file is never deleted before a successful publish — ``os.link``
  or ``os.replace`` are atomic renames on the same filesystem.

Residual limits, stated rather than papered over: these checks close the window
that matters (preflight → render → publish) on a local filesystem, and a rename
is atomic, but no filesystem offers an atomic "check then create" across *all*
operations on every mount. On a network or removable filesystem another client
can still race us; what is guaranteed is that this editor never knowingly
overwrites a source or a file the user did not approve, and that a failure
leaves the existing file exactly as it was.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG

from .identity import Identity


class DestinationSafetyError(Exception):
    """A write was refused before anything was changed."""


class DestinationProtectedError(DestinationSafetyError):
    """The destination is one of the media files this project reads."""


class DestinationExistsError(DestinationSafetyError):
    """A file appeared at the destination without authorised replacement."""


class DestinationReplacedError(DestinationSafetyError):
    """The file approved for replacement is no longer the file that is there."""


class PublicationError(DestinationSafetyError):
    """Publishing the finished file failed for a filesystem reason."""


# --------------------------------------------------------------- protection


@dataclass(frozen=True)
class ProtectedFile:
    """One path that must never be a write destination, and who it belongs to."""

    path: Path
    description: str

    @classmethod
    def of(cls, path: Path, description: str = "a file this project reads") -> ProtectedFile:
        return cls(path=Path(path), description=description)

    @classmethod
    def media(cls, path: Path) -> ProtectedFile:
        """A media file by name — the usual reason a path is protected."""
        path = Path(path)
        return cls(path=path, description=f"imported media {path.name}")


def aliases(path: Path) -> set[object]:
    """Every way this path can be recognised, for comparison with another path.

    ``("inode", dev, ino)`` catches hard links and symlinks pointing at the same
    file; the resolved and the normalised absolute spellings catch a destination
    that does not exist yet (so it has no inode to compare) but would land on a
    protected file.
    """
    path = Path(path)
    found: set[object] = set()
    try:
        stat = path.stat()
        if S_ISREG(stat.st_mode):
            found.add(("inode", stat.st_dev, stat.st_ino))
    except OSError:
        pass
    for form in (os.path.realpath(path), os.path.abspath(path)):
        found.add(("path", os.path.normcase(os.path.normpath(form))))
    return found


class DestinationGuard:
    """The set of paths a write must never land on."""

    def __init__(self, protected: Iterable[ProtectedFile] = ()) -> None:
        self._entries: tuple[tuple[ProtectedFile, set[object]], ...] = tuple(
            (item, aliases(item.path)) for item in protected
        )

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def extended(self, extra: Iterable[ProtectedFile]) -> DestinationGuard:
        """A new guard with more paths protected (the old one is unchanged)."""
        return DestinationGuard([item for item, _ in self._entries] + list(extra))

    def collision(self, candidate: Path) -> ProtectedFile | None:
        """The protected file *candidate* would overwrite, if any."""
        candidate_aliases = aliases(candidate)
        for item, protected_aliases in self._entries:
            if candidate_aliases & protected_aliases:
                return item
        return None

    def check(self, candidate: Path, action: str) -> None:
        """Refuse *candidate* when it is protected media; *action* names the write."""
        hit = self.collision(candidate)
        if hit is None:
            return
        same = _alias_note(candidate, hit.path)
        raise DestinationProtectedError(
            f"Refusing to {action} onto {candidate}: that is the same file as "
            f"{hit.description} ({hit.path}){same}. Confirming that an output may be "
            "replaced never authorises destroying source media — choose another "
            "destination name."
        )


def _alias_note(candidate: Path, protected: Path) -> str:
    if os.path.normcase(os.path.abspath(candidate)) == os.path.normcase(
        os.path.abspath(protected)
    ):
        return ""
    try:
        if candidate.stat().st_ino == protected.stat().st_ino:
            return " (the same file by a different name or link)"
    except OSError:  # pragma: no cover - the candidate usually does not exist
        pass
    return " (the same file after resolving links)"


# --------------------------------------------------------------- publication


@dataclass(frozen=True)
class Consent:
    """What the user was shown, and the file that stood there at that moment.

    Captured *after* the user agreed to replace an existing output. Publication
    compares the destination against this, so approval cannot be reused against
    a file that appeared or changed while the render was running.
    """

    path: Path
    existed: bool
    identity: Identity | None = None
    #: the destination belongs to this application (a cache file): replacing
    #: whatever is there is authorised by ownership, not by a user prompt
    owner_authorised: bool = False

    @classmethod
    def capture(cls, path: Path) -> Consent:
        path = Path(path)
        try:
            return cls(path=path, existed=True, identity=Identity.of(path))
        except FileNotFoundError:
            return cls(path=path, existed=False)
        except OSError as exc:
            raise DestinationSafetyError(
                f"cannot inspect {path} before writing to it: {exc.strerror or exc}"
            ) from exc

    @classmethod
    def as_owner(cls, path: Path) -> Consent:
        return cls(path=Path(path), existed=False, owner_authorised=True)


def publish(
    staging: Path,
    destination: Path,
    *,
    guard: DestinationGuard | None = None,
    consent: Consent | None = None,
    action: str = "write the finished file",
) -> str | None:
    """Move *staging* onto *destination* safely. Returns a warning, or None.

    Raises :class:`DestinationSafetyError` without changing anything when the
    destination turns out to be protected media, or when the file that is there
    now is not the file that was approved.
    """
    staging, destination = Path(staging), Path(destination)
    if guard is not None:
        guard.check(destination, action)
    current = _inspect(destination)
    if current is not None:  # something is there now
        if consent is None:
            raise DestinationExistsError(
                f"{destination} already exists and was not approved for replacement; "
                "nothing was written. Your existing file is untouched."
            )
        if not consent.owner_authorised:
            if not consent.existed:
                raise DestinationExistsError(
                    f"{destination} appeared while this was running. Nothing was "
                    "written: the file you (or another program) created is untouched."
                )
            if consent.identity is not None and consent.identity != current:
                raise DestinationReplacedError(
                    f"{destination} is not the file that was approved for replacement: "
                    f"it was {consent.identity}, it is now {current}. Nothing was "
                    "written — re-run the operation against the file that is there now."
                )
        os.replace(staging, destination)
        return None
    # nothing is there: publish without clobbering whatever may appear in between
    try:
        os.link(staging, destination)
    except FileExistsError as exc:
        raise DestinationExistsError(
            f"{destination} appeared while this was running. Nothing was written; "
            "the file that is there now is untouched."
        ) from exc
    except OSError as exc:
        raise PublicationError(
            f"could not put the finished file at {destination}: {exc.strerror or exc}"
        ) from exc
    staging.unlink(missing_ok=True)
    if consent is not None and consent.existed and not consent.owner_authorised:
        return (
            f"{destination} had been replaced by another program during this run and "
            "was no longer present; the new file is this run's output."
        )
    return None


def _inspect(path: Path) -> Identity | None:
    """Identity of *path* if it is a regular file, else None."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DestinationSafetyError(
            f"cannot inspect {path} before publishing: {exc.strerror or exc}"
        ) from exc
    if not S_ISREG(stat.st_mode):
        raise DestinationSafetyError(
            f"{path} is not a regular file; nothing was written to it"
        )
    return Identity(size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)

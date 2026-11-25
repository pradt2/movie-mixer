"""Detection and version reporting for external media tools.

The application must stay usable when ``ffmpeg``/``ffprobe`` are missing;
every consumer of these executables goes through this module so that the
same clear diagnostic is produced everywhere.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolStatus:
    """Availability and version of one external executable."""

    name: str
    path: str | None
    version: str | None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.path is not None and self.error is None


@dataclass(frozen=True)
class MediaToolStatus:
    """Combined status of the FFmpeg backend."""

    ffmpeg: ToolStatus
    ffprobe: ToolStatus

    @property
    def available(self) -> bool:
        return self.ffmpeg.available and self.ffprobe.available

    def diagnostic(self) -> str:
        """One-line human readable message suitable for a status bar."""
        if self.available:
            return f"FFmpeg backend: ffmpeg {self.ffmpeg.version}, ffprobe {self.ffprobe.version}"
        problems = []
        for tool in (self.ffmpeg, self.ffprobe):
            if tool.path is None:
                problems.append(f"{tool.name} executable not found")
            elif tool.error:
                problems.append(f"{tool.name}: {tool.error}")
        return (
            "Media backend unavailable — "
            + "; ".join(problems)
            + ". Install ffmpeg (e.g. 'sudo dnf install ffmpeg' or 'sudo apt install ffmpeg')."
        )


_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


def _probe_tool(name: str) -> ToolStatus:
    path = shutil.which(name)
    if path is None:
        return ToolStatus(name=name, path=None, version=None, error=f"{name} not found on PATH")
    try:
        proc = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolStatus(name=name, path=path, version=None, error=f"could not run: {exc}")
    if proc.returncode != 0:
        return ToolStatus(
            name=name,
            path=path,
            version=None,
            error=f"exited with status {proc.returncode}",
        )
    first_line = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
    match = _VERSION_RE.search(first_line)
    version = match.group(1) if match else "unknown"
    return ToolStatus(name=name, path=path, version=version)


def check_media_tools() -> MediaToolStatus:
    """Probe ffmpeg and ffprobe. Never raises; callers use ``diagnostic()``."""
    return MediaToolStatus(ffmpeg=_probe_tool("ffmpeg"), ffprobe=_probe_tool("ffprobe"))

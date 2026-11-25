"""Bounded FFmpeg-generated previews for a single stream.

Qt Multimedia plays whole files and chooses elementary streams itself. When
the user selects a specific stream that the playback backend cannot address
directly (e.g. a second audio track), we extract a short window of exactly
that stream instead of silently playing something else.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

MAX_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class StreamPreview:
    path: Path
    #: asset-local time at which the extracted window starts
    window_start: Fraction
    duration: Fraction
    owned: bool = True

    def covers(self, asset_local_time: Fraction) -> bool:
        return self.window_start <= asset_local_time < self.window_start + self.duration


def extract_stream_window(
    asset_path: Path,
    stream_index: int,
    *,
    kind: str,  # "video" | "audio"
    start: Fraction,
    duration: Fraction,
    out_dir: Path | None = None,
    timeout: float = 120,
) -> StreamPreview | None:
    """Extract ``[start, start+duration)`` (asset-local) of one stream.

    Returns None when ffmpeg cannot produce the preview; callers must then
    report the limitation instead of substituting different media.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    duration = min(duration, Fraction(MAX_WINDOW_SECONDS))
    if duration <= 0:
        return None
    directory = out_dir or Path(tempfile.mkdtemp(prefix="ve_preview_"))
    if kind == "audio":
        out_path = directory / f"stream_{stream_index}_preview.mka"
        codec_args = ["-vn", "-c:a", "pcm_s16le"]
    else:
        out_path = directory / f"stream_{stream_index}_preview.mp4"
        codec_args = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    start_f = float(start)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_f:.6f}",
        "-i",
        str(asset_path),
        "-t",
        f"{float(duration):.6f}",
        "-map",
        f"0:{stream_index}",
        *codec_args,
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not out_path.is_file():
        return None
    return StreamPreview(
        path=out_path, window_start=start, duration=duration, owned=True
    )

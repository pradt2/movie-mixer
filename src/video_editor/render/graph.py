"""Translate a :class:`~video_editor.render.plan.RenderPlan` into an FFmpeg job.

This module only *builds* the command line and the filter graph; executing it
(progress, cancellation, cleanup) is the exporter's job. Arguments are always
passed as lists, never through a shell.

How the plan maps onto FFmpeg
-----------------------------
* One input per rendered item slice, opened with ``-ss <seconds of material>``
  **before** ``-i``: FFmpeg then does an accurate seek (decoding from the
  preceding keyframe and discarding) and rebases timestamps to 0. That seek is
  measured from the **file's own start time**, not from 0, so the plan's
  sources record the container start alongside the asset-local timestamps and
  the seek is ``source_start - container_start``. The wanted
  elementary stream is addressed as ``[in:v:N]`` / ``[in:a:N]``: FFmpeg labels
  filtergraph inputs by *stream type and position within that type*, which is
  not the same number as the container stream index kept in the domain model.
  The plan's sources therefore carry both, and an unrecorded position is a
  hard error rather than a guess.
* Every item is forced to an **exact output-frame / output-sample count**
  (``trim=end_frame=N``; ``apad`` + ``atrim=end_sample=N``). Those counts come
  from the plan's global indices, so joining items cannot accumulate rounding
  error and splitting a render into chunks cannot move a boundary.
* Picture per item: ``setpts`` retime (``rate`` is source seconds per output
  second, so PTS are divided by it) → ``fps`` to the output rate → scale with
  aspect ratio preserved and padded → ``setsar`` → ``format``. Black areas use
  the ``color`` source filter.
* Audio per contribution: pitch-preserving tempo by chained ``atempo`` stages,
  pad/trim to the exact sample count, positioned at its exact global sample
  offset by concatenating leading silence, then gain + fade envelope via
  ``volume=eval=frame`` (piecewise linear in output time, matching
  ``core.model.envelope_gain``). The envelope must come after the positioning:
  its expression is written in time relative to the start of the rendered range.
  Contributions are summed with
  ``amix=normalize=0`` — a plain sum, never per-clip normalisation.
* Picture and audio are assembled as **two independent timelines** (picture by
  ``concat``, audio by globally anchored summation). Doing both inside one
  ``concat`` would let per-segment rounding of one stream drag the other.
* VFR input is normalised to the output frame rate by ``fps`` (nearest-frame
  sampling); timestamps are rebased per item. This is documented behaviour, not
  a claim of frame-exact VFR reproduction.
* Colour: geometry and pixel format are normalised, but no colour
  space/transfer-characteristic conversion is performed and no HDR handling is
  claimed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from ..core.model import StreamSource, TrackKind
from .plan import AudioItem, RenderPlan, VideoItem

F = Fraction


class RenderConfigError(Exception):
    """The plan cannot be expressed as a correct FFmpeg job."""


#: atempo accepts a limited factor range per stage; other rates are chained.
ATEMPO_MIN = F(1, 2)
ATEMPO_MAX = F(2)

#: Channel layouts we are willing to claim support for. Anything else is
#: refused rather than silently down-mixed into a different layout.
SUPPORTED_LAYOUTS = frozenset(
    {"mono", "stereo", "2.1", "3.0", "5.0", "5.1", "7.0", "7.1"}
)


def normalise_layout(layout: str) -> str:
    """Map ffprobe spellings onto FFmpeg layout names, or refuse."""
    if not layout:
        return "stereo"
    candidate = layout.replace("(side)", "")
    if candidate in SUPPORTED_LAYOUTS:
        return candidate
    raise RenderConfigError(
        f"Channel layout {layout!r} is not supported for export; supported: "
        + ", ".join(sorted(SUPPORTED_LAYOUTS))
    )


@dataclass(frozen=True)
class CodecPreset:
    """Output container + codecs."""

    name: str
    extension: str
    video_codec: str
    audio_codec: str
    pixel_format: str = "yuv420p"
    muxer: str = "mp4"
    extra_video_args: tuple[str, ...] = ()
    extra_audio_args: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        return f"{self.name} ({self.video_codec} + {self.audio_codec})"


#: Initial (and only claimed) delivery preset.
MP4_H264_AAC = CodecPreset(
    name="MP4 H.264/AAC",
    extension="mp4",
    video_codec="libx264",
    audio_codec="aac",
    extra_video_args=("-preset", "veryfast", "-crf", "20"),
    extra_audio_args=("-b:a", "192k"),
)

#: Preview variant of the delivery preset: same container, codecs and
#: composition semantics, but a coarser picture and a short GOP so a preview
#: renders quickly and seeking inside the file lands near any time. Quality is a
#: presentation choice; it never changes what is rendered (the planner keeps the
#: same cuts, stacking, gaps and mix, only the picture size differs).
PREVIEW_H264_FAST = CodecPreset(
    name="Preview MP4 H.264/AAC",
    extension="mp4",
    video_codec="libx264",
    audio_codec="aac",
    extra_video_args=("-preset", "veryfast", "-crf", "30", "-g", "15"),
    extra_audio_args=("-b:a", "96k"),
)

#: Intermediate used when a plan is rendered in chunks: PCM/FFV1 have no
#: encoder delay or tail padding, so chunk joins stay sample/frame exact while
#: the lossy encoders are initialised exactly once, at the final encode.
LOSSLESS_INTERMEDIATE = CodecPreset(
    name="Lossless intermediate",
    extension="mkv",
    video_codec="ffv1",
    audio_codec="pcm_s16le",
    pixel_format="yuv420p",
    muxer="matroska",
)


@dataclass
class RenderJob:
    """A runnable FFmpeg invocation plus what it represents."""

    args: list[str]
    duration: Fraction
    video_items: int
    audio_items: int
    warnings: tuple[str, ...] = ()
    temp_files: list[Path] = field(default_factory=list)


def decimal(value: Fraction, digits: int = 9) -> str:
    """Fixed-point text for the few FFmpeg options that cannot take rationals.

    Error is below one nanosecond for the values used here; everywhere exactness
    matters an integer frame/sample count is used instead.
    """
    return f"{float(value):.{digits}f}"


def rational(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def atempo_chain(rate: Fraction) -> list[str]:
    """Pitch-preserving tempo as chained atempo stages within [1/2, 2]."""
    if rate <= 0:
        raise RenderConfigError("Clip rate must be positive")
    factors: list[Fraction] = []
    remaining = rate
    while remaining > ATEMPO_MAX:
        factors.append(ATEMPO_MAX)
        remaining /= ATEMPO_MAX
    while remaining < ATEMPO_MIN:
        factors.append(ATEMPO_MIN)
        remaining /= ATEMPO_MIN
    if remaining != 1:
        factors.append(remaining)
    return [f"atempo={decimal(f)}" for f in factors]


def encoder_available(ffmpeg: str, codec: str) -> bool:
    """True when this FFmpeg build can encode with ``codec``."""
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) > 1 and fields[1] == codec:
            return True
    return False


class GraphBuilder:
    """Builds one FFmpeg job for one plan."""

    def __init__(
        self,
        plan: RenderPlan,
        asset_paths: dict[str, Path],
        sources: dict[tuple[str, int], StreamSource],
        ffmpeg: str,
    ) -> None:
        self.plan = plan
        self.asset_paths = asset_paths
        self.sources = sources
        self.ffmpeg = ffmpeg
        self.layout = normalise_layout(plan.channel_layout)
        self.inputs: list[list[str]] = []
        self._input_ids: dict[tuple[str, int, Fraction], int] = {}
        self.filter_parts: list[str] = []
        self._counter = 0

    # ---------------------------------------------------------------- shared
    def _label(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _stream_label(self, asset_id: str, stream_index: int, seek: Fraction) -> str:
        """One input per item slice, addressed exactly.

        Two facts about FFmpeg 8 input handling drive this, both measured (see
        ``tests/test_render_ffmpeg_features.py``):

        * input ``-ss`` is measured from the **presentation start** of the file
          (``format.start_time``), not from 0, so the container start recorded in
          the source is subtracted. Seeking a file whose timeline begins at 2.5 s
          to ``-ss 0`` yields its first frame.
        * input ``-ss`` is frame accurate: it seeks to the preceding keyframe and
          decodes-and-discards, so no extra frame skipping is needed (and none
          should be added, which would drop real frames).
        * filtergraph inputs are addressed as ``[in:v:N]`` / ``[in:a:N]``: type
          plus position within that type, which is *not* the container stream
          index kept in the domain model. An unrecorded position is refused
          rather than guessed.
        """
        path = self.asset_paths.get(asset_id)
        if path is None:
            raise RenderConfigError(
                f"Asset {asset_id} is not available for rendering (renamed or moved? "
                "relink it before exporting)"
            )
        source = self._source(asset_id, stream_index)
        input_seek = max(seek - source.input_start, F(0))
        key = (asset_id, stream_index, input_seek)
        index = self._input_ids.get(key)
        if index is None:
            index = len(self.inputs)
            self._input_ids[key] = index
            self.inputs.append(["-ss", decimal(input_seek), "-i", str(path)])
        kind = "v" if source.kind is TrackKind.VIDEO else "a"
        return f"[{index}:{kind}:{source.stream_position}]"

    def _source(self, asset_id: str, stream_index: int) -> StreamSource:
        """The registered stream, with the addressing data rendering requires."""
        source = self.sources.get((asset_id, stream_index))
        if source is None:
            raise RenderConfigError(
                f"Stream {stream_index} of asset {asset_id} is not registered in the "
                "project; it cannot be addressed for rendering"
            )
        if source.stream_position is None or source.stream_position < 0:
            raise RenderConfigError(
                f"Stream {stream_index} of asset {asset_id} has no recorded stream "
                "position; reimport the asset so rendering can address it exactly"
            )
        return source

    def _geometry(self) -> str:
        return f"{self.plan.width}x{self.plan.height}"

    # --------------------------------------------------------------- picture
    def build_video(self) -> str:
        labels = [
            self._black_item(item) if item.is_black else self._picture_item(item)
            for item in self.plan.video_items
        ]
        if not labels:
            return self._black_item(VideoItem(start_frame=0, frames=self.plan.total_frames))
        if len(labels) == 1:
            return labels[0]
        out = self._label("v")
        chain = "".join(f"[{label}]" for label in labels)
        self.filter_parts.append(
            f"{chain}concat=n={len(labels)}:v=1:a=0,format=yuv420p[{out}]"
        )
        return out

    def _black_item(self, item: VideoItem) -> str:
        out = self._label("black")
        rate = self.plan.quantisation.frame_rate
        duration = F(item.frames) / rate
        self.filter_parts.append(
            f"color=c=black:s={self._geometry()}:r={rational(rate)}:d={decimal(duration)},"
            f"trim=start_frame=0:end_frame={item.frames},"
            f"setsar=1,format=yuv420p[{out}]"
        )
        return out

    def _picture_item(self, item: VideoItem) -> str:
        clip = item.clip
        assert clip is not None
        source = self.sources.get((clip.asset_id, clip.stream_index))
        if source is None:
            raise RenderConfigError(
                f"Stream {clip.stream_index} of asset {clip.asset_id} is unknown"
            )
        if source.kind.value != "video":
            raise RenderConfigError(
                f"Clip {clip.id} uses a {source.kind.value} stream as picture"
            )
        rate = self.plan.quantisation.frame_rate
        label_in = self._stream_label(clip.asset_id, clip.stream_index, item.source_start)
        chain = [
            "setpts=PTS-STARTPTS",
            f"setpts=(PTS-STARTPTS)/{decimal(clip.rate)}",
            f"fps={rational(rate)}",
            f"scale={self.plan.width}:{self.plan.height}:force_original_aspect_ratio=decrease",
            "setsar=1",
            f"pad={self.plan.width}:{self.plan.height}:(ow-iw)/2:(oh-ih)/2",
            "format=yuv420p",
            # force the exact frame count the plan promises for this item
            f"trim=start_frame=0:end_frame={item.frames}",
        ]
        out = self._label("v")
        self.filter_parts.append(f"{label_in}{','.join(chain)}[{out}]")
        return out

    # ----------------------------------------------------------------- audio
    def build_audio(self) -> str:
        total = self.plan.total_samples
        if total <= 0:
            total = 1  # containers want *some* audio track; one sample is silent
        if not self.plan.audio_items:
            return self._pure_silence(total)
        labels = [self._audio_item(item) for item in self.plan.audio_items]
        if len(labels) == 1:
            out = self._label("a")
            self.filter_parts.append(
                f"[{labels[0]}]atrim=start_sample=0:end_sample={total},"
                f"{self._aformat()}[{out}]"
            )
            return out
        out = self._label("a")
        chain = "".join(f"[{label}]" for label in labels)
        self.filter_parts.append(
            f"{chain}amix=inputs={len(labels)}:normalize=0:duration=longest,"
            f"atrim=start_sample=0:end_sample={total},{self._aformat()}[{out}]"
        )
        return out

    def _aformat(self) -> str:
        q = self.plan.quantisation
        return f"aformat=sample_rates={q.sample_rate}:channel_layouts={self.layout}"

    def _pure_silence(self, total: int) -> str:
        q = self.plan.quantisation
        out = self._label("silence")
        self.filter_parts.append(
            f"anullsrc=r={q.sample_rate}:cl={self.layout},"
            f"atrim=start_sample=0:end_sample={total},{self._aformat()}[{out}]"
        )
        return out

    def _audio_item(self, item: AudioItem) -> str:
        clip = item.clip
        source = self.sources.get((clip.asset_id, clip.stream_index))
        if source is None:
            raise RenderConfigError(
                f"Stream {clip.stream_index} of asset {clip.asset_id} is unknown"
            )
        if source.kind.value != "audio":
            raise RenderConfigError(
                f"Clip {clip.id} uses a {source.kind.value} stream as sound"
            )
        label_in = self._stream_label(clip.asset_id, clip.stream_index, item.source_start)
        chain = ["asetpts=PTS-STARTPTS", *atempo_chain(clip.rate)]
        # pad first, then cap: guarantees exactly ``item.samples`` even if the
        # source (or tempo) runs a few samples short
        chain.append("apad")
        chain.append(f"atrim=start_sample=0:end_sample={item.samples}")
        chain.append(self._aformat())
        sliced = self._label("as")
        self.filter_parts.append(f"{label_in}{','.join(chain)}[{sliced}]")
        if item.start_sample <= 0:
            placed = sliced
        else:
            q = self.plan.quantisation
            silence = self._label("sil")
            self.filter_parts.append(
                f"anullsrc=r={q.sample_rate}:cl={self.layout},"
                f"atrim=start_sample=0:end_sample={item.start_sample},{self._aformat()}"
                f"[{silence}]"
            )
            placed = self._label("ap")
            self.filter_parts.append(
                f"[{silence}][{sliced}]concat=n=2:v=0:a=1[{placed}]"
            )
        # The envelope goes on *after* the contribution has been positioned, and
        # after the tail padding: its expression is written in time relative to
        # the start of the rendered range, which is only true of a stream that
        # already carries the leading silence. Applying it to the sliced clip
        # instead made `t` restart at 0, so a fade on a clip that does not start
        # at the range start never reached its anchor and did nothing at all.
        padded = self._pad_tail(placed)
        shaped = self._label("av")
        self.filter_parts.append(
            f"[{padded}]volume=volume={self._envelope(item)}:eval=frame[{shaped}]"
        )
        return shaped

    def _pad_tail(self, label: str) -> str:
        """Extend a contribution to the full range so the mix stays anchored."""
        out = self._label("at")
        self.filter_parts.append(
            f"[{label}]apad,atrim=start_sample=0:end_sample={self.plan.total_samples}[{out}]"
        )
        return out

    def _envelope(self, item: AudioItem) -> str:
        """Piecewise-linear gain expression == the domain envelope.

        Filter-local ``t`` runs from the render range start (leading silence is
        concatenated first), so absolute output-time fade anchors convert by
        subtracting ``plan.output_start``.
        """
        clip = item.clip
        base = self.plan.output_start
        parts = [decimal(clip.gain)]
        if clip.fade_in > 0:
            a0 = clip.output_start - base
            a1 = a0 + clip.fade_in
            parts.append(
                f"if(lt(t,{decimal(a0)}),0,"
                f"if(lt(t,{decimal(a1)}),(t-{decimal(a0)})/{decimal(a1 - a0)},1))"
            )
        if clip.fade_out > 0:
            b0 = clip.output_end - clip.fade_out - base
            b1 = clip.output_end - base
            parts.append(
                f"if(lt(t,{decimal(b0)}),1,"
                f"if(lt(t,{decimal(b1)}),({decimal(b1)}-t)/{decimal(b1 - b0)},0))"
            )
        return "'" + "*".join(parts) + "'"


def build_job(
    plan: RenderPlan,
    asset_paths: dict[str, Path],
    sources: dict[tuple[str, int], StreamSource],
    destination: Path,
    ffmpeg: str,
    preset: CodecPreset = MP4_H264_AAC,
) -> RenderJob:
    """Full FFmpeg argv for one plan (single process, one encoder pass)."""
    builder = GraphBuilder(plan, asset_paths, sources, ffmpeg)
    video_label = builder.build_video()
    audio_label = builder.build_audio()
    q = plan.quantisation
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    for group in builder.inputs:
        args.extend(group)
    args.extend(["-filter_complex", ";".join(builder.filter_parts)])
    args.extend(["-map", f"[{video_label}]", "-map", f"[{audio_label}]"])
    args.extend(["-f", preset.muxer])
    args.extend(["-c:v", preset.video_codec, "-pix_fmt", preset.pixel_format])
    args.extend(preset.extra_video_args)
    args.extend(["-r", rational(q.frame_rate)])
    args.extend(["-c:a", preset.audio_codec, "-ar", str(q.sample_rate)])
    args.extend(preset.extra_audio_args)
    if preset.muxer == "mp4":
        args.extend(["-movflags", "+faststart"])
    args.extend(["-progress", "pipe:1", "-nostats", str(destination)])
    return RenderJob(
        args=args,
        duration=plan.duration,
        video_items=len(plan.video_items),
        audio_items=len(plan.audio_items),
        warnings=plan.warnings,
    )

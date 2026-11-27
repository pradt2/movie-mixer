"""Execute a render plan with FFmpeg: progress, cancellation, safe output.

Guarantees and limits
--------------------
* Output is written to a temporary sibling file
  (``name.<token>.partial.<ext>``) and **published** only on success, so a
  cancelled or failed render never leaves a half-written movie. Publication
  (``video_editor.fs.protection.publish``) never clobbers: with no approval it
  links the file into place and fails if something appeared in the meantime, and
  an approved replacement is re-checked against the file that stood there when
  the user agreed. Cancellation is honoured once more immediately before
  publication, because the last progress event is not the last thing that
  happens.
* **Source media is never a destination.** The request carries a
  :class:`~video_editor.fs.protection.DestinationGuard` covering every asset of
  the project — placed, muted or unused — and a destination that resolves to one
  of those files (by path, symlink or hard link) is refused even when
  ``overwrite=True``. Confirmation authorises replacing an output, not destroying
  a source.
* **Sources are validated, not assumed.** Every asset this range actually reads
  is checked against its recorded identity (size + mtime_ns) before decoding and
  again before publication: a file replaced mid-render is refused rather than
  published as a mix of two encodes. Assets outside the rendered range are not
  required to be present.
* **A recorded source end is a claim, so the tail is measured before encoding.**
  For every stream whose end a range presents material up to, the last moments are
  decoded on the stream's own timeline: sound by the span that actually decodes
  (:meth:`RenderRequest.check_source_tails`), picture by the last real frame plus
  that frame's own recorded duration, reading frames rather than packets
  (:meth:`RenderRequest.check_picture_tails`). A shortage larger than the
  renderer's own rounding is refused — invented silence or a frame frozen across
  seconds are both endings the user did not choose. A smaller one is warned, and a
  tail that cannot be measured is warned and its bound left conservative.
* After rendering, the produced file is probed and its duration compared with
  the plan. Allowed difference: **one output frame** plus the documented audio
  encoder padding (``AUDIO_PADDING_SECONDS``). Beyond that the render is
  reported as failed rather than accepted silently. Duration alone proves
  nothing — a truncated file still reports a plausible one — so each stream is
  then checked on its own terms (``probe_streams``/``validate_output``): picture
  by counted frames and geometry, sound by the samples that can actually be
  decoded from it (``measure_decoded_span``). Packet arithmetic is not used:
  packet size is a container detail, measured here as 4096 samples for PCM in
  Matroska and 1024 for AAC. What the file holds is reported back on
  :class:`RenderResult`.
* Cancelling terminates FFmpeg and removes temporary files.
* Plans with many items are rendered in **chunks**: each chunk renders to a
  lossless intermediate (FFV1 + PCM, which have no encoder delay or tail
  padding), and the chunks are demuxed together for one final encode, so joins
  cannot introduce per-chunk priming gaps. Chunk ranges are produced by the
  same planner, so clip boundaries, black/silence fill and fade anchors stay
  identical to a single-pass render.
* **Picture this renderer cannot draw is refused, not approximated.** Interlaced
  picture and HDR picture cannot become SDR ``yuv420p`` without a deinterlacer or
  a tone mapper, neither of which exists here, so every picture stream this range
  actually shows is measured from the file before anything is decoded and a
  known-bad format is refused with the value that made it so (see
  :mod:`video_editor.media.capability`). A stream whose picture this range never
  shows - in the bin, on a disabled track, outside the range - is never the reason
  for a refusal, and a format the container does not state is warned, not refused.
* Only the MP4/H.264/AAC delivery preset is claimed, and encoder availability
  is checked up front. No bit-exact passthrough, HDR or arbitrary channel-layout
  support is claimed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from ..core.model import Clip, Project, StreamSource
from ..core.timeutils import ONE
from ..fs.identity import Identity, check
from ..fs.protection import (
    Consent,
    DestinationGuard,
    DestinationSafetyError,
    publish,
)
from ..media.capability import PictureCapabilityError, check_picture_capabilities
from ..media.framegrab import StillError, tail_picture_extent
from ..media.frameindex import FrameIndex
from .cache import PreviewCache
from .graph import (
    LOSSLESS_INTERMEDIATE,
    MP4_H264_AAC,
    CodecPreset,
    RenderJob,
    build_job,
    decimal,
    encoder_available,
)
from .plan import RenderPlan, build_plan

F = Fraction
ZERO = F(0)

#: AAC pads the tail to a whole audio frame; together with the half-frame
#: quantisation allowance this is the documented duration tolerance.
AUDIO_PADDING_SECONDS = F(2048, 48000)

#: How much of a stream's end is demux-probed when a clip reads that far.
SOURCE_TAIL_SECONDS = F(2)

#: Shortfall that still counts as the renderer's own padding rather than missing
#: source audio. One output frame at the slowest plausible rate is far more than
#: quantisation and tempo edges can cost, and far less than a truncated file.
SOURCE_TAIL_TOLERANCE = F(1, 24)

#: How much of a picture stream's end is decoded when a clip presents that far.
#: Same budget as the audio check, for the same reason: it is enough to see the
#: last frames of a stream and small enough that the probe stays a probe.
PICTURE_TAIL_SECONDS = F(2)

#: Wall-clock limit on that bounded decode. Past it the tail is called
#: unmeasurable and said out loud, never guessed at from the header.
PICTURE_TAIL_PROBE_TIMEOUT_SECONDS = 60.0

#: Plans with more items than this are rendered through lossless chunks.
MAX_ITEMS_PER_GRAPH = 96

#: Free space kept free besides the estimated temporary requirement.
TEMP_MARGIN_BYTES = 512 * 1024 * 1024


def estimate_intermediate_bytes(plan: RenderPlan) -> int:
    """Worst-case size of the lossless intermediates for ``plan`` (upper bound).

    FFV1 compresses, so this is deliberately an over-estimate: uncompressed
    4:2:0 picture plus 16-bit PCM sound. It is used only to refuse a render that
    would fill the disk, never to allocate anything.
    """
    frames = plan.total_frames
    picture = frames * plan.width * plan.height * 3 // 2
    seconds = float(plan.duration)
    channels = {"mono": 1, "stereo": 2}.get(plan.channel_layout, 6)
    audio = int(seconds * plan.quantisation.sample_rate * channels * 2)
    return picture + audio + 4096 * (len(plan.video_items) + len(plan.audio_items) + 1)

ProgressCallback = Callable[[float], None]


class RenderError(Exception):
    """Rendering failed; the destination was left untouched."""


class RenderCancelled(Exception):
    """Rendering was cancelled; temporary files were removed."""


@dataclass(frozen=True)
class RenderResult:
    path: Path
    duration: Fraction
    #: duration measured on the produced file
    produced_duration: Fraction
    warnings: tuple[str, ...] = ()
    chunks: int = 1
    #: picture frames counted in the produced file, and the sound samples per
    #: channel it really decodes to (for AAC that includes the encoder's own
    #: delay and tail padding: measured +256 samples at 48 kHz, +888 at 44.1)
    produced_frames: int = 0
    produced_audio_samples: int = 0


@dataclass
class RenderRequest:
    """What to render, from where, to where."""

    project: Project
    #: asset_id -> current filesystem path
    asset_paths: dict[str, Path]
    destination: Path
    out_start: Fraction = ZERO
    out_end: Fraction | None = None
    preset: CodecPreset = MP4_H264_AAC
    overwrite: bool = False
    ffmpeg: str | None = None
    ffprobe: str | None = None
    chunk_item_limit: int = MAX_ITEMS_PER_GRAPH
    #: picture-size multiplier; 1 for export, < 1 for a faster preview. Timing,
    #: stacking, gaps and mixing are identical either way (see plan_geometry).
    geometry_scale: Fraction = ONE
    #: when set, a successful render is registered with this cache (and evicted
    #: from it later if it falls outside the cache's budgets). Only cache-owned
    #: destinations may be used, so a preview never deletes a user's export.
    cache: PreviewCache | None = None
    #: asset id -> the file identity (size + mtime_ns) recorded when the asset was
    #: read. When supplied, every source this render reads is re-checked against it
    #: before decoding and again before publishing: a re-encode dropped at the same
    #: path mid-render is refused, not cut together from two different files.
    asset_identities: dict[str, Identity] = field(default_factory=dict)
    #: paths this render must never write to — every imported asset (placed or
    #: not) plus other protected project files. Enforced regardless of
    #: ``overwrite``: confirming a replacement is not consent to destroy a source.
    guard: DestinationGuard = field(default_factory=DestinationGuard)
    #: asset/stream -> measured picture-timestamp index (see ``stream_index_for``).
    #: Not an input: it is filled in while rendering, and deliberately survives
    #: chunking so a long export probes each stream once, not once per chunk.
    _timing_cache: dict[tuple[str, int], FrameIndex | None] = field(
        default_factory=dict, init=False, repr=False
    )

    def plan(self) -> RenderPlan:
        return self.plan_for(self.out_start, self.out_end)

    def plan_for(self, out_start: Fraction, out_end: Fraction) -> RenderPlan:
        """Plan one sub-range with this request's settings applied.

        Chunking re-plans sub-ranges through the same entry point, so a preview
        cannot drift from the composition semantics it is supposed to preview.
        """
        return build_plan(self.project, out_start, out_end, self.geometry_scale)

    # ------------------------------------------------------- stream timing

    def stream_index_for(self, asset_id: str, stream_index: int) -> FrameIndex | None:
        """Bounded picture-timestamp index for one stream, or None if unreadable.

        The picture chain needs two measurements to place its sampling grid where
        the plan says it lies: the container's timestamp granularity (so the seek
        lands on an exactly representable tick and FFmpeg's own rebase is exact)
        and the first real picture the slice can read (so a slice that starts
        before any readable frame holds a picture instead of starting late and
        shifting every later frame). Reading either is a subprocess, so it happens
        here - on the render thread - once per stream per request rather than once
        per chunk.

        ``None`` means "not measured", and is not silently treated as a guess:
        :attr:`RenderJob.unanchored` records the items whose grid could not be
        placed, and the report says the pictures are approximate.
        """
        key = (asset_id, stream_index)
        if key in self._timing_cache:
            return self._timing_cache[key]
        path = self.asset_paths.get(asset_id)
        found: FrameIndex | None = None
        if path is not None:
            try:
                _, ffprobe = self.resolve_tools()
                found = FrameIndex(Path(path), stream_index, ffprobe)
            except (StillError, OSError, ValueError):
                found = None  # reported as unanchored, never as exact
        self._timing_cache[key] = found
        return found

    def time_base_of(self, asset_id: str, stream_index: int) -> Fraction | None:
        index = self.stream_index_for(asset_id, stream_index)
        return None if index is None else index.time_base

    def first_frame_of(
        self, asset_id: str, stream_index: int, source_in: Fraction
    ) -> Fraction | None:
        index = self.stream_index_for(asset_id, stream_index)
        return None if index is None else index.at_or_after(source_in)

    def previous_frame_of(
        self, asset_id: str, stream_index: int, source_time: Fraction
    ) -> Fraction | None:
        """The last real picture at or before ``source_time``, when measured.

        This is what the first grid point of a picture item shows, so where the
        index knows it the reading window starts there rather than at a nominal
        preroll: less decoded material per item, and no estimated frame spacing
        for a variable-frame-rate stream.
        """
        index = self.stream_index_for(asset_id, stream_index)
        return None if index is None else index.at_or_before(source_time)

    def window_pictures_of(
        self, asset_id: str, stream_index: int, start: Fraction, end: Fraction
    ) -> tuple[Fraction, ...] | None:
        """Every real picture timestamp of ``[start, end)``, when all of it was read.

        These are the stamps an item's exact tempo is proved against: a stream whose
        container counts time more finely than the tempo can be divided on has an exact
        tick only because of where its pictures really sit
        (:func:`~video_editor.render.plan.picture_tempo`). An index that was never read,
        or a span it could not read across completely, answers ``None`` - pictures from
        a partly read window say nothing about the pictures that were not read, and the
        item keeps the approximate chain and its warning instead.
        """
        index = self.stream_index_for(asset_id, stream_index)
        if index is None:
            return None
        span = index.answer_frames_in(start, end)
        return span.pts if span.complete else None

    # ---------------------------------------------------------------- safety

    def capture_consent(self) -> Consent | None:
        """Record what stands at the destination now, as the approval's basis.

        A cache-owned destination is ours to replace, so ownership is the
        authorisation. Otherwise ``overwrite`` means the user approved replacing
        *the file they were shown*, and that file's identity is what publication
        is compared against a render later.
        """
        if self.cache is not None:
            if not self.cache.owns(self.destination):
                raise RenderError(
                    f"{self.destination} is a cache destination this cache does not "
                    "own; refusing to replace it"
                )
            return Consent.as_owner(self.destination)
        if self.overwrite:
            return Consent.capture(self.destination)
        return None

    def check_destination(self) -> None:
        """Refuse a destination that is this project's own media."""
        try:
            self.guard.check(self.destination, "write rendered media")
        except DestinationSafetyError as exc:
            raise RenderError(str(exc)) from exc

    def check_sources(self, asset_ids: set[str], when: str = "before rendering") -> None:
        """Validate every source *asset_ids* actually reads — once per asset.

        Only assets this range reads are checked (a range that ignores an offline
        asset can still be rendered), while destination protection covers every
        asset in the project: relaxing one would be a data-loss risk, relaxing
        the other is simply correct scoping.
        """
        for asset_id in sorted(asset_ids):
            path = self.asset_paths.get(asset_id)
            # an asset id is not something the user can act on: name the file the
            # project was saved with, so a moved source is findable
            if path is None:
                raise RenderError(
                    f"Asset {asset_id} is missing on disk: no file is recorded for it. "
                    "Relink it before exporting."
                )
            entry = check(
                Path(path), self.asset_identities.get(asset_id), Path(path).name
            )
            if entry.usable:
                continue
            # the state word is part of the message: "missing" and "changed"
            # need different remedies, and the user should not have to infer it
            raise RenderError(
                f"Asset {asset_id} cannot be used {when} [{entry.status.value}]: "
                f"{entry.describe()} Relink or reimport it, then render again."
            )

    @staticmethod
    def assets_used(plans: Iterable[RenderPlan]) -> set[str]:
        """Asset ids whose material a set of plans will decode."""
        used: set[str] = set()
        for plan in plans:
            for item in plan.video_items:
                if item.clip is not None:
                    used.add(item.clip.asset_id)
            for item in plan.audio_items:
                used.add(item.clip.asset_id)
        return used

    def resolve_tools(self) -> tuple[str, str]:
        """Locate ffmpeg/ffprobe, check codecs and the destination."""
        ffmpeg = self.ffmpeg or shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RenderError("ffmpeg is not available on PATH; install it to export")
        ffprobe = self.ffprobe or shutil.which("ffprobe")
        if ffprobe is None:
            raise RenderError("ffprobe is not available on PATH; cannot verify exports")
        for codec in (self.preset.video_codec, self.preset.audio_codec):
            if not encoder_available(ffmpeg, codec):
                raise RenderError(
                    f"this FFmpeg cannot encode {codec}; {self.preset.display} is "
                    "unavailable"
                )
        # protection first: an existing file is only replaceable if it is not media
        self.check_destination()
        if self.destination.exists() and not self.overwrite:
            raise RenderError(
                f"{self.destination} already exists; choose another name or confirm "
                "replacement"
            )
        if not self.destination.parent.is_dir():
            raise RenderError(f"Output directory does not exist: {self.destination.parent}")
        return ffmpeg, ffprobe

    def check_temp_space(self, plan: RenderPlan) -> None:
        """Refuse a chunked render that would fill the disk, before starting it.

        Chunked renders write lossless intermediates, which are large; failing
        with an estimate is better than running out of space half way through.
        """
        needed = estimate_intermediate_bytes(plan)
        try:
            free = shutil.disk_usage(self.destination.parent).free
        except OSError as exc:  # pragma: no cover - unreadable mount point
            raise RenderError(f"cannot check free space for {self.destination}: {exc}") from exc
        if needed + TEMP_MARGIN_BYTES > free:
            raise RenderError(
                f"this composition needs two FFmpeg passes and about "
                f"{needed / 1_000_000_000:.1f} GB of temporary space; only "
                f"{free / 1_000_000_000:.1f} GB is free. Export a shorter range, or "
                "free space, instead"
            )


    def check_source_tails(
        self,
        ffmpeg: str,
        plan: RenderPlan,
        sources: dict[tuple[str, int], StreamSource],
    ) -> list[str]:
        """Measure the end of every sound a clip presents up to its recorded end.

        The graph always produces the exact number of samples the plan promises,
        so material a stream does not actually contain would arrive as silence and
        look intentional. A container's recorded end is sometimes an estimate
        (``conservative_end``) or simply wrong, so for every contribution that
        presents sound that far the last moments are decoded here, on the stream's
        own timeline, and the outcome is decided on evidence:

        * short by more than :data:`SOURCE_TAIL_TOLERANCE` — refuse: the render
          would present invented silence as if it were the film's own ending;
        * short by less — keep going and warn (that much is quantisation, tempo
          and resampler residue, which the graph pads by definition);
        * not measurable (no stream position recorded, or the probe failed) —
          keep going and warn.
        """
        warnings: list[str] = []
        # one probe per stream, at the furthest source time this range can present
        wanted: dict[tuple[str, int], Fraction] = {}
        for item in plan.audio_items:
            source = sources.get((item.clip.asset_id, item.clip.stream_index))
            if source is None or not source.placeable:
                continue
            # the graph reads the clip whole, but only the part this range presents
            # can end up as invented silence, so a preview of the middle of a clip
            # that happens to end at EOF pays nothing for this check
            if item.source_end < source.end - SOURCE_TAIL_TOLERANCE:
                continue
            key = (item.clip.asset_id, item.clip.stream_index)
            wanted[key] = max(wanted.get(key, ZERO), item.source_end)
        for (asset_id, stream_index), expected_end in sorted(wanted.items()):
            source = sources[(asset_id, stream_index)]
            path = self.asset_paths.get(asset_id)
            if path is None:
                continue  # check_sources already refused this asset
            start = max(ZERO, expected_end - SOURCE_TAIL_SECONDS)
            available = probe_source_material(
                ffmpeg, Path(path), source, start, expected_end - start
            )
            if available is None:
                warnings.append(
                    f"Asset {asset_id} stream {stream_index} ends at its recorded bound, "
                    "but its last moments could not be measured; the end of that clip may "
                    "be shorter than it looks"
                )
                continue
            missing = (expected_end - start) - available
            if missing > SOURCE_TAIL_TOLERANCE:
                raise RenderError(
                    f"Asset {asset_id} stream {stream_index} stops "
                    f"{float(missing):.3f}s before its recorded end ({float(expected_end)}s), "
                    "which is where a clip needs it: rendering would replace that much of "
                    "the composition with silence. The source file is truncated or its "
                    "header is wrong — reimport it, or trim the clips that reach its end."
                )
            if missing > 0:
                warnings.append(
                    f"Asset {asset_id} stream {stream_index} ends "
                    f"{float(missing) * 1000:.1f} ms before its recorded end; that is within "
                    "the padding the renderer is allowed and is filled with silence"
                )
        return warnings

    def check_picture_tails(
        self,
        ffprobe: str,
        plan: RenderPlan,
        sources: dict[tuple[str, int], StreamSource],
    ) -> list[str]:
        """Measure the end of every picture a clip presents up to its recorded end.

        The picture analogue of :meth:`check_source_tails`, with a different harm.
        A missing sound arrives as silence and is noticed; a missing *picture*
        arrives as the last frame held for as long as the plan asks and looks
        exactly like an ending the user chose. So for every contribution that
        presents picture material as far as its stream's recorded end, the last
        moments are decoded here — **frames**, not packets, because a packet PTS
        is a seek hint and says nothing about what decodes — and the answer is
        taken from the last real frame's own recorded duration, never from a
        nominal frame rate:

        * the material found stops more than **one output frame** of held picture
          before the last source time this range samples — refuse: the render would
          freeze one frame across time the film does not contain;
        * within that one frame — keep going and warn, because that is the boundary
          rounding the plan already promises: ``Quantisation.render_range_frames``
          snaps a range edge forward, at most one output frame, so the last item's
          grid can ask for a hair past the clip's edge;
        * not measurable (the probe failed, or the container recorded no duration
          for the last picture) — keep going and warn. A bound that could not be
          disproved is not thereby proved: it stays conservative.
        """
        quant = plan.quantisation
        # furthest source time each stream is asked to supply by this range, with
        # the clip that asks for it: one bounded probe per stream, not per item
        wanted: dict[tuple[str, int], tuple[Fraction, Clip, Fraction]] = {}
        for item in plan.video_items:
            if item.clip is None:
                continue
            source = sources.get((item.clip.asset_id, item.clip.stream_index))
            if source is None or source.end is None:
                continue
            # the material this item really needs is the source time of its *last
            # sampled frame*: the item's own end edge is a boundary the plan may
            # snap past it, and past the clip's slice the graph holds the nearest
            # legal picture by contract, which is not missing footage
            sampled = item.clip.source_time_at(item.output_end - ONE / quant.frame_rate)
            needed = min(item.source_end, sampled)
            if needed < source.end - PICTURE_TAIL_SECONDS:
                continue  # this range never presents the end of the stream
            key = (item.clip.asset_id, item.clip.stream_index)
            previous = wanted.get(key)
            if previous is None or needed > previous[0]:
                wanted[key] = (needed, item.clip, source.end)
        warnings: list[str] = []
        for (asset_id, stream_index), (needed, clip, recorded_end) in sorted(
            wanted.items(), key=lambda entry: entry[0]
        ):
            source = sources[(asset_id, stream_index)]
            path = self.asset_paths.get(asset_id)
            if path is None:
                continue  # check_sources already refused this asset
            start = max(source.origin, needed - PICTURE_TAIL_SECONDS)
            try:
                tail = tail_picture_extent(
                    Path(path),
                    stream_index,
                    start,
                    needed + PICTURE_TAIL_SECONDS,
                    ffprobe,
                    PICTURE_TAIL_PROBE_TIMEOUT_SECONDS,
                )
            except StillError as exc:
                warnings.append(
                    f"Asset {asset_id} stream {stream_index} ends at its recorded bound, "
                    f"but its last pictures could not be measured ({exc}); the end of "
                    "that clip may be shorter than it looks"
                )
                continue
            if tail.last_pts is None:
                raise RenderError(
                    f"Asset {asset_id} stream {stream_index} has no picture that decodes "
                    f"between {float(start)}s and {float(needed)}s on its own time axis, "
                    f"which is where a clip ({clip.id}) needs one — its stream is "
                    f"recorded as running to {float(recorded_end)}s. The source file is "
                    "truncated or its header is wrong: reimport it, or trim the clips "
                    "that reach its end."
                )
            if tail.end is None:
                # no duration recorded for the last picture, so its end is known
                # only to within one picture of this stream's own nominal rate
                nominal = ONE / source.frame_rate if source.frame_rate else ZERO
                measured_end, slack = tail.last_pts, nominal
            else:
                measured_end, slack = tail.end, ZERO
            missing = needed - measured_end
            # the harm is measured on the output clock: the held picture covers
            # this much of the composition, whatever the clip's tempo was
            held = missing / clip.rate
            allowed = ONE / quant.frame_rate + slack / clip.rate
            if held > allowed:
                raise RenderError(
                    f"Asset {asset_id} stream {stream_index} stops {float(missing):.3f}s "
                    f"before the {float(needed)}s its clip {clip.id} samples: the last "
                    f"picture that really decodes starts at {float(tail.last_pts)}s and "
                    f"runs to {float(measured_end)}s, while the stream is recorded as "
                    f"running to {float(recorded_end)}s. Rendering would hold that one "
                    f"picture for {float(held):.3f}s of the composition (tolerance "
                    f"{float(allowed):.3f}s, one output frame). The source is truncated "
                    "or its header is wrong — reimport it, or trim the clips that reach "
                    "its end."
                )
            if held > 0:
                warnings.append(
                    f"Asset {asset_id} stream {stream_index} runs out of pictures "
                    f"{float(missing) * 1000:.1f} ms before the last frame this range "
                    f"samples; that picture is held for the difference, which is within "
                    f"the one output frame ({float(allowed) * 1000:.1f} ms) the plan's "
                    "boundary rounding allows"
                )
        return warnings


def render(
    request: RenderRequest,
    on_progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> RenderResult:
    """Render ``request`` to ``request.destination``.

    Order matters: the destination is proven safe, the approval is recorded, and
    the sources are validated *before* anything is decoded; the same checks run
    again immediately before publication, and a cancellation is honoured there
    too. A cancelled, refused or failed render never writes to the destination,
    and never replaces a file that appeared or changed while it was running.
    """
    ffmpeg, ffprobe = request.resolve_tools()
    plan = request.plan()
    if plan.total_frames <= 0:
        raise RenderError("Nothing to render: the requested range is empty")

    consent = request.capture_consent()
    plans: list[RenderPlan] = [plan]
    request.check_sources(request.assets_used(plans))
    # Picture the composition will actually show is checked against what this
    # renderer can produce - interlaced and HDR picture cannot be drawn into an
    # SDR yuv420p delivery without a deinterlacer or tone mapper, so it is refused
    # here, before anything is decoded, rather than rendered wrong. The whole
    # range is checked once: a chunk's picture items are a subset of this plan's,
    # because stacking and clip edges do not depend on where a chunk is cut.
    try:
        capability_warnings = check_picture_capabilities(
            request.project, plans, request.asset_paths, ffprobe
        )
    except PictureCapabilityError as exc:
        raise RenderError(str(exc)) from exc

    token = uuid.uuid4().hex[:12]
    staging = request.destination.with_name(
        f"{request.destination.stem}.{token}.partial.{request.preset.extension}"
    )
    owned: list[Path] = [staging]
    sources = {(s.asset_id, s.stream_index): s for s in request.project.sources}
    # a source whose recorded end is a lie is found before anything is encoded,
    # because the alternative is a render that quietly ends in invented silence
    # for sound, or with one picture frozen across seconds for picture
    tail_warnings = request.check_source_tails(ffmpeg, plan, sources)
    tail_warnings += request.check_picture_tails(ffprobe, plan, sources)
    # picture streams whose sampling grid could not be placed exactly (see
    # ``RenderJob.unanchored``) and streams drawn with the container-quantised
    # picture chain (``RenderJob.inexact_picture``): collected across chunks and
    # said out loud once
    unanchored: set[tuple[str, int]] = set()
    inexact: set[tuple[str, int]] = set()
    try:
        item_count = len(plan.video_items) + len(plan.audio_items)
        if item_count <= request.chunk_item_limit:
            job = build_job(
                plan, request.asset_paths, sources, staging, ffmpeg, request.preset,
                request.time_base_of, request.first_frame_of, request.previous_frame_of,
                request.window_pictures_of,
            )
            unanchored.update(job.unanchored)
            inexact.update(job.inexact_picture)
            _run(job, on_progress, cancel_event)
            chunks = 1
        else:
            request.check_temp_space(plan)
            chunks = _render_chunked(
                request, plan, sources, staging, token, ffmpeg, owned, on_progress,
                cancel_event, plans, unanchored, inexact,
            )
        container_duration = probe_duration(ffprobe, staging)
        tolerance = F(1) / plan.quantisation.frame_rate + AUDIO_PADDING_SECONDS
        if abs(container_duration - plan.duration) > tolerance:
            raise RenderError(
                f"rendered {float(container_duration):.3f}s but the plan calls for "
                f"{float(plan.duration):.3f}s (tolerance {float(tolerance):.3f}s); the "
                "source may end earlier than its recorded bounds"
            )
        # duration is a claim; each stream is checked on its own terms as well,
        # because a truncated file still reports a plausible container duration
        output_facts = verify_output(ffprobe, ffmpeg, staging, plan)
        if cancel_event is not None and cancel_event.is_set():
            # the last progress event is not the last thing that happens: a file
            # must not appear after the user asked for nothing
            raise RenderCancelled("render cancelled before the file was written")
        # the sources are re-validated: a file swapped mid-render would otherwise
        # publish a composition assembled from two different encodes
        request.check_destination()
        request.check_sources(request.assets_used(plans), when="after rendering")
        try:
            publish(
                staging,
                request.destination,
                guard=request.guard,
                consent=consent,
                action="write the rendered file",
            )
        except DestinationSafetyError as exc:
            raise RenderError(str(exc)) from exc
        owned.remove(staging)
        if request.cache is not None:
            request.cache.commit(request.destination)
        return RenderResult(
            path=request.destination,
            duration=plan.duration,
            produced_duration=container_duration,
            warnings=(
                plan.warnings
                + capability_warnings
                + tuple(tail_warnings)
                + _unanchored_warning(unanchored)
                + _inexact_picture_warning(inexact)
            ),
            chunks=chunks,
            produced_frames=output_facts.video.packets,
            produced_audio_samples=output_facts.audio_samples,
        )
    finally:
        for path in owned:
            path.unlink(missing_ok=True)


def _unanchored_warning(streams: set[tuple[str, int]]) -> tuple[str, ...]:
    """Say so when a picture stream's sampling grid could not be placed exactly.

    The grid is placed on an exactly representable container timestamp, which
    needs that timestamp granularity to have been read. When it could not be, the
    render is still the composition the plan describes, but a sampling time that
    falls exactly on a frame time can come out one frame away — so this is
    reported, never left for the user to discover as a shifted picture.
    """
    if not streams:
        return ()
    named = ", ".join(f"asset {asset} stream {index}" for asset, index in sorted(streams))
    return (
        f"the timestamp granularity of {named} could not be read, so its sampling "
        "grid was placed by FFmpeg's own rounding rather than on an exactly "
        "representable container timestamp: the picture at a frame boundary may be "
        "the one next to the frame the plan names",
    )


def _inexact_picture_warning(streams: set[tuple[str, int]]) -> tuple[str, ...]:
    """Say so when a picture stream had to be drawn the container-quantised way.

    The exact picture chain needs a filter tick that is a whole number of ticks per
    second and whose arithmetic FFmpeg can hold exactly. A stream whose tick could
    not be established - too fine, or a timeline long enough that it would not
    fit - is drawn on the container's own timestamps instead, where the tempo is
    quantised before a picture is chosen. Reported, never presented as exact: this
    is a refusal to claim the exact result, not a quiet approximation, and the
    ordinary case - a measured container time base and an ordinary tempo - always
    establishes one.
    """
    if not streams:
        return ()
    named = ", ".join(f"asset {asset} stream {index}" for asset, index in sorted(streams))
    return (
        f"no exact sampling tick could be established for {named}, so its picture "
        "was sampled on the container's timestamp grid: at a tempo that is not a "
        "whole division of that grid the picture at a boundary may be the one next "
        "to the frame the plan names",
    )


def _render_chunked(
    request: RenderRequest,
    plan: RenderPlan,
    sources,
    staging: Path,
    token: str,
    ffmpeg: str,
    owned: list[Path],
    on_progress: ProgressCallback | None,
    cancel_event: threading.Event | None,
    plans: list[RenderPlan] | None = None,
    unanchored: set[tuple[str, int]] | None = None,
    inexact: set[tuple[str, int]] | None = None,
) -> int:
    """Lossless chunks, then one final encode (lossy encoders start once)."""
    pieces = _chunk_ranges(plan, request.chunk_item_limit)
    intermediates: list[Path] = []
    for index, piece in enumerate(pieces):
        intermediate = staging.with_name(f"{staging.stem}.chunk{index:03d}.mkv")
        intermediates.append(intermediate)
        owned.append(intermediate)
        piece_plan = request.plan_for(piece[0], piece[1])
        if plans is not None:
            plans.append(piece_plan)
        job = build_job(
            piece_plan, request.asset_paths, sources, intermediate, ffmpeg,
            LOSSLESS_INTERMEDIATE, request.time_base_of, request.first_frame_of,
            request.previous_frame_of, request.window_pictures_of,
        )
        if unanchored is not None:
            unanchored.update(job.unanchored)
        if inexact is not None:
            inexact.update(job.inexact_picture)
        _run(job, _scaled_progress(plan, piece[0], on_progress), cancel_event)
    list_file = staging.with_name(f"{staging.stem}.concat.txt")
    owned.append(list_file)
    # single-quoted paths only: FFmpeg's concat list format has no other escaping
    escaped = [p.as_posix().replace("'", "'\\''") for p in intermediates]
    list_file.write_text("".join(f"file '{p}'\n" for p in escaped), encoding="utf-8")
    job = _join_job(plan, list_file, staging, ffmpeg, request.preset)
    _run(job, on_progress, cancel_event)
    return len(pieces)


def _scaled_progress(
    plan: RenderPlan, piece_start: Fraction, on_progress: ProgressCallback | None
) -> ProgressCallback | None:
    if on_progress is None:
        return None
    total = float(plan.duration)

    def report(fraction: float) -> None:
        base = float(piece_start - plan.output_start) / total
        on_progress(min(1.0, base + fraction * (1.0 - base)))

    return report


def _chunk_ranges(plan: RenderPlan, limit: int) -> list[tuple[Fraction, Fraction]]:
    """Split the range at **item** boundaries with ~``limit`` items per chunk.

    Both picture and audio items are counted: an audio-heavy composition must
    not defeat the bound, because each item is a filter chain and (per distinct
    asset/stream/seek) a decoder input in the FFmpeg graph. Cut points are the
    exact output times at which some item ends, so re-planning each chunk from
    the same snapshot yields contiguous ranges whose frame and sample totals add
    up to the whole plan exactly.
    """
    items = len(plan.video_items) + len(plan.audio_items)
    if items <= limit:
        return [(plan.output_start, plan.output_end)]
    q = plan.quantisation
    base_frame = q.frame_index(plan.output_start)
    base_sample = q.sample_index(plan.output_start)
    ends = sorted(
        [q.frame_time(base_frame + item.end_frame) for item in plan.video_items]
        + [
            q.sample_time(base_sample + item.start_sample + item.samples)
            for item in plan.audio_items
        ]
    )
    ranges: list[tuple[Fraction, Fraction]] = []
    start = plan.output_start
    count = 0
    for end in ends:
        count += 1
        if count >= limit and start < end < plan.output_end:
            ranges.append((start, end))
            start = end
            count = 0
    if start < plan.output_end:
        ranges.append((start, plan.output_end))
    return ranges or [(plan.output_start, plan.output_end)]


def _join_job(
    plan: RenderPlan, concat_list: Path, destination: Path, ffmpeg: str, preset: CodecPreset
) -> RenderJob:
    q = plan.quantisation
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-c:v",
        preset.video_codec,
        "-pix_fmt",
        preset.pixel_format,
        *preset.extra_video_args,
        "-r",
        f"{q.frame_rate.numerator}/{q.frame_rate.denominator}",
        "-c:a",
        preset.audio_codec,
        "-ar",
        str(q.sample_rate),
        *preset.extra_audio_args,
    ]
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


def measure_decoded_span(
    ffmpeg: str,
    path: Path,
    kind: str,
    position: int,
    start: Fraction = ZERO,
    span: Fraction | None = None,
) -> Fraction | None:
    """How much of one stream can actually be decoded from ``start`` onwards.

    Measured the way rendering reads a file — an input ``-ss`` relative to the
    container's own start, then whatever the decoder produces — and answered from
    FFmpeg's progress protocol rather than from a header the muxer wrote. A
    container whose declared length exceeds its data shows up here as a shorter
    span. ``None`` when the stream cannot be addressed or decoded at all.

    The answer is a duration, not a sample count: ``-progress`` reports whole
    microseconds, which for any audio rate in use here is far under one sample
    (measured against a byte-exact decode: 0.02 samples at 48 kHz).
    """
    if position < 0:
        return None
    if span is not None and span <= 0:
        return None
    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", decimal(max(start, ZERO)),
    ]
    if span is not None:
        args += ["-t", decimal(span)]
    args += [
        "-i", str(path),
        "-map", f"0:{kind}:{position}",
        "-f", "null", "-",
        "-progress", "pipe:1",
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    produced: Fraction | None = None
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        if key in ("out_time_us", "out_time_ms"):
            try:
                stamps = Fraction(int(value), 1_000_000)
            except ValueError:
                continue
            if produced is None or stamps > produced:
                produced = stamps
    return produced


def probe_source_material(
    ffmpeg: str, path: Path, source: StreamSource, start: Fraction, span: Fraction
) -> Fraction | None:
    """How much material a source stream really holds in ``[start, start + span)``.

    The times here are asset-local, so the seek is shifted by the stream's own
    container offset: exactly how the render graph addresses the same stream.
    """
    if source.stream_position is None:
        return None
    kind = "a" if source.kind.value == "audio" else "v"
    return measure_decoded_span(
        ffmpeg, path, kind, source.stream_position,
        max(start - source.input_start, ZERO), span,
    )


def probe_duration(ffprobe: str, path: Path) -> Fraction:
    """Container duration as an exact rational (parsed from ffprobe's text)."""
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_format", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError(f"cannot probe the rendered file: {exc}") from exc
    if proc.returncode != 0:
        raise RenderError(f"cannot probe the rendered file: {proc.stderr.strip()[:400]}")
    try:
        raw = json.loads(proc.stdout)["format"]["duration"]
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise RenderError("ffprobe did not report a duration for the rendered file") from exc
    return Fraction(raw)


#: Speakers each claimed output layout names, used to check the produced file
#: kept the channel count the project asked for.
LAYOUT_CHANNELS = {
    "mono": 1,
    "stereo": 2,
    "2.1": 3,
    "3.0": 3,
    "5.0": 5,
    "5.1": 6,
    "7.0": 7,
    "7.1": 8,
}


@dataclass(frozen=True)
class StreamFacts:
    """One stream of a produced file, counted from its own packets."""

    kind: str
    codec: str | None
    packets: int
    duration: Fraction | None
    sample_rate: int | None = None
    channels: int | None = None
    channel_layout: str | None = None
    width: int | None = None
    height: int | None = None
    #: which stream of its kind this is in the file, for FFmpeg-style addressing
    position: int = 0

    def describe(self) -> str:
        if self.kind == "video":
            return f"{self.codec} {self.width}x{self.height}, {self.packets} frame(s)"
        return (
            f"{self.codec} {self.sample_rate} Hz {self.channel_layout or '?'}"
            f", {self.packets} packet(s)"
        )


def probe_streams(ffprobe: str, path: Path) -> tuple[StreamFacts, ...]:
    """Per-stream facts of a produced file.

    Packets are *counted* (``-count_packets``), not read from a header the muxer
    wrote: a truncated or truncated-then-renamed file still reports a plausible
    container duration, which is exactly why ``format.duration`` alone cannot
    tell a complete render from a broken one.
    """
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-count_packets", "-show_streams", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError(f"cannot probe the rendered file: {exc}") from exc
    if proc.returncode != 0:
        raise RenderError(f"cannot probe the rendered file: {proc.stderr.strip()[:400]}")
    try:
        entries = json.loads(proc.stdout)["streams"]
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RenderError("ffprobe returned no stream information for the rendered file") from exc
    facts = []
    seen: dict[str, int] = {}
    for entry in entries:
        packets = entry.get("nb_read_packets")
        if packets is None:
            raise RenderError(
                f"ffprobe could not count the packets of the rendered file's "
                f"{entry.get('codec_type', 'unknown')} stream; the file is damaged"
            )
        raw_duration = entry.get("duration")
        kind = str(entry.get("codec_type", ""))
        position = seen.get(kind, 0)
        seen[kind] = position + 1
        facts.append(
            StreamFacts(
                kind=kind,
                codec=entry.get("codec_name"),
                packets=int(packets),
                duration=Fraction(raw_duration) if raw_duration else None,
                sample_rate=int(entry["sample_rate"]) if entry.get("sample_rate") else None,
                channels=int(entry["channels"]) if entry.get("channels") else None,
                channel_layout=entry.get("channel_layout"),
                width=entry.get("width"),
                height=entry.get("height"),
                position=position,
            )
        )
    if not facts:
        raise RenderError(
            f"{path} contains no readable streams: the rendered file is truncated or corrupt"
        )
    return tuple(facts)


@dataclass(frozen=True)
class OutputFacts:
    """What a produced file really holds, as measured rather than as claimed."""

    video: StreamFacts
    audio: StreamFacts
    #: sound samples per channel, measured by decoding the produced file
    audio_samples: int

    def describe(self) -> str:
        return (
            f"{self.video.describe()}; {self.audio.describe()}, "
            f"{self.audio_samples} sample(s)"
        )


def verify_output(ffprobe: str, ffmpeg: str, path: Path, plan: RenderPlan) -> OutputFacts:
    """Probe a produced file, measure its sound, and check it against the plan.

    Returns what the file was found to hold, so the caller reports a measurement
    instead of restating what was asked for.
    """
    streams = probe_streams(ffprobe, path)
    audio = next((s for s in streams if s.kind == "audio"), None)
    decoded = (
        measure_decoded_span(ffmpeg, path, "a", audio.position) if audio is not None else None
    )
    validate_output(plan, streams, decoded)
    assert audio is not None  # validate_output refused otherwise
    video = next(s for s in streams if s.kind == "video")
    samples = round(float(decoded * plan.quantisation.sample_rate))
    return OutputFacts(video=video, audio=audio, audio_samples=samples)


def validate_output(
    plan: RenderPlan, streams: tuple[StreamFacts, ...], audio_seconds: Fraction | None
) -> None:
    """Refuse a produced file that does not carry what the plan promised.

    Each stream is checked on its own terms: picture by counted frames and
    geometry, sound by the samples that can actually be decoded from it. A
    container that simply *claims* the right duration passes nothing, and neither
    does one whose packet arithmetic looks right — packet sizes are a container
    detail (PCM in Matroska measured 3840 samples per packet, AAC 1024), so the
    sound is measured by decoding it. The slack allowed is what encoders are known to
    add and nothing more: AAC priming and tail padding measured +256 samples at
    48 kHz and +888 at 44.1 kHz for a 2 s render, while the lossless intermediate
    is exact.
    """
    video = next((s for s in streams if s.kind == "video"), None)
    audio = next((s for s in streams if s.kind == "audio"), None)
    if video is None or audio is None:
        missing = ", ".join(
            kind for kind, stream in (("picture", video), ("sound", audio)) if stream is None
        )
        raise RenderError(
            f"the rendered file has no {missing} stream: rendering did not produce what the "
            "composition contains"
        )
    if video.width != plan.width or video.height != plan.height:
        raise RenderError(
            f"the rendered picture is {video.width}x{video.height}, but the composition asks "
            f"for {plan.width}x{plan.height}"
        )
    if video.packets != plan.total_frames:
        raise RenderError(
            f"the rendered picture has {video.packets} frame(s), but the plan calls for "
            f"{plan.total_frames}; material was lost or added"
        )
    if audio.packets <= 0:
        raise RenderError(
            "the rendered file's sound stream holds no packets: the composition has audio "
            "and nothing was written"
        )
    if audio_seconds is None:
        raise RenderError(
            "the rendered file's sound cannot be decoded: it is truncated, damaged, or "
            "undecodable with this FFmpeg"
        )
    want = plan.total_samples
    rate = plan.quantisation.sample_rate
    produced = audio_seconds * rate
    short_by = want - produced
    long_by = produced - want
    if short_by > 1 or long_by > AUDIO_PADDING_SECONDS * rate:
        raise RenderError(
            f"the rendered sound decodes to {float(produced):.0f} samples at {rate} Hz "
            f"({float(audio_seconds):.3f}s), but the plan calls for {want} "
            f"({float(F(want) / rate):.3f}s); up to {float(AUDIO_PADDING_SECONDS) * 1000:.0f} "
            "ms of encoder padding is allowed, never missing material"
        )
    if audio.sample_rate != plan.quantisation.sample_rate:
        raise RenderError(
            f"the rendered sound is {audio.sample_rate} Hz, but the composition asks for "
            f"{plan.quantisation.sample_rate} Hz"
        )
    want_channels = LAYOUT_CHANNELS.get(plan.channel_layout)
    if want_channels is not None and audio.channels not in (None, want_channels):
        raise RenderError(
            f"the rendered sound has {audio.channels} channel(s), but the composition asks "
            f"for {plan.channel_layout} ({want_channels})"
        )


def _run(
    job: RenderJob,
    on_progress: ProgressCallback | None,
    cancel_event: threading.Event | None,
) -> None:
    """Run one FFmpeg job, streaming progress and honouring cancellation."""
    try:
        proc = subprocess.Popen(
            job.args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except OSError as exc:  # pragma: no cover - environment specific
        raise RenderError(f"cannot start ffmpeg: {exc}") from exc
    assert proc.stdout is not None and proc.stderr is not None
    total = float(job.duration)
    try:
        for line in proc.stdout:
            if cancel_event is not None and cancel_event.is_set():
                _terminate(proc)
                raise RenderCancelled("render cancelled")
            line = line.strip()
            if on_progress is None or total <= 0:
                continue
            microseconds = _progress_microseconds(line)
            if microseconds is not None:
                on_progress(min(1.0, microseconds / 1_000_000 / total))
        proc.wait()
    except RenderCancelled:
        raise
    except BaseException:  # never leave a stray ffmpeg behind
        _terminate(proc)
        raise
    if proc.returncode != 0:
        raise RenderError(_ffmpeg_message(proc.stderr.read().strip()))
    if on_progress is not None:
        on_progress(1.0)


def _progress_microseconds(line: str) -> int | None:
    key, _, value = line.partition("=")
    if key == "out_time_us":
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - stubborn ffmpeg
        proc.kill()
        proc.wait(timeout=5)


def _ffmpeg_message(stderr: str) -> str:
    """Useful, bounded FFmpeg diagnostics (never media payloads)."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    tail = " | ".join(lines[-4:]) if lines else "no diagnostic output"
    return f"ffmpeg failed: {tail[:600]}"

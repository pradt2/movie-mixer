#!/usr/bin/env python3
"""Developer-only picture-phase diagnostic (TASK_12i evidence; not a product path).

For one fixture and composition this prints three answers to the same question,
"which picture does global output frame *i* show?"

``oracle``
    the fixed contract, computed in ``tests/picture_reference.py`` from the
    fixture's **decoded** PTS list with exact rational arithmetic. It never asks
    a plan, a filter chain or FFmpeg's ``fps`` filter what they intend.
``A`` - fixed origin
    ONE render of the composition from the clip's own origin, read at global
    frame indices: every index is obtained by trimming this one file, never by
    seeking again.
``B`` - independently sought ranges
    separate renders of sub-ranges, each with its own input ``-ss``, mapped back
    to the **same global indices** by the output lattice
    (``ceil(start * R) .. ceil(end * R) - 1``, computed here).

Every disagreement names the global index, the exact source time the grid asked
for, the real picture timestamps on both sides of it, the identity each side
actually shows, the distance to the nearer real timestamp, and the raw FFmpeg
arguments (the input ``-ss``/``-t`` group and the picture filter chain) that
produced each side.

Classes:

* ``tail-hold`` / ``head-hold`` - the grid asked past (or before) the newest (or
  oldest) real picture of the slice: the expected edge behaviour, not a fault.
* ``retiming`` - the grid asked for real material and a different picture arrived.
* ``phase/join`` - A and B show different pictures at the same global index.
* ``frame-count`` / ``coverage`` - a side did not produce the frames the lattice says.

Ties are quantified, never budgeted: per case the diagnostic states the
container tick, how many grid points sit within one tick of a real picture
boundary, the exact distance values with their counts, and whether the two
production paths agree at those indices. No allowance is ever expressed as a
number of frames.

Everything decoded is a short generated fixture (50-150 pictures at 64x48),
never a film, and each fixture is decoded exactly once (``picture_facts`` caches).

Usage
-----
    python scripts/diagnostics/picture_phase.py --list
    python scripts/diagnostics/picture_phase.py --case all
    python scripts/diagnostics/picture_phase.py --case cfr-frac-25_24 --showinfo
    python scripts/diagnostics/picture_phase.py --case cfr-25_24 --stills

Keep the output in a log:

    python scripts/diagnostics/picture_phase.py --case all > /tmp/m12i/matrix.log 2>&1
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from fractions import Fraction as F
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from media_fixtures import (  # noqa: E402
    HAS_FFMPEG,
    identity_at,
    make_identity_bframes,
    make_identity_offset,
    make_identity_vfr,
    make_identity_video,
    picture_facts,
    source_from_probe,
)
from picture_reference import (  # noqa: E402
    DecodedStream,
    PictureReference,
    PlacedClip,
    compare,
    output_frames,
    path_disagreements,
    range_frame_indices,
)
from video_editor.core.commands import AddClipCommand, RegisterSourceCommand  # noqa: E402
from video_editor.core.model import OutputSettings, Project, Track, TrackKind  # noqa: E402
from video_editor.media.framegrab import (  # noqa: E402
    StillError,
    extract_composition_still,
    stream_timing,
)
from video_editor.render.graph import LOSSLESS_INTERMEDIATE, build_job  # noqa: E402
from video_editor.render.plan import build_plan  # noqa: E402

FFMPEG = "ffmpeg"
RATE = F(25)  # the output rate every case is measured on
ZERO = F(0)

FIXTURE_MAKERS = {
    "cfr": lambda tmp: make_identity_video(tmp, name="identity.mkv", count=150),
    "cfr50": lambda tmp: make_identity_video(tmp, name="identity50.mkv", count=50),
    "bframes": lambda tmp: make_identity_bframes(tmp, name="identity_bframes.mp4", count=100),
    "offset": lambda tmp: make_identity_offset(tmp, name="identity_offset.mkv", count=100),
    "vfr": lambda tmp: make_identity_vfr(tmp, name="identity_vfr.mkv"),
}


@dataclass(frozen=True)
class Case:
    """One composition on one short fixture: a source slice and a tempo."""

    name: str
    fixture: str
    source_in: F
    source_out: F
    rate: F
    note: str = ""


CASES: tuple[Case, ...] = (
    # the rate matrix, all on one 150-picture constant-rate fixture
    Case("cfr-rate1", "cfr", F(1), F(6), F(1), "rate 1: every grid point is a frame time"),
    Case("cfr-25_24", "cfr", F(1), F(6), F(25, 24), "24 fps material in a 25 fps timeline"),
    Case("cfr-24_25", "cfr", F(1), F(6), F(24, 25), "25 fps material shown slower"),
    Case("cfr-1000_1001", "cfr", F(1), F(6), F(1000, 1001), "0.1% speed-up"),
    Case("cfr-rate4", "cfr", F(1), F(6), F(4), "4x fast forward"),
    Case("cfr-rate1_4", "cfr", F(1), F(3), F(1, 4), "4x slow motion"),
    # fractional in/out points
    Case("cfr-frac-in", "cfr", F(1, 3), F(5), F(1), "fractional in-point between frames"),
    Case("cfr-frac-25_24", "cfr", F(1, 3), F(6), F(25, 24), "the reviewer's join case"),
    Case("cfr-frac-1001_1000", "cfr", F(1, 3), F(6), F(1001, 1000), "fractional in-point, slowed"),
    # hand-worked edge shapes on a 50-picture fixture
    Case("cfr-tail-5_4", "cfr50", F(40, 25), F(50, 25), F(5, 4), "tail stops at the last frame"),
    Case("cfr-held-1_2", "cfr50", F(48, 25), F(50, 25), F(1, 2), "tail holds the last frame"),
    # real B-frame MP4 (measured census 22 I + 40 B + 38 P, container tick 1/12800)
    Case("bframes-25_24", "bframes", F(1, 2), F(4), F(25, 24), "B-frames, fine container tick"),
    Case("bframes-rate1", "bframes", F(1, 2), F(4), F(1), "B-frames at rate 1"),
    Case("bframes-24_25", "bframes", F(1, 2), F(4), F(24, 25), "B-frames slowed"),
    # variable frame rate: 1/25, then 83/1000 and 21/250
    Case("vfr-5_4", "vfr", F(3), F(8), F(5, 4), "VFR material, sped up"),
    Case("vfr-rate1", "vfr", F(3), F(8), F(1), "VFR material at rate 1"),
    # nonzero stream origin: pictures run 5/2 .. 323/50
    Case("offset-25_24", "offset", F(7, 2), F(13, 2), F(25, 24), "stream starts at 5/2"),
    Case("offset-rate1", "offset", F(11, 2), F(13, 2), F(1), "nonzero origin at rate 1"),
)


# ------------------------------------------------------------- production renders


def build_project(path: Path, frame_rate: F, size: tuple[int, int] = (64, 48)) -> Project:
    """A one-track project around one real stream, registered by probing it."""
    project = Project(output=OutputSettings(size[0], size[1], frame_rate, 48000, "stereo"))
    project = RegisterSourceCommand(source_from_probe(path, 0, "src")).apply(project)
    return project.with_tracks((Track("v1", TrackKind.VIDEO, "V1"),))


def place(project: Project, clip: PlacedClip) -> Project:
    return AddClipCommand(
        asset_id="src",
        stream_index=0,
        track_id="v1",
        output_start=clip.output_start,
        source_in=clip.source_in,
        source_out=clip.source_out,
        rate=clip.rate,
        clip_id=clip.clip_id,
    ).apply(project)


def time_base_lookup(paths: dict[str, Path]):
    """The real container time base, as the application gives the graph."""

    def lookup(asset_id: str, stream_index: int) -> F | None:
        try:
            return stream_timing(paths[asset_id], stream_index).time_base
        except Exception:
            return None

    return lookup


def split_stages(chain: str) -> list[str]:
    """Split an FFmpeg filter chain at top-level commas only.

    ``scale`` expressions contain commas inside quotes and parentheses, so a naive
    ``split(",")`` cuts them in half and leaves half of a ``scale`` behind.
    """
    stages: list[str] = []
    current = ""
    depth, quoted = 0, False
    for char in chain:
        if char == "'":
            quoted = not quoted
        if not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                stages.append(current)
                current = ""
                continue
        current += char
    if current:
        stages.append(current)
    return stages


def phase_chain(chain: str) -> str:
    """The stages of a picture chain that decide *which* picture, geometry elided.

    ``scale``/``setsar``/``pad``/``format`` decide where the picture sits and what
    its pixels are; they are the same on every side of every case here and would
    only push the arguments that matter past the width of a terminal, so they are
    dropped by name and the elision is said.
    """
    kept = [
        stage for stage in split_stages(chain)
        if not stage.startswith(("scale=", "setsar", "pad=", "format="))
    ]
    return ",".join(kept) + ",<scale/setsar/pad/format elided>"


def showinfo_stamps(args: list[str], dest: Path) -> list[str]:
    """Re-run the same graph with ``showinfo`` just before each sampling stage.

    Diagnostic instrumentation only: it renders to its own file and never changes
    what the measured render wrote. It prints the timestamps the chain really
    carries going into ``fps``, which is what turns "the picture moved by one"
    into an arithmetic statement about where those stamps fell relative to the
    sampling grid.
    """
    cut = args.index("-filter_complex")
    head, graph, tail = args[:cut], args[cut + 1], args[cut + 2:]
    # showinfo logs at FFmpeg's info level, and production renders quietly
    loud = [x if (i == 0 or head[i - 1] != "-loglevel" or x != "error") else "info"
            for i, x in enumerate(head)]
    shown = ";".join(
        part.replace("fps=round=up", "showinfo,fps=round=up", 1) for part in graph.split(";")
    )
    probe = dest.with_name(dest.stem + "_showinfo.mkv")
    proc = subprocess.run(
        [*loud, "-filter_complex", shown, *tail[:-1], str(probe)],
        capture_output=True,
        timeout=600,
    )
    stamps: list[str] = []
    for line in proc.stderr.decode("utf-8", "replace").splitlines():
        if "pts_time" not in line or "n:" not in line:
            continue
        fields = dict(re.findall(r"(\w+):\s*(\S+)", line))
        stamps.append(f"frame entering fps: n={fields.get('n', '-')} "
                      f"pts={fields.get('pts', '-')} pts_time={fields['pts_time']} "
                      f"tb={fields.get('tb', '-')} duration={fields.get('duration', '-')}")
    if not stamps:
        stamps.append(f"showinfo gave nothing (exit {proc.returncode}): "
                      f"{proc.stderr.decode('utf-8', 'replace')[:300]}")
    return stamps


@dataclass
class Side:
    """One production render: what it was asked for and the pictures it wrote."""

    label: str
    start: F
    end: F
    first_index: int
    plan_frames: int
    lattice_frames: int
    ids: list[int | None]
    seek: str
    chain: str
    stamps: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    case: Case
    total: int
    tick: F
    ref: PictureReference
    oracle_holds: list[tuple[str, int, int]]
    sides: list[Side] = field(default_factory=list)
    #: ``(side label, mismatch class, global index)``, -1 meaning a structural note
    mismatches: list[tuple[str, str, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    distances: dict[F, int] = field(default_factory=dict)
    stills: list[str] = field(default_factory=list)

    def count(self, kind: str, side: str | None = None) -> int:
        return sum(1 for s, k, _ in self.mismatches if k == kind and (side is None or s == side))

    def sides_used(self) -> list[str]:
        return sorted({s for s, _, _ in self.mismatches})

    def indices(self, kind: str) -> list[int]:
        return sorted({i for _, k, i in self.mismatches if k == kind and i >= 0})

    def kinds(self, side: str | None = None) -> list[str]:
        return sorted({k for s, k, _ in self.mismatches if (side is None or s == side)})

    def class_counts(self, sides: list[str]) -> dict[str, int]:
        """How many frames of each class those sides show, phase/join excluded."""
        counts: dict[str, int] = {}
        for s, k, _ in self.mismatches:
            if s in sides and k != "phase/join":
                counts[k] = counts.get(k, 0) + 1
        return counts

    @property
    def a_counts(self) -> dict[str, int]:
        return self.class_counts(["A"])

    @property
    def b_counts(self) -> dict[str, int]:
        return self.class_counts([s for s in self.sides_used() if s != "A"])

    @property
    def a_total(self) -> int:
        return sum(self.a_counts.values())

    @property
    def b_total(self) -> int:
        return sum(self.b_counts.values())


def render_side(
    label: str,
    project: Project,
    paths: dict[str, Path],
    start: F,
    end: F,
    first_index: int,
    expected_frames: int,
    dest: Path,
    showinfo: bool,
) -> Side:
    """Render one range losslessly and read the identities out of its pictures.

    ``None`` in ``ids`` is the composition's own black (every byte zero), which a
    fixture frame never is (``media_fixtures.IDENTITY_LIFT``).
    """
    plan = build_plan(project, start, end)
    sources = {(s.asset_id, s.stream_index): s for s in project.sources}
    job = build_job(
        plan, paths, sources, dest, FFMPEG, LOSSLESS_INTERMEDIATE,
        time_base_of=time_base_lookup(paths),
    )
    args = job.args
    cut = args.index("-filter_complex")
    inputs, graph = args[:cut], args[cut + 1]
    proc = subprocess.run(args, capture_output=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{label}: render of [{start}, {end}) failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')[:500]}"
        )
    raw = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(dest), "-map", "0:v:0",
         "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
        timeout=600,
    )
    if raw.returncode != 0:
        raise RuntimeError(f"{label}: cannot read back {dest}: {raw.stderr.decode()[:300]}")
    stride = plan.width * plan.height * 3
    frames = [raw.stdout[i * stride:(i + 1) * stride] for i in range(len(raw.stdout) // stride)]
    ids = [
        None if all(byte == 0 for byte in frame)
        else identity_at(frame, plan.width, plan.height)
        for frame in frames
    ]
    picture = [part for part in graph.split(";") if "fps=" in part]
    seek = next((inputs[i + 1] for i, a in enumerate(inputs) if a == "-ss"), "-")
    read = next((inputs[i + 1] for i, a in enumerate(inputs) if a == "-t"), "-")
    side = Side(
        label=label,
        start=start,
        end=end,
        first_index=first_index,
        plan_frames=plan.total_frames,
        lattice_frames=expected_frames,
        ids=ids,
        seek=f"-ss {seek} -t {read}",
        chain=phase_chain(picture[0] if picture else graph),
    )
    if showinfo and picture:
        side.stamps = showinfo_stamps(args, dest)
    return side


# ----------------------------------------------------------------------- the work


def verify_fixture(facts, name: str) -> None:
    """A fixture that does not hold what its name claims makes every row meaningless."""
    problems = []
    if facts.identities != tuple(range(facts.count)):
        problems.append("decoded frames do not carry their own numbers")
    if facts.declared_frames not in (None, facts.count):
        problems.append(f"container declares {facts.declared_frames} frames, {facts.count} decode")
    if problems:
        raise RuntimeError(f"fixture {name}: " + "; ".join(problems))


def split_points(clip: PlacedClip, scheme: str) -> list[F]:
    """Where the independently sought ranges are cut, in output time."""
    if scheme == "halves":
        return [F(output_frames(clip.output_end, RATE) // 2, RATE)]  # a frame's own instant
    return [clip.output_end / 3, 2 * clip.output_end / 3]  # arbitrary points, not frame times


def describe(ref: PictureReference, mismatch, side: str) -> str:
    """One mismatch, in the terms the reviewer asked for: index, wanted, candidates."""
    pick = ref.pick(mismatch.index)
    return (
        f"  {side:<9} index {mismatch.index:>4}: wanted {mismatch.wanted} "
        f"({float(mismatch.wanted):.6f} s), real PTS either side "
        f"{pick.prev_pts} / {pick.next_pts}; oracle {mismatch.expected} "
        f"(its PTS {pick.pts}, {pick.kind}); {side} shows {mismatch.observed}; "
        f"class {mismatch.kind}, offset {mismatch.offset} picture(s), "
        f"tie distance {mismatch.tie_distance}"
    )


def run_case(
    case: Case, facts, out_dir: Path, schemes: list[str], showinfo: bool, stills: bool
) -> CaseResult:
    stream = DecodedStream(pts=facts.pictures, identities=facts.identities,
                           time_base=facts.time_base)
    clip = PlacedClip("clip", case.source_in, case.source_out, ZERO, case.rate)
    ref = PictureReference(stream, RATE, (clip,))
    total = output_frames(clip.output_end, RATE)
    result = CaseResult(
        case=case,
        total=total,
        tick=facts.time_base,
        ref=ref,
        oracle_holds=ref.holds(0, total),
    )
    for index in range(total):
        tie = ref.tie(index)
        if tie and tie.distance is not None:
            result.distances[tie.distance] = result.distances.get(tie.distance, 0) + 1

    project = place(build_project(facts.path, RATE), clip)
    paths = {"src": facts.path}

    whole = render_side("A", project, paths, ZERO, clip.output_end, 0, total,
                        out_dir / f"{case.name}_whole.mkv", showinfo)
    result.sides.append(whole)
    if whole.plan_frames != whole.lattice_frames or len(whole.ids) != whole.lattice_frames:
        result.mismatches.append(("A", "frame-count", -1))
        result.notes.append(
            f"  A         frame count: the plan makes {whole.plan_frames} frames, the "
            f"rendered file holds {len(whole.ids)}, the lattice says "
            f"{whole.lattice_frames} (global indices 0..{total - 1})"
        )
    for m in compare(ref, whole.ids, "oracle vs A"):
        result.mismatches.append(("A", m.kind, m.index))
        result.notes.append(describe(ref, m, "A"))

    for scheme in schemes:
        points = [ZERO, *split_points(clip, scheme), clip.output_end]
        for n, (a, b) in enumerate(pairwise(points)):
            first, last = range_frame_indices(a, b, RATE)
            label = f"B-{scheme}{n}"
            side = render_side(label, project, paths, a, b, first, last - first,
                               out_dir / f"{case.name}_{scheme}{n}.mkv", showinfo)
            result.sides.append(side)
            rendered = len(side.ids)
            if side.plan_frames != side.lattice_frames or rendered != side.lattice_frames:
                result.mismatches.append((label, "frame-count", -1))
                result.notes.append(
                    f"  {label:<9} frame count for [{a}, {b}): plan {side.plan_frames}, "
                    f"file holds {rendered}, the lattice says {side.lattice_frames} "
                    f"(global indices {first}..{last - 1})"
                )
            for m in compare(ref, side.ids, f"oracle vs {label}", first_index=first):
                result.mismatches.append((label, m.kind, m.index))
                edge = (
                    "  <- the last frame of this range"
                    if m.index == last - 1 else
                    ("  <- the first frame of this range" if m.index == first else "")
                )
                result.notes.append(describe(ref, m, label) + edge)
            disagreements, structural = path_disagreements(whole.ids, 0, side.ids, first)
            for note in structural:
                if "no global index" in note:
                    result.mismatches.append((label, "frame-count", -1))
                    result.notes.append(f"  {label:<9} range [{a}, {b}): {note}")
                else:
                    # a sub-range covering fewer indices than A is what a sub-range does;
                    # it is said, but it is not a mismatch
                    result.notes.append(f"  {label:<9} note: {note}")
            for d in disagreements:
                result.mismatches.append((label, "phase/join", d.index))
                pick = ref.pick(d.index)
                edge = "  <- the last frame of this range" if d.index == last - 1 else ""
                result.notes.append(
                    f"  {label:<9} index {d.index:>4} (range [{a}, {b}), first index "
                    f"{first}): A shows {d.reference}, {label} shows {d.sought}, oracle "
                    f"{pick.identity} (wanted {pick.wanted} between real PTS "
                    f"{pick.prev_pts} / {pick.next_pts}, tie distance "
                    f"{ref.tie(d.index).distance}) - the two paths disagree{edge}"
                )

    if stills:
        result.stills = probe_stills(case, project, paths, total)
    return result


def probe_stills(case: Case, project: Project, paths: dict[str, Path], total: int) -> list[str]:
    """What the viewer's still path says at the composition's own frame instants."""
    out: list[str] = []
    for index in sorted({i for i in (0, 1, total // 2, total - 2, total - 1)
                         if 0 <= i < total}):
        moment = F(index, RATE)
        try:
            still = extract_composition_still(project, paths, moment)
            identity = identity_at(still.rgb, still.width, still.height)
            out.append(
                f"  still at {float(moment):.6f} s (index {index}): shows {identity}, "
                f"frame_index {still.frame_index}, reported source PTS "
                f"{still.decoded_source_pts}, grid_exact {still.grid_exact}"
            )
        except StillError as exc:
            out.append(f"  still at {float(moment):.6f} s (index {index}): StillError: "
                       f"{str(exc).splitlines()[0]}")
        except Exception as exc:  # a diagnostic reports a failure, it does not hide it
            out.append(f"  still at {float(moment):.6f} s (index {index}): "
                       f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
    return out


def print_case(result: CaseResult) -> None:
    case = result.case
    print(f"\n== {case.name}: [{case.source_in}, {case.source_out}) rate {case.rate} on "
          f"{case.fixture}; {result.total} output frames; holds in the reference: "
          f"{result.oracle_holds or 'none'}")
    print(f"   note: {case.note}")
    if result.total <= 24:
        # short enough to read whole: the whole point of the small fixtures is that
        # these tables can be checked by hand against the case above
        print(f"      oracle identities:  {result.ref.expected_identities(0, result.total)}")
        for side in result.sides:
            print(f"      {side.label} identities:      {side.ids}")
    for side in result.sides:
        print(f"   {side.label} range [{side.start}, {side.end}) -> global indices "
              f"{side.first_index}..{side.first_index + len(side.ids) - 1} "
              f"({len(side.ids)} frames rendered, lattice {side.lattice_frames}) {side.seek}")
        print(f"      chain: {side.chain}")
        for stamp in side.stamps:
            print(f"      {stamp}")
    for note in result.notes:
        print(note)
    for line in result.stills:
        print(line)


def _fmt(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}={n}" for k, n in sorted(counts.items())) or "identical"


def print_matrix(results: list[CaseResult]) -> None:
    print("\n" + "=" * 96)
    print("MATRIX  (per side: classes of frames that differ from the oracle; "
          "'A vs B' = same global index, different picture)")
    print(f"{'case':<20} {'frames':>6}  {'oracle vs A':<30} {'oracle vs B':<30} A vs B")
    for result in results:
        print(f"{result.case.name:<20} {result.total:>6}  {_fmt(result.a_counts):<30} "
              f"{_fmt(result.b_counts):<30} {result.count('phase/join')}")


def print_smallest(results: list[CaseResult]) -> None:
    print("\nSMALLEST REPRODUCTION PER MISMATCH CLASS")
    classes = sorted({k for r in results for _, k, _ in r.mismatches})
    if not classes:
        print("  (no mismatch of any class in the cases that ran)")
        return
    for kind in classes:
        holders = [r for r in results if r.count(kind)]
        smallest = min(holders, key=lambda r: r.total)
        print(f"  {kind:<16} {smallest.case.name} "
              f"[{smallest.case.source_in}, {smallest.case.source_out}) rate "
              f"{smallest.case.rate} on {smallest.case.fixture}, {smallest.total} frames: "
              f"indices {smallest.indices(kind)[:8]}")
        print(f"      reproduce: python scripts/diagnostics/picture_phase.py "
              f"--case {smallest.case.name}")


#: classes that are the contract's own edge behaviour rather than a production defect
HOLDS = ("tail-hold", "head-hold")


def print_stable_origin(results: list[CaseResult]) -> None:
    print("\nDOES A STABLE ORIGIN + GLOBAL-INDEX TRIM REMOVE THE MISMATCHES?")
    print("   A = frames where the fixed-origin render differs from the oracle; "
          "B = the same summed\n   over every independently sought range rendered; "
          "join = frames where A and B\n   disagree with each other. Tail and head "
          "holds are the contract's edge behaviour, so\n   they are excluded here and "
          "shown by class in the MATRIX and in each case's detail.")
    print(f"{'case':<20} {'frames':>6}  {'A':>4}  {'B':>4}  {'join':>5}  reading")
    for result in results:
        a = sum(n for k, n in result.a_counts.items() if k not in HOLDS)
        b = sum(n for k, n in result.b_counts.items() if k not in HOLDS)
        phase = result.count("phase/join")
        holds = sum(n for k, n in {**result.a_counts, **result.b_counts}.items() if k in HOLDS)
        if a == 0 and b == 0 and phase == 0:
            verdict = "nothing to remove: every path already matches the oracle"
        elif phase == 0 and (a or b):
            verdict = "A and B agree with each other; the fixed origin still differs"
        elif a == 0:
            verdict = "YES: A equals the oracle at every index, so trimming A answers correctly"
        else:
            verdict = "NO: A differs from the oracle itself, so trimming A keeps those frames"
        if holds:
            verdict += f" (+{holds} expected edge-hold frame(s))"
        print(f"{result.case.name:<20} {result.total:>6}  {a:>4}  {b:>4}  {phase:>5}  {verdict}")


def print_ties(results: list[CaseResult]) -> None:
    print("\nTIE ANALYSIS (distance from a sampling time to the nearer real picture boundary)")
    print("The stated tolerance is ONE container tick of the fixture; it is never a "
          "number of frames.")
    for result in results:
        within = [
            (index, tie.distance)
            for index in range(result.total)
            if (tie := result.ref.tie(index)) is not None
            and tie.distance is not None and tie.distance <= result.tick
        ]
        phase = result.indices("phase/join")
        retiming = sorted(
            {i for s, k, i in result.mismatches if k == "retiming"}
        )
        print(f"  {result.case.name:<20} tick {result.tick}: {len(within):>4} of "
              f"{result.total} grid points within one tick of a real frame boundary"
              + (f" (indices {[i for i, _ in within][:12]}"
                 f"{' ...' if len(within) > 12 else ''})" if within else ""))
        histogram = ", ".join(f"{d} x{n}" for d, n in sorted(result.distances.items()))
        print(f"      exact distances, as ``distance x count``: {histogram}")
        if within:
            agree = all(
                _sides_agree(result, index) for index, _ in within
            )
            print(f"      the two production paths agree at every one of those indices: {agree}")
        for label, index_list in (("phase/join", phase), ("retiming", retiming)):
            if index_list:
                print(f"      {label} frames and their tie distances: "
                      + ", ".join(
                          f"{i}={result.ref.tie(i).distance}" for i in index_list[:12]
                      ) + (" ..." if len(index_list) > 12 else ""))


def _sides_agree(result: CaseResult, index: int) -> bool:
    """Do all B ranges show what A shows at this global index?"""
    a = next(s for s in result.sides if s.label == "A")
    reference = a.ids[index - a.first_index]
    for side in result.sides:
        if side.label == "A" or not side.first_index <= index < side.first_index + len(side.ids):
            continue
        if side.ids[index - side.first_index] != reference:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="picture-phase diagnostic (TASK_12i)")
    parser.add_argument("--case", default="all", help="comma list of case names, or 'all'")
    parser.add_argument("--list", action="store_true", help="print the case names and exit")
    parser.add_argument("--scratch", default="/tmp/opencode/m12i/diag",
                        help="where generated fixtures and rendered files go")
    parser.add_argument("--splits", default="halves,thirds",
                        help="independent-range schemes: 'halves' (frame-aligned join) "
                             "and/or 'thirds' (arbitrary cut points)")
    parser.add_argument("--stills", action="store_true",
                        help="also probe the still path at the composition's own frames")
    parser.add_argument("--showinfo", action="store_true",
                        help="re-run each chain with showinfo, printing the real retimed stamps")
    parser.add_argument("--brief", action="store_true",
                        help="summaries only: no per-case chain dumps or mismatch lines")
    args = parser.parse_args(argv)

    if args.list:
        for case in CASES:
            print(f"{case.name:<20} {case.fixture:<8} [{case.source_in}, {case.source_out}) "
                  f"rate {case.rate}  {case.note}")
        return 0
    if not HAS_FFMPEG:
        print("ffmpeg fixtures are not available: nothing to measure")
        return 2

    known = {c.name for c in CASES}
    wanted = known if args.case == "all" else {c.strip() for c in args.case.split(",")}
    unknown = wanted - known
    if unknown:
        print(f"unknown case(s): {', '.join(sorted(unknown))}")
        print("known: " + ", ".join(c.name for c in CASES))
        return 2
    schemes = [s for s in args.splits.split(",") if s]

    scratch = Path(args.scratch)
    fixture_dir = scratch / "fixtures"
    out_dir = scratch / "renders"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    version = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True,
                             timeout=30).stdout.splitlines()[0]
    started = time.time()
    print(f"picture-phase diagnostic - {version}")
    print(f"output rate R = {RATE}; scratch = {scratch}; split schemes = {schemes}")
    print("A = one render from the clip origin, trimmed by global index")
    print("B = independently sought ranges, mapped to the same global indices")

    fixtures: dict[str, object] = {}
    results: list[CaseResult] = []
    for case in CASES:
        if case.name not in wanted:
            continue
        if case.fixture not in fixtures:
            path = FIXTURE_MAKERS[case.fixture](fixture_dir)
            facts = picture_facts(path)
            verify_fixture(facts, case.fixture)
            fixtures[case.fixture] = facts
            print(f"\nfixture {case.fixture}: {facts.path.name}, {facts.count} pictures, "
                  f"{facts.codec}, tick {facts.time_base}, PTS {facts.pictures[0]} .. "
                  f"{facts.last}, spacings {facts.spacings}, census {facts.pict_types}")
        result = run_case(case, fixtures[case.fixture], out_dir, schemes,
                          args.showinfo, args.stills)
        results.append(result)
        if not args.brief:
            print_case(result)

    if not results:
        print("no cases ran")
        return 2
    print_matrix(results)
    print_smallest(results)
    print_stable_origin(results)
    print_ties(results)
    print(f"\nelapsed {time.time() - started:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

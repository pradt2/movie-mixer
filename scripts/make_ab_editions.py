#!/usr/bin/env python3
"""Generate the two synthetic editions and print the manual edit recipe.

Two files, one "cut" of the same invented film each:

* ``edition_a.mkv`` — 24 fps, six shots of two seconds in order
  (red, green, blue, magenta, yellow, cyan), each shot's own tone;
* ``edition_b.mkv`` — 25 fps, five of those shots of three seconds in a
  **different order** (cyan, magenta, white, red, green, blue), with a white
  shot that A does not have and without A's yellow shot.

Neither file is the reference: the point is to practise choosing picture from
one and sound from the other, by eye and by number, with no automated matching.
The printed recipe is the composition that
``scripts/desktop_checks/editions.py`` builds and then *measures* in the
rendered file, so you can reproduce it by hand in the GUI and compare.

    python scripts/make_ab_editions.py --dir /tmp/ab-editions
    python scripts/make_ab_editions.py --dir /tmp/ab-editions --quiet

Existing files are never overwritten without ``--force``. Nothing but these two
generated files is written.
"""

from __future__ import annotations

import argparse
import sys
from fractions import Fraction as F
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "desktop_checks"))

from editions import (  # noqa: E402
    A_RATE,
    ALL_ITEMS,
    B_ONLY,
    B_RATE,
    OUT_RATE,
    PICTURE,
    SHOTS,
    SIZE,
    SOUND,
    SR,
    UNDERNEATH,
    Editions,
    expected_picture,
    expected_sounds,
    make_editions,
    output_end,
)


def describe_fixtures() -> str:
    lines = [f"Edition A — {A_RATE} fps, 2 s per shot, in this order:"]
    for shot in SHOTS:
        lines.append(f"  {float(shot.a_start):>5.1f}-{float(shot.a_end):<5.1f} "
                     f"{shot.colour:<8} {shot.hz} Hz")
    lines.append(f"Edition B — {B_RATE} fps, 3 s per shot, reordered, with B-only footage:")
    order = [(B_ONLY[0], B_ONLY[1], B_ONLY[2], B_ONLY[3])]
    for shot in SHOTS:
        if shot.b_start is not None:
            order.append((shot.colour, shot.hz, shot.b_start, shot.b_end))
    for colour, hz, start, end in sorted(order, key=lambda row: row[2]):
        note = "  <- not in A" if colour == B_ONLY[0] else ""
        lines.append(f"  {float(start):>5.1f}-{float(end):<5.1f} {colour:<8} {hz} Hz{note}")
    missing = [shot.colour for shot in SHOTS if shot.b_start is None]
    lines.append(f"Dropped from B: {', '.join(missing)}")
    return "\n".join(lines)


def _seconds(value: F) -> str:
    return str(value) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _spans(pred, end: F, step: F = F(1, 4)) -> list[tuple[F, F]]:
    """Contiguous half-open output spans where ``pred(time)`` holds.

    Computed from the item table rather than written out in prose: a note about
    where the two sound tracks overlap is only worth printing if it is true of
    the clips that were actually built.
    """
    spans: list[tuple[F, F]] = []
    start: F | None = None
    when = F(0)
    while when < end:
        if pred(when):
            if start is None:
                start = when
        elif start is not None:
            spans.append((start, when))
            start = None
        when += step
    if start is not None:
        spans.append((start, end))
    return spans


def _format_spans(spans: list[tuple[F, F]]) -> str:
    return ", ".join(f"{_seconds(a)}-{_seconds(b)}" for a, b in spans) or "none"


def _two_tracks_sounding(when: F) -> bool:
    """True where two different sound tracks are active at once."""
    return len({item.track for item in expected_sounds(when)}) >= 2


def _picture_and_sound_disagree(when: F) -> bool:
    """True where the winning picture is from an edition no sound is playing from."""
    picture = expected_picture(when)
    sounding = {item.edition for item in expected_sounds(when)}
    return picture is not None and bool(sounding) and picture.edition not in sounding


def describe_recipe(editions: Editions) -> str:
    """The composition to build by hand, one line per clip."""
    lines = [
        f"Build this output by hand (output {SIZE[0]}x{SIZE[1]}, {OUT_RATE} fps, "
        f"{SR} Hz stereo, {output_end()} s):",
        "  tracks, top to bottom: 'picture' (video), 'under' (video),",
        "  'dub' (audio, edition B), 'orig' (audio, edition A)",
        "",
        f"  {'clip':<10} {'track':<8} {'source':<26} {'source in-out':<18} "
        f"{'output start':<13} rate  extras",
    ]
    for item in (*PICTURE, *UNDERNEATH, *SOUND):
        source_file = editions.a if item.edition == "a" else editions.b
        source = f"{source_file.name} #{item.stream}"
        span = f"{_seconds(item.src_in)}-{_seconds(item.src_out)}"
        extras = []
        rate = getattr(item, "rate", F(1))
        if rate != 1:
            extras.append(f"rate {_seconds(rate)}")
        gain = getattr(item, "gain", F(1))
        if gain != 1:
            extras.append(f"gain {_seconds(gain)}")
        fade_out = getattr(item, "fade_out", F(0))
        if fade_out:
            extras.append(f"fade out {_seconds(fade_out)} s")
        lines.append(
            f"  {item.clip_id:<10} {item.track:<8} {source:<26} {span:<18} "
            f"{_seconds(item.out_start):<13} {' '.join(extras) or '-'}"
        )
    lines += [
        "",
        "Notes that make the result checkable (all computed from the table above):",
        f"  * A picture from B: {', '.join(i.clip_id for i in PICTURE if i.edition == 'b')}.",
        f"  * A sound from B: {', '.join(i.clip_id for i in SOUND if i.edition == 'b')}"
        f" (the rest take A's), so picture and sound are chosen separately.",
        "  * Picture and sound from different editions: "
        + _format_spans(_spans(_picture_and_sound_disagree, output_end())),
        f"  * Covered video: {', '.join(i.clip_id for i in UNDERNEATH)} sits on a lower track and",
        "    must never be seen where the track above has material.",
        "  * Two sound tracks sounding together: "
        + _format_spans(_spans(_two_tracks_sounding, output_end())),
        "  * Picture with no sound at all (a gap): "
        + _format_spans(_spans(lambda when: not expected_sounds(when), output_end())),
        "  * Fades: " + (", ".join(
            f"{item.clip_id} out {_seconds(item.fade_out)} s"
            for item in SOUND if item.fade_out) or "none"),
        "  * Gains other than 1: " + (", ".join(
            f"{item.clip_id} {_seconds(item.gain)}"
            for item in SOUND if item.gain != 1) or "none"),
        "  * Rates other than 1: " + (", ".join(
            f"{item.clip_id} {_seconds(item.rate)}"
            for item in ALL_ITEMS if getattr(item, "rate", F(1)) != 1) or "none"),
        "",
        "Expect no white frame and no 880 Hz tone anywhere: that footage exists only",
        "in edition B and this output deliberately does not use it.",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, required=True,
                        help="directory to write edition_a.mkv and edition_b.mkv into")
    parser.add_argument("--force", action="store_true",
                        help="replace these files if they already exist")
    parser.add_argument("--quiet", action="store_true", help="print the paths only")
    args = parser.parse_args()

    directory: Path = args.dir
    target_a, target_b = directory / "edition_a.mkv", directory / "edition_b.mkv"
    existing = [path for path in (target_a, target_b) if path.exists()]
    if existing and not args.force:
        print(f"refusing to overwrite: {', '.join(str(p) for p in existing)}")
        print("pass --force to replace them")
        return 2

    editions = make_editions(directory)
    print(f"edition A: {editions.a}")
    print(f"edition B: {editions.b}")
    if args.quiet:
        return 0
    print()
    print(describe_fixtures())
    print()
    print(describe_recipe(editions))
    print()
    print("Then: video-editor, import both files, and build the output above")
    print("without any automated matching. To have the same composition built")
    print("and measured automatically: python scripts/ab_workflow_check.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

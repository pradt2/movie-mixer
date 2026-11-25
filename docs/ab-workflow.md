# The two-edition manual workflow

This is the motivating workflow of the project, written down so it can be
repeated by a person and checked by a machine. It uses no automated matching:
every correspondence between the two editions is decided by the editor.

## The two editions

`python scripts/make_ab_editions.py --dir DIR` generates them (it refuses to
overwrite without `--force`, and writes nothing else):

| | edition A | edition B |
|---|---|---|
| frame rate | 24 fps | 25 fps |
| shot length | 2 s each | 3 s each |
| running order | red, green, blue, magenta, yellow, cyan | cyan, magenta, **white**, red, green, blue |
| footage only here | yellow (660 Hz) | white (880 Hz) |
| streams | 0 = video, 1 = audio | 0 = video, 1 = audio |
| picture / sound | 128×96, 48 kHz stereo | 128×96, 48 kHz stereo |

Each shot is a solid colour with its own sine tone, so both channels of the
result are measurable: colour identifies the picture, frequency identifies the
sound. **Neither file is the reference.** A is not "the good picture" and B is
not "the soundtrack"; the output chooses per clip.

## The output to build by hand

Output settings: 128×96, 24 fps, 48 kHz stereo, 12 s. Tracks, top to bottom:
`picture` (video), `under` (video), `dub` (audio, edition B), `orig` (audio,
edition A). Times are exact seconds; `source in-out` is asset-local time in the
file named.

| clip | track | source | source in–out | output start | notes |
|---|---|---|---|---|---|
| p-red | picture | edition_a.mkv #0 | 0–2 | 0 | |
| p-green | picture | edition_a.mkv #0 | 2–4 | 2 | |
| p-blue | picture | **edition_b** #0 | 15–17 | 4 | B's picture, at 24 fps output |
| p-magenta | picture | edition_a.mkv #0 | 6–8 | 6 | |
| p-yellow | picture | edition_a.mkv #0 | 8–10 | 8 | footage B does not have |
| p-cyan | picture | **edition_b** #0 | 0–3 | 10 | rate **3/2**: 3 source s in 2 output s |
| p-under | **under** | edition_a.mkv #0 | 0–2 | 4 | must stay invisible: covered from 4–6 s |
| s-red | dub | **edition_b** #1 | 9–11 | 0 | B's sound under A's picture |
| s-green | dub | edition_b #1 | 12–14 | 2 | fade out over its last 1 s |
| s-blue | orig | edition_a #1 | 4–6 | 4 | |
| s-magenta | dub | edition_b #1 | 3–5 | 6 | |
| s-overlap | orig | edition_a #1 | 10–12 | 6 | two sound tracks together, 6–8 s |
| s-yellow | orig | edition_a #1 | 8–9 | 8 | gain **1/2**; ends at 9, leaving silence |
| s-cyan | dub | edition_b #1 | 0–2 | 10 | |

What makes the result checkable:

- picture and sound come from different editions in 0–6 s, so neither track can
  be "the" source;
- 6–8 s is a real sum of two editions' sound (550 Hz from B, 770 Hz from A);
- 9–10 s has picture (A's yellow) and **no** sound, a deliberate gap;
- 10–12 s runs B's cyan at rate 3/2, so output length ≠ source length;
- no white frame and no 880 Hz tone may appear anywhere: that is B's extra
  footage, unused on purpose;
- A's red at 4–6 s sits on a lower video track and must never be seen.

## How a person does it

1. Import both files; Project ▸ Media Status… confirms the table above
   (24 fps against 25 fps, 12 s against 18 s, one video and one audio stream
   each).
2. Set the output in the inspector: 128×96, `24`, 48000, stereo.
3. Add the four tracks and rename them. The video track order matters: `picture`
   above `under`.
4. For each row: select the stream in the bin, set the playhead to the output
   start (`G`, type the time), place with `Ctrl+G`, then correct the edges in the
   inspector with the exact source numbers. `p-cyan` needs its rate set to `3/2`.
5. `s-green` gets a fade out of 1 s and `s-yellow` a gain of −6.02 dB (1/2) in
   the inspector. `s-overlap` is placed on `orig` while `s-magenta` is already on
   `dub`: overlapping clips on *different* tracks is the mixing mechanism.
6. Review boundaries with short preview intervals typed into the output viewer —
   e.g. 3.5–4.5 s across the switch from A's picture to B's, and 5.5–6.5 s where
   the two sound tracks meet.
7. Export and compare. Expect: 12.000 s, 288 frames at 24 fps, 576 000 samples at
   48 kHz; the colour sequence red, green, blue, magenta, yellow, cyan; the tones
   220, 330 (fading out), 440, 550, 550+770, 660 at half level, silence, 770.

## What the automated version measures

`python scripts/ab_workflow_check.py` builds exactly this composition through the
same commands the UI uses and then decodes the rendered file and measures it:
the colour at every output frame (index-selected, not seeked), each tone's
spectral magnitude in sample-accurate windows, the 550/770 mix as a 0.50/0.50
share, the half-gain ratio (0.499 measured), the fade-out ramp measured inside the
ramp, silence in the gap, the frame/sample counts, a 5–7 s interval rendered on
its own, a refused interval outside the composition, a cancelled render leaving no
file and no temporary, a moved source refused then relinked, and the project
saved and reopened. The claims and the composition come from one table
(`scripts/desktop_checks/editions.py`: `PICTURE`, `UNDERNEATH`, `SOUND`), so the
check cannot drift away from the fixtures it built.

What it cannot do is judge how the result *looks and sounds*: a flash at a cut,
pitch shift after the rate change, a fade that pops. That is the human part, and
it is the reason this file exists.

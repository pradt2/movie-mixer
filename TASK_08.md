# TASK_08 — Manual editor completion and realistic workflow validation

Prerequisite: TASK_07. Read AGENTS.md. Validate the manual workflow as a coherent tool, fix blocking usability or correctness issues, and stop before assisted alignment.

## Implement
- Add bounded, lazy timeline thumbnails and audio waveform envelopes (downsample per visible scale, cache by stream and asset identity). Background extraction must support cancellation and remain responsive. Audio waveforms must reflect the selected stream and be positioned using actual asset-local time/rate; don't fabricate waveforms when decoding fails. Keep clip geometry visible without these caches.
- Ensure basic editing shortcuts, precise time entry, independent audio/video source selection, snap toggle, track mute/enable, source preview, output preview, project save and export are discoverable and coherent. Add explicit output interval selection for review. Fix only workflow blockers and defects identified by the acceptance exercise; avoid feature creep.
- Create reproducible synthetic editions A and B with equivalent material but distinct audio/picture, different resolutions, a known rate difference, additional and removed footage, a reordered sequence, different shot durations, multiple audio streams and timestamp offsets. Document a manual edit recipe for reconstructing a specified output using parts of both editions, without automated matching. Generate fixtures at test time or via a documented CLI.
- Exercise a long-duration project (at least two hours, hundreds of clips), profiling timeline responsiveness and bounded cache/process usage. No need to render a full-length movie in CI; test full-duration planning and selected render windows.
- Document user workflow and known limitations in README; update architecture and progress docs. Add an optional real-film smoke-test procedure for the human with no bundled copyrighted media.

## Acceptance workflow
1. Import A and B; inspect their streams and choose arbitrary output settings.
2. Add picture and sound from either source onto independent tracks; manually align using picture and waveform inspection, precise seeking, shifts and rates.
3. Split, trim, move and substitute around missing/extra/reordered material. Include at least one region using A picture/B audio and one using B picture/A audio, plus a mixed audio region, fade and gap.
4. Preview intervals spanning boundaries, revise an edit using undo/redo, then save/reopen.
5. Export and verify expected video colours/order, tone frequencies, gap timing, rates and duration by decoding the result. Check manually for obvious audiovisual glitches. Test cancellation and missing source recovery.
6. Report defects or limitations discovered, distinguish automated checks from manual playback checks, and list any outstanding acceptance failures. Do not call the editor ready if a core operation is unreliable.

## Stop condition
Manual edition mixing is possible without any automated content matching. Request human feedback on the manual workflow before designing/implementing assisted alignment tasks. Record potential future improvements as observations, not implementation commitments. No matching algorithms, automatic segmentation, global reference edition or new specialised workflow in this milestone.

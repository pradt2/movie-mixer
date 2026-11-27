# Current corrective roadmap — from the paused TASK_12 to manual-editor readiness

Starting code: `52f586e9e14dfed721460eaf14fee539c5e853d0`.
TASK_00–08 originally delivered the manual editor, TASK_09–11 were reported complete, and TASK_12 was deliberately paused with ten failing picture-identity tests. Preserve their code and documentation unless an assigned correction requires changes. The old TASK_13–15 are superseded by this sequence; their exact original specifications remain in Git history at `378d1ea`. This plan changes no implementation and adds no features beyond the original readiness goal.

Read AGENTS.md and only the next assigned task. Complete its acceptance before advancing; a blocked task needs reviewer attention. The tasks are intentionally small enough to be assigned individually. Diagnostic-only tasks may report a named red baseline, as expressly specified. Do not treat a table of tasks as authority to execute all of them in one agent context.

| Group | Original scope | Current sequence (in order) | Exit criterion |
|---|---|---|---|
| A | Unfinished TASK_12 | 12b, 12c, 12d, 12e, 12f, 12g, 12h, 12i, 12j, 12k, 12l, 12m, 12n | Truthful, testable picture and source navigation; full picture checkpoint |
| B | Former TASK_13 | 13, 14, 15, 16, 17, 17b, 17c, 18, 19, 20, 21, 22, 22b, 22c, 23 | Bounded cancellable jobs; session isolation; clean repeated shutdown; monotonic progress |
| C | Former TASK_14 | 24, 25, 26, 27, 28, 28b, 28c, 29, 30, 31, 32 | Measured scaling and bounded resource policy; verified joins; range-local safe cache |
| D | Former TASK_15 | 33, 34, 35, 36, 37 | Smaller orchestration boundary, stale-safe edit seam, CI/recovery and human approval |

## Reviewer decisions carried across the assignments

- Keep exact rational clip times. Render range/clip boundaries use the ceil lattice; playhead inspection has a distinct floor rule. A source frame is not a resampled composition frame.
- Keep last legal decoded source picture at/before the mapped time; edge hold only where the legal slice genuinely contains a picture. A backend tie tolerance must be quantified and must never excuse still/range/full disagreement. No invented measured PTS, no new per-frame logs in normal exports; instrumentation is diagnostic.
- Separate actual decoded source navigation from output rendering. Mark In uses visible verified PTS, Mark Out uses it as exclusive boundary. Unsupported HDR/interlaced processing is refused explicitly, without a speculative colour-management feature.
- Establish simple correct reference behaviour on small fixtures before optimising. A bounded/explicitly refused pathological workload is better than a wrong result or invisible full-film decode.
- Keep one persistent document/session transaction boundary. Never introduce a preferred movie edition.
- End at the same gate as former TASK_15: the human must still try representative real films. No assisted matching just because tests are green.

## Exact assignment list

### A — Picture correctness

- [TASK_12b.md](TASK_12b.md) — Repair the picture acceptance tests, not the renderer
- [TASK_12c.md](TASK_12c.md) — Correct square-pixel display geometry
- [TASK_12d.md](TASK_12d.md) — Make inspecting a playhead distinct from quantising a render range
- [TASK_12e.md](TASK_12e.md) — Establish truthful source bounds for nonzero-origin video
- [TASK_12f.md](TASK_12f.md) — A bounded index of actual decoded presentation timestamps
- [TASK_12g.md](TASK_12g.md) — Decode one source picture with its own actual timestamp
- [TASK_12h.md](TASK_12h.md) — Make source stepping and marking truthful in the UI
- [TASK_12i.md](TASK_12i.md) — Build a slow, independent picture reference for diagnosis
- [TASK_12j.md](TASK_12j.md) — Make whole and independently rendered ranges choose the same pictures
- [TASK_12k.md](TASK_12k.md) — Composition stills agree with the real render
- [TASK_12l.md](TASK_12l.md) — Explicit handling of unimplemented interlaced and HDR inputs
- [TASK_12m.md](TASK_12m.md) — Picture correctness checkpoint
- [TASK_12n.md](TASK_12n.md) — Fine-timescale streams must not lose a held last picture *(added at the TASK_12m checkpoint from the defect it named; the checkpoint itself stayed verification-only)*

### B — Media jobs and lifecycle

- [TASK_13.md](TASK_13.md) — Build and test one cancellable subprocess primitive
- [TASK_14.md](TASK_14.md) — Bound and genuinely cancel media imports
- [TASK_15.md](TASK_15.md) — Isolate import completions to their originating project
- [TASK_16.md](TASK_16.md) — Make render subprocesses cancellable through validation and publication
- [TASK_17.md](TASK_17.md) — Make still and decoded-PTS probes cancellable
- [TASK_17b.md](TASK_17b.md) — Make thumbnail and waveform jobs cancellable
- [TASK_17c.md](TASK_17c.md) — Make source-window extraction cancellable
- [TASK_18.md](TASK_18.md) — Move relink probing off the GUI thread
- [TASK_19.md](TASK_19.md) — Move source-window extraction off the GUI thread
- [TASK_20.md](TASK_20.md) — Make still/source-step request scheduling race-free
- [TASK_21.md](TASK_21.md) — Make preview and decoration completion session-safe
- [TASK_22.md](TASK_22.md) — Give every worker a reliable, explicit stop/join contract
- [TASK_22b.md](TASK_22b.md) — Assemble deterministic window close and session shutdown
- [TASK_22c.md](TASK_22c.md) — Prove shutdown actually exits cleanly
- [TASK_23.md](TASK_23.md) — Make render progress monotonic and meaningful

### C — Scalability and cache safety

- [TASK_24.md](TASK_24.md) — Measure the long-project bottlenecks before optimising
- [TASK_25.md](TASK_25.md) — Index immutable snapshots for ID and interval queries
- [TASK_26.md](TASK_26.md) — Plan video winners and audio level warnings by event sweep
- [TASK_27.md](TASK_27.md) — Stop mixing one full-duration padded buffer per audio clip
- [TASK_28.md](TASK_28.md) — Measure lossy-audio joins and late-range decode work
- [TASK_28b.md](TASK_28b.md) — Avoid repeatedly processing whole audio clips for short ranges
- [TASK_28c.md](TASK_28c.md) — Verify bounded picture-range decoding near a film's end
- [TASK_29.md](TASK_29.md) — Partition render pieces by actual graph budget
- [TASK_30.md](TASK_30.md) — Give lossless staging a bounded, honest disk policy
- [TASK_31.md](TASK_31.md) — Key preview renders by the resolved content of their range
- [TASK_32.md](TASK_32.md) — Pin in-use cache files and reuse unchanged stills/previews safely

### D — Architecture and acceptance

- [TASK_33.md](TASK_33.md) — Extract one cohesive preview/inspection coordinator from MainWindow
- [TASK_34.md](TASK_34.md) — Provide a stale-safe transaction seam for proposed edits
- [TASK_35.md](TASK_35.md) — Reproducible Linux CI and verification commands
- [TASK_36.md](TASK_36.md) — Verify recovery across an actual process crash
- [TASK_37.md](TASK_37.md) — Final manual-editor readiness gate (before assisted matching)

**Next assignment:** TASK_14.md. Group A is complete and TASK_13 (the cancellable subprocess runner, `media/process.py`, no callers migrated yet) is done: its 14 tests pass repeatedly in about 2.1 s with `ruff` clean, and its cancellation, pipe-drain, termination and reaping behaviour was re-measured independently. Group B now migrates the call sites onto it; the confirmed defects waiting there are listed under *Outstanding environment checks* in `docs/progress.md`.

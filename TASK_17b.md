# TASK_17b — Make thumbnail and waveform jobs cancellable

Prerequisite: TASK_17. Scope: media/thumbnails.py, waveform.py and existing decoration worker. No cache redesign.

## Work
Use TASK_13 runner for existing decoration probes and decodes; pass the active job token, timeout and explicit binary/capture limits. Thumbnail bytes bounded by validated output size; waveform decoded window bounded or streamed rather than captured without limit. Cancel on supersession/shutdown, no partial decoration published. Preserve selection of stream and actual identity.

## Acceptance
Fake hanging decoder and stderr flood cancel/reap; real thumbnail and waveform fixtures retain expected colours/levels, missing/changed assets refuse. Run media thumbnails/waveform and decoration tests. Keep worker queue/ownership API simple; TASK_21 handles result gates.

# TASK_28 — Measure lossy-audio joins and late-range decode work

Prerequisite: TASK_27. Scope: diagnostic/acceptance tests only, no new render strategy.

## Work
TASK_11 rebuilds each audio clip from its own start for each requested range. Use long synthetic source and a varied speech-like synthetic signal; compare decoded PCM single-pass vs joined ranges of AAC and AC3 material at rates 1, 25/24 and 24/25, including a fade and boundary. Measure sample counts, holes/priming, sample/RMS deviation and decoded extent/time of a short range near end. Record the earlier PCM reference and actual cost of picture path near end from TASK_12j. Separate codec seek/priming effects from tempo-state effects. Use fixtures that are independently verified to contain the intended signal.

## Acceptance / stop
Provide reproducible small measurement table, expected sample positions and diagnostic test for both PCM and lossy inputs. Quantify the smallest practical error bound supported by measurement; do not change tests to fit unmeasured behaviour. Name the actual performance bottleneck and recommend a single bounded strategy for TASK_28b. Red lossy comparison is diagnostic, not permission to weaken PCM checks.

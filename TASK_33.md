# TASK_33 — Extract one cohesive preview/inspection coordinator from MainWindow

Prerequisite: TASK_32. Scope: small, behaviour-preserving extraction, not general architecture overhaul.

## Work
MainWindow owns >2500 lines and too much job/cache state. Extract the cohesive preview/inspection orchestration now stabilised by TASK_19–22 and 31–32 (generation/epoch, immutable requests, result acceptance, cache/player lifecycle as appropriate) into a small explicit coordinator. Keep widgets, visual updates and signal wiring in UI; pass snapshot providers and narrow callbacks, no service locator/global state. Move only one coherent responsibility in this task; choose the smallest subset isolating useful state if whole extraction large. Preserve existing public interactions, command API and ownership. Make separate move-only edit before any cosmetic rename.

## Acceptance
Focused coordinator tests with fake callbacks proving existing gating and lifecycle; existing GUI/preview/editing/teardown tests unchanged except mechanical interface updates. Compare MainWindow responsibilities before/after. Run relevant full UI suite, report remaining responsibilities rather than moving everything. No alignment types, algorithms or plugin architecture.

# TASK_07 — Project files, recovery and relinking

Prerequisite: TASK_06. Read AGENTS.md. Support long-running real-world editing sessions without losing work.

## Implement
- Versioned JSON project format storing asset IDs/references and metadata, output settings, tracks, clips, links, exact fractions and optional UI navigation settings. Preserve stable IDs. Use relative paths where sensible with explicit resolution relative to the project directory; support absolute paths too. Validate schema and domain invariants on load. Reject unknown future schema versions with useful diagnostics; do not silently discard fields. Document the initial schema and migration policy.
- File > New/Open/Save/Save As; atomic write using a sibling temporary file and replacement of the intended project file. Save As must prompt before overwriting. Never modify media. Dirty-state tracking respects undo/redo relative to saved content. Prompt before closing or replacing unsaved work; failed load must leave current project intact.
- Missing and changed asset detection using recorded identity. Offer explicit relinking, probe replacement and validate stream mappings/bounds. Never silently substitute mismatched streams or modify clip timing. Permit offline timeline inspection and save; block rendering unavailable material with precise diagnostic. Invalidate affected preview caches when asset changes.
- Periodic recovery snapshot to a separate app-owned location, not the normal project file. Offer explicit restore/discard after abnormal exit, and do not silently replace an existing project. Session recovery and history persistence are distinct; history need not survive reopening (document this). Restore useful playhead/zoom when possible.

## Acceptance
- Save a mixed project, reopen and verify exact fractions, clip positions, rate, links, track order/settings and stable IDs. Export before/after and compare decoded results. Move an asset and relink it; reject incompatible replacement. Handle corrupted/unsupported project files without losing current edits.
- Test atomic-save failure, dirty tracking through undo/redo, recovery restoration, relative paths and missing files. Confirm no project or source data is lost on cancellation.

# Unified Design Sync Implementation Plan

**Goal:** One design command, actual differences only, no repeated ordinary approval or unnecessary PCB restart.

**Architecture:** Keep legacy API destinations compatible; add `design` orchestration. Verify identity and full source versions before every transaction. A completed rename is no geometry request; a same-name replacement still requires target geometry verification.

**Spec:** User request 2026-10-26: continue the current PCB stage, consolidate sync, reduce window churn.

## Constraints
- Production is an operation target only after disposable tests, never a test fixture.
- Preserve KiCad 10 schematic transaction guard, connected-terminal confirmation, uncertain-result no-replay and full disk verification.
- Release a new version, verify public assets, install only after normal host exit.

## Review Focus
- Completed rename evidence is stale, but first rename geometry checks remain strict.
- Mixed unchanged/changed selections transact only changed instances; outside objects remain verified.
- Blank missing fields are not changes; nonempty values remain changes.
- Ordinary replacement auto-continues; terminal loss and unknown identity never auto-approve.
- Saving failure keeps editors and evidence; successful save does not restart PCB.

## Tasks
- [x] Add red tests for completed rename, changed transaction scope and empty fields; implement and run focused regressions.
- [x] Add `design` coordinator tests for no schematic replay, automatic ordinary continuation and exceptional risks; implement with progress preserved.
- [x] Add keep-open save tests; retain mandatory schematic guard and full live/disk validation.
- [x] Consolidate toolbar/row actions, hide no-op rows and auto-start safe preflights; test Chromium interaction.
- [x] Run disposable native PCB workflow, full suite and independent review.
- [ ] Release/install verified version, then resume production PCB only after fresh readonly preflight; record actual outcome.

## Verification
- Full suite: 1630 passed; existing Python string-escape deprecation warnings remain.
- Native `--with-footprints`: field-only, library rename and standard-library replacement completed; exact transaction outside-object comparison, pads/net/pose checks, repeat no-op and same-session saved PCB verified.
- Native report: external `design-keep-open-20261026-r16/result.json`; passed, process and directory cleanup true. No production CAD used as a test fixture.
- Independent review: stale provenance, source handoff, restoration errors and bounded incomplete IPC inventory findings addressed.

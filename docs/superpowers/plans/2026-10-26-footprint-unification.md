# Existing Footprint Reuse And Batch Unification

**Goal:** Reuse existing library identifiers without automatic `_mapped` copies; unify equivalent footprints across equal Comment/Value groups without moving copper.

**Decisions:** Keep standard library bytes unchanged and solve PCB instance placement by numbered pads. Preview dragging alone must not enable adaptation. Explicit renumbering, terminal dropping and intentional adapted geometry remain opt-in. Automatic batch operations skip actual copper differences, missing/ambiguous pads and conflicting material codes. The user selected this skip policy on 2026-10-26.

**Evidence:** OPA2210IDGKR U39 has 4.318 mm pad-row spacing, U102 4.3688 mm, U51 4.4196 mm. VSSOP-8 matches U39. U102/U51 require minimum residuals of 0.0254/0.0508 mm and must not be marked zero-movement equivalents.

## Tasks
- [x] Add failing equivalence, batch-preservation and preview-reuse regressions.
- [x] Separate normal library reuse from explicit adaptation in footprint adjustment UI; align comparison previews by numbered pads.
- [x] Add reusable batch audit/apply command using saved PCB instances, unchanged library bytes, full source revalidation, external backup, undo snapshots and one atomic record write. Keep C codes, parameters and confirmation intact. Do not sync production CAD automatically.
- [x] Measure stock lookup stages; optimize only measured overhead while retaining quantity, dispatch evidence, fallback, bounded reads and cancellation.
- [x] Run focused, Chromium, full and disposable native CAD tests; audit/apply only verified zero-copper-movement candidates and retain an external report (zero eligible production changes; GUI limitations below).
- [ ] Update rules/docs, publish a new verified formal version, reinstall and compare files while preserving converter.

## Review Focus
- Manual dropped/extra pads must still create explicitly approved adapted copies.
- Same part labels do not prove equal copper; real pitch, size, drill and layer changes are skipped.
- Back-side instances and origin/rotation changes require proper rigid transforms, not mirror guessing.
- Source changes during audit must stop publication; no partial record batches or production CAD test writes.
- Faster inventory reads must not infer delivery from stock totals or leave subprocesses/queries blocking exit.

## Validation Notes
- Production audit: 27 cross-footprint groups, 199 noncanonical candidates, no safe changes under strict equivalence and material-preservation checks. OPA2210IDGKR U102/U51 skipped with the measured residuals above. No production records/CAD/library writes.
- Native pcbnew saved-file roundtrip: front/back origin/rotation differences unified to an existing library; actual saved/reloaded pad-center error 1 nm, nets and tracks preserved. This is not a GUI/IPC test.
- Native GUI adapter checks: actual workbench selection, schematic synchronization, PCB placement/identity/nets/tracks, one undo and explicit save passed; manager reopen stopped with unknown document ownership. Full design cycle stopped at Search panel unavailable navigation. Error reports remain outside the repository; neither entire GUI run is described as passed.
- Independent review found a planned-arrival conflict when targeting an unused library ID. The final grouping guard now checks existing and incoming members; code/review conflict regressions verify real workbench reload preserves selection and confirmation.

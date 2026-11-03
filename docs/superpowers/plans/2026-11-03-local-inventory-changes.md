# Local Inventory Change Details

**Goal:** Show the latest successfully saved local-stock quantity change beside design changes, without treating stock as a CAD property or pending design sync.

**Architecture:** Extend each C-code record in the existing project-owned inventory JSON with optional old/new/time metadata. Validate this metadata against the current balance. Refresh a separate saved-change marker in the review cell, preserving CAD markers, focus, counts and selection. No new data file or schema migration.

**Scope:** Manual quantity edits; same-code rows share a balance. Unknown is not zero. Failed writes do not publish change metadata. Import invalidates a stale quantity marker when its balance/status changes. Historical edits without evidence are not invented.

- [x] Add failing persistence/unknown/zero/shared-code/legacy tests and Chromium marker assertions.
- [x] Implement atomic quantity metadata plus independently rendered saved-change detail.
- [x] Verify focused tests, full regression and offline Chromium; update user documentation/version.
- [x] Independently review; add the settings-source and post-backup failure regression checks.

Release gate: commit/push the reviewed source and v0.1.56 tag, then install safely and verify the published assets. Publication/installation evidence is external to the source checkout, avoiding a self-referential release commit.

# Table Loading Implementation Plan

Goal: Show continuous loading feedback in the main window and reduce table display latency without changing design or inventory records.

Architecture: Keep synchronous row rendering for existing partial updates. Add a bounded, cancellable initial-render path and a native HTML progress indicator. Measure backend costs independently and optimize only demonstrated duplicate work.

Constraints: No production CAD writes, no UIAutomation, preserve pending-state evidence, source changes and edits. Base b4b258301237afb1968babf42a69a0814bbed888 (clean linked worktree; no stash object).

- [x] Reproduce missing loading feedback and measure a synthetic large table.
- [x] Add progress and incremental rendering with stale-request guards, failure and empty-result handling.
- [x] Measure backend and remove proven redundant processing with regression tests.
- [x] Run full tests, isolated Chromium screenshots, independent review (service unavailable; direct review used).
- [ ] Bump version, push release, verify public assets and install normally.

Read-only comparison on the saved 32 MB board: selective status parsing 5.515 s versus full parsing 12.561 s, exact status dictionaries equal. Synthetic 650-row browser rendering: first rows appear before completion, total around 0.7-0.9 s; these are measurements, not latency guarantees. Parser tests cover quoted heads, escaping, damaged skipped trees and source offsets. Request-scoped target reads do not authorize CAD writes or persist between passes.

Independent subagent review could not run because the service reported a usage limit; implementation was reviewed directly, with differential parser tests and actual Chromium error/empty/reload/failure cases.

Local full collection initially ran 1917 cases: 1915 passed; two minimal Node fixtures lacked the new loading/DOM dependencies. After updating those fixtures, both affected modules passed all 20 tests. Final source also passed 60 design/table status regressions, 10 parser/table tests, and the separate local-inventory Chromium smoke. Final collection is 1919 cases; the release workflow reruns the complete collection and must pass before publication is reported. Downstream asset/install evidence stays outside the source tree.

"""Real browser regressions using only synthetic footprint API responses."""
import shutil
import subprocess

import pytest

from run_footprint_overlay_smoke import run_checks


@pytest.fixture(scope="module", autouse=True)
def browser_dependencies():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the offline Chromium footprint UI checks")
    result = subprocess.run([node, "-e", "require.resolve('playwright')"], capture_output=True, timeout=15)
    if result.returncode:
        pytest.skip("Playwright is unavailable to Node.js; set NODE_PATH to run real Chromium UI checks")


@pytest.mark.parametrize("scenario", ["overlay", "rename", "stale", "library", "library-pcb-reference", "pcb", "conflict", "navigation", "pane-radios", "rename-retry", "escape", "incompatible-preview", "native-layout", "manual", "manual-safety", "pad-mapping", "custom-name", "batch"])
def test_footprint_overlay_contract(scenario):
    result, _ = run_checks(scenario)
    assert result["passed"] is True
    assert result["page_errors"] == []

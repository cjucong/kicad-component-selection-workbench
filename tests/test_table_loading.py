from unittest.mock import patch
import os
from pathlib import Path
import subprocess
import sys

from part_selection_design_status import DesignStatus


def test_status_target_reads_are_shared_only_within_one_pass(scratch_project):
    from types import SimpleNamespace
    import hashlib
    path = scratch_project / 'part.kicad_mod'
    path.write_bytes(b'footprint')
    status = DesignStatus(SimpleNamespace(project_root=scratch_project, config={'bom_dir':scratch_project}))
    evidence = {'targets':[{'scope':'project','path':path.name,'hash':hashlib.sha256(b'footprint').hexdigest()}]}
    original = type(path).read_bytes
    reads = []
    def read(target):
        reads.append(target)
        return original(target)
    with patch.object(type(path), 'read_bytes', read):
        cache = {}
        for _ in range(100):
            assert status._valid_targets(evidence, read_cache=cache)
        assert len(reads) == 1
        path.write_bytes(b'changed')
        assert not status._valid_targets(evidence, read_cache={})


def test_large_table_progress_failure_and_reload_in_chromium():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-B', str(root / 'tests/run_table_loading_smoke.py')],
        cwd=root, env=os.environ.copy(), capture_output=True, text=True, encoding='utf-8', timeout=80)
    assert result.returncode == 0, result.stdout + result.stderr

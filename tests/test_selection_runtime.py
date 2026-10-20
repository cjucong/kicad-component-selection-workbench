import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))


def test_runtime_is_external_stable_and_project_specific(scratch_project):
    from part_selection_runtime import runtime_directory
    project = scratch_project / 'design'
    project.mkdir()
    runtime = runtime_directory(project)
    assert runtime == runtime_directory(project / '.')
    assert runtime.is_dir()
    assert not runtime.is_relative_to(project)
    assert runtime.anchor == project.anchor
    other = scratch_project / 'other' / 'design'
    other.mkdir(parents=True)
    assert runtime != runtime_directory(other)
    assert not (project / 'Temp').exists()

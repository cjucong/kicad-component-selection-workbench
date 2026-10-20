"""Disposable test projects, isolated from KiCad's project backup tree."""
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from part_selection_runtime import runtime_directory


class _TemporaryProject:
    def __init__(self, prefix):
        directory = runtime_directory(ROOT) / "tests"
        directory.mkdir(parents=True, exist_ok=True)
        self._container = tempfile.TemporaryDirectory(prefix=prefix, dir=directory)
        # Runtime caches are siblings of the project, so contain both together.
        self.name = str(Path(self._container.name) / "project")
        try:
            Path(self.name).mkdir()
        except BaseException:
            self._container.cleanup()
            raise

    def cleanup(self):
        self._container.cleanup()

    def __enter__(self):
        return self.name

    def __exit__(self, *args):
        self.cleanup()


def temporary_project(prefix="test_"):
    return _TemporaryProject(prefix)

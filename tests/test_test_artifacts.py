"""Guard test workspace placement and cleanup without touching design files."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TestArtifactLifecycleTests(unittest.TestCase):
    def test_scratch_is_outside_project_and_removed_on_success(self):
        from _scratch import temporary_project

        with temporary_project() as directory:
            path = Path(directory).resolve()
            self.assertFalse(path.is_relative_to(ROOT))
            (path / "fixture.txt").write_text("disposable", encoding="ascii")
        self.assertFalse(path.exists())

    def test_scratch_is_removed_after_failure(self):
        from _scratch import temporary_project

        with self.assertRaisesRegex(RuntimeError, "test failure"):
            with temporary_project() as directory:
                path = Path(directory)
                (path / "fixture.txt").write_text("disposable", encoding="ascii")
                raise RuntimeError("test failure")
        self.assertFalse(path.exists())

    def test_reusable_tests_have_lifecycle_documentation(self):
        self.assertTrue((ROOT / "tests/README.md").is_file())

    def test_scratch_removes_sibling_runtime_cache(self):
        from _scratch import temporary_project, runtime_directory

        with temporary_project() as directory:
            cache = runtime_directory(Path(directory))
            (cache / "marker.txt").write_text("disposable", encoding="ascii")
        self.assertFalse(cache.exists())

    def test_explicit_cleanup_removes_sibling_runtime_after_failure(self):
        from _scratch import temporary_project, runtime_directory

        scratch = temporary_project()
        self.addCleanup(scratch.cleanup)
        cache = runtime_directory(Path(scratch.name))
        with self.assertRaisesRegex(RuntimeError, "test failure"):
            try:
                raise RuntimeError("test failure")
            finally:
                scratch.cleanup()
        self.assertFalse(cache.exists())
        self.assertFalse(Path(scratch.name).exists())


if __name__ == "__main__":
    unittest.main()

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StartPartSelectionToolTest(unittest.TestCase):
    def launcher_text(self):
        return (PROJECT_ROOT / "tools" / "start_part_selection_tool.bat").read_text(
            encoding="ascii"
        ).lower()

    def find_python(self, env):
        command = [
            os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
            "/d",
            "/c",
            "call",
            str(PROJECT_ROOT / "tools" / "start_part_selection_tool.bat"),
            "--find-python",
        ]
        return subprocess.run(command, env=env, capture_output=True, text=True, check=False)

    def test_launcher_starts_hidden_background_worker(self):
        script = self.launcher_text()

        self.assertIn("-windowstyle hidden", script)
        self.assertIn("pythonw", script)
        self.assertIn("--open", script)
        self.assertIn("part_selection_tool_startup.log", script)
        self.assertIn("http://127.0.0.1:8765/api/config", script)
        self.assertIn("skipped duplicate launch", script)
        self.assertNotIn("pause", script)
        self.assertNotIn('"%python%" -b part_selection_server.py', script)

    def test_launcher_discovers_and_validates_python_without_version_lock(self):
        script = self.launcher_text()

        self.assertIn("part_selection_python", script)
        self.assertIn("--find-python", script)
        self.assertIn(":validate_python", script)
        self.assertIn("sys.version_info", script)
        self.assertIn('set "search_path=%path:"=%"', script)
        self.assertIn("for %%v in", script)
        self.assertIn("\\anaconda\\python.exe", script)
        self.assertIn("%programdata%", script)
        self.assertIn('set "pythonw=%python%"', script)
        self.assertNotIn("python312", script)
        self.assertNotIn('set "python=python"', script)

    def test_find_python_prefers_explicit_valid_interpreter(self):
        env = os.environ.copy()
        env["PART_SELECTION_PYTHON"] = sys.executable
        env["PATH"] = ""

        result = self.find_python(env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), Path(sys.executable))

    def test_find_python_accepts_quoted_path_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env.pop("PART_SELECTION_PYTHON", None)
            env["PATH"] = f'"{Path(sys.executable).parent}"'
            env["LOCALAPPDATA"] = temp_dir
            env["USERPROFILE"] = temp_dir
            env["ProgramData"] = temp_dir

            result = self.find_python(env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), Path(sys.executable))


if __name__ == "__main__":
    unittest.main()

import os
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
from _scratch import temporary_project


class StartPartSelectionToolTest(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows hidden launcher')
    def test_hidden_vbs_preserves_arguments_with_spaces(self):
        with temporary_project(prefix='hidden launcher ') as directory:
            work = Path(directory)
            for name in ('start_part_selection_tool.bat', 'start_part_selection_tool.vbs'):
                shutil.copy2(PROJECT_ROOT / 'tools' / name, work / name)
            output = work / 'arguments.json'
            (work / 'launch_part_selection.py').write_text(
                'import json, os, sys\nfrom pathlib import Path\n'
                'Path(os.environ["LAUNCH_TEST_OUTPUT"]).write_text(json.dumps(sys.argv[1:]))\n')
            arguments = ['--config', str(work / 'settings with spaces.json')]
            env = {**os.environ, 'PART_SELECTION_PYTHON': sys.executable,
                   'LAUNCH_TEST_OUTPUT': str(output), 'TEMP': str(work)}
            subprocess.run(['cscript.exe', '//B', str(work / 'start_part_selection_tool.vbs'),
                            *arguments], env=env, capture_output=True, check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
            deadline = time.monotonic() + 15
            while not output.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            log = work / 'part_selection_tool_startup.log'
            self.assertTrue(output.exists(), log.read_text(errors='replace') if log.exists() else 'Worker did not start')
            self.assertEqual(json.loads(output.read_text()), ['--hidden', *arguments])

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

        self.assertIn("wscript.exe //b", script)
        self.assertIn("pythonw", script)
        self.assertIn("launch_part_selection.py %*", script)
        self.assertIn("part_selection_tool_startup.log", script)
        self.assertNotIn('--project "%~dp0.."', script)
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

"""Legacy PCB launcher, independent of KiCad's cross-editor IPC button registry."""
from pathlib import Path
import os
import subprocess
import sys

try:
    import pcbnew
except (ImportError, OSError):
    # IPC discovery does not require the optional embedded SWIG runtime.
    pcbnew = None


class PartSelectionAction(pcbnew.ActionPlugin if pcbnew is not None else object):
    def defaults(self):
        self.name = "\u5668\u4ef6\u9009\u6599\u52a9\u624b (PCB \u517c\u5bb9\u5165\u53e3)"
        self.category = "SMU"
        self.description = "Open the part-selection workbench for the active saved board"
        self.show_toolbar_button = True
        self.icon_file_name = str(Path(__file__).resolve().parent / "icon.png")
        self.dark_icon_file_name = self.icon_file_name

    def Run(self):
        try:
            board = pcbnew.GetBoard()
            filename = board.GetFileName() if board is not None else ""
            path = Path(filename)
            if (not filename or not path.is_absolute() or path.suffix.lower() != ".kicad_pcb"
                    or not path.is_file()):
                raise ValueError("Save the active board in its KiCad project before opening the workbench")
            path = path.resolve()
            # Embedded Python's sys.executable is KiCad, not a Python interpreter.
            python = Path(sys.executable).with_name("pythonw.exe")
            if not python.is_file():
                raise RuntimeError(f"Bundled Python interpreter not found: {python}")
            env = os.environ.copy()
            for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "KICAD_API_SOCKET", "KICAD_API_TOKEN"):
                env.pop(name, None)
            cache = Path(env.get("LOCALAPPDATA", str(Path.home() / ".cache"))) / "KiCadPartSelection"
            cache.mkdir(parents=True, exist_ok=True)
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            with (cache / "pcb-launch.log").open("ab") as output:
                output.write(f"PCB launch project={path} python={python}\n".encode("utf-8"))
                output.flush()
                subprocess.Popen(
                    [str(python), "-B", str(Path(__file__).resolve().parent / "desktop.py"),
                     "--project", str(path)], cwd=path.parent, env=env,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=output, close_fds=True, **options,
                )
        except Exception as exc:
            import wx
            wx.MessageBox(str(exc), self.name, wx.OK | wx.ICON_ERROR)


if pcbnew is not None:
    PartSelectionAction().register()

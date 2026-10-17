"""Windows desktop host for the existing workbench; also the IPC plugin entrypoint."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen

TITLE = "器件选料助手"
HERE = Path(__file__).resolve().parent
CORE = HERE / "app" if (HERE / "app").is_dir() else HERE.parent


def validate_project(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ValueError("Project path must be absolute; refusing to guess from the working directory")
    root = path.parent if path.suffix.lower() in {".kicad_pro", ".kicad_pcb", ".kicad_sch"} else path
    root = root.resolve()
    if not root.is_dir() or not any(root.glob("*.kicad_pro")):
        raise ValueError(f"No saved KiCad project in {root}")
    if not any(root.glob("*.kicad_sch")):
        raise ValueError(f"No top-level schematic in {root}")
    return root


def project_from_ipc(client) -> Path:
    try:
        path = client.get_board().get_project().path
    except Exception:
        from kipy.proto.common.types.base_types_pb2 import DocumentType
        documents = client.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
        roots = {str(client.get_project(doc).path) for doc in documents}
        if len(roots) != 1:
            raise ValueError("Cannot identify one schematic project; select its project file")
        path = roots.pop()
    if not path or not Path(path).is_absolute():
        raise ValueError("KiCad did not supply an absolute project path; select its project file")
    return validate_project(Path(path))


def enable_browser_shortcuts(window) -> None:
    try:
        from System import Action

        def enable():
            window.native.webview.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled = True

        window.native.Invoke(Action(enable))
    except Exception:
        logging.exception("Could not enable WebView2 browser shortcuts")


def choose_project() -> Path | None:
    script = """
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Filter = 'KiCad project (*.kicad_pro)|*.kicad_pro'
$dialog.CheckFileExists = $true
try { if ($dialog.ShowDialog() -eq 'OK') { [Console]::WriteLine($dialog.FileName) } }
finally { $dialog.Dispose() }
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Sta", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    path = result.stdout.strip()
    return Path(path) if path else None


def ensure_config(root: Path) -> Path:
    sys.path.insert(0, str(CORE))
    from part_selection_overrides import load_config
    config = root / "BOM" / "part_selection_config.json"
    if config.exists():
        if load_config(config)["project_root"].resolve() != root.resolve():
            raise ValueError(f"Configuration points to another project: {config}")
        return config
    data = {
        "project_name": f"{root.name} {TITLE}", "project_root": "..",
        "schematic_dir": "DOC" if (root / "DOC").is_dir() else ".",
        "source_bom": "__schematic__", "lcsc_bom_pattern": "BOM/*LCSC*.csv",
        "input_refs": "BOM/part_selection_input.txt",
        "output_overrides": "BOM/part_selection_overrides.json",
        "stock_cache": "BOM/lcsc_stock_cache.json", "kicad_protocol": "kicad-sch-search",
    }
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        with config.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError:
        return ensure_config(root)
    return config


def matches_service(payload: object, root: Path, config: Path) -> bool:
    if not isinstance(payload, dict) or not payload.get("config_path") or not payload.get("project_root"):
        return False
    if payload.get("desktop_launcher_version") != 2:
        return False
    try:
        return (Path(payload["project_root"]).resolve() == root.resolve()
                and Path(payload["config_path"]).resolve() == config.resolve())
    except (TypeError, OSError, ValueError):
        return False


def service_info(port: int) -> object:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/config", timeout=0.5) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def project_key(root: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(root.resolve())).encode("utf-8")).hexdigest()[:12]


def cache_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".cache")))
    directory = base / "KiCadPartSelection"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def ensure_desktop_runtime(logs: Path) -> Path:
    # KiCad's managed installer accepts wheels only; WebView's pure-Python
    # proxy_tools dependency ships as source. Keep that install out of KiCad's venv.
    runtime = logs / "desktop-runtime"
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "runtime-install.log").open("ab") as output:
        def run(command, timeout=300):
            return subprocess.run(command, check=True, stdin=subprocess.DEVNULL,
                                  stdout=output, stderr=output, timeout=timeout, **options)
        if python.exists():
            try:
                run([str(python), "-B", "-c", "import webview,openpyxl,kipy"], timeout=30)
                return python.with_name("pythonw.exe") if os.name == "nt" else python
            except subprocess.CalledProcessError:
                pass
        else:
            run([sys.executable, "-m", "venv", str(runtime)], timeout=120)
        run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
             "--only-binary=:none:", "-r", str(HERE / "desktop-requirements.txt")])
        run([str(python), "-B", "-c", "import webview,openpyxl,kipy"], timeout=30)
    return python.with_name("pythonw.exe") if os.name == "nt" else python


def ensure_backend(root: Path, config: Path, logs: Path) -> str:
    first = 18000 + int(project_key(root)[:6], 16) % 18000
    ports = [8765, *(first + offset for offset in range(10))]
    # Prefer the stable project-specific port if already running.
    for port in ports:
        if matches_service(service_info(port), root, config):
            logging.info("Reusing matching service on %s", port)
            return f"http://127.0.0.1:{port}/"
    for port in ports:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        if os.name == "nt" and pythonw.exists():
            python = pythonw
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with (logs / "server.log").open("ab") as output:
            process = subprocess.Popen(
                [str(python), "-B", str(CORE / "part_selection_server.py"),
                 "--config", str(config), "--host", "127.0.0.1", "--port", str(port)],
                cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=output, **options,
            )
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                if matches_service(service_info(port), root, config):
                    logging.info("Started matching service on %s pid=%s", port, process.pid)
                    return f"http://127.0.0.1:{port}/"
                time.sleep(0.15)
        finally:
            if process.poll() is None and not matches_service(service_info(port), root, config):
                process.terminate()
                process.wait(timeout=5)
    raise RuntimeError(f"Could not start a matching service. See {logs / 'server.log'}")


def acquire_window(root: Path):
    if os.name != "nt":
        return None, True
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, "Local\\KiCadPartSelection_" + project_key(root))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle, ctypes.get_last_error() != 183


def release_window(handle) -> None:
    if handle:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(handle)


def activate_window(title: str) -> bool:
    user = ctypes.windll.user32
    user.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user.FindWindowW.restype = ctypes.c_void_p
    user.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    for _ in range(40):
        hwnd = user.FindWindowW(None, title)
        if hwnd:
            user.ShowWindow(hwnd, 9)
            user.SetForegroundWindow(hwnd)
            return True
        time.sleep(0.15)
    return False


def show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10)
    else:
        print(message, file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="KiCad part-selection desktop host")
    parser.add_argument("--project", type=Path, help="Absolute project file or directory")
    parser.add_argument("--browser", action="store_true", help="Use the system browser instead of WebView2")
    args = parser.parse_args(argv)
    log_base = cache_directory()
    logging.basicConfig(filename=log_base / "desktop.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    handle = None
    try:
        if args.project:
            root = validate_project(args.project)
        else:
            try:
                if not os.environ.get("KICAD_API_SOCKET"):
                    raise ValueError("No launching KiCad instance supplied")
                from kipy import KiCad
                root = project_from_ipc(KiCad())
            except Exception:
                logging.exception("IPC project discovery failed; requesting explicit project")
                path = choose_project()
                if path is None:
                    return 0
                root = validate_project(path)
        logs = log_base / project_key(root)
        logs.mkdir(exist_ok=True)
        if not args.browser:
            try:
                import webview
            except ImportError:
                installer, installing = acquire_window(logs / "runtime-install")
                try:
                    if not installing:
                        raise RuntimeError(f"Desktop dependencies are installing. See {logs / 'runtime-install.log'}")
                    logging.info("Preparing private desktop environment for %s", root)
                    python = ensure_desktop_runtime(logs)
                except Exception:
                    logging.exception("Private desktop environment failed; using browser")
                    show_error(f"Desktop dependencies could not be prepared; using the browser.\n{logs / 'runtime-install.log'}")
                    args.browser = True
                else:
                    with (logs / "window.log").open("ab") as output:
                        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                        subprocess.Popen([str(python), "-B", str(HERE / "desktop.py"),
                                          "--project", str(root)], cwd=root, stdin=subprocess.DEVNULL,
                                         stdout=output, stderr=output, **options)
                    return 0
                finally:
                    release_window(installer)
        title = f"{TITLE} | {root.name} [{project_key(root)[:6]}]"
        handle, acquired = acquire_window(root)
        if not acquired and not args.browser:
            if not activate_window(title):
                raise RuntimeError("Workbench is starting. Retry shortly; see desktop.log if it does not appear.")
            return 0
        config = ensure_config(root)
        url = ensure_backend(root, config, logs)
        logging.info("Opening project=%s url=%s", root, url)
        if args.browser:
            webbrowser.open(url)
            return 0
        try:
            import webview
            window = webview.create_window(title, url, width=1380, height=900, min_size=(800, 500))
            window.events.loaded += lambda: enable_browser_shortcuts(window)
            webview.start(gui="edgechromium", private_mode=False,
                          storage_path=str(logs / "webview"))
        except Exception:
            logging.exception("WebView2 failed; falling back to browser")
            webbrowser.open(url)
            show_error(f"WebView2 could not start; opened the browser instead.\n"
                       f"Install Microsoft Edge WebView2 Runtime and plugin dependencies.\n{log_base / 'desktop.log'}")
        return 0
    except Exception as exc:
        logging.exception("Desktop launch failed")
        show_error(f"{exc}\n\n{log_base / 'desktop.log'}")
        return 1
    finally:
        release_window(handle)


if __name__ == "__main__":
    raise SystemExit(main())

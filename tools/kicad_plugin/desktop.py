"""Windows desktop host for the existing workbench; also the IPC plugin entrypoint."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

TITLE = "器件选料助手"
HERE = Path(__file__).resolve().parent
CORE = HERE / "app" if (HERE / "app").is_dir() else HERE.parent


def validate_project(path: Path) -> Path:
    path = Path(os.path.expandvars(str(path))).expanduser()
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
        logging.info("IPC PCB project path=%r", path)
        if not path:
            raise ValueError("PCB supplied an empty project path")
        return validate_project(Path(path))
    except Exception as exc:
        logging.info("IPC PCB project unavailable; checking schematic documents: %s", exc)
    try:
        from kipy.proto.common.types.base_types_pb2 import DocumentType
        documents = client.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
        logging.info("IPC schematic document count=%s", len(documents))
        roots = set()
        for doc in documents:
            path = client.get_project(doc).path
            logging.info("IPC schematic project path=%r filename=%r", path,
                         getattr(doc, "board_filename", ""))
            try:
                if not path:
                    raise ValueError("Schematic supplied an empty project path")
                root = validate_project(Path(path))
            except ValueError:
                root = schematic_project_from_environment(doc)
            roots.add(root)
        if len(roots) != 1:
            raise ValueError("Cannot identify one schematic project; select its project file")
        return roots.pop()
    except Exception as exc:
        raise ValueError("KiCad did not supply one valid saved project; select its project file") from exc


def schematic_project_from_environment(document) -> Path:
    # Some KiCad builds omit project metadata but pass KIPRJMOD to plugin processes.
    path = os.environ.get("KIPRJMOD", "")
    filename = getattr(document, "board_filename", "")
    logging.info("IPC schematic environment KIPRJMOD=%r filename=%r", path, filename)
    if not path or not filename:
        raise ValueError("No KiCad project environment or schematic filename")
    root = validate_project(Path(path))
    schematic = Path(filename)
    if not schematic.is_absolute():
        if schematic.name != filename:
            raise ValueError("Schematic filename is not a top-level filename")
        schematic = root / schematic
    schematic = schematic.resolve()
    if (schematic.parent != root or schematic.suffix.lower() != ".kicad_sch"
            or not schematic.is_file() or not schematic.with_suffix(".kicad_pro").is_file()):
        raise ValueError("KiCad project environment does not match the open top-level schematic")
    return root


def enable_browser_shortcuts(window) -> None:
    try:
        from System import Action

        def enable():
            window.native.webview.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled = True
            window.native.webview.CoreWebView2.Settings.AreDefaultContextMenusEnabled = True

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


def ensure_config(root: Path, config_path: Path | None = None) -> Path:
    sys.path.insert(0, str(CORE))
    from part_selection_overrides import load_config
    config = config_path or root / "BOM" / "part_selection_config.json"
    config = Path(os.path.expandvars(str(config))).expanduser().resolve()
    if config_path is not None and not config.is_file():
        raise FileNotFoundError(f"Configuration not found: {config}")
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
        return ensure_config(root, config_path)
    return config


def service_targets(payload: object, root: Path, config: Path) -> bool:
    if not isinstance(payload, dict) or not payload.get("config_path") or not payload.get("project_root"):
        return False
    try:
        return (Path(payload["project_root"]).resolve() == root.resolve()
                and Path(payload["config_path"]).resolve() == config.resolve())
    except (TypeError, OSError, ValueError):
        return False


def matches_service(payload: object, root: Path, config: Path, owner_token=None) -> bool:
    if not service_targets(payload, root, config) or payload.get('desktop_launcher_version') != 3:
        return False
    try:
        if not payload.get('backend_directory') or Path(payload['backend_directory']).resolve() != CORE.resolve():
            return False
    except (TypeError, OSError, ValueError):
        return False
    if owner_token is not None:
        from part_selection_lifecycle import owner_id
        return payload.get('desktop_owner_id') == owner_id(owner_token)
    return True


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
    configured = os.environ.get("KICAD_PART_SELECTION_HOME")
    directory = Path(os.path.expandvars(configured)).expanduser() if configured else base / "KiCadPartSelection"
    if not directory.is_absolute():
        raise ValueError("KICAD_PART_SELECTION_HOME must be an absolute path")
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


def ensure_backend(root: Path, config: Path, logs: Path, *, owner_token: str) -> str:
    first = 18000 + int(project_key(root)[:6], 16) % 18000
    ports = [8765, *(first + offset for offset in range(10))]
    # Prefer the stable project-specific port if already running.
    for port in ports:
        info = service_info(port)
        if service_targets(info, root, config):
            if not matches_service(info, root, config, owner_token):
                raise RuntimeError('该工程仍有旧版或其他工作台后台运行。请先退出原工作台；旧版后台需手动退出一次。不要重复启动或结束 KiCad。')
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
                 "--config", str(config), "--host", "127.0.0.1", "--port", str(port),
                 "--desktop-owner-pid", str(os.getpid())],
                cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=output, **options,
                env={**os.environ, 'KICAD_PART_SELECTION_DESKTOP_TOKEN':owner_token},
            )
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                if matches_service(service_info(port), root, config, owner_token):
                    logging.info("Started matching service on %s pid=%s", port, process.pid)
                    return f"http://127.0.0.1:{port}/"
                time.sleep(0.15)
        finally:
            if process.poll() is None and not matches_service(service_info(port), root, config, owner_token):
                process.terminate()
                process.wait(timeout=5)
    raise RuntimeError(f"Could not start a matching service. See {logs / 'server.log'}")


def relaunch_desktop(root, config, logs):
    options = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    python = Path(sys.executable)
    if os.name == 'nt' and python.with_name('pythonw.exe').exists():
        python = python.with_name('pythonw.exe')
    with (logs / 'window.log').open('ab') as output:
        subprocess.Popen([str(python),'-B',str(HERE / 'desktop.py'),
                          '--project',str(root),'--config',str(config)],cwd=root,
                         stdin=subprocess.DEVNULL,stdout=output,stderr=output,**options)


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
    log_base = cache_directory()
    logging.basicConfig(filename=log_base / "desktop.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    logging.info("Desktop entry started entrypoint=%s python=%s pid=%s cwd=%s "
                 "ipc_socket_present=%s ipc_token_present=%s",
                 Path(__file__).resolve(), sys.executable, os.getpid(), Path.cwd(),
                 bool(os.environ.get("KICAD_API_SOCKET")), bool(os.environ.get("KICAD_API_TOKEN")))
    parser = argparse.ArgumentParser(description="KiCad part-selection desktop host")
    parser.add_argument("--project", type=Path, help="Absolute project file or directory")
    parser.add_argument("--config", type=Path, help="Explicit project configuration JSON")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        logging.info("Argument parser exited status=%s", exc.code)
        raise
    handle = None
    lifecycle = None
    try:
        if args.config:
            args.config = Path(os.path.expandvars(str(args.config))).expanduser().resolve()
        if args.project:
            root = validate_project(args.project)
        elif args.config:
            sys.path.insert(0, str(CORE))
            from part_selection_overrides import load_config
            if not args.config.is_file():
                raise FileNotFoundError(f"Configuration not found: {args.config}")
            root = validate_project(load_config(args.config)["project_root"])
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
        config = ensure_config(root, args.config) if args.config else ensure_config(root)
        sys.path.insert(0, str(CORE))
        from part_selection_overrides import load_config
        from part_selection_settings import apply_runtime_settings
        apply_runtime_settings(load_config(config))
        log_base = cache_directory()
        if log_base.resolve().is_relative_to(root.resolve()):
            raise ValueError("Desktop runtime must be outside the KiCad project")
        logs = log_base / project_key(root)
        logs.mkdir(exist_ok=True)
        try:
            import webview
        except ImportError:
            installer, installing = acquire_window(logs / "runtime-install")
            try:
                if not installing:
                    raise RuntimeError(f"Desktop dependencies are installing. See {logs / 'runtime-install.log'}")
                python = ensure_desktop_runtime(logs)
                with (logs / "window.log").open("ab") as output:
                    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                    subprocess.Popen([str(python), "-B", str(HERE / "desktop.py"),
                                      "--project", str(root), "--config", str(config)], cwd=root, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=output, **options)
                return 0
            finally:
                release_window(installer)
        title = f"{TITLE} | {root.name} [{project_key(root)[:6]}]"
        handle, acquired = acquire_window(root)
        if not acquired:
            if not activate_window(title):
                raise RuntimeError("Workbench is starting. Retry shortly; see desktop.log if it does not appear.")
            return 0
        from part_selection_lifecycle import DesktopLifecycle, DesktopBridge
        token = secrets.token_urlsafe(32)
        url = ensure_backend(root, config, logs, owner_token=token)
        lifecycle = DesktopLifecycle(url, token)
        logging.info("Opening project=%s url=%s", root, url)
        window = webview.create_window(title, url, width=1380, height=900, min_size=(800, 500),
                                       text_select=True, js_api=DesktopBridge(lifecycle))
        lifecycle.window = window
        window.events.loaded += lambda: enable_browser_shortcuts(window)
        window.events.closing += lifecycle.on_closing
        webview.start(gui="edgechromium", private_mode=False,
                      storage_path=str(logs / "webview"))
        if lifecycle.restart:
            release_window(handle)
            handle = None
            relaunch_desktop(root, config, logs)
        return 0
    except Exception as exc:
        logging.exception("Desktop launch failed")
        show_error(f"{exc}\n\n{log_base / 'desktop.log'}")
        return 1
    finally:
        if lifecycle and not lifecycle.allow_close:
            try:
                lifecycle._stop_backend()
            except Exception:
                logging.exception('Desktop startup/exit cleanup deferred to owner watchdog')
        release_window(handle)


if __name__ == "__main__":
    raise SystemExit(main())

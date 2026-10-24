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


class AmbiguousLaunchProject(ValueError):
    pass


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


def project_from_ipc(client, *, allow_environment=True, expected_token=None) -> Path:
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
                if (not allow_environment or (expected_token is not None and
                        getattr(getattr(client, '_client', None), '_kicad_token', '') != expected_token)):
                    raise
                root = schematic_project_from_environment(doc)
            roots.add(root)
        if len(roots) > 1:
            raise AmbiguousLaunchProject("检测到多个原理图工程，请核对当前入口后重试")
        if not roots:
            raise ValueError("Cannot identify one schematic project; select its project file")
        return roots.pop()
    except AmbiguousLaunchProject:
        raise
    except Exception as exc:
        raise ValueError("KiCad did not supply one valid saved project; select its project file") from exc


def verify_launching_schematic(root: Path) -> bool:
    """Read-only current-manager check; no save, opening, or endpoint adoption."""
    script = CORE / 'kicad_schematic_search.ps1'
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-WindowStyle','Hidden',
        '-ExecutionPolicy','Bypass','-File',str(script),'-ProjectRoot',str(root),'-VerifySchematicProject'],
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW)
    return result.returncode == 0 and result.stdout.strip() == 'SchematicProjectVerified=1'


def resolve_launch_project() -> Path:
    """Recover a stale entry from independently verified native project ownership."""
    from kipy import KiCad
    started = time.monotonic()
    inherited = os.environ.get("KICAD_API_SOCKET", "")
    expected = None
    if os.environ.get("KIPRJMOD"):
        try:
            expected = validate_project(Path(os.environ["KIPRJMOD"]))
        except ValueError:
            logging.warning("Launching project environment could not be validated")
    try:
        client = KiCad(socket_path=inherited, kicad_token=os.environ.get("KICAD_API_TOKEN", ""),
                       client_name="component-workbench-launch", timeout_ms=400)
        root = project_from_ipc(client, allow_environment=bool(os.environ.get('KICAD_API_TOKEN')),
                                expected_token=os.environ.get('KICAD_API_TOKEN', ''))
    except AmbiguousLaunchProject:
        raise
    except Exception as exc:
        logging.info("Launching IPC unavailable; checking current manager (%s)", type(exc).__name__)
    else:
        if expected is not None and root != expected:
            raise ValueError("启动工程与 KiCad 文档不一致，请从目标工程原理图重新点击入口后重试")
        return root

    if expected is not None and verify_launching_schematic(expected):
        # The workbench reads saved project data and can discover the PCB later.
        # Never attach a discovered same-basename server's socket/token here.
        os.environ.pop('KICAD_API_SOCKET', None)
        os.environ.pop('KICAD_API_TOKEN', None)
        logging.info('Recovered launching project through verified manager elapsed_ms=%.1f',
                     (time.monotonic()-started)*1000)
        return expected
    raise ValueError("原理图连接暂时不可用，请完成 KiCad 中的对话框后重试；无需彻底退出 KiCad。"
                     "未能核验工程，不会自动打开其他工程或要求重新选择文件。")


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


def show_edit_menu(window, context):
    """Pages can request a popup, never execute clipboard commands themselves."""
    import math
    from System import Action
    from System.Drawing import Point
    from System.Windows.Forms import ContextMenuStrip, ToolStripMenuItem

    if not isinstance(context, dict) or any(type(context.get(key)) is not bool
                                          for key in ('selected', 'writable')):
        return {'ok': False}
    if any(type(context.get(key)) not in (int, float) or not math.isfinite(context[key])
           for key in ('x', 'y', 'width', 'height')):
        return {'ok': False}
    if not (0 <= context['x'] <= context['width'] <= 100000 and
            0 <= context['y'] <= context['height'] <= 100000 and
            context['width'] > 0 and context['height'] > 0):
        return {'ok': False}

    def show():
        view = window.native.webview
        source = str(view.CoreWebView2.Source)
        generation = getattr(window, '_workbench_edit_generation', 0)
        previous = getattr(window, '_workbench_edit_popup', None)
        if previous is not None:
            previous.Close()
            previous.Dispose()
        menu = ContextMenuStrip()
        window._workbench_edit_popup = menu
        commands = [('undo', '撤销', 'Ctrl+Z'), ('redo', '重做', 'Ctrl+Y'),
                    ('cut', '剪切', 'Ctrl+X'), ('copy', '复制', 'Ctrl+C'),
                    ('paste', '粘贴', 'Ctrl+V'), ('selectAll', '全选', 'Ctrl+A')]
        for name, label, shortcut in commands:
            item = ToolStripMenuItem(label)
            item.ShortcutKeyDisplayString = shortcut
            item.Enabled = (context['selected'] if name == 'copy' else True if name == 'selectAll'
                            else context['writable'] and (name != 'cut' or context['selected']))

            def execute(sender, event, command=name):
                # Only a native menu selection reaches this handler. Refuse a
                # stale popup after navigation; no page-supplied code is run.
                if (str(view.CoreWebView2.Source) != source or
                        getattr(window, '_workbench_edit_generation', 0) != generation):
                    return
                view.CoreWebView2.CallDevToolsProtocolMethodAsync('Input.dispatchKeyEvent',
                    json.dumps({'type': 'rawKeyDown', 'commands': [command]}))

            item.Click += execute
            menu.Items.Add(item)
        menu.Show(view, Point(int(context['x'] * view.ClientSize.Width / context['width']),
                              int(context['y'] * view.ClientSize.Height / context['height'])))
    window.native.Invoke(Action(show))
    return {'ok': True}


def enable_browser_shortcuts(window) -> None:
    try:
        from System import Action

        def enable():
            core = window.native.webview.CoreWebView2
            core.Settings.AreBrowserAcceleratorKeysEnabled = True
            core.Settings.AreDefaultContextMenusEnabled = True
            if not getattr(window, '_workbench_edit_navigation', None) and hasattr(core, 'NavigationStarting'):
                def invalidate_menu(sender, args):
                    window._workbench_edit_generation = getattr(window, '_workbench_edit_generation', 0) + 1
                    popup = getattr(window, '_workbench_edit_popup', None)
                    if popup is not None:
                        popup.Close()
                core.NavigationStarting += invalidate_menu
                window._workbench_edit_navigation = invalidate_menu
            if not getattr(window, '_workbench_edit_menu', None) and hasattr(core, 'ContextMenuRequested'):
                def edit_menu(sender, args):
                    # Keep WebView2's native edit commands and selection handling;
                    # never read the clipboard or grant a clipboard API to pages.
                    allowed = {'undo', 'redo', 'cut', 'copy', 'paste', 'pasteandmatchstyle', 'selectall'}
                    for index in range(args.MenuItems.Count - 1, -1, -1):
                        if str(args.MenuItems[index].Name).lower() not in allowed:
                            args.MenuItems.RemoveAt(index)
                core.ContextMenuRequested += edit_menu
                window._workbench_edit_menu = edit_menu

        window.native.Invoke(Action(enable))
        if not getattr(window, '_workbench_edit_bridge', None):
            def workbench_edit(context):
                return show_edit_menu(window, context)
            window.expose(workbench_edit)
            window._workbench_edit_bridge = workbench_edit
        script = (CORE / 'web' / 'edit-menu.js').read_text(encoding='utf-8')
        window.native.Invoke(Action(lambda: window.native.webview.CoreWebView2.ExecuteScriptAsync(script)))
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
    from part_selection_paths import resolve_config
    return resolve_config(root, config_path)


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


def port_available(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def occupied_ports(ports: list[int]) -> list[int]:
    return [port for port in ports if not port_available(port)]


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
    startup_started = time.perf_counter()
    first = 18000 + int(project_key(root)[:6], 16) % 18000
    ports = [8765, *(first + offset for offset in range(10))]
    # Empty ports need no HTTP request; probing each one with a timeout added
    # up to several seconds to every cold launch.
    occupied = occupied_ports(ports)
    logging.info("Backend port scan elapsed_ms=%.1f occupied=%s",
                 (time.perf_counter() - startup_started) * 1000, occupied)
    # Prefer the stable project-specific port if already running.
    for port in occupied:
        info = service_info(port)
        if isinstance(info, dict) and info.get('project_root') and Path(info['project_root']).resolve() == root.resolve():
            if not matches_service(info, root, config, owner_token):
                raise RuntimeError('该工程仍有旧版或其他工作台后台运行。请先退出原工作台；旧版后台需手动退出一次。不要重复启动或结束 KiCad。')
            logging.info("Reusing matching service on %s elapsed_ms=%.1f",
                         port, (time.perf_counter() - startup_started) * 1000)
            return f"http://127.0.0.1:{port}/"
    for port in ports:
        if not port_available(port):
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
                    logging.info("Started matching service on %s pid=%s elapsed_ms=%.1f",
                                 port, process.pid, (time.perf_counter() - startup_started) * 1000)
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
    updater = None
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
        elif os.environ.get("KICAD_API_SOCKET"):
            root = resolve_launch_project()
        else:
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
        from updater import PluginUpdater
        updater = PluginUpdater(HERE)
        updater.register(project=root)
        from part_selection_lifecycle import DesktopLifecycle, DesktopBridge
        from part_selection_tool_windows import ToolWindows
        token = secrets.token_urlsafe(32)
        url = ensure_backend(root, config, logs, owner_token=token)
        lifecycle = DesktopLifecycle(url, token)
        lifecycle.project_id = str(root.resolve())
        lifecycle.updater = updater
        logging.info("Opening project=%s url=%s", root, url)
        window = webview.create_window(title, url, width=1380, height=900, min_size=(800, 500),
                                       text_select=True, js_api=DesktopBridge(lifecycle))
        lifecycle.window = window
        lifecycle.tools = ToolWindows(lifecycle, webview.create_window, enable_browser_shortcuts)
        window.events.loaded += lambda: enable_browser_shortcuts(window)
        window.events.closing += lifecycle.on_closing
        webview.start(gui="edgechromium", private_mode=False,
                      storage_path=str(logs / "webview"))
        if lifecycle.install_update:
            try:
                updater.apply()
            except Exception as exc:
                logging.exception('Plugin update failed; previous installation retained')
                show_error(f'插件更新失败，已保留或恢复原安装。\n{exc}\n\n更新日志：{updater.home}')
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
        if updater:
            updater.unregister()
        if lifecycle and not lifecycle.allow_close:
            try:
                lifecycle._stop_backend()
            except Exception:
                logging.exception('Desktop startup/exit cleanup deferred to owner watchdog')
        release_window(handle)


if __name__ == "__main__":
    raise SystemExit(main())

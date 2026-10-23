"""Typed IPC discovery and conservative opening of a uniquely identified PCB."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import threading
import time


def process_arguments(command):
    if not command or os.name != 'nt':
        return None
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not pointer:
        return None
    try:
        return [pointer[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(pointer)


def inspect_processes():
    if os.name != 'nt':
        return {'complete':False,'processes':[]}
    command = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
               "@(Get-CimInstance Win32_Process -Filter \"Name='pcbnew.exe'\" | "
               "Select-Object ProcessId,CreationDate,CommandLine) | ConvertTo-Json -Compress")
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                            capture_output=True,text=True,encoding='utf-8-sig',timeout=8,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        return {'complete':False,'processes':[], 'error':result.stderr[:300]}
    rows = json.loads(result.stdout or '[]')
    if isinstance(rows, dict):
        rows = [rows]
    processes = []
    for row in rows:
        args = process_arguments(row.get('CommandLine'))
        paths = [arg for arg in args or [] if arg.lower().endswith('.kicad_pcb')]
        files = [str(Path(path).resolve()) for path in paths] if all(Path(path).is_absolute() for path in paths) else None
        processes.append({'pid':row['ProcessId'],'started':row.get('CreationDate'), 'files':files})
    enabled = None
    config = Path(os.environ.get('APPDATA','')) / 'kicad/10.0/kicad_common.json'
    try:
        enabled = json.loads(config.read_text(encoding='utf-8-sig')).get('api', {}).get('enable_server')
    except (OSError, ValueError):
        pass
    return {'complete':True,'processes':processes,'ipc_enabled':enabled}


def launch_editor(target):
    from part_selection_footprint_geometry import _find_kicad_python
    python = _find_kicad_python()
    if python is None or os.name != 'nt':
        raise ValueError('找不到可启动的 KiCad PCB 编辑器')
    executable = python.parent / 'pcbnew.exe'
    if not executable.is_file():
        raise ValueError('KiCad PCB 编辑器程序不存在')
    env = dict(os.environ)
    env.pop('KICAD_API_SOCKET', None)
    env.pop('KICAD_API_TOKEN', None)
    return subprocess.Popen([str(executable), str(target)],env=env)


class PcbConnection:
    def __init__(self, root, *, discover=None, inspect_processes=None, launch=None, clock=None):
        self.root = Path(root).resolve()
        self.discover = discover or self._discover
        self.inspect_processes = inspect_processes or globals()['inspect_processes']
        self.launch = launch or launch_editor
        self.clock = clock or time
        self.lock = threading.RLock()
        self.opened = None

    def _discover(self):
        from part_selection_pcb_session import PcbSession
        return PcbSession(self.root).discover()

    def target(self):
        boards = list(self.root.glob('*.kicad_pcb'))
        projects = list(self.root.glob('*.kicad_pro'))
        if len(boards) != 1 or len(projects) != 1 or boards[0].stem != projects[0].stem:
            raise ValueError('无法唯一确定当前工程主 PCB；请核对工程和文件')
        return boards[0].resolve()

    def inspect(self):
        try:
            discovery = self.discover()
        except Exception as exc:
            discovery = {'state':'unknown','reason':str(exc)}
        if discovery.get('identity_unverified') or discovery.get('state') in (
                'connected','busy','ipc_disabled','ambiguous','disconnected'):
            return discovery
        try:
            target = self.target()
        except ValueError as exc:
            return {'state':'ambiguous','reason':discovery.get('reason') or str(exc)}
        try:
            scan = self.inspect_processes()
        except Exception as exc:
            return {'state':'unknown','reason':'无法核实 PCB 进程：' + str(exc)}
        if not scan.get('complete'):
            return {'state':'unknown','reason':'无法完整核实正在运行的 PCB 编辑器；请打开当前工程 PCB 并重试'}
        unknown = False
        for process in scan.get('processes', []):
            files = process.get('files')
            if not files or not process.get('started'):
                unknown = True
                continue
            if target in [Path(path).resolve() for path in files]:
                state = 'ipc_disabled' if scan.get('ipc_enabled') is False else 'busy'
                return {'state':state, 'file':str(target), 'process':process,
                        'reason':'当前工程 PCB 已打开，但 IPC 未响应；请启用 IPC 或关闭阻塞对话框后重试'}
        if unknown:
            return {'state':'unknown','reason':'有 PCB 进程无法核实文档；请打开当前工程 PCB 并重试，不会重复打开'}
        return {'state':'closed','file':str(target),'reason':'已核实当前工程 PCB 未打开'}

    def ensure(self, *, open_if_closed, progress=None):
        notify = progress or (lambda *args:None)
        with self.lock:
            result = self.inspect()
            if result['state'] != 'closed' or not open_if_closed:
                return result
            if self.opened is not None:
                return {'state':'disconnected','reason':'此前已请求打开 PCB，但尚未连接；请检查已打开窗口后重试，不会重复启动'}
            target = self.target()
            notify('opening','正在打开当前工程 PCB')
            try:
                self.opened = self.launch(target)
                # Injected launchers need not return a process handle.
                if self.opened is None:
                    self.opened = str(target)
            except Exception as exc:
                return {'state':'unknown','reason':'PCB 启动未确认：' + str(exc)}
            notify('connecting','正在等待 PCB 原生接口就绪')
            deadline = self.clock.monotonic() + 30
            while self.clock.monotonic() < deadline:
                result = self.inspect()
                if result['state'] == 'connected':
                    notify('connected','已连接当前工程 PCB')
                    return result
                if result['state'] == 'ambiguous':
                    return result
                self.clock.sleep(.3)
            return {'state':'disconnected','file':str(target),
                    'reason':'PCB 已请求打开，等待 IPC 超时；编辑器已保留，请检查 IPC 设置后重试'}

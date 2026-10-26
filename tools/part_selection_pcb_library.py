"""Read complete native library definitions without placing them on the user PCB.

KiCad 10.0's IPC has no library-read command. A short-lived, read-only editor
on a private Windows desktop serializes a fixture; results are cached by hash.
"""
from copy import deepcopy
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

_CACHE = {}
_LOCK = threading.RLock()
_CACHE_LIMIT = 256


def cache_key(source, content_hash, back, lib_id):
    return (str(source), content_hash, bool(back), lib_id)


class _StartupInfo(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('lpReserved', wintypes.LPWSTR),
        ('lpDesktop', wintypes.LPWSTR), ('lpTitle', wintypes.LPWSTR),
        ('dwX', wintypes.DWORD), ('dwY', wintypes.DWORD),
        ('dwXSize', wintypes.DWORD), ('dwYSize', wintypes.DWORD),
        ('dwXCountChars', wintypes.DWORD), ('dwYCountChars', wintypes.DWORD),
        ('dwFillAttribute', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
        ('wShowWindow', wintypes.WORD), ('cbReserved2', wintypes.WORD),
        ('lpReserved2', ctypes.POINTER(wintypes.BYTE)),
        ('hStdInput', wintypes.HANDLE), ('hStdOutput', wintypes.HANDLE),
        ('hStdError', wintypes.HANDLE)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [('hProcess', wintypes.HANDLE), ('hThread', wintypes.HANDLE),
               ('dwProcessId', wintypes.DWORD), ('dwThreadId', wintypes.DWORD)]


class _JobLimits(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]


class _ExtendedJobLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _JobLimits), ('IoInfo', ctypes.c_uint64 * 6),
        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class _JobAccounting(ctypes.Structure):
    _fields_ = [('times', ctypes.c_int64 * 4), ('TotalPageFaultCount', wintypes.DWORD),
        ('TotalProcesses', wintypes.DWORD), ('ActiveProcesses', wintypes.DWORD),
        ('TotalTerminatedProcesses', wintypes.DWORD)]


def _bind(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.restype, function.argtypes = result, list(arguments)
    return function


class _ReaderProcess:
    def __init__(self, kernel, info, args):
        self.kernel, self.handle, self.pid, self.args = kernel, info.hProcess, info.dwProcessId, args
        self.thread_id = info.dwThreadId

    def poll(self):
        return self._wait(0)

    def _wait(self, milliseconds):
        status = self.kernel.WaitForSingleObject(self.handle, milliseconds)
        if status == 0x102:  # WAIT_TIMEOUT; do not confuse exit code 259 with a live process.
            return None
        if status != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    def wait(self, timeout):
        code = self._wait(max(0, int(timeout * 1000)))
        if code is None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return code


class _ReaderDesktop:
    """Own only the reader's desktop/process handles; never switch desktops."""
    def __enter__(self):
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.name = 'kw-pcb-reader-' + uuid4().hex
        self.process = None
        _bind(self.user, 'CreateDesktopW', wintypes.HANDLE, wintypes.LPCWSTR,
              wintypes.LPCWSTR, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID)
        _bind(self.user, 'CloseDesktop', wintypes.BOOL, wintypes.HANDLE)
        _bind(self.kernel, 'CreateProcessW', wintypes.BOOL, wintypes.LPCWSTR,
              wintypes.LPWSTR, wintypes.LPVOID, wintypes.LPVOID, wintypes.BOOL,
              wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
              ctypes.POINTER(_StartupInfo), ctypes.POINTER(_ProcessInfo))
        _bind(self.kernel, 'CloseHandle', wintypes.BOOL, wintypes.HANDLE)
        _bind(self.kernel, 'WaitForSingleObject', wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
        _bind(self.kernel, 'GetExitCodeProcess', wintypes.BOOL, wintypes.HANDLE,
              ctypes.POINTER(wintypes.DWORD))
        _bind(self.kernel, 'CreateJobObjectW', wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCWSTR)
        _bind(self.kernel, 'SetInformationJobObject', wintypes.BOOL, wintypes.HANDLE,
              ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
        _bind(self.kernel, 'AssignProcessToJobObject', wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE)
        _bind(self.kernel, 'QueryInformationJobObject', wintypes.BOOL, wintypes.HANDLE,
              ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID)
        _bind(self.kernel, 'TerminateJobObject', wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
        _bind(self.kernel, 'TerminateProcess', wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
        _bind(self.kernel, 'ResumeThread', wintypes.DWORD, wintypes.HANDLE)
        # READOBJECTS | CREATEWINDOW | CREATEMENU | ENUMERATE | WRITEOBJECTS.
        self.handle = self.user.CreateDesktopW(self.name, None, None, 0, 0x00C7, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.job = self.kernel.CreateJobObjectW(None, None)
        limits = _ExtendedJobLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway.
        if not self.job or not self.kernel.SetInformationJobObject(
                self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            if self.job:
                self.kernel.CloseHandle(self.job)
            self.user.CloseDesktop(self.handle)
            raise error
        return self

    def start(self, args, env, cwd):
        if not self.handle or self.process is not None:
            raise ValueError('A library reader requires its own open private desktop')
        startup = _StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.lpDesktop = self.name
        startup.dwFlags = 0x81  # STARTF_USESHOWWINDOW | STARTF_FORCEOFFFEEDBACK.
        startup.wShowWindow = 0
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
        environment = ctypes.create_unicode_buffer('\0'.join(
            key + '=' + value for key, value in sorted(env.items(), key=lambda item: item[0].upper())) + '\0\0')
        info = _ProcessInfo()
        if not self.kernel.CreateProcessW(args[0], command, None, None, False,
                0x08000404, environment, str(cwd), ctypes.byref(startup), ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.process = _ReaderProcess(self.kernel, info, args)
        try:
            # Assign while suspended: a helper can never escape before Job ownership.
            if not self.kernel.AssignProcessToJobObject(self.job, info.hProcess):
                error = ctypes.WinError(ctypes.get_last_error())
                self.kernel.TerminateProcess(info.hProcess, 1)  # Our never-resumed child only.
                self.process.wait(timeout=5)
                raise error
            if self.kernel.ResumeThread(info.hThread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(info.hThread)
        return self.process

    def __exit__(self, *exception):
        try:
            # Normally the editor has exited via WM_CLOSE. Reap only Job-owned
            # orphan helpers (or our own failed scratch reader), never user PIDs.
            if not self.kernel.TerminateJobObject(self.job, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            deadline = time.monotonic() + 5
            accounting = _JobAccounting()
            while True:
                if not self.kernel.QueryInformationJobObject(self.job, 1,
                        ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not accounting.ActiveProcesses:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('Private library reader helpers did not exit')
                time.sleep(.05)
        finally:
            if self.process is not None:
                self.kernel.CloseHandle(self.process.handle)
            self.kernel.CloseHandle(self.job)
            if not self.user.CloseDesktop(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


def _close_reader(process, desktop):
    user = desktop.user
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _bind(user, 'EnumDesktopWindows', wintypes.BOOL, wintypes.HANDLE, callback_type, wintypes.LPARAM)
    _bind(user, 'GetWindowThreadProcessId', wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _bind(user, 'GetWindowTextW', ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _bind(user, 'PostMessageW', wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == process.pid:
            title = ctypes.create_unicode_buffer(1024)
            user.GetWindowTextW(hwnd, title, len(title))
            if 'LibraryRead' in title.value:
                user.PostMessageW(hwnd, 0x0010, 0, 0)
        return True
    callback = callback_type(visit)
    deadline = time.monotonic() + 15
    while process.poll() is None:
        ctypes.set_last_error(0)
        if not user.EnumDesktopWindows(desktop.handle, callback, 0):
            error = ctypes.get_last_error()
            if error:
                raise ctypes.WinError(error)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, 15)
        try:
            process.wait(timeout=min(.25, remaining))
        except subprocess.TimeoutExpired:
            continue


@contextmanager
def _reader_workspace(anchor):
    work = Path(tempfile.mkdtemp(prefix='kw-pcb-reader-', dir=anchor))
    try:
        yield work
    finally:
        original = sys.exc_info()[1]
        for attempt in range(21):
            try:
                shutil.rmtree(work)
                break
            except OSError as error:
                if attempt == 20:
                    message = 'Library reader scratch cleanup failed; evidence retained at ' + str(work)
                    if original is None:
                        raise RuntimeError(message) from error
                    if hasattr(original, 'add_note'):
                        original.add_note(message)
                    break
                time.sleep(.25)


def _build(payload):
    import pcbnew as p
    root = Path(payload['root'])
    if root.name != 'project' or not root.parent.name.startswith('kw-pcb-reader-'):
        raise ValueError('Not an isolated library reader')
    board = p.BOARD()
    source = Path(payload['source'])
    fp = p.FootprintLoad(str(source.parent), source.stem)
    if fp is None:
        raise ValueError('KiCad could not load the library footprint')
    nickname, name = payload['id'].split(':', 1)
    fp.SetFPID(p.LIB_ID(nickname, name))
    board.Add(fp)
    fp.SetReference('LibraryTarget')
    if payload['back']:
        fp.Flip(p.VECTOR2I(0, 0), False)
    fp.SetOrientationDegrees(0)
    fp.SetPosition(p.VECTOR2I(0, 0))
    p.SaveBoard(str(root / 'LibraryRead.kicad_pcb'), board)
    (root / 'LibraryRead.kicad_pro').write_text('{}', encoding='utf-8')


def read_library(project_root, lib_id, *, back=False):
    from part_selection_footprints import FootprintChoices, checked_path, item_name
    from part_selection_footprint_geometry import _find_kicad_python
    from part_selection_pcb_session import describe, select_document
    from kipy import KiCad
    from kipy.board import Board
    from kipy.proto.common.types import DocumentType
    root = Path(project_root).resolve()
    if not isinstance(lib_id, str) or lib_id.count(':') != 1:
        raise ValueError('请选择有效的工程库封装')
    nickname, name = (item_name(value) for value in lib_id.split(':'))
    libraries = FootprintChoices(root).libraries()
    if nickname not in libraries:
        raise ValueError('目标封装必须位于工程已注册的本地库中')
    source = checked_path(root, libraries[nickname] / (name + '.kicad_mod'))
    with _LOCK:
        content_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        key = cache_key(source, content_hash, back, lib_id)
        if key in _CACHE:
            cached = _CACHE.pop(key)
            _CACHE[key] = cached
            return deepcopy(cached)
        native = _find_kicad_python()
        if native is None or os.name != 'nt':
            raise ValueError('当前平台缺少已验证的 KiCad 原生库读取环境')
        # Short external paths also avoid legacy Windows converter/path limits.
        with _reader_workspace(root.anchor) as work:
            fixture = work / 'project'
            fixture.mkdir()
            subprocess.run([str(native), '-B', str(Path(__file__)), '--worker'],
                input=json.dumps({'root': str(fixture), 'source': str(source), 'id': lib_id, 'back': bool(back)}),
                capture_output=True, text=True, check=True, timeout=45,
                creationflags=subprocess.CREATE_NO_WINDOW)
            settings = work / 'config' / '10.0'
            settings.mkdir(parents=True)
            for path in (Path(os.environ['APPDATA']) / 'kicad' / '10.0').iterdir():
                if path.is_file() and (path.suffix == '.json' or path.name.endswith('lib-table')):
                    shutil.copy2(path, settings / path.name)
            common = settings / 'kicad_common.json'
            config = json.loads(common.read_text(encoding='utf-8'))
            config.setdefault('api', {})['enable_server'] = True
            config.setdefault('auto_backup', {})['enabled'] = False
            common.write_text(json.dumps(config), encoding='utf-8')
            ipc = work / 'ipc'
            ipc.mkdir()
            env = dict(os.environ, KICAD_CONFIG_HOME=str(settings.parent), TEMP=str(ipc), TMP=str(ipc))
            # Do not inherit a production API socket/token into the reader.
            env.pop('KICAD_API_SOCKET', None)
            env.pop('KICAD_API_TOKEN', None)
            with _ReaderDesktop() as desktop:
                process = desktop.start([str(native.parent / 'pcbnew.exe'),
                    str(fixture / 'LibraryRead.kicad_pcb')], env, fixture)
                result = None
                try:
                    client = KiCad(socket_path='ipc://' + str(ipc / 'kicad' / 'api.sock'),
                                   client_name='workbench-library-reader', kicad_token='', timeout_ms=1000)
                    last_error = None
                    for _ in range(100):
                        if process.poll() is not None:
                            raise ValueError('Native library reader exited before IPC verification')
                        try:
                            document = select_document(client.get_open_documents(DocumentType.DOCTYPE_PCB), fixture)
                            board = Board(client._client, document)
                            footprints = board.get_footprints()
                            if len(footprints) != 1:
                                raise ValueError('Unexpected objects in native library reader')
                            result = describe(footprints[0])
                            break
                        except Exception as exc:
                            last_error = exc
                            time.sleep(.25)
                    if result is None:
                        raise ValueError('原生库读取失败：' + str(last_error))
                finally:
                    _close_reader(process, desktop)
        if hashlib.sha256(source.read_bytes()).hexdigest() != content_hash:
            raise ValueError('读取期间目标库已改变，请重试')
        result.update(source_path=str(source), source_hash=content_hash)
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = result
        return deepcopy(result)


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit('This module is an isolated native reader worker')
    _build(json.load(sys.stdin))

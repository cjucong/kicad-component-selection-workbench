"""Read complete native library definitions without placing them on the user PCB.

KiCad 10.0's IPC has no library-read command. A short-lived, hidden, read-only
editor serializes an isolated library fixture; results are cached by file hash.
"""
from copy import deepcopy
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

_CACHE = {}
_LOCK = threading.RLock()


def cache_key(source, content_hash, back, lib_id):
    return (str(source), content_hash, bool(back), lib_id)


def _close_reader(process):
    user = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
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
    user.EnumWindows(callback, 0)
    process.wait(timeout=15)


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
    content_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    key = cache_key(source, content_hash, back, lib_id)
    with _LOCK:
        if key in _CACHE:
            return deepcopy(_CACHE[key])
        native = _find_kicad_python()
        if native is None or os.name != 'nt':
            raise ValueError('当前平台缺少已验证的 KiCad 原生库读取环境')
        # Short external paths also avoid legacy Windows converter/path limits.
        with tempfile.TemporaryDirectory(prefix='kw-pcb-reader-', dir=root.anchor) as temporary:
            work = Path(temporary)
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
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            process = subprocess.Popen([str(native.parent / 'pcbnew.exe'), str(fixture / 'LibraryRead.kicad_pcb')],
                env=env, startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
            client = KiCad(socket_path='ipc://' + str(ipc / 'kicad' / 'api.sock'),
                           client_name='workbench-library-reader', kicad_token='', timeout_ms=1000)
            result = None
            try:
                last_error = None
                for _ in range(100):
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
                _close_reader(process)
        if hashlib.sha256(source.read_bytes()).hexdigest() != content_hash:
            raise ValueError('读取期间目标库已改变，请重试')
        result.update(source_path=str(source), source_hash=content_hash)
        if len(_CACHE) >= 32:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = result
        return deepcopy(result)


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit('This module is an isolated native reader worker')
    _build(json.load(sys.stdin))

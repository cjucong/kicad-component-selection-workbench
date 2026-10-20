"""Isolated EasyKiConverter imports; never replaces placed symbols or PCB data."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import hashlib
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from fpga_pin_assignment import parse_sexpr
from part_selection_runtime import runtime_directory

PINNED_VERSION = '3.1.11'
PINNED_COMMIT = '1acc47c028ed1f75066ba134f6a2f327c0751c0e'
IMPORT_LOCK = threading.RLock()


def find_converter(configured=None):
    candidates = [configured, os.environ.get('PART_SELECTION_CONVERTER')]
    for parent in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
        candidates.append(parent / 'third_party/EasyKiConverter/runtime/easykiconverter.exe')
    if os.name == 'nt':
        documents = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, documents):
            candidates.append(Path(documents.value) / 'KiCad/10.0/plugins/com_smu_part-selection/third_party/EasyKiConverter/runtime/easykiconverter.exe')
    candidates.append(shutil.which('easykiconverter'))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise FileNotFoundError('EasyKiConverter not installed: use the full plugin package or set PART_SELECTION_CONVERTER')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def proxy_diagnostics():
    # Qt can use Windows settings independently of HTTP_PROXY. Never log credentials.
    result = {'proxy_environment_present': any(os.environ.get(k) for k in
              ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')),
              'system_proxy_enabled': None, 'routing': 'converter default; global settings unchanged'}
    if os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
                result['system_proxy_enabled'] = bool(winreg.QueryValueEx(key, 'ProxyEnable')[0])
        except OSError:
            pass
    return result


def save_converter_log(temp, stamp, work, stdout=b'', stderr=b''):
    folder = checked_path(temp, temp / 'part_selection_converter_logs')
    folder.mkdir(exist_ok=True)
    def as_bytes(value):
        return value.encode('utf-8') if isinstance(value, str) else value or b''
    content = json.dumps(proxy_diagnostics()).encode() + b'\n' + as_bytes(stdout) + b'\n' + as_bytes(stderr)
    if (work / 'converter.log').is_file():
        content += b'\n' + (work / 'converter.log').read_bytes()
    path = folder / (stamp + '.log')
    path.write_bytes(content)
    return str(path.resolve())


def hidden_converter_options():
    if os.name != 'nt':
        return {}
    # v3.1.11 calls AllocConsole itself: CREATE_NO_WINDOW alone cannot hide it.
    # Give it an existing hidden console so its AllocConsole call cannot create one.
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {'creationflags': subprocess.CREATE_NEW_CONSOLE, 'startupinfo': startup}


def validate_with_kicad(folder, nickname, work):
    cli = shutil.which('kicad-cli')
    if not cli and os.name == 'nt':
        candidate = Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'KiCad/10.0/bin/kicad-cli.exe'
        cli = str(candidate) if candidate.is_file() else None
    if not cli:
        raise FileNotFoundError('KiCad CLI is required to validate imported libraries')
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    for kind, source in (('sym', folder / (nickname + '.kicad_sym')), ('fp', folder / (nickname + '.pretty'))):
        output = work / ('validate-' + kind)
        output.mkdir()
        result = subprocess.run([cli, kind, 'export', 'svg', '-o', str(output), str(source)],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=60, **flags)
        if result.returncode or not list(output.rglob('*.svg')):
            error = result.stderr.decode('utf-8', errors='replace')[-2000:]
            raise ValueError('KiCad rejected imported ' + kind + ' library: ' + error)


def safe_name(value):
    if not value or value in ('.', '..') or any(c in value for c in '/\\:\x00\r\n'):
        raise ValueError('Unsafe library item name')
    return value


def checked_path(root, path):
    path = Path(path)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Library path escapes project')
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.is_symlink():
            raise ValueError('Linked library paths are not supported')
    return path


def table_text(path, nickname, uri, kind):
    original = path.read_bytes().decode('utf-8-sig') if path.exists() else f'({kind} (version 7)\n)\n'
    tree = parse_sexpr(original, kind)
    matches = [lib for lib in tree.children_named('lib') if lib.child('name') and lib.child('name').arg() == nickname]
    if matches:
        if len(matches) != 1 or not matches[0].child('uri') or matches[0].child('uri').arg() != uri or not matches[0].child('type') or matches[0].child('type').arg() != 'KiCad':
            raise ValueError(f'Library registration collision: {nickname}')
        return original
    entry = f'  (lib (name {json.dumps(nickname)}) (type "KiCad") (uri {json.dumps(uri)}) (options "") (descr "Imported LCSC component"))\n'
    return original[:tree.end - 1] + entry + original[tree.end - 1:]


class LibraryImporter:
    def __init__(self, project_root, converter=None):
        self.root = Path(project_root).resolve()
        self.configured = converter

    def _normalize(self, folder, nickname):
        # Upstream emits a legacy namespace and dangling model links even without --3d-model.
        pretty = checked_path(folder, folder / (nickname + '.pretty'))
        for module in pretty.glob('*.kicad_mod'):
            checked_path(folder, module)
            source = module.read_text(encoding='utf-8-sig')
            tree = parse_sexpr(source, 'footprint')
            patches = [(node.start, node.end, '') for node in tree.children_named('model')]
            if tree.arg().startswith('easykiconverter:'):
                node = tree.children[1]
                patches.append((node.start, node.end, json.dumps(tree.arg().split(':', 1)[1])))
            for start, end, replacement in sorted(patches, reverse=True):
                source = source[:start] + replacement + source[end:]
            if patches:
                module.write_text(source, encoding='utf-8')

    def _validate(self, folder, nickname):
        symbol_path = folder / (nickname + '.kicad_sym')
        pretty = folder / (nickname + '.pretty')
        for path in (symbol_path, pretty):
            checked_path(folder, path)
        if not symbol_path.is_file() or not pretty.is_dir():
            raise ValueError('Converter did not produce both symbol and footprint libraries')
        if symbol_path.stat().st_size > 20_000_000:
            raise ValueError('Symbol library is too large')
        tree = parse_sexpr(symbol_path.read_text(encoding='utf-8-sig'), 'kicad_symbol_lib')
        symbols = tree.children_named('symbol')
        if len(symbols) != 1:
            raise ValueError('Expected exactly one converted component')
        symbol = symbols[0]
        if symbol.child('extends'):
            raise ValueError('Converted symbol must be standalone, not extend an external parent')
        safe_name(symbol.arg())
        props = [p.arg(2) for p in symbol.children_named('property') if p.arg() == 'Footprint']
        if len(props) != 1 or not props[0].startswith(nickname + ':'):
            raise ValueError('Converted symbol has no matching footprint reference')
        name = safe_name(props[0].split(':', 1)[1])
        modules = list(pretty.glob('*.kicad_mod'))
        if len(modules) != 1 or modules[0].name != name + '.kicad_mod':
            raise ValueError('Converted footprint does not match symbol')
        module = checked_path(folder, modules[0])
        if module.stat().st_size > 20_000_000:
            raise ValueError('Footprint is too large')
        footprint = parse_sexpr(module.read_text(encoding='utf-8-sig'), 'footprint')
        if footprint.arg() not in (name, 'easykiconverter:' + name):
            raise ValueError('Footprint name mismatch')
        if footprint.children_named('model'):
            raise ValueError('Unexpected external 3D models in symbol/footprint-only import')
        return {'symbol': nickname + ':' + symbol.arg(), 'footprint': props[0]}, [symbol_path, module]

    def convert(self, code, progress=None):
        if not isinstance(code, str) or not re.fullmatch(r'C[0-9]+', code):
            raise ValueError('A single valid LCSC C code is required')
        notify = progress or (lambda stage, message: None)
        with IMPORT_LOCK:
            nickname = 'LCSC_' + code
            destination = checked_path(self.root, self.root / 'Libraries/DSMU/LCSC' / code)
            tables = [checked_path(self.root, self.root / name) for name in ('sym-lib-table', 'fp-lib-table')]
            uribase = '${KIPRJMOD}/' + destination.relative_to(self.root).as_posix() + '/'
            uris = [uribase + nickname + '.kicad_sym', uribase + nickname + '.pretty']
            if destination.exists():
                notify('validating', 'Checking existing imported libraries')
                manifest_path = checked_path(self.root, destination / 'manifest.json')
                if not manifest_path.is_file():
                    raise ValueError('Existing library has no import manifest; refusing overwrite')
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                try:
                    result, paths = self._validate(destination, nickname)
                except ValueError as exc:
                    raise ValueError('Imported library was modified: ' + str(exc)) from exc
                expected = {p.relative_to(destination).as_posix(): digest(p) for p in paths}
                if manifest.get('files') != expected or manifest.get('c_code') != code or manifest.get('library') != result:
                    raise ValueError('Imported library was modified; preserve edits and resolve manually')
                for path, uri, kind in zip(tables, uris, ('sym_lib_table', 'fp_lib_table')):
                    text = table_text(path, nickname, uri, kind)
                    if not path.exists() or path.read_bytes().decode('utf-8-sig') != text:
                        raise ValueError('Imported library registration was modified or removed')
                return {**result, 'cached': True, 'c_code': code}
            converter = find_converter(self.configured)
            temp = runtime_directory(self.root)
            with tempfile.TemporaryDirectory(prefix='lcsc-convert-', dir=temp) as td:
                work = Path(td)
                output = work / 'output'
                output.mkdir()
                config = work / 'config.json'
                config.write_text('{}', encoding='utf-8')
                args = [str(converter), 'convert', 'component', '--component', code, '--output', str(output), '--lib-name', nickname,
                        '--symbol', '--footprint', '--no-overwrite', '--language', 'en', '--quiet',
                        '--config', str(config), '--cache-dir', str(work / 'cache'), '--log-file', str(work / 'converter.log')]
                notify('converting', 'Downloading and converting symbol and footprint (up to 180s)')
                stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_' + code
                try:
                    completed = subprocess.run(args, cwd=work, stdin=subprocess.DEVNULL, capture_output=True, timeout=180, **hidden_converter_options())
                except subprocess.TimeoutExpired as exc:
                    log_path = save_converter_log(temp, stamp, work, exc.stdout, exc.stderr)
                    raise RuntimeError('EasyKiConverter network/conversion timeout; selection was not saved. '
                                       'If using a proxy, retry with EasyEDA direct routing; see ' + log_path) from exc
                log_path = save_converter_log(temp, stamp, work, completed.stdout, completed.stderr)
                if completed.returncode:
                    raise RuntimeError(f'EasyKiConverter conversion failed ({completed.returncode}); selection was not saved; see {log_path}')
                notify('validating', 'Validating generated library names and files')
                self._normalize(output, nickname)
                result, paths = self._validate(output, nickname)
                validate_with_kicad(output, nickname, work)
                original = {p: p.read_bytes() if p.exists() else None for p in tables}
                contents = [table_text(p, nickname, uri, kind) for p, uri, kind in zip(tables, uris, ('sym_lib_table', 'fp_lib_table'))]
                publish = work / 'publish'
                publish.mkdir()
                for path in paths:
                    target = publish / path.relative_to(output)
                    target.parent.mkdir(exist_ok=True, parents=True)
                    shutil.copy2(path, target)
                manifest = {'c_code': code, 'library': result, 'converter': str(converter), 'created_at': datetime.now().isoformat(),
                            'files': {p.relative_to(output).as_posix(): digest(p) for p in paths}}
                (publish / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
                backups = checked_path(temp, temp / 'part_selection_library_backups' / stamp)
                backups.mkdir(parents=True)
                for path, data in original.items():
                    if data is not None:
                        (backups / path.name).write_bytes(data)
                notify('registering', 'Registering project-local libraries')
                touched = []
                published = False
                try:
                    for path, data in original.items():
                        if (path.read_bytes() if path.exists() else None) != data:
                            raise ValueError('Library table changed during conversion; retry after saving KiCad')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        raise ValueError('Library destination appeared during conversion')
                    publish.rename(destination)
                    published = True
                    for index, (path, content) in enumerate(zip(tables, contents)):
                        if (path.read_bytes() if path.exists() else None) != original[path]:
                            raise ValueError('Library table changed before registration; refusing overwrite')
                        if original[path] is not None and original[path].decode('utf-8-sig') == content:
                            continue
                        staged = work / ('table-' + str(index))
                        staged.write_bytes(content.encode('utf-8'))
                        os.replace(staged, path)
                        touched.append(path)
                except Exception as exc:
                    conflicts = []
                    for path in reversed(touched):
                        ours = contents[tables.index(path)].encode('utf-8')
                        if not path.exists() or path.read_bytes() != ours:
                            conflicts.append(path.name)
                            continue
                        if original[path] is None:
                            path.unlink()
                        else:
                            path.write_bytes(original[path])
                    if published and not conflicts:
                        shutil.rmtree(destination)
                    if conflicts:
                        raise RuntimeError('Concurrent library edits preserved; imported files and backups retained for manual recovery: ' + ', '.join(conflicts)) from exc
                    raise
                return {**result, 'cached': False, 'c_code': code}


class SelectionJobs:
    """One conversion at a time, bounded queue and polling snapshots."""
    def __init__(self, select):
        self.select = select
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='library-import')
        self.jobs = OrderedDict()

    def start(self, payload):
        with self.lock:
            for key, state in list(self.jobs.items()):
                if state['done'] and (len(self.jobs) >= 32 or time.monotonic() - state['_time'] > 1800):
                    del self.jobs[key]
            if sum(not state['done'] for state in self.jobs.values()) >= 4:
                raise ValueError('Conversion queue is busy; please wait')
            key = uuid4().hex
            state = {'ok': True, 'job_id': key, 'done': False, 'stage': 'queued', 'message': 'Waiting for converter', '_time': time.monotonic()}
            self.jobs[key] = state
            self.pool.submit(self._run, key, deepcopy(payload))
            return self.status(key)

    def status(self, key):
        with self.lock:
            state = self.jobs.get(key)
            if state is None:
                return {'ok': False, 'done': True, 'error': 'Selection task expired'}
            return deepcopy({k: v for k, v in state.items() if not k.startswith('_')})

    def _run(self, key, payload):
        def progress(stage, message):
            with self.lock:
                self.jobs[key].update(stage=stage, message=message)
        try:
            result = self.select(payload, progress=progress)
            with self.lock:
                self.jobs[key].update(done=True, stage='complete', message='Selection saved; synchronize schematic to apply', result=result)
        except Exception as exc:
            with self.lock:
                self.jobs[key].update(ok=False, done=True, stage='failed', error=str(exc), message=str(exc))

    def close(self):
        self.pool.shutdown(wait=True)

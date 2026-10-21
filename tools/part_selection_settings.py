"""Project path settings, with non-destructive data relocation."""
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

from part_selection_overrides import (DATA_FILE_NAMES, DATA_DIRECTORY, SYNC_LOG_NAME,
                                      EXPORT_PREFIXES, configuration_defaults, load_config)
from part_selection_runtime import runtime_directory
from part_selection_library import find_converter
from part_selection_fields import normalize_field_mapping, field_mapping_snapshot


PATH_FIELDS = (
    'schematic_dir', 'bom_dir', 'source_bom', 'input_refs',
    'output_overrides', 'stock_cache', 'converter_executable',
    'runtime_cache', 'desktop_home',
)
EDITABLE_FIELDS = (*PATH_FIELDS, 'project_name', 'lcsc_bom_pattern', 'jump_cursor_behavior',
                   'open_lcsc_search_on_replace')
DATA_FIELDS = ('input_refs', 'output_overrides', 'stock_cache')
LOG_NAMES = (SYNC_LOG_NAME, 'part_selection_schematic_sync_log.csv')
EXPORT_PATTERNS = tuple(prefix + '*.csv' for prefix in (*EXPORT_PREFIXES,
    'part_selection_pump_table_', 'part_selection_change_log_'))


class SettingsGate:
    """Keep requests concurrent, but change paths only while all work is idle."""
    def __init__(self):
        self.lock = threading.RLock()
        self.active = 0
        self.stopping = False

    def request(self, method):
        @wraps(method)
        def wrapped(*args, **kwargs):
            with self.lock:
                if self.stopping:
                    args[0].send_error(503, 'Workbench is shutting down')
                    return
                self.active += 1
            try:
                return method(*args, **kwargs)
            finally:
                with self.lock:
                    self.active -= 1
        return wrapped

    @contextmanager
    def editing(self):
        with self.lock:
            if self.active > 1:
                raise ValueError('仍有数据请求正在处理，请稍后保存路径配置')
            yield


def _raw(path):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}


def _revision(path):
    return hashlib.sha256(path.read_bytes() if path.exists() else b'').hexdigest()


def settings_snapshot(config):
    path, root = config['config_path'], config['project_root'].resolve()
    raw = _raw(path)
    values = {**configuration_defaults(path, raw), **raw}
    try:
        converter = str(find_converter(config.get('converter_executable')))
    except OSError:
        converter = ''
    schematic_default = 'DOC' if any((root / 'DOC').glob('*.kicad_sch')) else (
        '.' if any(root.glob('*.kicad_sch')) else str(values['schematic_dir']))
    imported = config['source_bom']
    import_source = '' if imported.name == '__schematic__' else (
        imported.relative_to(root).as_posix() if imported.is_relative_to(root) else str(imported))
    return {
        'ok': True, 'revision': _revision(path), 'config_path': str(path), 'project_root': str(root),
        'values': {key: str(values.get(key) or '') for key in EDITABLE_FIELDS},
        'resolved': {key: str(config.get(key) or '') for key in PATH_FIELDS},
        'runtime_directory': str(runtime_directory(root)),
        'desktop_home_effective': str(Path(os.environ.get('KICAD_PART_SELECTION_HOME') or
            str(Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.cache'))) / 'KiCadPartSelection'))),
        'plugin_directory': str(Path(__file__).resolve().parent),
        'default_data_directory': DATA_DIRECTORY, 'data_file_names': DATA_FILE_NAMES,
        'converter_path': converter, 'schematic_default': schematic_default, 'import_source': import_source,
        'field_mapping': field_mapping_snapshot(config['schematic_dir'], config.get('field_mapping')),
    }


def open_settings_directory(config, payload):
    directories = {
        'project': config['project_root'], 'data': config['bom_dir'],
        'temporary': runtime_directory(config['project_root']),
        'desktop': settings_snapshot(config)['desktop_home_effective'],
    }
    if payload.get('target') not in directories:
        raise ValueError('请选择已配置的目录')
    path = Path(directories[payload['target']]).resolve()
    if not path.is_dir():
        raise ValueError(f'目录不存在：{path}')
    if os.name == 'nt':
        os.startfile(path)
    else:
        subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(path)])
    return {'ok': True, 'path': str(path)}


def preview_field_mapping(config, payload):
    mapping = normalize_field_mapping(payload.get('field_mapping'))
    directory = _path(config['project_root'], str(payload.get('schematic_dir') or config['schematic_dir']))
    if not directory.is_dir():
        raise ValueError('原理图目录不存在')
    return {'ok': True, 'field_mapping': field_mapping_snapshot(directory, mapping)}


def _path(root, value):
    path = Path(os.path.expandvars(value)).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _runtime_path(root, value, *, same_device=False):
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        raise ValueError('临时缓存和桌面运行目录必须使用绝对路径')
    path = path.resolve()
    if path.is_relative_to(root):
        raise ValueError('临时缓存和桌面运行目录必须位于工程目录之外')
    ancestor = path
    while not ancestor.exists():
        ancestor = ancestor.parent
    if not ancestor.is_dir() or path.exists() and not path.is_dir():
        raise ValueError(f'目录路径无效：{path}')
    if same_device and root.stat().st_dev != ancestor.stat().st_dev:
        raise ValueError('临时缓存必须与工程位于同一磁盘')
    return path


def apply_runtime_settings(config):
    root = config['project_root'].resolve()
    for key, env, same_device in (
        ('runtime_cache', 'KICAD_PART_SELECTION_CACHE', True),
        ('desktop_home', 'KICAD_PART_SELECTION_HOME', False),
    ):
        if config.get(key):
            os.environ[env] = str(_runtime_path(root, str(config[key]), same_device=same_device))


def choose_path(config, payload):
    key = payload.get('field')
    if os.name != 'nt' or key not in PATH_FIELDS:
        raise ValueError('请选择有效的路径字段；非 Windows 环境请手动输入')
    kind = 'folder' if key in ('schematic_dir', 'bom_dir', 'runtime_cache', 'desktop_home') else (
        'open' if key in ('source_bom', 'converter_executable') else 'save')
    script = """
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$kind=$env:PART_SETTINGS_DIALOG_KIND
if ($kind -eq 'folder') {
  $dialog=New-Object System.Windows.Forms.FolderBrowserDialog
  $dialog.Description='KiCad - select directory'
  $dialog.SelectedPath=$env:PART_SETTINGS_DIALOG_INITIAL
} elseif ($kind -eq 'open') {
  $dialog=New-Object System.Windows.Forms.OpenFileDialog
  $dialog.CheckFileExists=$true
} else {
  $dialog=New-Object System.Windows.Forms.SaveFileDialog
  $dialog.OverwritePrompt=$false
}
if ($kind -ne 'folder') { $dialog.FileName=$env:PART_SETTINGS_DIALOG_INITIAL }
$owner=New-Object System.Windows.Forms.Form
$owner.TopMost=$true
try {
  if ($dialog.ShowDialog($owner) -eq 'OK') {
    if ($kind -eq 'folder') { [Console]::WriteLine($dialog.SelectedPath) }
    else { [Console]::WriteLine($dialog.FileName) }
  }
} finally { $dialog.Dispose(); $owner.Dispose() }
"""
    initial = str(payload.get('value') or '')
    if initial and initial != '__schematic__':
        initial = str(_path(config['project_root'], initial))
    else:
        initial = str(config['project_root'])
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
        '-Sta', '-EncodedCommand', base64.b64encode(script.encode('utf-16-le')).decode('ascii')],
        capture_output=True, text=True, encoding='utf-8-sig', timeout=180, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
        env={**os.environ, 'PART_SETTINGS_DIALOG_KIND': kind, 'PART_SETTINGS_DIALOG_INITIAL': initial})
    return {'ok': True, 'path': result.stdout.strip()}


def _validate(root, config_path, values):
    resolved = {}
    for key in PATH_FIELDS:
        value = values.get(key, '')
        if key in ('runtime_cache', 'desktop_home'):
            if value:
                resolved[key] = _runtime_path(root, value, same_device=key == 'runtime_cache')
        elif key == 'converter_executable':
            if value:
                resolved[key] = _path(root, value)
                if not resolved[key].is_file():
                    raise ValueError('转换器程序不存在，请选择可执行文件或留空自动查找')
        elif key == 'source_bom' and value == '__schematic__':
            continue
        else:
            if not value:
                raise ValueError(f'路径不能为空：{key}')
            resolved[key] = _path(root, value)
    if not resolved['schematic_dir'].is_dir():
        raise ValueError('原理图目录不存在')
    if 'source_bom' in resolved and (not resolved['source_bom'].is_file() or
            resolved['source_bom'].suffix.lower() not in ('.csv', '.xlsx', '.xlsm')):
        raise ValueError('导入 BOM 必须为 __schematic__ 或已有 CSV/XLSX 文件')
    files = [config_path.resolve()]
    for key in DATA_FIELDS:
        path = resolved[key]
        if path.suffix.lower() != ('.txt' if key == 'input_refs' else '.json'):
            raise ValueError(f'文件扩展名不正确：{key}')
        files.append(path)
    if len(set(files)) != len(files):
        raise ValueError('配置、选料记录、库存缓存和位号文件不能使用同一路径')
    protected = [resolved.get('source_bom'), resolved.get('converter_executable')]
    plugin = Path(__file__).resolve().parent
    for key in ('bom_dir', *DATA_FIELDS):
        path = resolved[key]
        if path.is_relative_to(plugin) or path == config_path.resolve() or path in protected:
            raise ValueError('数据输出不能覆盖配置、输入文件或插件程序')
        if path.exists() and (path.is_file() if key == 'bom_dir' else path.is_dir()):
            raise ValueError(f'文件与目录类型不匹配：{path}')
    pattern = values.get('lcsc_bom_pattern', '')
    if not pattern or Path(pattern).is_absolute() or '..' in Path(pattern).parts:
        raise ValueError('立创 BOM 匹配规则必须为工程内相对路径')
    return resolved


def _write_atomic(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.path-settings-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _copy_exclusive(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = path.open('xb')
    except FileExistsError:
        if path.is_file() and path.read_bytes() == data:
            return False
        raise ValueError(f'目标已有不同数据，未覆盖：{path}') from None
    identity = os.fstat(stream.fileno())
    try:
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if path.exists() and os.path.samestat(identity, path.stat()):
            path.unlink()
        raise
    return True


def save_settings(config, payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('values'), dict):
        raise ValueError('配置内容必须为对象')
    path, root = config['config_path'], config['project_root'].resolve()
    if payload.get('revision') != _revision(path):
        raise ValueError('配置已被其他窗口修改，请重新打开后再保存')
    updates = payload['values']
    if set(updates) - set(EDITABLE_FIELDS) or any(not isinstance(v, str) for v in updates.values()):
        raise ValueError('包含不支持的配置字段或字段类型')
    raw = _raw(path)
    values = {**configuration_defaults(path, raw), **raw, **{k: v.strip() for k, v in updates.items()}}
    mapping = normalize_field_mapping(payload.get('field_mapping', config.get('field_mapping')))
    if values['jump_cursor_behavior'] not in ('return', 'stay'):
        raise ValueError('请选择跳转后鼠标返回原位或留在原理图')
    if values['open_lcsc_search_on_replace'] not in ('true', 'false'):
        raise ValueError('请选择更换型号时是否自动打开网页搜索')
    targets = _validate(root, path, values)
    # Mapping is configuration only; saving never changes schematic properties.
    preview = field_mapping_snapshot(targets['schematic_dir'], mapping)
    if 'field_mapping' in payload and any(item['conflicts'] for item in preview['fields']):
        raise ValueError('字段映射仍有冲突，请为冲突字段手动选择不同属性')
    copies = []
    for key in DATA_FIELDS:
        old, new = Path(config[key]).resolve(), targets[key]
        if old != new and old.is_file():
            copies.append((old, new))
    old_bom, new_bom = Path(config['bom_dir']).resolve(), targets['bom_dir']
    if old_bom != new_bom:
        for name in LOG_NAMES:
            if (old_bom / name).is_file():
                copies.append((old_bom / name, new_bom / name))
        for pattern in EXPORT_PATTERNS:
            copies.extend((old, new_bom / old.name) for old in old_bom.glob(pattern) if old.is_file())
    # Never silently open an empty record set in place of existing selections.
    if copies and payload.get('copy_existing') is not True:
        raise ValueError('这些路径已有数据，请确认复制已有数据；原文件会保留')
    planned = {}
    for old, new in copies:
        data = old.read_bytes()
        if new in planned and planned[new] != data:
            raise ValueError(f'迁移目标冲突：{new}')
        if new.exists() and (not new.is_file() or new.read_bytes() != data):
            raise ValueError(f'目标已有不同数据，未覆盖：{new}')
        planned[new] = data
    if any(new in (path.resolve(), *[old.resolve() for old, _ in copies]) for new in planned):
        raise ValueError('迁移目标与原文件交叉，请另选空目录')
    backup = runtime_directory(root) / 'path_config_backups' / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    backup.mkdir(parents=True)
    if path.exists():
        shutil.copy2(path, backup / 'config.json')
    # Copy a recovery snapshot before changing paths. Originals remain untouched.
    for index, (old, _) in enumerate(copies):
        shutil.copy2(old, backup / f'{index}_{old.name}')
    created = []
    try:
        for new, data in planned.items():
            if _copy_exclusive(new, data):
                created.append((new, data))
        if payload.get('revision') != _revision(path):
            raise ValueError('配置已被其他窗口修改，请重新打开后再保存')
        # Preserve unknown engineering options and the original project identity.
        raw.update({key: values[key] for key in EDITABLE_FIELDS if key in updates})
        if 'field_mapping' in payload:
            raw['field_mapping'] = mapping
        _write_atomic(path, (json.dumps(raw, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    except Exception:
        for new, data in reversed(created):
            if new.exists() and new.read_bytes() == data:
                new.unlink()
        raise
    restart = any(str(config.get(key) or '') != str(values.get(key) or '')
        for key in ('runtime_cache', 'desktop_home'))
    return {'ok': True, 'copied_files': len(planned), 'backup_path': str(backup),
            'restart_required': restart, 'config': load_config(path)}

"""Explicit project/configuration selection shared by server and desktop host."""
import json
import os
import hashlib
from datetime import datetime
from pathlib import Path
from urllib.request import build_opener, ProxyHandler
from uuid import uuid4

from part_selection_overrides import (load_config, DATA_DIRECTORY, CONFIG_NAME, DATA_FILE_NAMES,
                                      LEGACY_PATHS, SYNC_LOG_NAME, RUNTIME_LOG_NAME, EXPORT_PREFIXES)
from part_selection_runtime import runtime_directory


def expanded_path(value):
    return Path(os.path.expandvars(str(value))).expanduser().resolve()


def _check_legacy_service(root):
    """Do not fork live selections into a new set of files while an older host is active."""
    digest = hashlib.sha256(os.path.normcase(str(root.resolve())).encode('utf-8')).hexdigest()
    first = 18000 + int(digest[:6], 16) % 18000
    opener = build_opener(ProxyHandler({}))
    for port in [8765, *range(first, first + 10)]:
        try:
            with opener.open(f'http://127.0.0.1:{port}/api/config', timeout=0.3) as response:
                info = json.load(response)
        except (OSError, ValueError):
            continue
        if isinstance(info, dict) and info.get('project_root') and expanded_path(info['project_root']) == root:
            raise ValueError('旧工作台后台仍在运行，请先正常关闭工作台，再重新打开以迁移中文数据文件。')


def _check_migration_sources(raw):
    for filename, digest in raw.get('legacy_data_sources', {}).items():
        path = Path(filename)
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f'旧数据在迁移后又被修改，请先核对新旧记录，未覆盖：{path}')


def migrate_legacy_config(root, old_path, new_path):
    from part_selection_settings import _copy_exclusive

    original_config = old_path.read_bytes()
    old = load_config(old_path)
    if old_path.read_bytes() != original_config:
        raise ValueError('配置正在被修改，请关闭旧工作台后重试')
    if old['project_root'].resolve() != root:
        raise ValueError(f'Configuration points to another project: {old_path}')
    if new_path.exists():
        raw = json.loads(new_path.read_text(encoding='utf-8-sig'))
        if load_config(new_path)['project_root'].resolve() != root:
            raise ValueError(f'Configuration points to another project: {new_path}')
        if raw.get('legacy_config_path') != str(old_path):
            raise ValueError(f'新旧配置同时存在，无法确定采用哪一份，未覆盖：{new_path}')
        _check_migration_sources(raw)
        return new_path
    _check_legacy_service(root)
    raw = json.loads(original_config.decode('utf-8-sig'))
    data = dict(raw)
    data.update(project_root=os.path.relpath(root, new_path.parent).replace('\\', '/'),
                data_layout_version=2, legacy_config_path=str(old_path))
    old_dir = old['bom_dir'].resolve()
    new_dir = root / DATA_DIRECTORY if old_dir == root / 'BOM' else old_dir
    data['bom_dir'] = new_dir.relative_to(root).as_posix() if new_dir.is_relative_to(root) else str(new_dir)
    copies = []
    for key, name in DATA_FILE_NAMES.items():
        source = old[key].resolve()
        default_name = Path(LEGACY_PATHS[key]).name
        destination = new_dir / name if source == old_dir / default_name or source == root / LEGACY_PATHS[key] else source
        data[key] = os.path.relpath(destination, root).replace('\\', '/') if destination.is_relative_to(root) else str(destination)
        if source != destination and source.is_file():
            copies.append((source, destination))
    runtime = runtime_directory(root)
    for source, destination in (
        (old_dir / 'part_selection_schematic_sync_log.csv', new_dir / SYNC_LOG_NAME),
        (old_dir / 'part_selection_schematic_sync_runtime.jsonl', runtime / RUNTIME_LOG_NAME),
    ):
        if source.is_file():
            copies.append((source, destination))
    for old_prefix, new_prefix in zip(('part_selection_pump_table_', 'part_selection_change_log_'), EXPORT_PREFIXES):
        copies.extend((source, new_dir / source.name.replace(old_prefix, new_prefix, 1))
                      for source in old_dir.glob(old_prefix + '*.csv') if source.is_file())
    contents = {source: source.read_bytes() for source, _ in copies}
    contents[old_path] = original_config
    planned = {}
    for source, destination in copies:
        content = contents[source]
        if destination in planned and planned[destination] != content:
            raise ValueError(f'迁移目标冲突：{destination}')
        if destination.exists() and (not destination.is_file() or destination.read_bytes() != content):
            raise ValueError(f'目标已有不同数据，未覆盖：{destination}')
        planned[destination] = content
    data['legacy_data_sources'] = {str(path): hashlib.sha256(content).hexdigest() for path, content in contents.items()}
    backup = runtime / '中文数据迁移备份' / f'{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}'
    backup.mkdir(parents=True)
    for index, (source, content) in enumerate(contents.items()):
        (backup / f'{index}_{source.name}').write_bytes(content)
    data['data_migration_backup'] = str(backup)
    created = []
    try:
        for destination, content in planned.items():
            if _copy_exclusive(destination, content):
                created.append((destination, content))
        _check_migration_sources(data)
        content = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        if _copy_exclusive(new_path, content):
            created.append((new_path, content))
    except Exception:
        for destination, content in reversed(created):
            if destination.is_file() and destination.read_bytes() == content:
                destination.unlink()
        raise
    return new_path


def resolve_config(project=None, config=None):
    if project is None and config is None:
        raise ValueError('Specify --project or --config; the plugin source is not a KiCad project')
    explicit = config is not None
    path = expanded_path(config) if explicit else None
    if explicit and not path.is_file():
        raise FileNotFoundError(f'Configuration not found: {path}')
    root = expanded_path(project) if project is not None else load_config(path)['project_root'].resolve()
    if root.suffix.lower() in {'.kicad_pro', '.kicad_sch', '.kicad_pcb'}:
        root = root.parent
    if not root.is_dir() or not any(root.glob('*.kicad_pro')) or not any(root.glob('*.kicad_sch')):
        raise ValueError(f'No saved KiCad project with top-level schematic in {root}')
    legacy = root / 'BOM/part_selection_config.json'
    default = root / DATA_DIRECTORY / CONFIG_NAME
    if (path is None or path == legacy) and legacy.is_file():
        return migrate_legacy_config(root, legacy, default)
    path = path or default
    if path.exists():
        if load_config(path)['project_root'].resolve() != root:
            raise ValueError(f'Configuration points to another project: {path}')
        return path
    data = {'project_name': root.name, 'project_root': '../..', 'data_layout_version': 2,
            'schematic_dir': 'DOC' if (root / 'DOC').is_dir() else '.',
            'source_bom': '__schematic__', 'lcsc_bom_pattern': 'BOM/*LCSC*.csv'}
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    except FileExistsError:
        return resolve_config(root, path)
    return path

"""Explicit project/configuration selection shared by server and desktop host."""
import json
import os
from pathlib import Path

from part_selection_overrides import load_config


def expanded_path(value):
    return Path(os.path.expandvars(str(value))).expanduser().resolve()


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
    path = path or root / 'BOM/part_selection_config.json'
    if path.exists():
        if load_config(path)['project_root'].resolve() != root:
            raise ValueError(f'Configuration points to another project: {path}')
        return path
    data = {'project_name': root.name, 'project_root': '..',
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

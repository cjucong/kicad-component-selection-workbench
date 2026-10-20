"""Project-specific scratch storage outside KiCad's recursive project backups."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def runtime_directory(project_root):
    project = Path(project_root).resolve()
    key = hashlib.sha256(os.path.normcase(str(project)).encode('utf-8')).hexdigest()[:12]
    configured = os.environ.get('KICAD_PART_SELECTION_CACHE', '')
    base = Path(os.path.expandvars(configured)).expanduser() if configured else project.parent / '.kicad-part-selection-cache'
    if not base.is_absolute():
        raise ValueError('Runtime cache path must be absolute')
    directory = base / (project.name + '-' + key)
    resolved = directory.resolve()
    if resolved.is_relative_to(project):
        raise ValueError('Runtime cache must be outside the KiCad project')
    # Staging and publication must share a filesystem for atomic rename/replace.
    directory.mkdir(parents=True, exist_ok=True)
    if project.stat().st_dev != directory.stat().st_dev:
        raise ValueError('Runtime cache must be on the project filesystem')
    return resolved

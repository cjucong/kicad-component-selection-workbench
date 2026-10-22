from pathlib import Path
from types import SimpleNamespace as Obj

import pytest

from part_selection_pcb_session import select_document


def doc(root, filename='test.kicad_pcb'):
    return Obj(project=Obj(path=str(root)), board_filename=filename)


def test_document_requires_exact_project_identity(scratch_project):
    root = scratch_project
    selected = doc(root)
    assert select_document([doc(root / 'other'), selected], root) is selected
    with pytest.raises(ValueError):
        select_document([doc(root / 'other')], root)
    with pytest.raises(ValueError):
        select_document([selected, doc(root, 'other.kicad_pcb')], root)


def test_absolute_filename_cannot_escape_document_project(scratch_project):
    with pytest.raises(ValueError):
        select_document([doc(scratch_project, str(scratch_project.parent / 'other.kicad_pcb'))], scratch_project)

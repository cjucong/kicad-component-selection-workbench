from pathlib import Path

import pytest

from part_selection_pcb import schematic_identity


def hierarchy(root):
    (root / 'Design.kicad_pro').write_text('{}')
    pages = root / 'DOC'
    pages.mkdir()
    (root / 'Design.kicad_sch').write_text('''(kicad_sch (uuid "new-root")
        (sheet (uuid "top-sheet") (property "Sheetfile" "DOC/Top.kicad_sch")
          (instances (project "Design" (path "/old-root" (page "1"))))))''')
    (pages / 'Top.kicad_sch').write_text('''(kicad_sch (uuid "top-file")
        (sheet (uuid "out-sheet") (property "Sheetfile" "sub/Out.kicad_sch")
          (instances (project "Design" (path "/old-root/top-sheet" (page "2"))))))''')
    (pages / 'sub').mkdir()
    child = pages / 'sub' / 'Out.kicad_sch'
    child.write_text('''(kicad_sch (uuid "out-file") (symbol (uuid "l9-symbol")
        (property "Reference" "L9") (instances (project "Design"
          (path "/old-root/top-sheet/out-sheet" (reference "L9") (unit 1))))))''')
    return pages, child


def test_hierarchical_identity_follows_files_and_verified_legacy_root(scratch_project):
    pages, child = hierarchy(scratch_project)
    result = schematic_identity(scratch_project, pages, ['L9'])
    assert '/top-sheet/out-sheet/l9-symbol' in result['paths']['L9']
    assert '/old-root/top-sheet/out-sheet/l9-symbol' in result['paths']['L9']
    assert '/new-root/top-sheet/out-sheet/l9-symbol' in result['paths']['L9']
    assert str(child.resolve()) in result['hashes']


@pytest.mark.parametrize('bad', ['/old-root/wrong-sheet/out-sheet',
                                '/unrelated-root/top-sheet/out-sheet'])
def test_unrelated_instance_path_does_not_pass_same_reference(scratch_project, bad):
    pages, child = hierarchy(scratch_project)
    child.write_text(child.read_text().replace('/old-root/top-sheet/out-sheet', bad))
    with pytest.raises(ValueError, match='L9'):
        schematic_identity(scratch_project, pages, ['L9'])


def test_unreferenced_page_cannot_supply_missing_identity(scratch_project):
    pages, child = hierarchy(scratch_project)
    (pages / 'Orphan.kicad_sch').write_text(child.read_text())
    child.write_text('(kicad_sch (uuid "out-file"))')
    with pytest.raises(ValueError, match='L9'):
        schematic_identity(scratch_project, pages, ['L9'])


def test_other_project_instance_cannot_supply_identity(scratch_project):
    pages, child = hierarchy(scratch_project)
    child.write_text(child.read_text().replace('project "Design"', 'project "Other"'))
    with pytest.raises(ValueError, match='L9'):
        schematic_identity(scratch_project, pages, ['L9'])


def test_missing_hierarchy_file_reports_file_not_missing_uuid(scratch_project):
    pages, child = hierarchy(scratch_project)
    child.unlink()
    with pytest.raises(ValueError, match='Out.kicad_sch'):
        schematic_identity(scratch_project, pages, ['L9'])


def test_hierarchy_cycle_is_rejected(scratch_project):
    pages, child = hierarchy(scratch_project)
    child.write_text('''(kicad_sch (uuid "out-file") (sheet (uuid "cycle")
        (property "Sheetfile" "../Top.kicad_sch")))''')
    with pytest.raises(ValueError, match='循环'):
        schematic_identity(scratch_project, pages, ['L9'])


def test_root_symbol_accepts_both_kicad_path_forms(scratch_project):
    (scratch_project / 'Design.kicad_pro').write_text('{}')
    (scratch_project / 'Design.kicad_sch').write_text('''(kicad_sch (uuid "root")
      (symbol (uuid "symbol") (instances (project "Design"
        (path "/root" (reference "L9") (unit 1))))))''')
    paths = schematic_identity(scratch_project, scratch_project, ['L9'])['paths']['L9']
    assert paths == ['/root/symbol', '/symbol']

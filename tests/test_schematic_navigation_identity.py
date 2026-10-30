"""Synthetic saved-file tests only: no KiCad, UI, network, or production CAD."""
import hashlib
import json
from unittest.mock import patch

import pytest

from kicad_sexpr import parse_sexpr
from part_selection_navigation import load_navigation_identity


def library(count=1):
    return '(lib_symbols (symbol "Local:Part" ' + ''.join(
        f'(symbol "Part_{unit}_1")' for unit in range(1, count + 1)) + '))'


def symbol(ref, identifier, paths, *, unit=1):
    return f'''(symbol (lib_id "Local:Part") (uuid "{identifier}") (unit {unit})
      (property "Reference" "{ref}") (instances (project "Design"
      {''.join(f'(path "{path}" (reference "{name}") (unit {number}))' for path, name, number in paths)})))'''


def project(root, body, *, count=1, settings=None):
    (root / 'Design.kicad_pro').write_text(json.dumps({'schematic': settings or {}}), encoding='utf-8')
    (root / 'Design.kicad_sch').write_text(
        '(kicad_sch (uuid "root") ' + library(count) + body + ')', encoding='utf-8')
    return root / 'Design.kicad_pro'


def sheet(identifier, name, filename):
    return f'(sheet (uuid "{identifier}") (property "Sheetname" "{name}") (property "Sheetfile" "{filename}"))'


def test_root_single_unit_is_exact_and_read_only(scratch_project):
    file = project(scratch_project,
        symbol('U1', 'one', [('/root', 'U1', 1)])
        + symbol('U10', 'ten', [('/root', 'U10', 1)]))
    before = {p: p.read_bytes() for p in scratch_project.iterdir()}
    index = load_navigation_identity(file, ['U1'])
    assert index.search_names('u1') == ('U1',)
    target, = index.lookup('U1')
    assert (target.page_name, target.page_path, target.title_prefix) == ('Design', 'Design', 'Design')
    assert target.instance_path == '/root/one'
    assert not index.matches('U1', 'U10')
    assert not index.matches('U1', 'U1A')
    with pytest.raises(ValueError):
        index.lookup('U10')
    assert index.is_current()
    assert before == {p: p.read_bytes() for p in scratch_project.iterdir()}


@pytest.mark.parametrize('first,separator,expected', [
    (65, 0, ('U1A', 'U1B')), (97, 46, ('U1.a', 'U1.b')),
    (49, 95, ('U1_1', 'U1_2')), (53, 0, ('U11', 'U12')),
])
def test_actual_instance_units_and_configured_suffixes(scratch_project, first, separator, expected):
    file = project(scratch_project,
        symbol('WRONG', 'a', [('/root', 'U1', 1)], unit=2)
        + symbol('WRONG', 'b', [('/root', 'U1', 2)], unit=1), count=2,
        settings={'subpart_first_id': first, 'subpart_id_separator': separator})
    index = load_navigation_identity(file, ['U1'])
    assert index.search_names('U1') == expected
    assert [target.unit for target in index.lookup('U1')] == [1, 2]
    assert not index.matches('U1', 'U1')


def test_alphabetic_suffix_beyond_z(scratch_project):
    file = project(scratch_project, symbol('U1', 'a', [('/root', 'U1', 27)]), count=28)
    assert load_navigation_identity(file).search_names('U1') == ('U1AA',)


def test_multiunit_suffix_even_when_only_first_unit_placed(scratch_project):
    file = project(scratch_project, symbol('U1', 'a', [('/root', 'U1', 1)]), count=2)
    assert load_navigation_identity(file).search_names('U1') == ('U1A',)


def test_single_unit_does_not_gain_a_suffix(scratch_project):
    file = project(scratch_project, symbol('U1', 'a', [('/root', 'U1', 1)]),
                   settings={'subpart_first_id': 97, 'subpart_id_separator': 46})
    assert load_navigation_identity(file).search_names('U1') == ('U1',)


def test_nested_names_are_not_filenames_and_repeated_file_instances_are_distinct(scratch_project):
    file = project(scratch_project,
                   sheet('left', 'Left', 'branch.kicad_sch')
                   + sheet('right', 'Right', 'branch.kicad_sch'))
    (scratch_project / 'branch.kicad_sch').write_text(
        '(kicad_sch (uuid "branch") ' + sheet('nested', 'Amplifier', 'child.kicad_sch') + ')')
    (scratch_project / 'child.kicad_sch').write_text(
        '(kicad_sch (uuid "child") ' + library(2)
        + symbol('STALE', 'shared', [('/root/left/nested', 'U1', 1),
                                     ('/root/right/nested', 'U10', 2)]) + ')')
    index = load_navigation_identity(file)
    left, = index.lookup('U1')
    right, = index.lookup('U10')
    assert left.search_name == 'U1A' and right.search_name == 'U10B'
    assert left.page_name == right.page_name == 'Amplifier'
    assert left.page_path == 'Design/Left/Amplifier'
    assert right.page_path == 'Design/Right/Amplifier'
    assert left.title_prefix == 'child [Design/Left/Amplifier]'
    assert left.instance_path == '/root/left/nested/shared'
    assert not index.matches('U1', 'U1A', page_path=right.page_path)
    assert not index.matches('U1', 'U10B')


def test_same_display_name_on_repeated_instances_is_not_silently_collapsed(scratch_project):
    file = project(scratch_project,
                   sheet('left', 'Left', 'child.kicad_sch')
                   + sheet('right', 'Right', 'child.kicad_sch'))
    (scratch_project / 'child.kicad_sch').write_text('(kicad_sch (uuid "child") '
        + library() + symbol('STALE', 'a', [('/root/left', 'U1', 1),
                                            ('/root/right', 'U1', 1)]) + ')')
    index = load_navigation_identity(file, ['U1'])
    assert index.search_names('U1') == ('U1',)
    assert len(index.matches('U1', 'U1')) == 2
    target, = index.matches('U1', 'U1', page_path='Design/Left')
    assert target.instance_path == '/root/left/a'


def test_verified_historical_root_anchor_reuses_existing_hierarchy_rules(scratch_project):
    file = project(scratch_project, sheet('left', 'Left', 'child.kicad_sch').replace(
        '(sheet (uuid "left")',
        '(sheet (instances (project "Design" (path "/old-root" (page "2")))) (uuid "left")'))
    (scratch_project / 'child.kicad_sch').write_text('(kicad_sch (uuid "child") '
        + library(2) + symbol('STALE', 'a', [('/old-root/left', 'U1', 2)]) + ')')
    target, = load_navigation_identity(file).lookup('U1')
    assert target.search_name == 'U1B'
    assert target.instance_path == '/root/left/a'
    assert target.page_path == 'Design/Left'


def test_saved_byte_changes_invalidate_index_even_with_preserved_timestamp(scratch_project):
    import os
    file = project(scratch_project, symbol('U1', 'a', [('/root', 'U1', 1)]))
    index = load_navigation_identity(file)
    page = file.with_suffix('.kicad_sch')
    stamp = page.stat()
    page.write_text(page.read_text().replace('U1', 'U2'))
    os.utime(page, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert not index.is_current()


@pytest.mark.parametrize('wrong', ['/elsewhere/left', '/root/wrong'])
def test_unrelated_instance_cannot_supply_a_reference(scratch_project, wrong):
    file = project(scratch_project, sheet('left', 'Left', 'child.kicad_sch'))
    (scratch_project / 'child.kicad_sch').write_text('(kicad_sch (uuid "child") '
        + library() + symbol('U1', 'a', [(wrong, 'U1', 1)]) + ')')
    with pytest.raises(ValueError, match='U1'):
        load_navigation_identity(file, ['U1'])


def test_stale_other_project_and_orphan_cannot_supply_reference(scratch_project):
    file = project(scratch_project, '')
    (scratch_project / 'orphan.kicad_sch').write_text('(kicad_sch (uuid "orphan") '
        + library() + symbol('U1', 'a', [('/root', 'U1', 1)]) + ')')
    with pytest.raises(ValueError, match='U1'):
        load_navigation_identity(file, ['U1'])
    file.with_suffix('.kicad_sch').write_text('(kicad_sch (uuid "root") '
        + library() + symbol('U1', 'a', [('/root', 'U1', 1)]).replace('project "Design"', 'project "Other"') + ')')
    with pytest.raises(ValueError, match='U1'):
        load_navigation_identity(file, ['U1'])


def test_lookup_is_io_free_and_shared_parser_is_used_once_per_file(scratch_project):
    file = project(scratch_project, symbol('U1', 'a', [('/root', 'U1', 1)]))
    calls = []
    def read_page(path):
        calls.append(path)
        content = path.read_bytes()
        return parse_sexpr(content.decode()), hashlib.sha256(content).hexdigest()
    index = load_navigation_identity(file, read_page=read_page)
    assert calls == [file.with_suffix('.kicad_sch')]
    with patch('pathlib.Path.read_bytes', side_effect=AssertionError('lookup performed I/O')):
        for _ in range(20):
            assert index.search_names('U1') == ('U1',)
            assert index.matches('U1', 'U1')
    file.write_text('{"schematic":{"subpart_first_id":97}}')
    assert not index.is_current()


@pytest.mark.parametrize('change', ['missing_library', 'invalid_unit', 'bad_settings', 'relative', 'ambiguous'])
def test_unknown_identity_fails_closed(scratch_project, change):
    file = project(scratch_project, symbol('U1', 'a', [('/root', 'U1', 1)]))
    page = file.with_suffix('.kicad_sch')
    if change == 'missing_library':
        page.write_text(page.read_text().replace(library(), '(lib_symbols)'))
    elif change == 'invalid_unit':
        page.write_text(page.read_text().replace('(unit 1)', '(unit 2)'))
    elif change == 'bad_settings':
        file.write_text('{"schematic":{"subpart_first_id":true}}')
    elif change == 'relative':
        file = type(file)('Design.kicad_pro')
    else:
        (scratch_project / 'Other.kicad_pro').write_text('{}')
    with pytest.raises(ValueError):
        load_navigation_identity(file)


def test_server_uses_saved_unit_and_full_page_identity(scratch_project):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from part_selection_design_source import DesignSource
    from part_selection_server import PartSelectionApp
    file = project(scratch_project, symbol('U1', 'a', [('/root', 'U1', 1)]), count=2)
    app = PartSelectionApp.__new__(PartSelectionApp)
    app.project_root, app.config = scratch_project, {}
    sources = DesignSource(app)
    app.design_sync = SimpleNamespace(sources=sources)
    app.run_kicad_script = Mock(return_value={'ok': True})
    assert app.jump_to_schematic('kicad-sch-search://U1')['ok']
    app.run_kicad_script.assert_called_with('kicad-sch-search://U1', [
        '-Mode', 'SearchPanel', '-NavigationName', 'U1A', '-NavigationTitle', 'Design'])
    with patch('part_selection_navigation.load_navigation_identity', side_effect=AssertionError('reparsed')):
        assert app.jump_to_schematic('kicad-sch-search://U1')['ok']
    file.write_text('{"schematic":{"subpart_first_id":97}}')
    assert app.jump_to_schematic('kicad-sch-search://U1')['ok']
    assert app.run_kicad_script.call_args.args[1][3] == 'U1a'
    app.run_kicad_script.reset_mock()
    assert not app.jump_to_schematic('kicad-sch-search://U10')['ok']
    app.run_kicad_script.assert_not_called()


def test_navigation_rejects_ambiguous_repeated_instance(scratch_project):
    from types import SimpleNamespace
    from part_selection_design_source import DesignSource
    project(scratch_project, sheet('left', 'Left', 'child.kicad_sch')
            + sheet('right', 'Right', 'child.kicad_sch'))
    (scratch_project/'child.kicad_sch').write_text('(kicad_sch (uuid "child") '
        + library() + symbol('U1', 'a', [('/root/left', 'U1', 1), ('/root/right', 'U1', 1)]) + ')')
    with pytest.raises(ValueError, match='U1'):
        DesignSource(SimpleNamespace(project_root=scratch_project)).navigation_target('U1')

"""One hierarchical design read supplies both identity and mapped properties."""
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_pcb_schematic_identity import hierarchy


def app_for(root):
    pages, child = hierarchy(root)
    child.write_text(child.read_text().replace('(property "Reference" "L9")',
        '(property "Reference" "L9") (property "值" "10uH") '
        '(property "封装" "Local:1206") (property "备注" "inductor")'))
    records = root / 'records.json'
    records.write_text('{"L9":{"review_status":"Confirmed"}}')
    return SimpleNamespace(project_root=root, overrides_path=records,
        config={'schematic_dir':pages, 'field_mapping':{'value':'值','footprint':'封装'}}), child


def test_capture_uses_one_parse_per_page_and_maps_only_present_fields(scratch_project):
    from part_selection_design_source import DesignSource
    from kicad_sexpr import parse_sexpr
    app, child = app_for(scratch_project)
    with patch('part_selection_design_source.parse_sexpr', wraps=parse_sexpr) as parse:
        result = DesignSource(app).capture(['L9'])
    assert parse.call_count == 3
    assert result['fields']['L9'] == {'value':'10uH','footprint':'Local:1206','comment':'inductor'}
    assert result['records']['L9']['review_status'] == 'Confirmed'
    assert '/old-root/top-sheet/out-sheet/l9-symbol' in result['identity']['paths']['L9']
    assert str(child.resolve()) in result['versions']['hashes']


def test_validate_hashes_bytes_and_configuration_without_reparsing(scratch_project):
    from part_selection_design_source import DesignSource
    app, child = app_for(scratch_project)
    source = DesignSource(app)
    snapshot = source.capture(['L9'])
    with patch('part_selection_design_source.parse_sexpr', side_effect=AssertionError('parsed twice')):
        assert source.validate(snapshot)
    stamp = child.stat()
    child.write_text(child.read_text().replace('10uH','22uH'))
    os.utime(child, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert not source.validate(snapshot)
    snapshot = source.capture(['L9'])
    app.config['field_mapping']['value'] = 'Value'
    assert not source.validate(snapshot)


def test_added_page_or_records_change_invalidates_source(scratch_project):
    from part_selection_design_source import DesignSource
    app, _ = app_for(scratch_project)
    source = DesignSource(app)
    snapshot = source.capture(['L9'])
    orphan = app.config['schematic_dir'] / 'Added.kicad_sch'
    orphan.write_text('(kicad_sch (uuid "extra"))')
    assert not source.validate(snapshot)
    snapshot = source.capture(['L9'])
    app.overrides_path.write_text('{}')
    assert not source.validate(snapshot)


def test_orphan_fields_cannot_supply_a_missing_hierarchy_instance(scratch_project):
    from part_selection_design_source import DesignSource
    app, child = app_for(scratch_project)
    (app.config['schematic_dir'] / 'Orphan.kicad_sch').write_text(child.read_text())
    child.write_text('(kicad_sch (uuid "out-file"))')
    with pytest.raises(ValueError, match='L9'):
        DesignSource(app).capture(['L9'])


def test_native_save_refresh_accepts_formatting_but_rejects_selected_field_changes(scratch_project):
    from part_selection_design_source import DesignSource
    app, child = app_for(scratch_project)
    source = DesignSource(app)
    snapshot = source.capture(['L9'])
    child.write_text(child.read_text() + '\n')
    assert not source.validate(snapshot)
    assert source.refresh_after_save(snapshot)
    assert source.validate(snapshot)
    child.write_text(child.read_text().replace('10uH', '22uH'))
    assert not source.refresh_after_save(snapshot)
    assert snapshot['fields']['L9']['value'] == '10uH'


def test_native_save_refresh_never_accepts_library_or_records_changes(scratch_project):
    from part_selection_design_source import DesignSource
    app, _ = app_for(scratch_project)
    source = DesignSource(app)
    snapshot = source.capture(['L9'])
    (scratch_project / 'fp-lib-table').write_text('(fp_lib_table)')
    assert not source.refresh_after_save(snapshot)

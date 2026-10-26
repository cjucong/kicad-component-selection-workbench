"""One hierarchical design read supplies both identity and mapped properties."""
import os
from copy import deepcopy
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


@pytest.mark.parametrize('intervening', ['', 'value', 'record', 'identity', 'library'])
def test_post_write_source_only_accepts_the_expected_write(scratch_project, intervening):
    import json
    from part_selection_design_source import DesignSource
    app, child = app_for(scratch_project)
    source = DesignSource(app)
    before = source.capture(['L9'])
    desired = {'L9':{'value':'22uH'}}
    child.write_text(child.read_text().replace('10uH','22uH'))
    records = deepcopy(before['records'])
    records['L9'] = {**records['L9'], 'updated_at':'now','schematic_synced_at':'now',
        'schematic_sync_summary':'value','schematic_field_snapshot':{'value':'22uH'}}
    app.overrides_path.write_text(json.dumps(records))
    if intervening == 'value':
        child.write_text(child.read_text().replace('22uH','33uH'))
    elif intervening == 'record':
        records['L9']['footprint_change'] = {'schema':1,'operation':'replace'}
        app.overrides_path.write_text(json.dumps(records))
    elif intervening == 'identity':
        child.write_text(child.read_text().replace('l9-symbol','other-symbol'))
    elif intervening == 'library':
        (scratch_project/'fp-lib-table').write_text('(fp_lib_table)')
    if intervening:
        with pytest.raises(ValueError, match='阶段'):
            source.after_write(before,desired)
    else:
        assert source.after_write(before,desired)['fields']['L9']['value']=='22uH'


def test_capture_all_enumerates_hierarchy_instances_not_property_references(scratch_project):
    from part_selection_design_source import DesignSource
    app, child = app_for(scratch_project)
    path = scratch_project / 'Design.kicad_sch'
    path.write_text(path.read_text().replace(' (uuid "new-root")', ''' (uuid "new-root")
      (symbol (uuid "root-symbol") (property "Reference" "R1")
        (property "Footprint" "Local:1206")
        (instances (project "Design" (path "/old-root" (reference "R1")))))''', 1))
    child.write_text(child.read_text().replace('(property "Reference" "L9")', '(property "Reference" "L999")'))
    result = DesignSource(app).capture()
    assert set(result['refs']) == {'R1', 'L9'}
    assert result['fields']['L9']['footprint'] == 'Local:1206'
    assert set(result['identity']['paths']) == {'R1', 'L9'}


def test_capture_all_excludes_orphans_and_invalidates_identity_only_change(scratch_project):
    from part_selection_design_source import DesignSource
    app, child = app_for(scratch_project)
    (app.config['schematic_dir'] / 'Orphan.kicad_sch').write_text(child.read_text().replace('L9', 'L99'))
    source = DesignSource(app)
    result = source.capture()
    assert result['refs'] == ['L9']
    child.write_text(child.read_text().replace('l9-symbol', 'new-symbol'))
    assert not source.validate(result)


def test_legacy_capture_is_explicit_and_cannot_relax_real_identity(scratch_project):
    from part_selection_design_source import DesignSource
    records = scratch_project / 'records.json'
    records.write_text('{}')
    page = scratch_project / 'Legacy.kicad_sch'
    page.write_text('(kicad_sch (symbol (property "Reference" "R1") (property "Footprint" "Local:R")))')
    source = DesignSource(SimpleNamespace(project_root=scratch_project, overrides_path=records,
        config={'schematic_dir':scratch_project}))
    with pytest.raises(ValueError, match='UUID'):
        source.capture(['R1'])
    snapshot = source.capture(allow_legacy=True)
    assert snapshot['refs'] == ['R1']
    assert snapshot['fields']['R1']['footprint'] == 'Local:R'
    assert not snapshot['identity']['paths']['R1']
    page.write_text(page.read_text() + '\n')
    assert not source.validate(snapshot)
    page.write_text(page.read_text().replace('(kicad_sch ', '(kicad_sch (uuid "real-root") '))
    with pytest.raises(ValueError, match='实例|关联|位号'):
        source.capture(['R1'], allow_legacy=True)


def test_legacy_opt_in_does_not_guess_missing_project_identity(scratch_project):
    from part_selection_design_source import DesignSource
    from part_selection_server import PartSelectionApp
    from part_selection_overrides import load_overrides
    app, _ = app_for(scratch_project)
    (scratch_project / 'Design.kicad_pro').unlink()
    with pytest.raises(ValueError, match='L9'):
        DesignSource(app).capture(['L9'], allow_legacy=True)
    with pytest.raises(ValueError, match='完整库封装'):
        PartSelectionApp.footprint_scope(app, ['L9'], load_overrides(app.overrides_path))

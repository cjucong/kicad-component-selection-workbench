"""Field mappings use disposable schematics; never touch installed user projects."""
import json
from unittest.mock import patch

import pytest

from part_selection_fields import normalize_field_mapping, field_mapping_snapshot
from part_selection_helper import (load_schematic_parts, read_schematic_symbol_field_values,
                                   update_schematic_symbol_field_map, update_schematic_symbol_fields,
                                   parse_symbol_properties, iter_symbol_blocks)
from part_selection_server import PartSelectionApp
from part_selection_overrides import load_config, save_override
from part_selection_settings import settings_snapshot, preview_field_mapping


def symbol(ref, **props):
    props = {'Reference': ref, **props}
    return '(symbol (lib_id "Device:R") ' + ' '.join(
        f'(property "{key}" "{value}" (at 0 0 0))' for key, value in props.items()) + ')'


@pytest.fixture
def mapped_app(scratch_project):
    root = scratch_project
    (root / 'BOM').mkdir()
    config = root / 'BOM/config.json'
    config.write_text(json.dumps({'project_root': '..', 'schematic_dir': '.', 'source_bom': '__schematic__'}))
    (root / 'example.kicad_sch').write_text('(kicad_sch ' + symbol('R1', **{
        'Comment': 'resistor', 'Value': '10k', '值': '20k', 'Footprint': 'Library:R_0603',
        '装配封装': 'Library:R_0805', '参数详情': 'original parameters', '立创料号': 'C100',
    }) + ')', encoding='utf-8')
    app = PartSelectionApp(config)
    yield app
    app.search_jobs.close()
    app.selection_jobs.close()


def configure(app, mapping):
    return app.save_path_settings({'revision': settings_snapshot(app.config)['revision'],
                                  'values': {}, 'field_mapping': mapping})


def test_default_aliases_and_standard_precedence(scratch_project):
    (scratch_project / 'a.kicad_sch').write_text('(kicad_sch ' + symbol('C1', **{
        '注释': 'capacitor', '值': '100nF', '封装': 'Capacitor_SMD:C_0603',
        '物料参数': '50V X7R', '立创编号': 'C123',
    }) + symbol('C2', Value='1uF', **{'值': '100nF'}) + ')', encoding='utf-8')
    parts = load_schematic_parts(scratch_project)
    assert (parts['C1'].comment, parts['C1'].value, parts['C1'].accepted_c_code) == ('capacitor', '100nF', 'C123')
    assert parts['C1'].description == '50V X7R'
    assert parts['C2'].value == '1uF'
    update_schematic_symbol_field_map(scratch_project, {'C1': {'value': '220nF'}})
    props = parse_symbol_properties(next(iter_symbol_blocks((scratch_project / 'a.kicad_sch').read_text(encoding='utf-8'))))
    assert props['值'] == props['Value'] == '220nF'
    update_schematic_symbol_field_map(scratch_project, {'C1': {'value': '330nF'}})
    props = parse_symbol_properties(next(iter_symbol_blocks((scratch_project / 'a.kicad_sch').read_text(encoding='utf-8'))))
    assert props['值'] == props['Value'] == '330nF'


def test_manual_mapping_persists_and_drives_read_edit_sync_verify(mapped_app):
    app = mapped_app
    before = (app.project_root / 'example.kicad_sch').read_bytes()
    configure(app, {'value': '值', 'footprint': '装配封装', 'description': '详细规格'})
    assert (app.project_root / 'example.kicad_sch').read_bytes() == before
    assert load_config(app.config['config_path'])['field_mapping']['value'] == '值'
    row = app.rows()[0]
    assert row['value'] == '20k'
    assert row['footprint'] == 'Library:R_0805'
    app.save_field_override({'refs': 'R1', 'field_overrides': {'value': '30k', 'footprint': 'Library:R_1206', 'description': 'new parameters'}})
    save_override(app.overrides_path, 'R1', {'manual_c_code': 'C100', 'field_overrides': {
        'description': 'new parameters', 'description_c_code': 'C100'}})
    with patch.object(app, 'prepare_kicad_for_external_schematic_write') as prepare:
        result = app.bulk_sync_bom_state_to_schematic('__schematic__', selected_refs=['R1'], notify=False, prepare=False)
        prepare.assert_not_called()
    assert result['schematic_verify']['ok']
    props = parse_symbol_properties((app.project_root / 'example.kicad_sch').read_text(encoding='utf-8'))
    assert props['Value'] == props['值'] == '30k'
    assert props['Footprint'] == props['装配封装'] == 'Library:R_1206'
    assert props['详细规格'] == 'new parameters'
    assert props['参数详情'] == 'original parameters'
    assert not app.rows()[0]['field_changed_refs']


def test_new_custom_fields_are_hidden_and_only_requested_refs_change(scratch_project):
    path = scratch_project / 'a.kicad_sch'
    path.write_text('(kicad_sch ' + symbol('U1', Value='ADC') + symbol('U2', Value='DAC') + ')', encoding='utf-8')
    update_schematic_symbol_field_map(scratch_project, {'U1': {'description': '24 bit'}}, {'description': '参数详情'})
    text = path.read_text(encoding='utf-8')
    assert '(property "参数详情" "24 bit" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))' in text
    values = read_schematic_symbol_field_values(scratch_project, ['U1', 'U2'], ['description'], {'description': '参数详情'})
    assert values == {'U1': {'description': '24 bit'}, 'U2': {'description': ''}}


@pytest.mark.parametrize('mapping', [[], {'unknown': 'x'}, {'value': None}, {'value': 'x\ny'},
    {'value': 'Reference'}, {'comment': 'Value'}, {'description': 'Datasheet'},
    {'value': 'same', 'footprint': 'SAME'}])
def test_reject_invalid_or_overlapping_mapping(mapping):
    with pytest.raises(ValueError):
        normalize_field_mapping(mapping)


def test_ambiguous_alias_requires_manual_mapping_and_no_partial_write(scratch_project):
    first = scratch_project / 'a.kicad_sch'
    second = scratch_project / 'b.kicad_sch'
    first.write_text('(kicad_sch ' + symbol('C1', Value='100nF') + ')', encoding='utf-8')
    second.write_text('(kicad_sch ' + symbol('C2', **{'值': '1uF', '数值': '2uF'}) + ')', encoding='utf-8')
    original = first.read_bytes()
    preview = field_mapping_snapshot(scratch_project)
    assert next(field for field in preview['fields'] if field['key'] == 'value')['conflicts'] == 1
    assert next(field for field in preview['fields'] if field['key'] == 'description')['conflicts'] == 0
    with pytest.raises(ValueError, match='手动映射'):
        update_schematic_symbol_field_map(scratch_project, {'C1': {'value': '22nF'}, 'C2': {'value': '22nF'}})
    assert first.read_bytes() == original
    assert load_schematic_parts(scratch_project, {'value': '值'})['C2'].value == '1uF'


def test_single_row_sync_and_reset_to_auto(mapped_app):
    app = mapped_app
    configure(app, {'value': '值', 'description': '规格'})
    with patch.object(app, 'prepare_kicad_for_external_schematic_write', return_value={'ok': True}), \
            patch.object(app, 'run_kicad_script', return_value={'ok': True}):
        result = app.sync_schematic({'refs': 'R1', 'field_overrides': {'value': '47k'}, 'notify': False})
    assert result['schematic_verify']['ok']
    assert app.schematic_parts()['R1'].value == '47k'
    configure(app, {})
    assert app.config['field_mapping'] == {}
    assert app.schematic_parts()['R1'].description == 'original parameters'


def test_preview_is_read_only_and_matches_new_directory(mapped_app):
    app = mapped_app
    before = app.config['config_path'].read_bytes()
    result = preview_field_mapping(app.config, {'field_mapping': {'description': '新参数'}})
    assert next(item for item in result['field_mapping']['fields'] if item['key'] == 'description')['missing'] == 1
    assert app.config['config_path'].read_bytes() == before
    configure(app, {'comment': '参数详情'})
    assert app.schematic_parts()['R1'].comment == 'original parameters'
    assert app.schematic_parts()['R1'].description == ''  # Explicit assignment reserves the property.


def test_mirror_builtin_even_when_mapped_value_already_matches(mapped_app):
    configure(mapped_app, {'value': '值'})
    assert not mapped_app.verify_schematic_field_values({'R1': {'value': '20k'}})['ok']
    directory = mapped_app.config['schematic_dir']
    result = update_schematic_symbol_fields(directory, ['R1'], {'value': '20k'}, {'value': '值'})
    assert result['updated_refs'] == ['R1']
    assert read_schematic_symbol_field_values(directory, ['R1'], ['value'])['R1']['value'] == '20k'
    assert mapped_app.verify_schematic_field_values({'R1': {'value': '20k'}})['ok']


def test_explicit_english_case_uses_existing_property(mapped_app):
    app = mapped_app
    configure(app, {'value': 'value', 'comment': 'comment'})
    assert app.schematic_parts()['R1'].value == '10k'
    app.write_schematic_fields(app.config['schematic_dir'], {'R1': {'value': '33k'}})
    props = parse_symbol_properties((app.project_root / 'example.kicad_sch').read_text(encoding='utf-8'))
    assert props['Value'] == '33k'
    assert 'value' not in props


def test_new_manual_field_reserves_another_automatic_alias(mapped_app):
    app = mapped_app
    configure(app, {'description': '备注', 'value': '值'})
    app.write_schematic_fields(app.config['schematic_dir'], {'R1': {'description': 'new parameters', 'value': '22k'}})
    assert app.schematic_parts()['R1'].description == 'new parameters'
    assert app.schematic_parts()['R1'].comment == 'resistor'
    assert app.verify_schematic_field_values({'R1': {'value': '22k'}})['ok']

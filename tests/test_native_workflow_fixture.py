from copy import deepcopy

import pytest

from kicad_sexpr import parse_sexpr
from run_native_footprint_workflow import symbol_pins, validate_netlist, verify_snapshot


def test_flat_and_nested_converter_symbol_pins():
    definition = parse_sexpr('(symbol "R" (pin input line (number "1")) '
                             '(symbol "R_1_1" (pin input line (number "2"))))', 'symbol')
    assert [p.child('number').arg() for p in symbol_pins(definition)] == ['1', '2']


def test_fixture_rejects_disconnected_pins():
    parts = [{'ref': 'R1', 'pin_numbers': ['1']}]
    validate_netlist('(export (nets (net (name "/R1_1") (node (ref "R1") (pin "1")))))', parts)
    with pytest.raises(AssertionError, match='fixture net'):
        validate_netlist('(export (nets (net (name "unconnected") (node (ref "R1") (pin "1")))))', parts)


@pytest.mark.parametrize('stage', ['rename', 'converted'])
def test_verification_requires_actual_stage_and_preserves_geometry(scratch_project, stage):
    root = scratch_project
    target = 'Local:' + ('Renamed_R1' if stage == 'rename' else 'Converted_R1')
    (root / 'FlowQA.kicad_sch').write_text(
        '(kicad_sch (symbol (property "Reference" "R1") '
        f'(property "Footprint" "{target}")))', encoding='utf-8')
    baseline = {'tracks': [], 'parts': {'R1': {
        'id': 'Local:Original_R1', 'position_nm': [100, 200], 'angle': 37, 'back': False,
        'path': '/fixture', 'pads': {'1': {
            'position_nm': [110, 210], 'size_nm': [80, 90], 'angle': 127, 'net': '/R1_1'}}}}}
    current = deepcopy(baseline)
    current['parts']['R1']['id'] = target
    verify_snapshot(root, baseline, current, stage)
    with pytest.raises(AssertionError, match='Wrong workflow stage'):
        verify_snapshot(root, baseline, current, 'converted' if stage == 'rename' else 'rename')
    current['parts']['R1']['pads']['1']['position_nm'][0] += 10
    with pytest.raises(AssertionError, match='position'):
        verify_snapshot(root, baseline, current, stage)

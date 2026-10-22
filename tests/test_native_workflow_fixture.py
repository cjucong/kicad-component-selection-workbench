from copy import deepcopy
import json
from pathlib import Path

import pytest

import run_native_footprint_workflow as workflow
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


@pytest.fixture
def workflow_snapshot(scratch_project):
    root = scratch_project
    baseline = {'tracks': [[[110, 210], [120, 210], 15, 0, '/R1_1']], 'parts': {'R1': {
        'id': 'Local:Original_R1', 'position_nm': [100, 200], 'angle': 37, 'back': False,
        'path': '/fixture', 'pads': {'1': {
            'position_nm': [110, 210], 'size_nm': [80, 90], 'angle': 127, 'net': '/R1_1'}}},
        'U1': {'id': 'Local:Original_U1', 'position_nm': [300, 200], 'angle': 90, 'back': True,
               'path': '/back-fixture', 'pads': {'1': {
                   'position_nm': [310, 210], 'size_nm': [60, 70], 'angle': 180, 'net': '/U1_1'}}}}}
    return root, baseline


def stage_snapshot(root, baseline, prefix):
    current = deepcopy(baseline)
    symbols = []
    for ref, part in current['parts'].items():
        part['id'] = 'Local:' + prefix + ref
        symbols.append(f'(symbol (property "Reference" "{ref}") '
                       f'(property "Footprint" "{part["id"]}"))')
    (root / 'FlowQA.kicad_sch').write_text('(kicad_sch ' + ''.join(symbols) + ')', encoding='utf-8')
    return current


@pytest.mark.parametrize('stage,prefix', [
    ('rename', 'Renamed_'), ('converted', 'Converted_'), ('library', 'Library_')])
def test_verification_requires_actual_stage_and_preserves_geometry(workflow_snapshot, stage, prefix):
    root, baseline = workflow_snapshot
    current = stage_snapshot(root, baseline, prefix)
    verify_snapshot(root, baseline, current, stage)
    with pytest.raises(AssertionError, match='Wrong workflow stage'):
        verify_snapshot(root, baseline, current, 'converted' if stage == 'rename' else 'rename')
    current['parts']['R1']['pads']['1']['position_nm'][0] += 10
    with pytest.raises(AssertionError, match='position'):
        verify_snapshot(root, baseline, current, stage)


@pytest.mark.parametrize('prefix', ['Original_', 'Renamed_', 'Converted_', 'Target_'])
def test_library_verification_rejects_wrong_id_even_when_schematic_matches(workflow_snapshot, prefix):
    root, baseline = workflow_snapshot
    current = stage_snapshot(root, baseline, prefix)
    with pytest.raises(AssertionError, match='Wrong workflow stage'):
        verify_snapshot(root, baseline, current, 'library')


@pytest.mark.parametrize('stage,prefix', [
    ('rename', 'Renamed_'), ('converted', 'Converted_'), ('library', 'Library_')])
@pytest.mark.parametrize('key,value', [('size_nm', [40, 50]), ('angle', 42)])
def test_only_rename_requires_unchanged_pad_geometry(workflow_snapshot, stage, prefix, key, value):
    root, baseline = workflow_snapshot
    current = stage_snapshot(root, baseline, prefix)
    current['parts']['U1']['pads']['1'][key] = value
    if stage == 'rename':
        with pytest.raises(AssertionError, match=key):
            verify_snapshot(root, baseline, current, stage)
    else:
        verify_snapshot(root, baseline, current, stage)


@pytest.mark.parametrize('ref', ['R1', 'U1'])
@pytest.mark.parametrize('key,value', [
    ('position_nm', [101, 200]), ('angle', 12), ('back', None), ('path', '/changed')])
def test_library_verification_preserves_board_placement(workflow_snapshot, ref, key, value):
    root, baseline = workflow_snapshot
    current = stage_snapshot(root, baseline, 'Library_')
    current['parts'][ref][key] = value
    if key in ('position_nm', 'angle'):
        verify_snapshot(root, baseline, current, 'library')
    else:
        with pytest.raises(AssertionError, match=key):
            verify_snapshot(root, baseline, current, 'library')


@pytest.mark.parametrize('change,message', [
    ('tracks', 'tracks'), ('parts', 'Footprints added/deleted'), ('pads', 'pad numbers'),
    ('position_nm', 'position'), ('net', 'net'), ('schematic', 'PCB does not match schematic')])
def test_library_verification_rejects_connectivity_changes(workflow_snapshot, change, message):
    root, baseline = workflow_snapshot
    current = stage_snapshot(root, baseline, 'Library_')
    if change == 'tracks':
        current['tracks'][0][0][0] += 1
    elif change == 'parts':
        del current['parts']['U1']
    elif change == 'pads':
        current['parts']['U1']['pads']['2'] = deepcopy(current['parts']['U1']['pads']['1'])
    elif change == 'schematic':
        stage_snapshot(root, baseline, 'Target_')
    else:
        current['parts']['U1']['pads']['1'][change] = [320, 210] if change == 'position_nm' else '/wrong'
    with pytest.raises(AssertionError, match=message):
        verify_snapshot(root, baseline, current, 'library')


def test_verify_session_accepts_library_stage(workflow_snapshot, monkeypatch, capsys):
    root, baseline = workflow_snapshot
    current = stage_snapshot(root, baseline, 'Library_')
    report = root / 'report'
    report.mkdir()
    (report / 'baseline.json').write_text(json.dumps(baseline), encoding='utf-8')
    session = report / 'session.json'
    session.write_text(json.dumps({'root': str(root), 'report': str(report)}), encoding='utf-8')
    monkeypatch.setattr(workflow, 'native', lambda payload: current)
    monkeypatch.setattr(workflow.sys, 'argv', [
        str(workflow.__file__), '--verify-session', str(session), '--stage', 'library'])
    workflow.main()
    assert json.loads((report / 'library.json').read_text(encoding='utf-8')) == current
    assert 'VERIFIED library' in capsys.readouterr().out


def test_fixture_library_targets_copy_unaligned_converter_modules(scratch_project, monkeypatch):
    import part_selection_footprint_geometry as geometry
    import part_selection_library as library

    root = scratch_project
    raw_module = b'(footprint "Raw" (pad "1" smd rect (at 1 2 17) (size 1 2) (layers "F.Cu")))\r\n'

    class OfflineImporter:
        def __init__(self, project, converter):
            self.project = project

        def convert(self, code, progress):
            folder = self.project / 'Libraries/DSMU/LCSC' / code
            pretty = folder / 'Raw.pretty'
            pretty.mkdir(parents=True)
            (folder / 'Raw.kicad_sym').write_text(
                '(kicad_symbol_lib (symbol "Raw" (pin input line (at 1 2 0) (number "1"))))',
                encoding='utf-8')
            (pretty / 'Raw.kicad_mod').write_bytes(raw_module)
            return {'footprint': 'Raw:Raw'}

    def export_netlist(command, **kwargs):
        Path(command[command.index('-o') + 1]).write_text(
            '(export (nets (net (name "/R1_1") (node (ref "R1") (pin "1"))) '
            '(net (name "/U1_1") (node (ref "U1") (pin "1")))))', encoding='utf-8')

    monkeypatch.setattr(library, 'LibraryImporter', OfflineImporter)
    monkeypatch.setattr(geometry, '_find_kicad_python', lambda: root / 'python.exe')
    monkeypatch.setattr(workflow.subprocess, 'run', export_netlist)
    payload = workflow.make_project(root, 'offline-converter')
    assert [(p['ref'], p['back'], p['angle']) for p in payload['parts']] == [
        ('R1', False, 37), ('U1', True, 90)]
    for ref in ('R1', 'U1'):
        assert (root / 'Local.pretty' / ('Target_' + ref + '.kicad_mod')).read_bytes() == raw_module
        assert not (root / 'Local.pretty' / ('Original_' + ref + '.kicad_mod')).exists()

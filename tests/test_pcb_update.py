from copy import deepcopy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from part_selection_pcb import PcbUpdates


@pytest.fixture
def updates(scratch_project):
    root = scratch_project
    (root / 'test.kicad_pro').write_text('{}')
    (root / 'test.kicad_sch').write_text('(kicad_sch (uuid "sheet") (symbol (uuid "symbol") '
        '(property "Reference" "R1") (instances (project "test" (path "/sheet" (reference "R1"))))))')
    overrides = root / 'overrides.json'
    overrides.write_text(json.dumps({'R1': {'decision': 'Accept', 'stock_c_code': 'C123',
        'field_overrides': {'footprint': 'Local:New'}}}))
    app = SimpleNamespace(project_root=root, config={'schematic_dir': root}, overrides_path=overrides,
        selection_save_lock=threading.RLock(), read_schematic_fields=Mock(return_value={'R1': {'footprint': 'Local:New'}}))
    part = {'ref': 'R1', 'uuid': 'r1', 'footprint': 'Local:Old', 'position_mm': [0, 0],
        'rotation_deg': 0, 'back': False, 'symbol_path': '/sheet/symbol', 'pads': [
            {'number': '1', 'position_mm': [-1, 0]}, {'number': '2', 'position_mm': [1, 0]}], 'proto': 'old'}
    pretty = root / 'Local.pretty'
    pretty.mkdir()
    (root / 'fp-lib-table').write_text('(fp_lib_table (lib (name "Local") (type "KiCad") (uri "${KIPRJMOD}/Local.pretty")))')
    library = pretty / 'New.kicad_mod'
    library.write_text('target')
    import hashlib
    target = {**deepcopy(part), 'footprint': 'Local:New', 'proto': 'new',
        'source_path': str(library), 'source_hash': hashlib.sha256(b'target').hexdigest()}
    snapshot = {'session_id': 'session', 'revision': '1', 'parts': {'R1': part}, 'text': 'board'}
    session = SimpleNamespace(snapshot=Mock(return_value=snapshot), read_library=Mock(return_value=target),
        apply=Mock(return_value={'ok': True, 'saved': False, 'refs': ['R1'], 'snapshot': snapshot}))
    service = PcbUpdates(app, session=session, build=lambda source, selected, placement: 'new')
    return service, app, session, library


def test_preview_and_apply_do_not_touch_choice_or_confirmation(updates):
    service, app, session, _ = updates
    before = app.overrides_path.read_bytes()
    preview = service.preview(['R1'], '__schematic__')
    assert preview['ok'] and preview['items'][0]['ref'] == 'R1'
    session.apply.assert_not_called()
    result = service.apply(preview['preview_id'], True)
    assert result['ok'] and result['saved'] is False
    assert before == app.overrides_path.read_bytes()
    assert service.apply(preview['preview_id'], True) == result
    session.apply.assert_called_once()


@pytest.mark.parametrize('problem', ['no_ack', 'source_changed', 'record_changed', 'library_changed', 'schematic_changed'])
def test_preflight_changes_never_apply(updates, problem):
    service, app, session, library = updates
    preview = service.preview(['R1'], '')
    if problem == 'source_changed':
        session.snapshot.return_value = {**session.snapshot.return_value, 'revision': '2'}
    elif problem == 'record_changed':
        app.overrides_path.write_text('{}')
    elif problem == 'library_changed':
        library.write_text('changed')
    elif problem == 'schematic_changed':
        app.read_schematic_fields.return_value = {'R1': {'footprint': 'Local:Other'}}
    with pytest.raises(ValueError):
        service.apply(preview['preview_id'], problem != 'no_ack')
    session.apply.assert_not_called()


def test_unsynced_schematic_and_empty_selection_are_blocked(updates):
    service, app, session, _ = updates
    with pytest.raises(ValueError):
        service.preview([], '')
    app.read_schematic_fields.return_value = {'R1': {'footprint': 'Local:Old'}}
    with pytest.raises(ValueError, match='同步原理图'):
        service.preview(['R1'], '')
    session.apply.assert_not_called()


def test_uncertain_failure_is_not_automatically_repeated(updates):
    service, app, session, _ = updates
    preview = service.preview(['R1'], '')
    session.apply.side_effect = RuntimeError('connection lost after write')
    with pytest.raises(RuntimeError):
        service.apply(preview['preview_id'], True)
    with pytest.raises(ValueError, match='重新预检'):
        service.apply(preview['preview_id'], True)
    session.apply.assert_called_once()


def test_footprint_edit_persists_pcb_check_separately_from_schematic_sync():
    from part_selection_overrides import merge_override_record
    before = {'review_status': 'Confirmed', 'field_overrides': {'footprint': 'Local:Old'}}
    edited = merge_override_record(before, {'field_overrides': {'footprint': 'Local:New'}}, 'now', capture_undo=True)
    assert edited['pcb_update_required']['target'] == 'Local:New'
    synced = merge_override_record(edited, {'schematic_synced_at': 'later', 'clear_workbench_undo': True}, 'later')
    assert synced['pcb_update_required'] == edited['pcb_update_required']
    assert synced['review_status'] == 'Confirmed'


def test_lost_start_response_resolves_to_same_job(updates):
    service, _, _, _ = updates
    preview = service.preview(['R1'], '')
    jobs = SimpleNamespace(start=Mock(return_value={'ok': True, 'job_id': 'one'}),
                           status=Mock(return_value={'ok': True, 'job_id': 'one'}))
    first = service.start(preview['preview_id'], True, jobs)
    second = service.start(preview['preview_id'], True, jobs)
    assert first['job_id'] == second['job_id'] == service.job_id(preview['preview_id'])
    jobs.start.assert_called_once()


def test_comparison_is_bound_to_preflight_ref_and_live_source(updates):
    service, _, session, _ = updates
    token = service.preview(['R1'], '')['preview_id']
    with pytest.raises(ValueError):
        service.comparison(token, 'R2')
    session.snapshot.return_value = {**session.snapshot.return_value, 'revision': 'changed'}
    with pytest.raises(ValueError, match='改变'):
        service.comparison(token, 'R1')
    session.apply.assert_not_called()


def test_recreated_schematic_symbol_does_not_reuse_old_pcb_association(updates):
    service, app, session, _ = updates
    path = app.project_root / 'test.kicad_sch'
    path.write_text(path.read_text().replace('uuid "symbol"', 'uuid "new-symbol"'))
    with pytest.raises(ValueError, match='关联'):
        service.preview(['R1'], '')


def test_redirected_library_nickname_invalidates_preview(updates):
    service, app, session, _ = updates
    token = service.preview(['R1'], '')['preview_id']
    path = app.project_root / 'fp-lib-table'
    path.write_text(path.read_text().replace('Local.pretty', 'Other.pretty'))
    with pytest.raises(ValueError, match='改变'):
        service.apply(token, True)
    session.apply.assert_not_called()


def test_pcb_execution_obeys_selection_then_override_lock_order():
    import ast
    import inspect
    import textwrap
    from part_selection_server import PartSelectionApp
    tree = ast.parse(textwrap.dedent(inspect.getsource(PartSelectionApp.run_selection_job)))
    block = next(node for node in ast.walk(tree) if isinstance(node, ast.With))
    locks = [ast.unparse(item.context_expr) for item in block.items]
    assert locks.index('self.selection_save_lock') < locks.index('OVERRIDES_UPDATE_LOCK')

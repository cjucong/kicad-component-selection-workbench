"""Destination and stage state machine; writes are scoped and never replayed."""
from copy import deepcopy
import threading
import time
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture(autouse=True)
def no_native_navigation_in_unit_tests(monkeypatch):
    from part_selection_server import PartSelectionApp
    monkeypatch.setattr(PartSelectionApp, 'reopen_and_locate_design', Mock(return_value={'ok':True}))


@pytest.mark.parametrize('closed', [False, True])
def test_legacy_native_commit_closes_only_existing_schematic_then_revalidates(monkeypatch, closed):
    from part_selection_server import PartSelectionApp
    command = SimpleNamespace(DESCRIPTOR=SimpleNamespace(fields_by_name={}))
    monkeypatch.setitem(sys.modules, 'kipy.proto.common.commands.editor_commands_pb2', SimpleNamespace(BeginCommit=command))
    app = PartSelectionApp.__new__(PartSelectionApp)
    app.selection_save_lock = threading.RLock()
    events = []
    app.kicad_url_for_refs = Mock(return_value='verified-project-url')
    app.run_kicad_script = Mock(side_effect=lambda *args: events.append('close') or
        {'ok':True,'message':'SchematicClosed=' + str(int(closed))})
    app.notify_kicad_schematic_changed = Mock(side_effect=lambda *args:events.append('restore') or {'ok':True})
    app.pcb_updates = SimpleNamespace(previews={'token':{'refs':['R1']}},
        apply=Mock(side_effect=lambda *args, **kwargs:events.append('validated apply') or {'ok':True,'saved':False}))
    assert app.apply_design_pcb('token')['ok']
    assert events == ['close','validated apply'] + (['restore'] if closed else [])
    assert '-ExistingOnly' in app.run_kicad_script.call_args.args[1]
    app.pcb_updates.apply.assert_called_once_with('token', True, schematic_close_verified=True)


def test_failed_legacy_editor_close_never_begins_pcb_commit(monkeypatch):
    from part_selection_server import PartSelectionApp
    monkeypatch.setitem(sys.modules, 'kipy.proto.common.commands.editor_commands_pb2',
        SimpleNamespace(BeginCommit=SimpleNamespace(DESCRIPTOR=SimpleNamespace(fields_by_name={}))))
    app = PartSelectionApp.__new__(PartSelectionApp)
    app.selection_save_lock = threading.RLock()
    app.kicad_url_for_refs = Mock(return_value='verified-project-url')
    app.run_kicad_script = Mock(return_value={'ok':False,'message':'save blocked'})
    app.pcb_updates = SimpleNamespace(previews={'token':{'refs':['R1']}},apply=Mock())
    with pytest.raises(ValueError,match='save blocked'):
        app.apply_design_pcb('token')
    app.pcb_updates.apply.assert_not_called()


def test_jump_cannot_reopen_schematic_during_pcb_transaction(monkeypatch):
    import part_selection_server as module
    app = module.PartSelectionApp.__new__(module.PartSelectionApp)
    app.project_root = '.'
    acquired = threading.Event()
    app._run_kicad_script = Mock(side_effect=lambda *args:acquired.set() or {'ok':True})
    with module.SCHEMATIC_SYNC_TRANSACTION_LOCK:
        worker = threading.Thread(target=lambda:app.run_kicad_script('url', []))
        worker.start()
        assert not acquired.wait(.1)
    worker.join(2)
    assert acquired.is_set()


def test_pcb_no_changes_does_not_close_schematic():
    from part_selection_server import PartSelectionApp
    app = PartSelectionApp.__new__(PartSelectionApp)
    app.selection_save_lock = threading.RLock()
    app.run_kicad_script = Mock(side_effect=AssertionError('Unnecessary editor close'))
    app.pcb_updates = SimpleNamespace(previews={'token':{'refs':['R1'],'items':[{'kind':'none'}]}},
        apply=Mock(return_value={'ok':True,'no_changes':True}))
    assert app.apply_design_pcb('token')['no_changes']
    app.run_kicad_script.assert_not_called()


def test_missing_close_verification_marker_never_authorizes_pcb():
    from part_selection_server import PartSelectionApp
    app = PartSelectionApp.__new__(PartSelectionApp)
    app.selection_save_lock = threading.RLock()
    app.kicad_url_for_refs = Mock(return_value='url')
    app.run_kicad_script = Mock(return_value={'ok':True,'message':'unverified'})
    app.pcb_updates = SimpleNamespace(previews={'token':{'refs':['R1']}},apply=Mock())
    with pytest.raises(ValueError,match='核验'):
        app.apply_design_pcb('token')
    app.pcb_updates.apply.assert_not_called()


def test_pcb_stale_preview_after_editor_save_restores_editor_without_writing(monkeypatch):
    from part_selection_server import PartSelectionApp
    monkeypatch.setitem(sys.modules, 'kipy.proto.common.commands.editor_commands_pb2',
        SimpleNamespace(BeginCommit=SimpleNamespace(DESCRIPTOR=SimpleNamespace(fields_by_name={}))))
    app = PartSelectionApp.__new__(PartSelectionApp)
    app.selection_save_lock = threading.RLock()
    app.kicad_url_for_refs = Mock(return_value='verified-project-url')
    app.run_kicad_script = Mock(return_value={'ok':True,'message':'SchematicClosed=1'})
    app.notify_kicad_schematic_changed = Mock(return_value={'ok':True})
    app.pcb_updates = SimpleNamespace(previews={'token':{'refs':['R1']}},
        apply=Mock(side_effect=ValueError('source changed; re-preview')))
    with pytest.raises(ValueError,match='source changed'):
        app.apply_design_pcb('token')
    app.notify_kicad_schematic_changed.assert_called_once_with({'updated_refs':['R1']})


def coordinator():
    from part_selection_design_sync import DesignSync
    snapshot = {'refs':['R1'], 'fields':{'R1':{'value':'100k','footprint':'Local:New'}},
                'records':{'R1':{}}, 'identity':{}, 'mapping':{}, 'versions':{}}
    sources = SimpleNamespace(capture=Mock(side_effect=lambda refs:deepcopy(snapshot)),validate=Mock(return_value=True),
        refresh_after_save=Mock(return_value=True),after_write=Mock(side_effect=lambda *_:deepcopy(snapshot)))
    pcb = SimpleNamespace(preview=Mock(return_value={'ok':True,'preview_id':'pcb-one',
        'blocked':False,'items':[{'ref':'R1','kind':'fields','needs_ack':False,'field_changes':[]}]}),
        apply=Mock(return_value={'ok':True,'saved':False}))
    app = SimpleNamespace(project_root='synthetic',selection_save_lock=threading.RLock(),
        design_sync_fields=Mock(return_value={'R1':{'value':'22k','footprint':'Local:New'}}),
        bulk_sync_bom_state_to_schematic=Mock(return_value={'ok':True,'schematic_verify':{'ok':True}}),
        pcb_updates=pcb, reopen_and_locate_design=Mock(return_value={'ok':True}))
    app.sync_design_pcb = lambda token, progress=None:pcb.apply(token,True)
    return DesignSync(app,sources=sources), app, sources


def await_job(service, job_id):
    deadline = time.monotonic()+3
    while time.monotonic()<deadline:
        state = service.status(job_id)
        if state['done']:
            return state
        time.sleep(.005)
    raise AssertionError('Synthetic sync task did not finish')


def test_design_automatically_continues_pcb_without_replaying_current_schematic():
    service, app, sources = coordinator()
    app.design_sync_fields.return_value = deepcopy(sources.capture(['R1'])['fields'])
    app.sync_design_pcb = Mock(return_value={'ok':True,'saved':True,'editors_preserved':True})
    try:
        preview = service.preview({'refs':['R1'],'destination':'design'})
        state = await_job(service,service.start(preview['preview_id'],{})['job_id'])
        assert state['stage'] == 'complete'
        app.bulk_sync_bom_state_to_schematic.assert_not_called()
        app.sync_design_pcb.assert_called_once()
        assert app.sync_design_pcb.call_args.kwargs['keep_open'] is True
    finally:
        service.close()


def test_design_missing_empty_field_is_not_a_schematic_change():
    service, app, sources = coordinator()
    app.design_sync_fields.return_value = {**deepcopy(sources.capture(['R1'])['fields'])}
    app.design_sync_fields.return_value['R1']['procurement_url'] = ''
    try:
        assert service.preview({'refs':['R1'],'destination':'design'})['schematic_changes'] == []
    finally:
        service.close()


def test_failed_automatic_pcb_apply_retains_actual_diff_for_the_renderer():
    service, app, sources = coordinator()
    app.sync_design_pcb = Mock(side_effect=ValueError('synthetic staged rejection'))
    try:
        preview = service.preview({'refs':['R1'],'destination':'design'})
        state = await_job(service,service.start(preview['preview_id'],{})['job_id'])
        assert state['stage'] == 'partial'
        assert state['phase_results']['schematic']['ok']
        assert state['pcb_preview'] == app.pcb_updates.preview.return_value
        assert state['schematic_changes'] == preview['schematic_changes']
    finally:
        service.close()


@pytest.mark.parametrize('retry_fails',[False,True])
def test_rechecked_pcb_continuation_reports_the_new_diff_not_the_old_one(retry_fails):
    service, app, sources = coordinator()
    app.notify_kicad_schematic_changed=Mock(return_value={'ok':True})
    app.sync_design_pcb = Mock(side_effect=ValueError('first rejection'))
    try:
        preview = service.preview({'refs':['R1'],'destination':'design'})
        state = await_job(service,service.start(preview['preview_id'],{})['job_id'])
        assert state['stage']=='partial'
        original_changes=deepcopy(state['schematic_changes'])
        app.design_sync_fields.return_value=deepcopy(sources.capture(['R1'])['fields'])
        app.pcb_updates.preview.return_value={'ok':True,'preview_id':'pcb-two','blocked':False,
            'items':[{'ref':'R1','kind':'rename','field_changes':[]}]}
        retry=service.preview({'refs':['R1'],'destination':'pcb'})
        app.sync_design_pcb.side_effect=ValueError('second rejection') if retry_fails else None
        app.sync_design_pcb.return_value={'ok':True,'saved':True,'editors_preserved':True}
        continued=service.continue_pcb(state['job_id'],retry['preview_id'],{})
        state=await_job(service,continued['job_id'])
        assert state['stage']==('partial' if retry_fails else 'complete')
        assert state['pcb_preview']==retry['pcb_preview']
        assert state['schematic_changes']==original_changes
        assert app.sync_design_pcb.call_args.args[0]=='pcb-two'
    finally:
        service.close()


@pytest.mark.parametrize('risk', ['ordinary', 'unverified', 'dropped', 'unknown', 'locked'])
def test_pcb_retry_reports_automatic_design_continuation_policy(risk):
    service, app, sources = coordinator()
    app.design_sync_fields.return_value = deepcopy(sources.capture(['R1'])['fields'])
    item = {'ref':'R1', 'kind':'replace', 'needs_ack':True,
            'selection_verified':risk != 'unverified', 'max_error_mm':.2}
    if risk == 'dropped':
        item['dropped_terminals'] = [{'number':'9', 'net_assigned':True}]
    if risk == 'unknown':
        item['kind'] = 'unknown'
    app.pcb_updates.preview.return_value.update(items=[item], blocked=risk in ('unknown', 'locked'))
    try:
        preview = service.preview({'refs':['R1'], 'destination':'pcb'})
        assert preview['pcb_automatic'] is (risk == 'ordinary')
        app.pcb_updates.apply.assert_not_called()
    finally:
        service.close()


def test_automatic_design_recheck_resumes_waiting_job_without_repeating_schematic():
    service, app, sources = coordinator()
    app.sync_design_pcb = Mock(return_value={'ok':True, 'saved':True})
    app.notify_kicad_schematic_changed = Mock(return_value={'ok':True})
    app.pcb_updates.preview.return_value['blocked'] = True
    try:
        first = service.preview({'refs':['R1'], 'destination':'design'})
        waiting = await_job(service, service.start(first['preview_id'], {})['job_id'])
        assert waiting['stage'] == 'awaiting_pcb_confirmation'
        app.design_sync_fields.return_value = deepcopy(sources.capture(['R1'])['fields'])
        app.pcb_updates.preview.return_value['blocked'] = False
        retry = service.preview({'refs':['R1'], 'destination':'design'})
        assert retry['automatic'] and retry['pcb_automatic']
        resumed = await_job(service, service.continue_pcb(waiting['job_id'], retry['preview_id'], {})['job_id'])
        assert resumed['stage'] == 'complete'
        app.bulk_sync_bom_state_to_schematic.assert_called_once()
        app.sync_design_pcb.assert_called_once()
    finally:
        service.close()


def test_automatic_design_recheck_with_new_schematic_edits_cannot_resume_pcb():
    service, app, _ = coordinator()
    app.sync_design_pcb = Mock()
    app.pcb_updates.preview.return_value['blocked'] = True
    try:
        first = service.preview({'refs':['R1'], 'destination':'design'})
        waiting = await_job(service, service.start(first['preview_id'], {})['job_id'])
        retry = service.preview({'refs':['R1'], 'destination':'design'})
        assert retry['schematic_changes']
        with pytest.raises(ValueError):
            service.continue_pcb(waiting['job_id'], retry['preview_id'], {})
        app.bulk_sync_bom_state_to_schematic.assert_called_once()
        app.sync_design_pcb.assert_not_called()
    finally:
        service.close()


def test_fresh_retry_cannot_adopt_unapproved_native_schematic_fields():
    service, app, sources = coordinator()
    app.sync_design_pcb = Mock(return_value={'ok':True, 'saved':True})
    app.pcb_updates.preview.return_value['blocked'] = True
    try:
        first = service.preview({'refs':['R1'], 'destination':'design'})
        waiting = await_job(service, service.start(first['preview_id'], {})['job_id'])
        current = deepcopy(sources.capture(['R1']))
        current['fields']['R1']['value'] = 'unapproved native edit'
        sources.capture.side_effect = lambda refs:deepcopy(current)
        sources.validate.side_effect = lambda snapshot:snapshot['fields'] == current['fields']
        sources.refresh_after_save.side_effect = sources.validate.side_effect
        app.design_sync_fields.return_value = deepcopy(current['fields'])
        app.pcb_updates.preview.return_value['blocked'] = False
        retry = service.preview({'refs':['R1'], 'destination':'pcb'})
        assert retry['pcb_automatic'] and not retry['schematic_changes']
        with pytest.raises(ValueError):
            service.continue_pcb(waiting['job_id'], retry['preview_id'], {})
        app.sync_design_pcb.assert_not_called()
    finally:
        service.close()


def test_automatic_handoff_rejects_intervening_source_changes():
    service, app, sources = coordinator()
    sources.after_write.side_effect = ValueError('同步阶段交接期间选料记录已改变')
    app.sync_design_pcb = Mock()
    try:
        preview = service.preview({'refs':['R1'],'destination':'design'})
        state = await_job(service,service.start(preview['preview_id'],{})['job_id'])
        assert state['stage'] == 'partial'
        assert state['phase_results']['schematic']['ok']
        assert '阶段交接' in state['error']
        app.pcb_updates.preview.assert_not_called()
        app.sync_design_pcb.assert_not_called()
    finally:
        service.close()


@pytest.mark.parametrize('failure', [ValueError('restore failed'), RuntimeError('restore failed'), ValueError(), RuntimeError()])
def test_schematic_restore_failure_preserves_verified_pcb_success(failure):
    service, app, _ = coordinator()
    app.sync_design_pcb = Mock(return_value={'ok':True,'saved':True})
    app.notify_kicad_schematic_changed = Mock(side_effect=failure)
    try:
        preview = service.preview({'refs':['R1'],'destination':'design'})
        job = service.start(preview['preview_id'],{})
        state = await_job(service,job['job_id'])
        assert state['stage'] == 'navigation_failed'
        assert state['phase_results']['pcb']['ok']
        assert state['phase_results']['pcb']['saved']
        assert state['result']['retry_stage'] == 'navigation'
        state = await_job(service,service.retry_navigation(job['job_id'])['job_id'])
        assert state['stage'] == 'complete'
        app.sync_design_pcb.assert_called_once()
        app.bulk_sync_bom_state_to_schematic.assert_called_once()
    finally:
        service.close()


@pytest.mark.parametrize('risk', ['ordinary', 'unverified', 'dropped', 'unknown'])
def test_design_auto_continuation_keeps_exceptional_risk_checks(risk):
    service, app, sources = coordinator()
    app.sync_design_pcb = Mock(return_value={'ok':True,'saved':True})
    app.notify_kicad_schematic_changed = Mock(return_value={'ok':True})
    item = {'ref':'R1','kind':'replace','needs_ack':True,'selection_verified':risk!='unverified',
            'max_error_mm':.05}
    if risk == 'dropped':
        item['dropped_terminals'] = [{'number':'9','net_assigned':True}]
    if risk == 'unknown':
        item['kind'] = 'unknown'
    app.pcb_updates.preview.return_value.update(items=[item], blocked=risk=='unknown')
    try:
        preview = service.preview({'refs':['R1'],'destination':'design'})
        state = await_job(service,service.start(preview['preview_id'],{})['job_id'])
        assert state['stage'] == ('complete' if risk=='ordinary' else 'awaiting_pcb_confirmation')
        app.bulk_sync_bom_state_to_schematic.assert_called_once()
        assert app.bulk_sync_bom_state_to_schematic.call_args.kwargs['notify'] is False
        assert app.sync_design_pcb.call_count == (1 if risk=='ordinary' else 0)
    finally:
        service.close()


def test_both_stages_require_post_schematic_preview_and_never_repeat_schematic():
    service, app, sources = coordinator()
    try:
        preview = service.preview({'refs':['R1'],'destination':'both'})
        app.pcb_updates.preview.assert_not_called()
        job = service.start(preview['preview_id'],{})
        state = await_job(service,job['job_id'])
        assert state['stage']=='awaiting_pcb_confirmation'
        assert state['phase_results']['schematic']['ok'] is True
        assert state['needs_confirmation'] is True
        app.pcb_updates.apply.assert_not_called()
        token = state['pcb_preview']['preview_id']
        resumed = service.continue_pcb(job['job_id'],token,{})
        final = await_job(service,resumed['job_id'])
        assert final['stage']=='complete' and final['phase_results']['pcb']['ok']
        app.bulk_sync_bom_state_to_schematic.assert_called_once()
        app.pcb_updates.apply.assert_called_once_with(token,True)
        assert service.start(preview['preview_id'],{})['job_id'] == job['job_id']
    finally:
        service.close()


def test_schematic_failure_stops_before_pcb():
    service, app, _ = coordinator()
    app.bulk_sync_bom_state_to_schematic.side_effect=ValueError('save blocked')
    try:
        preview=service.preview({'refs':['R1'],'destination':'both'})
        state=await_job(service,service.start(preview['preview_id'],{})['job_id'])
        assert state['stage']=='failed' and state['phase_results']['schematic']['ok'] is False
        app.pcb_updates.preview.assert_not_called()
        app.pcb_updates.apply.assert_not_called()
    finally:service.close()


def test_nested_missing_verification_refs_never_report_schematic_success():
    service, app, _ = coordinator()
    app.bulk_sync_bom_state_to_schematic.return_value = {
        'ok':True, 'schematic_verify':{'ok':True,'missing_refs':['R1']}}
    try:
        preview = service.preview({'refs':['R1'],'destination':'both'})
        state = await_job(service, service.start(preview['preview_id'], {})['job_id'])
        assert state['stage'] == 'failed' and not state['phase_results']['schematic']['ok']
        app.pcb_updates.preview.assert_not_called()
        app.reopen_and_locate_design.assert_not_called()
    finally:
        service.close()


def test_locate_failure_preserves_verified_schematic_and_only_retries_navigation():
    service, app, _ = coordinator()
    app.reopen_and_locate_design.side_effect = ValueError('locate failed')
    try:
        preview = service.preview({'refs':['R1'], 'destination':'schematic'})
        state = await_job(service, service.start(preview['preview_id'], {})['job_id'])
        assert state['phase_results']['schematic']['ok'] is True
        assert state['stage'] == 'navigation_failed'
        assert state['result']['retry_stage'] == 'navigation'
        assert any(step['status'] == 'failed' for step in state['steps'])
        app.reopen_and_locate_design.side_effect = None
        result = service.retry_navigation(state['job_id'])
        final = await_job(service, result['job_id'])
        assert final['stage'] == 'complete'
        app.bulk_sync_bom_state_to_schematic.assert_called_once()
    finally:
        service.close()


def test_already_synced_schematic_does_not_navigate():
    service, app, _ = coordinator()
    app.bulk_sync_bom_state_to_schematic.return_value.update(already_synced=True)
    try:
        preview = service.preview({'refs':['R1'], 'destination':'schematic'})
        state = await_job(service, service.start(preview['preview_id'], {})['job_id'])
        assert state['stage'] == 'complete'
        app.reopen_and_locate_design.assert_not_called()
        assert any(step['status'] == 'skipped' for step in state['steps'])
    finally:
        service.close()


def test_pcb_navigation_failure_is_retryable_without_replaying_apply():
    service, app, _ = coordinator()
    app.design_sync_fields.return_value = {'R1':{'value':'100k','footprint':'Local:New'}}
    app.pcb_updates.apply.return_value.update(saved=True, cycle_complete=True, navigation_error='locate failed')
    try:
        preview = service.preview({'refs':['R1'], 'destination':'pcb'})
        state = await_job(service, service.start(preview['preview_id'], {})['job_id'])
        assert state['stage'] == 'navigation_failed'
        assert state['phase_results']['pcb']['saved']
        final = await_job(service, service.retry_navigation(state['job_id'])['job_id'])
        assert final['stage'] == 'complete'
        assert 'navigation_error' not in final['phase_results']['pcb']
        app.pcb_updates.apply.assert_called_once()
        app.bulk_sync_bom_state_to_schematic.assert_not_called()
    finally:
        service.close()


def test_pcb_only_unsynced_fields_offer_both_without_writes():
    service, app, _ = coordinator()
    try:
        preview=service.preview({'refs':['R1'],'destination':'pcb'})
        assert preview['blocked'] and preview['suggested_destination']=='both'
        with pytest.raises(ValueError):service.start(preview['preview_id'],{})
        app.bulk_sync_bom_state_to_schematic.assert_not_called()
        app.pcb_updates.preview.assert_not_called()
    finally:service.close()


def test_saved_pcb_reconnect_retry_verifies_without_repeating_apply():
    service, app, _ = coordinator()
    app.design_sync_fields.return_value = {'R1':{'value':'100k','footprint':'Local:New'}}
    app.pcb_updates.apply.return_value.update(saved=True, navigation_error='IPC timeout', resume_preview_id='pcb-one')
    app.resume_design_pcb_navigation = Mock(return_value={'ok':True,'saved':True,'reopened':True,'cycle_complete':True})
    try:
        preview = service.preview({'refs':['R1'], 'destination':'pcb'})
        state = await_job(service, service.start(preview['preview_id'], {})['job_id'])
        assert state['stage'] == 'navigation_failed' and state['phase_results']['pcb']['saved']
        final = await_job(service, service.retry_navigation(state['job_id'])['job_id'])
        assert final['stage'] == 'complete' and final['phase_results']['pcb']['reopened']
        app.resume_design_pcb_navigation.assert_called_once()
        assert app.resume_design_pcb_navigation.call_args.args == ('pcb-one',)
        app.pcb_updates.apply.assert_called_once()
        app.bulk_sync_bom_state_to_schematic.assert_not_called()
        app.reopen_and_locate_design.assert_not_called()
    finally:
        service.close()


def test_schematic_diff_separates_recorded_rename_from_unclassified_name_edit():
    service, app, sources = coordinator()
    app.design_sync_fields.return_value = {'R1':{'value':'100k','footprint':'Local:Renamed'}}
    try:
        preview = service.preview({'refs':['R1'],'destination':'schematic'})
        assert preview['schematic_changes'][0]['kind'] == 'unknown'
        snapshot = sources.capture(['R1'])
        snapshot['records']['R1'] = {'footprint_change':{'schema':1,'operation':'rename',
            'before':'Local:New','after':'Local:Renamed'}}
        sources.capture.side_effect = lambda refs:deepcopy(snapshot)
        preview = service.preview({'refs':['R1'],'destination':'schematic'})
        assert preview['schematic_changes'][0]['kind'] == 'rename'
        assert preview['schematic_changes'][0]['requires_pcb_verification'] is True
        app.pcb_updates.preview.assert_not_called()
    finally:service.close()


def test_stale_preview_cannot_start_and_double_click_does_not_replay():
    service, app, sources=coordinator()
    try:
        preview=service.preview({'refs':['R1'],'destination':'schematic'})
        sources.validate.return_value=False
        with pytest.raises(ValueError):service.start(preview['preview_id'],{})
        app.bulk_sync_bom_state_to_schematic.assert_not_called()
        sources.validate.return_value=True
        job=service.start(preview['preview_id'],{})
        assert service.start(preview['preview_id'],{})['job_id']==job['job_id']
        await_job(service,job['job_id'])
        app.bulk_sync_bom_state_to_schematic.assert_called_once()
    finally:service.close()


def test_pcb_disconnect_keeps_successful_stage_and_requires_fresh_preview():
    service, app, _=coordinator()
    app.pcb_updates.preview.side_effect=ValueError('IPC disconnected')
    try:
        preview=service.preview({'refs':['R1'],'destination':'both'})
        job=service.start(preview['preview_id'],{})
        state=await_job(service,job['job_id'])
        assert state['stage']=='partial' and state['phase_results']['schematic']['ok']
        assert state['result']['retry_stage']=='pcb'
        app.pcb_updates.preview.side_effect=None
        app.design_sync_fields.return_value={'R1':{'value':'100k','footprint':'Local:New'}}
        retry=service.preview({'refs':['R1'],'destination':'pcb'})
        resumed=service.continue_pcb(job['job_id'],retry['preview_id'],{})
        assert await_job(service,resumed['job_id'])['stage']=='complete'
        app.bulk_sync_bom_state_to_schematic.assert_called_once()
    finally:service.close()


def test_uncertain_pcb_publish_is_not_automatically_repeated():
    service, app, _=coordinator()
    app.design_sync_fields.return_value={'R1':{'value':'100k','footprint':'Local:New'}}
    app.pcb_updates.apply.side_effect=RuntimeError('publish not verified')
    try:
        preview=service.preview({'refs':['R1'],'destination':'pcb'})
        job=service.start(preview['preview_id'],{})
        state=await_job(service,job['job_id'])
        assert state['stage']=='uncertain'
        assert service.start(preview['preview_id'],{})['job_id']==job['job_id']
        with pytest.raises(ValueError):service.continue_pcb(job['job_id'],preview['preview_id'],{})
        app.pcb_updates.apply.assert_called_once()
    finally:service.close()


def test_unverified_save_retains_applied_stage_and_cannot_replay():
    from part_selection_pcb_session import PcbSaveUnverified
    service, app, _ = coordinator()
    app.design_sync_fields.return_value = {'R1':{'value':'100k','footprint':'Local:New'}}
    app.pcb_updates.apply.side_effect = PcbSaveUnverified('保存结果未确认', '/external/recovery')
    try:
        preview = service.preview({'refs':['R1'],'destination':'pcb'})
        job = service.start(preview['preview_id'], {})
        state = await_job(service, job['job_id'])
        assert state['stage'] == 'uncertain' and state['result']['retry_stage'] is None
        pcb = state['phase_results']['pcb']
        assert pcb['applied'] and pcb['readback_verified'] and pcb['save_uncertain'] and not pcb['saved']
        assert service.start(preview['preview_id'], {})['job_id'] == job['job_id']
        with pytest.raises(ValueError): service.continue_pcb(job['job_id'],preview['preview_id'],{})
        app.pcb_updates.apply.assert_called_once()
    finally:
        service.close()


def test_real_app_desired_fields_are_read_only_and_missing_is_not_empty(scratch_project):
    import json
    from part_selection_server import PartSelectionApp
    from part_selection_overrides import save_overrides
    config = scratch_project / 'settings.json'
    config.write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.','bom_dir':'data'}))
    app = PartSelectionApp(config)
    try:
        save_overrides(app.overrides_path, {'R1':{'field_overrides':{'value':'22k'},
            'accepted_c_code':'C123', 'manual_c_code':'C123', 'decision':'AcceptManual',
            'candidate_model':'TEST', 'review_status':'Confirmed'}})
        records = app.overrides_path.read_bytes()
        from part_selection_overrides import load_overrides
        source = {'refs':['R1'],'fields':{'R1':{'value':'100k','footprint':'Local:R'}},
                  'records':load_overrides(app.overrides_path)}
        app.rows = Mock(side_effect=AssertionError('Preview must not normalize or save rows'))
        result = app.design_sync_fields(source,'__schematic__')
        assert result['R1']['value'] == '22k'
        assert result['R1']['footprint'] == 'Local:R'
        assert result['R1']['lcsc_c_code'] == 'C123'
        assert 'comment' not in result['R1'] and 'description' not in result['R1']
        assert 'lcsc_alt' not in result['R1'] and 'procurement_info' not in result['R1']
        assert app.overrides_path.read_bytes() == records
    finally:
        app.search_jobs.close(); app.selection_jobs.close()
        if hasattr(app,'design_sync'): app.design_sync.close()


@pytest.mark.parametrize('property_name', ['Description', 'Parameters', None])
@pytest.mark.parametrize('review_status', ['Confirmed', 'Pending'])
@pytest.mark.parametrize('destination', ['schematic', 'design'])
def test_changed_code_cached_parameters_sync_and_clear_pending_warning(scratch_project, property_name, review_status, destination):
    import json
    from part_selection_server import PartSelectionApp, stock_param_text
    from part_selection_overrides import load_overrides, save_overrides
    config = scratch_project / 'settings.json'
    config.write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.','bom_dir':'data',
                                 'field_mapping':{'description':property_name or 'Description'}}))
    (scratch_project / 'Design.kicad_pro').write_text('{}')
    schematic = scratch_project / 'Design.kicad_sch'
    schematic.write_text('''(kicad_sch (uuid "root")
      (symbol (uuid "symbol") (property "Reference" "U1")
        (property "Value" "NEW-IC") (property "Comment" "NEW-IC")
        (property "Footprint" "Local:SOIC16")
        (property "Description" "old parameters")
        (instances (project "Design" (path "/root" (reference "U1") (unit 1))))))''')
    text = schematic.read_text()
    schematic.write_text(text.replace('(property "Description" "old parameters")',
        '(property "' + property_name + '" "old parameters")' if property_name else ''))
    app = PartSelectionApp(config)
    try:
        save_overrides(app.overrides_path, {'U1':{'manual_c_code':'C200',
            'accepted_c_code':'C200','decision':'AcceptManual','review_status':review_status,
            'field_overrides':{'description':'old parameters','description_c_code':'C100'}}})
        stock = {'c_code':'C200','model':'NEW-IC','param':'Digital Isolator',
                 'param_detail':'Channels: 4','brand':'TEST','package':'SOIC16','ok':True}
        app.stock_cache_path.parent.mkdir(parents=True, exist_ok=True)
        app.stock_cache_path.write_text(json.dumps({'C200':stock}))
        app.prepare_kicad_for_external_schematic_write = Mock(return_value={'ok':True})
        app.notify_kicad_schematic_changed = Mock(return_value={'ok':True})
        app.verify_schematic_after_notify = Mock(side_effect=lambda fields, **kwargs:
            app.verify_schematic_field_values(fields))
        app.pcb_updates.preview = Mock(return_value={'ok':True,'preview_id':'pcb', 'blocked':False,'items':[]})
        app.sync_design_pcb = Mock(return_value={'ok':True,'no_changes':True})
        records = app.overrides_path.read_bytes()
        preview = app.design_sync.preview({'refs':['U1'],'destination':destination})
        assert app.overrides_path.read_bytes() == records
        assert any(change['name']=='description' and change['after']==stock_param_text(stock)
                   for change in preview['schematic_changes'])
        job = app.design_sync.start(preview['preview_id'], {})
        state = await_job(app.design_sync, job['job_id'])
        assert state['stage'] == 'complete', state
        assert app.read_schematic_fields(scratch_project,['U1'],['description'])['U1']['description'] == stock_param_text(stock)
        rows = app.rows('', '__schematic__', include_stock=False)
        assert len(rows) == 1
        assert rows[0].get('field_edit_status') != 'NeedsSchematicUpdate', rows[0].get('field_diffs')
        assert load_overrides(app.overrides_path)['U1']['review_status'] == review_status
        repeated = app.design_sync.preview({'refs':['U1'],'destination':'schematic'})
        assert repeated['schematic_changes'] == []
        app.prepare_kicad_for_external_schematic_write.reset_mock()
        job = app.design_sync.start(repeated['preview_id'], {})
        assert await_job(app.design_sync, job['job_id'])['stage'] == 'complete'
        app.prepare_kicad_for_external_schematic_write.assert_not_called()
    finally:
        app.design_sync.close(); app.search_jobs.close(); app.selection_jobs.close()


@pytest.mark.parametrize('cache_code,saved_code', [('C200','C100'), ('C999','C100'), (None,'C100'), ('C200','C200')])
def test_sync_parameter_source_and_snapshot_validation(scratch_project, cache_code, saved_code):
    import json
    from part_selection_server import PartSelectionApp, stock_param_text
    from part_selection_overrides import save_overrides
    config = scratch_project / 'settings.json'
    config.write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.','bom_dir':'data'}))
    (scratch_project / 'Design.kicad_pro').write_text('{}')
    schematic = scratch_project / 'Design.kicad_sch'
    schematic.write_text('''(kicad_sch (uuid "root") (symbol (uuid "symbol")
      (property "Reference" "U1") (property "Value" "IC")
      (instances (project "Design" (path "/root" (reference "U1") (unit 1))))))''')
    app = PartSelectionApp(config)
    try:
        save_overrides(app.overrides_path, {'U1':{'manual_c_code':'C200',
            'accepted_c_code':'C200','decision':'AcceptManual','review_status':'Confirmed',
            'field_overrides':{'description':'explicit parameters','description_c_code':saved_code}}})
        stock = {'c_code':cache_code,'model':'IC','param_detail':'Channels: 4','stock':100}
        app.stock_cache_path.parent.mkdir(parents=True, exist_ok=True)
        def write_cache():
            app.stock_cache_path.write_text(json.dumps({'C200':stock} if cache_code else {}))
        write_cache()
        source = app.design_sync.sources.capture(['U1'])
        desired = app.design_sync_fields(source, '__schematic__')['U1']
        expected = 'explicit parameters' if saved_code == 'C200' else (
            stock_param_text(stock) if cache_code == 'C200' else '')
        assert desired.get('description', '') == expected
        if cache_code:
            stock['stock'] = 200
            write_cache()
            assert app.design_sync.sources.validate(source)
            preview = app.design_sync.preview({'refs':['U1'],'destination':'schematic'})
            before = schematic.read_bytes()
            stock['param_detail'] = 'Channels: 8'
            write_cache()
            # The approved desired values stay frozen even after cache refresh.
            assert app.design_sync_fields(source, '__schematic__')['U1'] == desired
            if cache_code == 'C200' and saved_code != 'C200':
                assert not app.design_sync.sources.validate(source)
                with pytest.raises(ValueError, match='改变'):
                    app.design_sync.start(preview['preview_id'], {})
                assert schematic.read_bytes() == before
            else:
                assert app.design_sync.sources.validate(source)
    finally:
        app.design_sync.close(); app.search_jobs.close(); app.selection_jobs.close()


@pytest.mark.parametrize('code', ['c200', 'C 200', ' C200 '])
def test_sync_normalizes_schematic_codes_before_resolving_parameters(scratch_project, code):
    import json
    from part_selection_server import PartSelectionApp, stock_param_text
    config = scratch_project / 'settings.json'
    config.write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.','bom_dir':'data'}))
    app = PartSelectionApp(config)
    try:
        stock = {'c_code':'C200','model':'IC','param_detail':'Channels: 4'}
        app.stock_cache_path.parent.mkdir(parents=True, exist_ok=True)
        app.stock_cache_path.write_text(json.dumps({'C200':stock}))
        source = {'refs':['U1'], 'records':{}, 'fields':{'U1':{'value':'IC','lcsc_c_code':code}}}
        desired = app.design_sync_fields(source, '__schematic__')['U1']
        assert desired['lcsc_c_code'] == 'C200'
        assert desired['description'] == stock_param_text(stock)
    finally:
        app.design_sync.close(); app.search_jobs.close(); app.selection_jobs.close()


def test_prepared_schematic_sync_revalidates_after_editor_save(scratch_project):
    import json
    from part_selection_server import PartSelectionApp
    config = scratch_project / 'settings.json'
    config.write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.','bom_dir':'data'}))
    path = scratch_project / 'test.kicad_sch'
    path.write_text('(kicad_sch (symbol (property "Reference" "R1") (property "Value" "100k")))')
    app = PartSelectionApp(config)
    try:
        app.rows = Mock(side_effect=AssertionError('Do not rebuild rows from a new source'))
        app.prepare_kicad_for_external_schematic_write = Mock(return_value={'ok':True})
        events = []
        with pytest.raises(ValueError, match='改变'):
            app.bulk_sync_bom_state_to_schematic('__schematic__',selected_refs=['R1'],
                prepared_fields={'R1':{'value':'22k'}},notify=False,
                validate_source=Mock(side_effect=[True,False]),
                progress=lambda stage,message:events.append(stage))
        assert '100k' in path.read_text() and '22k' not in path.read_text()
        assert 'bulk_prepare_started' in events
    finally:
        app.search_jobs.close(); app.selection_jobs.close()
        if hasattr(app,'design_sync'): app.design_sync.close()


def test_prepared_sync_writes_only_confirmed_fields_with_real_transaction(scratch_project):
    import json
    from part_selection_server import PartSelectionApp
    config = scratch_project / 'settings.json'
    config.write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.','bom_dir':'data'}))
    path = scratch_project / 'test.kicad_sch'
    path.write_text('(kicad_sch (symbol (property "Reference" "R1") (property "Value" "100k")) '
                    '(symbol (property "Reference" "R2") (property "Value" "10k")))')
    app = PartSelectionApp(config)
    try:
        app.rows = Mock(side_effect=AssertionError('Must use the approved snapshot'))
        result = app.bulk_sync_bom_state_to_schematic('__schematic__',selected_refs=['R1'],
            prepared_fields={'R1':{'value':'22k'}},prepare=False,notify=False,validate_source=lambda:True)
        assert result['updated_refs'] == ['R1'] and result['schematic_verify']['ok']
        assert app.read_schematic_fields(scratch_project,['R1','R2'],['value']) == {
            'R1':{'value':'22k'},'R2':{'value':'10k'}}
        assert 'Comment' not in path.read_text()
    finally:
        app.design_sync.close(); app.search_jobs.close(); app.selection_jobs.close()


def test_design_sync_http_routes_and_idle_shutdown_guard(scratch_project):
    import json
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen
    from part_selection_server import PartSelectionApp, make_handler
    from part_selection_lifecycle import ServiceLifecycle
    config = scratch_project / 'settings.json'
    config.write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.','bom_dir':'data'}))
    app = PartSelectionApp(config)
    app.design_sync.close()
    service, fake, _ = coordinator()
    service.app.settings_gate = app.settings_gate
    app.design_sync = service
    server = ThreadingHTTPServer(('127.0.0.1',0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        def request(route, payload=None):
            req = Request('http://127.0.0.1:'+str(server.server_port)+route,
                data=None if payload is None else json.dumps(payload).encode(),
                headers={'Content-Type':'application/json'})
            with urlopen(req,timeout=3) as response:return json.load(response)
        preview = request('/api/design_sync_preview',{'refs':['R1'],'destination':'both'})
        started, finish = threading.Event(), threading.Event()
        def write(*args,**kwargs):
            started.set(); assert finish.wait(3)
            return {'ok':True,'schematic_verify':{'ok':True}}
        fake.bulk_sync_bom_state_to_schematic.side_effect=write
        job = request('/api/start_design_sync',{'preview_id':preview['preview_id']})
        assert started.wait(3)
        with pytest.raises(ValueError): ServiceLifecycle(app,server,'owner').shutdown('owner')
        finish.set()
        state = await_job(service,job['job_id'])
        assert request('/api/design_sync_job?id='+job['job_id'])['stage']=='awaiting_pcb_confirmation'
        continued = request('/api/continue_design_sync',{'job_id':job['job_id'],
            'preview_id':state['pcb_preview']['preview_id']})
        assert await_job(service,continued['job_id'])['stage']=='complete'
    finally:
        finish.set(); service.close(); server.shutdown(); thread.join(); server.server_close()
        app.search_jobs.close(); app.selection_jobs.close()


def test_post_schematic_confirmation_stays_bound_to_exact_source():
    service, app, sources = coordinator()
    try:
        preview = service.preview({'refs':['R1'],'destination':'both'})
        job = service.start(preview['preview_id'],{})
        state = await_job(service,job['job_id'])
        sources.validate.return_value = False
        with pytest.raises(ValueError,match='失效'):
            service.continue_pcb(job['job_id'],state['pcb_preview']['preview_id'],{})
        app.pcb_updates.apply.assert_not_called()
        app.bulk_sync_bom_state_to_schematic.assert_called_once()
    finally:service.close()


def test_geometry_and_moving_pads_require_separate_consent():
    service, app, _ = coordinator()
    app.design_sync_fields.return_value={'R1':{'value':'100k','footprint':'Local:New'}}
    app.pcb_updates.preview.return_value['items']=[{
        'ref':'R1','kind':'replace','needs_ack':True,'max_error_mm':.2}]
    try:
        preview=service.preview({'refs':['R1'],'destination':'pcb'})
        with pytest.raises(ValueError):service.start(preview['preview_id'],{})
        with pytest.raises(ValueError):service.start(preview['preview_id'],{'geometry':True})
        job=service.start(preview['preview_id'],{'geometry':True,'pad_movement':True})
        assert await_job(service,job['job_id'])['stage']=='complete'
    finally:service.close()


def test_all_row_scope_is_not_limited_to_300_designators():
    from part_selection_design_sync import DesignSync
    refs=['C'+str(n) for n in range(1,1256)]
    assert DesignSync._payload({'refs':refs,'destination':'schematic'})['refs']==refs
    with pytest.raises(ValueError):DesignSync._payload({'refs':[{}],'destination':'pcb'})


def test_async_preflight_reports_progress_while_native_connection_is_waiting():
    service, app, _ = coordinator()
    app.design_sync_fields.return_value={'R1':{'value':'100k','footprint':'Local:New'}}
    entered, release = threading.Event(), threading.Event()
    def preview(*args, progress=None, **kwargs):
        progress('connecting', 'Waiting for verified PCB IPC')
        entered.set()
        assert release.wait(3)
        return {'ok':True,'preview_id':'pcb','blocked':False,'items':[]}
    app.pcb_updates.preview.side_effect=preview
    try:
        job=service.start_preview({'refs':['R1'],'destination':'pcb'})
        assert entered.wait(2)
        state=service.status(job['job_id'])
        assert not state['done'] and state['stage'].endswith('connecting')
        assert state['elapsed_seconds'] >= state['stage_elapsed_seconds'] >= 0
        assert service.busy()
        release.set()
        state=await_job(service,job['job_id'])
        assert state['preview']['pcb_preview']['preview_id']=='pcb'
        assert any(item['stage'].endswith('connecting') for item in state['timings'])
        app.pcb_updates.apply.assert_not_called()
    finally:
        release.set();service.close()


def test_async_preflight_failure_stops_timer_and_never_applies():
    service, app, _ = coordinator()
    app.design_sync_fields.side_effect=ValueError('source unavailable')
    try:
        state=await_job(service, service.start_preview({'refs':['R1'],'destination':'pcb'})['job_id'])
        assert state['done'] and state['ok'] is False and state['stage']=='failed'
        assert state['error']=='source unavailable'
        assert not service.busy()
        time.sleep(.01)
        assert service.status(state['job_id'])['elapsed_seconds']==state['elapsed_seconds']
        app.pcb_updates.apply.assert_not_called()
    finally:service.close()


def test_locked_preflight_reports_successful_read_and_separate_block_without_writes():
    service, app, _ = coordinator()
    app.design_sync_fields.return_value={'R1':{'value':'100k','footprint':'Local:New'}}
    def preview(*args, progress=None, **kwargs):
        progress('reading', 'Reading live PCB')
        progress('read_complete', 'PCB read complete')
        return {'ok':True,'preview_id':'pcb','blocked':True,'reason':'R1 已锁定，请先解锁',
                'items':[{'ref':'R1','kind':'replace','locked':True,'blocked_reason':'R1 已锁定，请先解锁'}]}
    app.pcb_updates.preview.side_effect=preview
    try:
        state=await_job(service, service.start_preview({'refs':['R1'],'destination':'pcb'})['job_id'])
        assert state['ok'] and state['preview']['blocked']
        assert state['steps'][-1]['status']=='blocked'
        assert all(step['status']=='succeeded' for step in state['steps'][:-1])
        with pytest.raises(ValueError, match='R1.*解锁'):
            service.start(state['preview']['preview_id'],{})
        app.pcb_updates.apply.assert_not_called()
        app.bulk_sync_bom_state_to_schematic.assert_not_called()
    finally:service.close()


def test_blocked_geometry_cannot_close_schematic_before_rejection():
    from part_selection_server import PartSelectionApp
    app=PartSelectionApp.__new__(PartSelectionApp)
    app.selection_save_lock=threading.RLock()
    app.pcb_updates=SimpleNamespace(previews={'token':{'items':[{'kind':'replace','blocked_reason':'R1 已锁定'}]}},
        apply=Mock(side_effect=ValueError('R1 已锁定')))
    app.run_kicad_script=Mock()
    with pytest.raises(ValueError,match='锁定'):
        app.apply_design_pcb('token')
    app.run_kicad_script.assert_not_called()


def test_superseded_queued_preflight_is_cancelled_without_reading_design():
    service, app, sources = coordinator()
    entered, release = threading.Event(), threading.Event()
    def fields(*args):
        entered.set(); assert release.wait(3)
        return app.design_sync_fields.return_value
    app.design_sync_fields.side_effect=fields
    try:
        first=service.start_preview({'refs':['R1'],'destination':'both'})
        assert entered.wait(2)
        queued=service.start_preview({'refs':['R1'],'destination':'both'})
        state=service.cancel_preview(queued['job_id'])
        assert state['done'] and state['stage']=='cancelled'
        release.set()
        assert await_job(service, first['job_id'])['ok']
        assert sources.capture.call_count==1
        with pytest.raises(ValueError):service.cancel_preview('not-a-preflight')
    finally:
        release.set();service.close()

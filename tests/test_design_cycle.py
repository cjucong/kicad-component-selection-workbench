"""Save/restart orchestration boundaries; real native counterpart is opt-in."""
from copy import deepcopy
import threading
from types import SimpleNamespace as Obj
from unittest.mock import Mock

import pytest


def app_for(root, monkeypatch):
    from part_selection_server import PartSelectionApp
    import part_selection_pcb_session as native
    monkeypatch.setattr(native, 'decode', lambda value:value)
    monkeypatch.setattr(native, 'persisted_footprint_matches', lambda a,b:a==b)
    app=PartSelectionApp.__new__(PartSelectionApp)
    app.project_root=root
    app.selection_save_lock=threading.RLock()
    expected={'parts':{'R1':{'proto':'complete footprint'}}}
    state={'refs':['R1'],'snapshot':{'before':True},'replacements':['new']}
    session=Obj(snapshot=Mock(return_value=expected), verify=Mock(),
        save_verified=Mock(return_value={'saved':True,'path':str(root/'test.kicad_pcb'),'backup_dir':str(root)}))
    app.pcb_updates=Obj(previews={'token':state},session=session)
    app.apply_design_pcb=Mock(side_effect=lambda *args,**kwargs:deepcopy(state.get('result',{'ok':True,'saved':False})))
    app.prepare_kicad_for_external_schematic_write=Mock(return_value={'ok':True,'message':'ProjectClosed=1'})
    app.reopen_and_locate_design=Mock(return_value={'ok':True})
    return app,state,session


def test_cycle_saves_reopens_locates_and_does_not_replay_completed_cycle(scratch_project,monkeypatch):
    app,state,session=app_for(scratch_project,monkeypatch)
    result=app.sync_design_pcb('token')
    assert result['saved'] and result['cycle_complete'] and result['located_ref']=='R1'
    app.apply_design_pcb.assert_called_once_with('token',progress=None,restore_schematic=False,auto_save=True)
    session.verify.assert_called_once()
    session.save_verified.assert_called_once()
    app.prepare_kicad_for_external_schematic_write.assert_called_once_with(['R1'])
    app.reopen_and_locate_design.assert_called_once()
    assert app.sync_design_pcb('token')==result
    session.save_verified.assert_called_once()


def test_save_failure_never_closes_project_or_claims_saved(scratch_project,monkeypatch):
    app,state,session=app_for(scratch_project,monkeypatch)
    session.save_verified.side_effect=RuntimeError('disk full')
    with pytest.raises(RuntimeError,match='disk full'):app.sync_design_pcb('token')
    app.prepare_kicad_for_external_schematic_write.assert_not_called()
    app.reopen_and_locate_design.assert_not_called()
    assert not state.get('result',{}).get('saved')


def test_close_failure_keeps_saved_evidence_without_claiming_complete(scratch_project,monkeypatch):
    app,state,session=app_for(scratch_project,monkeypatch)
    app.prepare_kicad_for_external_schematic_write.return_value={'ok':False,'message':'blocking dialog'}
    with pytest.raises(RuntimeError,match='blocking dialog'):app.sync_design_pcb('token')
    assert state['result']['saved'] and not state['result'].get('cycle_complete')
    app.reopen_and_locate_design.assert_not_called()


def test_reopen_content_mismatch_stops_before_locating(scratch_project,monkeypatch):
    app,state,session=app_for(scratch_project,monkeypatch)
    session.snapshot.side_effect=[{'parts':{'R1':{'proto':'before'}}},{'parts':{'R1':{'proto':'wrong'}}}]
    with pytest.raises(RuntimeError,match='核验失败'):app.sync_design_pcb('token')
    assert state['result']['saved']
    app.reopen_and_locate_design.assert_not_called()
    import json
    assert json.loads((scratch_project/'before-reopen.json').read_text(encoding='utf-8'))['parts']['R1']['proto']=='before'
    assert json.loads((scratch_project/'after-reopen.json').read_text(encoding='utf-8'))['parts']['R1']['proto']=='wrong'


def test_no_manager_does_not_claim_a_completed_restart(scratch_project,monkeypatch):
    app,state,session=app_for(scratch_project,monkeypatch)
    app.prepare_kicad_for_external_schematic_write.return_value={'ok':True,'message':'ProjectClosed=0'}
    with pytest.raises(RuntimeError,match='未核验当前工程退出'):app.sync_design_pcb('token')
    assert state['result']['saved'] and not state['result'].get('cycle_complete')
    app.reopen_and_locate_design.assert_not_called()

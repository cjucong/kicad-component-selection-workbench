"""Connection decisions never confuse unavailable IPC with a closed editor."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def connection(root, states, processes=None):
    from part_selection_pcb_connection import PcbConnection
    (root / 'Test.kicad_pro').write_text('{}')
    (root / 'Test.kicad_pcb').write_text('(kicad_pcb)')
    discover = Mock(side_effect=states)
    inspect = Mock(return_value={'complete':True,'processes':processes or [], 'ipc_enabled':True})
    launch = Mock()
    time = [0.0]
    clock = SimpleNamespace(monotonic=lambda:time[0], sleep=lambda seconds:time.__setitem__(0,time[0]+seconds))
    return PcbConnection(root, discover=discover, inspect_processes=inspect, launch=launch, clock=clock), launch, inspect


def test_open_editor_is_reused_without_process_scan_or_launch(scratch_project):
    service, launch, inspect = connection(scratch_project, [{'state':'connected','session':'verified'}])
    assert service.ensure(open_if_closed=True)['state'] == 'connected'
    launch.assert_not_called()
    inspect.assert_not_called()


def test_confirmed_closed_opens_once_and_reports_real_stages(scratch_project):
    service, launch, _ = connection(scratch_project, [{'state':'unknown'}, {'state':'unknown'}, {'state':'connected'}])
    progress = Mock()
    assert service.ensure(open_if_closed=True,progress=progress)['state'] == 'connected'
    launch.assert_called_once_with(scratch_project / 'Test.kicad_pcb')
    assert [call.args[0] for call in progress.call_args_list] == ['opening','connecting','connected']


@pytest.mark.parametrize('state', ['busy','ipc_disabled','ambiguous','disconnected'])
def test_known_unavailable_state_never_launches_duplicate(scratch_project, state):
    service, launch, _ = connection(scratch_project, [{'state':state}])
    assert service.ensure(open_if_closed=True)['state'] == state
    launch.assert_not_called()


@pytest.mark.parametrize('process', [
    {'pid':1,'started':'first','files':[]},
    {'pid':2,'started':'first','files':None},
    {'pid':3,'started':'first','files':['TARGET']},
])
def test_process_with_unknown_or_target_document_prevents_open(scratch_project, process):
    if process['files'] == ['TARGET']:
        process['files'] = [str(scratch_project / 'Test.kicad_pcb')]
    service, launch, _ = connection(scratch_project, [{'state':'unknown'}], [process])
    assert service.ensure(open_if_closed=True)['state'] != 'closed'
    launch.assert_not_called()


def test_incomplete_process_scan_is_unknown_not_closed(scratch_project):
    service, launch, inspect = connection(scratch_project, [{'state':'unknown'}])
    inspect.return_value = {'complete':False,'processes':[]}
    assert service.ensure(open_if_closed=True)['state'] == 'unknown'
    launch.assert_not_called()


def test_pid_reuse_is_reinspected_and_ipc_disabled_diagnosed(scratch_project):
    service, launch, inspect = connection(scratch_project, [{'state':'unknown'}, {'state':'unknown'}])
    inspect.return_value = {'complete':True,'ipc_enabled':False,'processes':[
        {'pid':42,'started':'old','files':[str(scratch_project / 'Other.kicad_pcb')]}]}
    assert service.inspect()['state'] == 'closed'
    inspect.return_value['processes'] = [{'pid':42,'started':'new','files':[str(scratch_project / 'Test.kicad_pcb')]}]
    assert service.ensure(open_if_closed=True)['state'] == 'ipc_disabled'
    launch.assert_not_called()


def test_timeout_retains_launched_editor_and_never_relaunches(scratch_project):
    service, launch, _ = connection(scratch_project, [])
    service.discover.side_effect = None
    service.discover.return_value = {'state':'unknown'}
    assert service.ensure(open_if_closed=True)['state'] == 'disconnected'
    assert service.ensure(open_if_closed=True)['state'] == 'disconnected'
    launch.assert_called_once()


def test_multiple_project_boards_cannot_choose_by_filename_guess(scratch_project):
    service, launch, _ = connection(scratch_project, [{'state':'unknown'}])
    (scratch_project / 'Other.kicad_pcb').write_text('(kicad_pcb)')
    assert service.ensure(open_if_closed=True)['state'] == 'ambiguous'
    launch.assert_not_called()

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


def test_recently_verified_editor_recovers_transient_ipc_without_reopening(scratch_project):
    service, launch, _ = connection(scratch_project, [
        {'state':'connected'}, {'state':'busy'}, {'state':'unknown'}, {'state':'connected'}])
    assert service.ensure(open_if_closed=True)['state']=='connected'
    progress=Mock()
    assert service.ensure(open_if_closed=True,progress=progress)['state']=='connected'
    launch.assert_not_called()
    assert any(call.args[0]=='reconnecting' for call in progress.call_args_list)


def test_transient_recovery_never_ignores_ambiguous_identity(scratch_project):
    service, launch, _ = connection(scratch_project, [
        {'state':'connected'}, {'state':'busy'}, {'state':'ambiguous'}])
    service.ensure(open_if_closed=True)
    assert service.ensure(open_if_closed=True)['state']=='ambiguous'
    launch.assert_not_called()


def test_transient_recovery_is_bounded_and_never_reopens(scratch_project):
    service, launch, _ = connection(scratch_project, [{'state':'connected'}])
    service.ensure(open_if_closed=True)
    service.discover.side_effect=None
    service.discover.return_value={'state':'busy'}
    assert service.ensure(open_if_closed=True)['state']=='busy'
    assert service.clock.monotonic()<=5.5
    launch.assert_not_called()


def test_recovery_propagates_remaining_budget_to_slow_discovery(scratch_project):
    service, launch, _ = connection(scratch_project, [{'state':'connected'}])
    service.ensure(open_if_closed=True)
    budgets=[]
    def slow_discover(*, timeout_seconds=None):
        if timeout_seconds is not None:
            budgets.append(timeout_seconds)
            service.clock.sleep(min(1.8, timeout_seconds))
        return {'state':'busy'}
    service.discover=slow_discover
    assert service.ensure(open_if_closed=True)['state']=='busy'
    assert service.clock.monotonic()<=5
    assert budgets and budgets[-1]<budgets[0]
    launch.assert_not_called()


def test_stale_connection_does_not_trigger_transient_recovery(scratch_project):
    service, launch, _ = connection(scratch_project, [{'state':'connected'}, {'state':'busy'}])
    service.ensure(open_if_closed=True)
    service.clock.sleep(10)
    assert service.ensure(open_if_closed=True)['state']=='busy'
    assert service.discover.call_count==2
    launch.assert_not_called()


def test_recovery_stops_at_unverified_identity(scratch_project):
    service, launch, _ = connection(scratch_project, [
        {'state':'connected'}, {'state':'busy'}, {'state':'unknown','identity_unverified':True}])
    service.ensure(open_if_closed=True)
    assert service.ensure(open_if_closed=True)['identity_unverified']
    assert service.discover.call_count==3
    launch.assert_not_called()


def test_unresolved_pcb_lock_prevents_automatic_open(scratch_project):
    service, launch, _ = connection(scratch_project, [{'state':'unknown'}])
    (scratch_project/'~Test.kicad_pcb.lck').write_text('{"username":"other","hostname":"remote"}')
    assert service.ensure(open_if_closed=True)['state'] == 'unknown'
    launch.assert_not_called()


def test_lock_appearing_after_inventory_prevents_launch(scratch_project):
    service, launch, _ = connection(scratch_project, [])
    def classified():
        (scratch_project/'~Test.kicad_pcb.lck').write_text('locked')
        return {'state':'closed'}
    service.inspect = classified
    assert service.ensure(open_if_closed=True)['state'] == 'unknown'
    launch.assert_not_called()


def test_confirmed_closed_opens_once_and_reports_real_stages(scratch_project):
    service, launch, _ = connection(scratch_project, [{'state':'unknown'}, {'state':'unknown'}, {'state':'connected'}])
    progress = Mock()
    assert service.ensure(open_if_closed=True,progress=progress)['state'] == 'connected'
    launch.assert_called_once_with(scratch_project / 'Test.kicad_pcb')
    stages = [call.args[0] for call in progress.call_args_list]
    assert stages[0]=='opening' and stages[-1]=='connected'
    assert set(stages[1:-1])=={'connecting'}


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


def test_exited_owned_editor_can_be_opened_again_after_verified_absence(scratch_project):
    service, launch, _ = connection(scratch_project, [
        {'state':'unknown'}, {'state':'connected'}, {'state':'unknown'}, {'state':'connected'}])
    handle = Mock()
    handle.poll.return_value = None
    launch.return_value = handle
    assert service.ensure(open_if_closed=True)['state'] == 'connected'
    handle.poll.return_value = 0
    assert service.ensure(open_if_closed=True)['state'] == 'connected'
    assert launch.call_count == 2


def test_manager_hosted_pcb_is_included_in_process_inventory(monkeypatch):
    import part_selection_pcb_connection as module
    monkeypatch.setattr(module, 'process_arguments', lambda command: ['kicad.exe', 'E:/Test/Test.kicad_pro'])
    monkeypatch.setattr(module.subprocess, 'run', lambda command, **kwargs: SimpleNamespace(returncode=0,
        stdout='[{"Name":"kicad.exe","ProcessId":7,"CreationDate":"now","CommandLine":"manager"}]'))
    monkeypatch.setattr(module, 'manager_has_pcb', lambda pid: True, raising=False)
    scan = module.inspect_processes()
    assert scan['processes'][0]['files'] == [str(__import__('pathlib').Path('E:/Test/Test.kicad_pcb').resolve())]


def test_launch_uses_verified_manager_command_not_standalone(scratch_project, monkeypatch):
    import part_selection_pcb_connection as module
    import part_selection_footprint_geometry as geometry
    python = scratch_project / 'python.exe'
    (scratch_project / 'kicad.exe').touch()
    monkeypatch.setattr(geometry, '_find_kicad_python', lambda: python)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='PcbOpenRequested=1', stderr=''))
    monkeypatch.setattr(module.subprocess, 'run', run)
    direct = Mock(side_effect=AssertionError('must not spawn standalone editor'))
    monkeypatch.setattr(module.subprocess, 'Popen', direct)
    module.launch_editor(scratch_project / 'Test.kicad_pcb')
    command = run.call_args.args[0]
    assert '-OpenPcb' in command
    assert '-ProjectRoot' in command and str(scratch_project) in command
    assert '-KicadExe' in command and str(scratch_project / 'kicad.exe') in command
    direct.assert_not_called()


def test_startup_wait_only_polls_ipc_not_cim_inventory(scratch_project):
    service, _, inventory = connection(scratch_project, [
        {'state':'unknown'}, {'state':'unknown'}, {'state':'unknown'}, {'state':'connected'}])
    assert service.ensure(open_if_closed=True)['state'] == 'connected'
    assert inventory.call_count == 1


def test_manager_launcher_without_handle_can_reopen_after_confirmed_close(scratch_project):
    service, launch, _ = connection(scratch_project, [
        {'state':'unknown'}, {'state':'connected'}, {'state':'unknown'}, {'state':'connected'}])
    launch.return_value = None
    assert service.ensure(open_if_closed=True)['state'] == 'connected'
    assert service.ensure(open_if_closed=True)['state'] == 'connected'
    assert launch.call_count == 2


def test_late_connection_after_timeout_clears_pending_open(scratch_project):
    service, launch, _ = connection(scratch_project, [])
    launch.return_value = None
    service.discover.side_effect = None
    service.discover.return_value = {'state':'unknown'}
    assert service.ensure(open_if_closed=True)['state'] == 'disconnected'
    service.discover.return_value = {'state':'connected'}
    assert service.ensure(open_if_closed=True)['state'] == 'connected'
    service.discover.side_effect = [{'state':'unknown'}, {'state':'connected'}]
    assert service.ensure(open_if_closed=True)['state'] == 'connected'
    assert launch.call_count == 2


def test_transient_discovery_exception_during_startup_is_retried(scratch_project):
    service, launch, inventory = connection(scratch_project, [
        {'state':'unknown'}, OSError('IPC not ready'), {'state':'connected'}])
    assert service.ensure(open_if_closed=True)['state'] == 'connected'
    launch.assert_called_once()
    assert inventory.call_count == 1


@pytest.mark.parametrize('result', [
    SimpleNamespace(returncode=1, stdout='', stderr='manager busy'),
    SimpleNamespace(returncode=0, stdout='', stderr=''),
])
def test_manager_launch_requires_success_marker(scratch_project, monkeypatch, result):
    import part_selection_pcb_connection as module
    import part_selection_footprint_geometry as geometry
    (scratch_project / 'kicad.exe').touch()
    monkeypatch.setattr(geometry, '_find_kicad_python', lambda: scratch_project / 'python.exe')
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: result)
    with pytest.raises(ValueError):
        module.launch_editor(scratch_project / 'Test.kicad_pcb')


def test_manager_timeout_can_reopen_after_fresh_verified_absence(scratch_project):
    service, launch, _ = connection(scratch_project, [])
    launch.return_value = None
    service.discover.side_effect = None
    service.discover.return_value = {'state':'unknown'}
    assert service.ensure(open_if_closed=True)['state']=='disconnected'
    service.discover.side_effect = [{'state':'unknown'}, {'state':'connected'}]
    assert service.ensure(open_if_closed=True)['state']=='connected'
    assert launch.call_count == 2

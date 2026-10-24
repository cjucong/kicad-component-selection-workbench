from pathlib import Path
from types import SimpleNamespace as Obj
import sys

import pytest

from part_selection_pcb_session import select_document
import part_selection_pcb_session as sessions


def doc(root, filename='test.kicad_pcb'):
    return Obj(project=Obj(path=str(root)), board_filename=filename)


def test_document_requires_exact_project_identity(scratch_project):
    root = scratch_project
    selected = doc(root)
    assert select_document([doc(root / 'other'), selected], root) is selected
    with pytest.raises(ValueError):
        select_document([doc(root / 'other')], root)
    with pytest.raises(ValueError):
        select_document([selected, doc(root, 'other.kicad_pcb')], root)


def test_verified_save_requires_unchanged_live_board_and_matching_saved_bytes(monkeypatch, scratch_project):
    import threading
    from unittest.mock import Mock
    board_path = scratch_project / 'test.kicad_pcb'
    original = '(kicad_pcb (version 20241229) (general))'
    expected = {'document_path':str(board_path), 'text':original, 'parts':{'R1':{}}, 'revision':'v1'}
    session = sessions.PcbSession(scratch_project)
    session.lock = threading.RLock()
    session.snapshot = Mock(return_value=expected)
    board = Obj(save=Mock(side_effect=lambda:board_path.write_text(original)), get_as_string=lambda:original)
    session._board = Mock(return_value=(None,board,None))
    board_path.write_text('old disk')
    assert session.save_verified(expected, scratch_project.parent / 'save-backup')['saved']
    board.save.assert_called_once()
    assert (scratch_project.parent / 'save-backup' / 'disk-before.kicad_pcb').read_text() == 'old disk'
    session.snapshot.return_value={**expected,'revision':'newer'}
    with pytest.raises(ValueError, match='改变'):
        session.save_verified(expected, scratch_project.parent / 'unused-save')
    board.save.assert_called_once()


def test_absolute_filename_cannot_escape_document_project(scratch_project):
    with pytest.raises(ValueError):
        select_document([doc(scratch_project, str(scratch_project.parent / 'other.kicad_pcb'))], scratch_project)


def test_saved_board_comparison_ignores_only_standalone_footprint_headers():
    disk = '(kicad_pcb (version 20260206) (footprint "Lib:R" (at 1 2 90) (pad "1" (net "VCC"))))'
    live = disk.replace('(footprint "Lib:R"', '(footprint "Lib:R" (version 20260206) (generator "pcbnew") (generator_version "10.0")')
    assert sessions.saved_board_data(live) == sessions.saved_board_data(disk)
    for before, after in [('1 2 90', '2 2 90'), ('1 2 90', '1 2 0'), ('VCC', 'GND'),
                          ('Lib:R', 'Lib:Other'), ('20260206', '20241229')]:
        assert sessions.saved_board_data(disk.replace(before, after)) != sessions.saved_board_data(live)
    assert sessions.saved_board_data(disk.replace('(pad', '(property "Value" "wrong") (pad')) != sessions.saved_board_data(live)


def fake_api(monkeypatch, endpoints):
    calls = []
    def client(**kwargs):
        calls.append(kwargs)
        docs, token = endpoints[kwargs['socket_path']]
        if isinstance(docs, Exception):
            raise docs
        return Obj(_client=Obj(_kicad_token=token), get_open_documents=lambda kind: docs)
    monkeypatch.setitem(sys.modules, 'kipy', Obj(KiCad=client))
    monkeypatch.setitem(sys.modules, 'kipy.board', Obj(Board=lambda client, document: document))
    monkeypatch.setitem(sys.modules, 'kipy.proto.common.types', Obj(DocumentType=Obj(DOCTYPE_PCB=1)))
    monkeypatch.setattr(sessions, 'pcb_socket_candidates', lambda inherited: list(endpoints), raising=False)
    return calls


def test_schematic_entry_resolves_pcb_only_when_requested(monkeypatch, scratch_project):
    target = doc(scratch_project)
    calls = fake_api(monkeypatch, {'ipc://schematic': ([], 'sch'), 'ipc://pcb': ([target], 'pcb')})
    session = sessions.PcbSession(scratch_project, socket='ipc://schematic')
    assert not calls
    _, board, _ = session._board()
    assert board is target
    assert session.socket == 'ipc://pcb'
    assert all(call['kicad_token'] == '' for call in calls)


def test_pcb_opened_after_workbench_start_can_be_discovered(monkeypatch, scratch_project):
    endpoints = {'ipc://schematic': ([], 'sch')}
    fake_api(monkeypatch, endpoints)
    session = sessions.PcbSession(scratch_project, socket='ipc://schematic')
    with pytest.raises(ValueError, match='打开.*PCB'):
        session._board()
    endpoints['ipc://pcb'] = ([doc(scratch_project)], 'pcb')
    assert session._board()[1].project.path == str(scratch_project)


def test_no_inherited_socket_can_find_exact_project(monkeypatch, scratch_project):
    monkeypatch.delenv('KICAD_API_SOCKET', raising=False)
    target = doc(scratch_project)
    fake_api(monkeypatch, {'ipc://other': ([doc(scratch_project / 'other')], 'other'),
                           'ipc://pcb': ([target], 'pcb')})
    assert sessions.PcbSession(scratch_project)._board()[1] is target


@pytest.mark.parametrize('same_endpoint', [False, True])
def test_multiple_matching_boards_are_rejected(monkeypatch, scratch_project, same_endpoint):
    endpoints = {'ipc://one': ([doc(scratch_project)], 'one')}
    if same_endpoint:
        endpoints['ipc://one'][0].append(doc(scratch_project, 'another.kicad_pcb'))
    else:
        endpoints['ipc://two'] = ([doc(scratch_project)], 'two')
    fake_api(monkeypatch, endpoints)
    with pytest.raises(ValueError, match='多个|唯一'):
        sessions.PcbSession(scratch_project)._board()


def test_unresponsive_or_wrong_project_never_selected(monkeypatch, scratch_project):
    fake_api(monkeypatch, {'ipc://dead': (OSError('closed'), ''),
                           'ipc://wrong': ([doc(scratch_project / 'other')], 'other')})
    with pytest.raises(ValueError, match='打开.*PCB'):
        sessions.PcbSession(scratch_project)._board()


def test_discovery_requires_server_identity(monkeypatch, scratch_project):
    fake_api(monkeypatch, {'ipc://pcb': ([doc(scratch_project)], '')})
    with pytest.raises(ValueError, match='身份|核验'):
        sessions.PcbSession(scratch_project)._board()


def test_aliases_for_same_server_and_document_are_not_ambiguous(monkeypatch, scratch_project):
    target = doc(scratch_project)
    fake_api(monkeypatch, {'ipc://one': ([target], 'same'), 'ipc://alias': ([target], 'same')})
    assert sessions.PcbSession(scratch_project)._board()[1] is target


def test_empty_project_metadata_is_not_current_directory(monkeypatch, scratch_project):
    with monkeypatch.context() as context:
        context.chdir(scratch_project)
        with pytest.raises(ValueError):
            select_document([doc('')], scratch_project)


def test_windows_socket_discovery_only_accepts_kicad_api_pipes(monkeypatch):
    class Pipes:
        def __enter__(self):
            return iter(Obj(name=name) for name in [r'C:\Temp\kicad\api.sock',
                r'C:\Temp\kicad\api-123.sock', r'C:\Temp\kicad\other.sock',
                r'C:\Temp\unrelated\api.sock'])
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(sessions.os, 'scandir', lambda path: Pipes())
    paths = sessions.pcb_socket_candidates('tcp://untrusted')
    assert r'ipc://C:\Temp\kicad\api.sock' in paths
    assert r'ipc://C:\Temp\kicad\api-123.sock' in paths
    assert not any('untrusted' in path or 'other.sock' in path or 'unrelated' in path for path in paths)


def test_discovery_keeps_errors_and_connection_state(monkeypatch, scratch_project):
    fake_api(monkeypatch, {'ipc://busy':(TimeoutError('editor busy'), '')})
    result = sessions.PcbSession(scratch_project).discover()
    assert result['state'] == 'unknown'
    assert result['errors'][0]['socket'] == 'ipc://busy'
    assert 'busy' in result['errors'][0]['reason']


def test_injected_connection_is_used_and_rechecks_each_board_access(monkeypatch, scratch_project):
    from unittest.mock import Mock
    target = doc(scratch_project)
    fake_api(monkeypatch, {})
    transport = Obj(send_timeout=600, recv_timeout=600)
    client = Obj(_client=Obj(_kicad_token='verified', _conn=transport))
    connection = Obj(ensure=Mock(side_effect=[
        {'state':'connected','socket':'ipc://target','client':client,'document':target},
        {'state':'disconnected','reason':'session exited'}]))
    session = sessions.PcbSession(scratch_project, connection=connection)
    assert session._board()[1] is target
    assert transport.send_timeout == transport.recv_timeout == 4000
    with pytest.raises(ValueError, match='session exited'):
        session._board()
    assert connection.ensure.call_count == 2


def test_explicit_native_busy_response_never_becomes_closed(monkeypatch, scratch_project):
    class NativeBusy(Exception):
        code = 7
    fake_api(monkeypatch, {'ipc://busy':(NativeBusy('native modal'), '')})
    monkeypatch.setitem(sys.modules, 'kipy.proto.common.envelope_pb2', Obj(AS_BUSY=7))
    assert sessions.PcbSession(scratch_project).discover()['state'] == 'busy'

from copy import deepcopy
import ctypes
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import part_selection_pcb_library as reader
from part_selection_pcb_library import cache_key


def test_library_cache_separates_two_nicknames_for_one_directory():
    assert cache_key('same.kicad_mod', 'hash', False, 'First:1206') != cache_key('same.kicad_mod', 'hash', False, 'Second:1206')


@pytest.fixture
def win32(monkeypatch):
    user, kernel = Mock(), Mock()
    desktop_handle = 0x123456789
    user.CreateDesktopW.return_value = desktop_handle
    user.CloseDesktop.return_value = 1
    kernel.CloseHandle.return_value = 1
    kernel.WaitForSingleObject.return_value = 0x102
    kernel.CreateJobObjectW.return_value = 0x456789123
    kernel.SetInformationJobObject.return_value = 1
    kernel.AssignProcessToJobObject.return_value = 1
    kernel.ResumeThread.return_value = 1
    kernel.TerminateJobObject.return_value = 1
    kernel.QueryInformationJobObject.return_value = 1

    def create_process(exe, command, process_security, thread_security, inherit,
                       flags, env, cwd, startup, info):
        startup = startup._obj
        assert startup.cb == ctypes.sizeof(reader._StartupInfo)
        assert startup.lpDesktop == user.CreateDesktopW.call_args.args[0]
        assert startup.lpDesktop.startswith('kw-pcb-reader-')
        assert inherit is False
        assert flags & 0x400  # CREATE_UNICODE_ENVIRONMENT.
        info._obj.hProcess, info._obj.hThread = 0x234567891, 0x345678912
        info._obj.dwProcessId, info._obj.dwThreadId = 12345, 54321
        return 1

    kernel.CreateProcessW.side_effect = create_process
    monkeypatch.setattr(reader.ctypes, 'WinDLL',
                        lambda name, **kwargs: {'user32': user, 'kernel32': kernel}[name])
    return SimpleNamespace(user=user, kernel=kernel, handle=desktop_handle)


@pytest.fixture
def library_read(monkeypatch, scratch_project, win32):
    import part_selection_footprints as footprints
    import part_selection_footprint_geometry as geometry
    import part_selection_pcb_session as session

    source = scratch_project / 'Local.pretty' / 'Target.kicad_mod'
    source.parent.mkdir()
    source.write_text('(footprint "Target" (layer "F.Cu"))', encoding='utf-8')
    settings = scratch_project / 'settings' / 'kicad' / '10.0'
    settings.mkdir(parents=True)
    (settings / 'kicad_common.json').write_text('{}', encoding='utf-8')
    monkeypatch.setenv('APPDATA', str(settings.parents[1]))
    monkeypatch.setattr(reader.os, 'environ', {'APPDATA': str(settings.parents[1]),
                        'KICAD_API_SOCKET': 'production-socket', 'KICAD_API_TOKEN': 'production-token'})
    monkeypatch.setattr(footprints.FootprintChoices, 'libraries', lambda self: {'Local': source.parent})
    monkeypatch.setattr(geometry, '_find_kicad_python', lambda: Path('C:/KiCad/bin/python.exe'))
    monkeypatch.setattr(reader.subprocess, 'run', Mock())
    monkeypatch.setattr(reader.subprocess, 'Popen', Mock(side_effect=AssertionError(
        'subprocess.Popen does not assign a private Windows desktop')))
    close = Mock()
    monkeypatch.setattr(reader, '_close_reader', close)
    client = Mock(_client=object())
    client.get_open_documents.return_value = [object()]
    connect = Mock(return_value=client)
    monkeypatch.setitem(sys.modules, 'kipy', SimpleNamespace(KiCad=connect))
    board = Mock()
    board.get_footprints.return_value = [object()]
    monkeypatch.setitem(sys.modules, 'kipy.board', SimpleNamespace(Board=Mock(return_value=board)))
    monkeypatch.setitem(sys.modules, 'kipy.proto.common.types',
                        SimpleNamespace(DocumentType=SimpleNamespace(DOCTYPE_PCB=1)))
    monkeypatch.setattr(session, 'select_document', lambda documents, root: documents[0])
    definition = {'footprint': 'Local:Target', 'back': False, 'pads': [{'number': '1'}],
                  'proto': 'complete-native-protobuf'}
    monkeypatch.setattr(session, 'describe', lambda fp: deepcopy(definition))
    monkeypatch.setattr(reader.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(reader, '_CACHE', {})
    return SimpleNamespace(root=scratch_project, source=source, close=close, win32=win32,
                           board=board, connect=connect, client=client, definition=definition)


def test_read_library_launches_editor_on_private_desktop(library_read):
    case = library_read
    result = reader.read_library(case.root, 'Local:Target')
    assert result['proto'] == case.definition['proto']
    api = case.win32
    api.kernel.CreateProcessW.assert_called_once()
    call = api.kernel.CreateProcessW.call_args.args
    assert call[0].endswith('pcbnew.exe') and 'LibraryRead.kicad_pcb' in call[1].value
    assert Path(call[7]).name == 'project'
    environment = ''.join(call[6])
    assert 'KICAD_API_SOCKET' not in environment and 'KICAD_API_TOKEN' not in environment
    assert 'KICAD_CONFIG_HOME=' in environment and 'TEMP=' in environment
    assert call[8]._obj.lpDesktop != 'Default'
    assert call[5] & 0x4  # No reader/helper code runs before Job assignment.
    api.kernel.AssignProcessToJobObject.assert_called_once_with(0x456789123, 0x234567891)
    api.kernel.ResumeThread.assert_called_once_with(0x345678912)
    assert api.user.CreateDesktopW.restype is reader.wintypes.HANDLE
    api.user.CloseDesktop.assert_called_once_with(api.handle)
    assert [call.args[0] for call in api.kernel.CloseHandle.call_args_list] == [
        0x345678912, 0x234567891, 0x456789123]
    process, desktop = case.close.call_args.args
    assert process.pid == 12345 and desktop.handle is None


@pytest.mark.parametrize('failure', ['desktop', 'process'])
def test_read_library_fails_closed_when_isolation_or_launch_fails(library_read, failure):
    case = library_read
    if failure == 'desktop':
        case.win32.user.CreateDesktopW.return_value = None
    else:
        case.win32.kernel.CreateProcessW.side_effect = None
        case.win32.kernel.CreateProcessW.return_value = 0
    with pytest.raises(OSError):
        reader.read_library(case.root, 'Local:Target')
    assert not reader._CACHE
    case.connect.assert_not_called()
    case.close.assert_not_called()
    if failure == 'desktop':
        case.win32.kernel.CreateProcessW.assert_not_called()
        case.win32.user.CloseDesktop.assert_not_called()
    else:
        case.win32.user.CloseDesktop.assert_called_once_with(case.win32.handle)


@pytest.mark.parametrize('failure', ['connection', 'document', 'geometry', 'exit'])
def test_read_library_cleans_up_failures_without_caching(library_read, monkeypatch, failure):
    import part_selection_pcb_session as session
    case = library_read
    if failure == 'connection':
        case.connect.side_effect = RuntimeError('connection failed')
    elif failure == 'document':
        monkeypatch.setattr(session, 'select_document', Mock(side_effect=ValueError('wrong project')))
    elif failure == 'geometry':
        case.board.get_footprints.return_value = [object(), object()]
    else:
        case.win32.kernel.WaitForSingleObject.return_value = 0
        case.win32.kernel.GetExitCodeProcess.return_value = 1
    with pytest.raises((RuntimeError, ValueError)):
        reader.read_library(case.root, 'Local:Target')
    case.close.assert_called_once()
    case.win32.user.CloseDesktop.assert_called_once_with(case.win32.handle)
    assert not reader._CACHE


def test_read_library_cleanup_failure_does_not_cache(library_read):
    case = library_read
    case.close.side_effect = TimeoutError('reader did not close')
    with pytest.raises(TimeoutError):
        reader.read_library(case.root, 'Local:Target')
    case.win32.user.CloseDesktop.assert_called_once_with(case.win32.handle)
    assert not reader._CACHE


def test_library_cache_rehashes_sources_and_returns_independent_complete_results(library_read):
    case = library_read
    first = reader.read_library(case.root, 'Local:Target')
    first['pads'][0]['number'], first['proto'] = 'bad', 'bad'
    second = reader.read_library(case.root, 'Local:Target')
    assert second['proto'] == case.definition['proto'] and second['pads'][0]['number'] == '1'
    assert case.win32.kernel.CreateProcessW.call_count == 1
    stat = case.source.stat()
    case.source.write_text('(footprint "Edited" (layer "F.Cu"))', encoding='utf-8')
    os.utime(case.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    third = reader.read_library(case.root, 'Local:Target')
    assert third['source_hash'] != second['source_hash']
    assert third['source_hash'] == hashlib.sha256(case.source.read_bytes()).hexdigest()
    assert case.win32.kernel.CreateProcessW.call_count == 2


def test_library_cache_separates_sides_and_aliases(library_read, monkeypatch):
    from part_selection_footprints import FootprintChoices
    case = library_read
    monkeypatch.setattr(FootprintChoices, 'libraries', lambda self:
                        {'Local': case.source.parent, 'Alias': case.source.parent})
    for lib_id, back in [('Local:Target', False), ('Local:Target', True), ('Alias:Target', False)]:
        reader.read_library(case.root, lib_id, back=back)
    assert case.win32.kernel.CreateProcessW.call_count == 3


def test_library_change_during_read_is_not_cached(library_read):
    case = library_read
    case.close.side_effect = lambda *args: case.source.write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='读取期间目标库已改变'):
        reader.read_library(case.root, 'Local:Target')
    assert not reader._CACHE


def test_close_reader_enumerates_only_private_desktop_and_owned_windows(win32):
    user = win32.user
    process = Mock(pid=12345)
    process.poll.side_effect = [None, 0]
    desktop = SimpleNamespace(user=user, handle=win32.handle)

    def enumerate_windows(handle, callback, unused):
        assert handle == desktop.handle
        for hwnd in [10, 20, 30]:
            callback(hwnd, 0)
        return 1

    def owner(hwnd, pointer):
        pointer._obj.value = 9876 if hwnd == 20 else process.pid
        return 4321

    def title(hwnd, buffer, length):
        buffer.value = 'LibraryRead - PCB Editor' if hwnd != 30 else 'Unrelated'
        return len(buffer.value)

    user.EnumDesktopWindows.side_effect = enumerate_windows
    user.GetWindowThreadProcessId.side_effect = owner
    user.GetWindowTextW.side_effect = title
    reader._close_reader(process, desktop)
    user.EnumWindows.assert_not_called()
    user.PostMessageW.assert_called_once_with(10, 0x0010, 0, 0)
    process.terminate.assert_not_called()
    process.kill.assert_not_called()


def test_close_reader_waits_for_starting_window_and_retries_close(win32):
    process = Mock(pid=12345)
    process.poll.side_effect = [None, None, 0]
    process.wait.side_effect = [reader.subprocess.TimeoutExpired('reader', .25), 0]
    win32.user.EnumDesktopWindows.return_value = 1
    reader._close_reader(process, SimpleNamespace(user=win32.user, handle=win32.handle))
    assert win32.user.EnumDesktopWindows.call_count == 2


def test_close_reader_accepts_empty_desktop_error_zero_race(win32, monkeypatch):
    process = Mock(pid=12345)
    process.poll.side_effect = [None, 0]
    win32.user.EnumDesktopWindows.return_value = 0
    monkeypatch.setattr(reader.ctypes, 'get_last_error', lambda: 0)
    reader._close_reader(process, SimpleNamespace(user=win32.user, handle=win32.handle))
    process.wait.assert_called_once()


def test_reader_process_distinguishes_exit_259_from_running(win32):
    info = reader._ProcessInfo(hProcess=0x234567891, dwProcessId=12345)
    process = reader._ReaderProcess(win32.kernel, info, ['reader'])
    assert process.poll() is None
    with pytest.raises(reader.subprocess.TimeoutExpired):
        process.wait(.001)
    win32.kernel.WaitForSingleObject.return_value = 0

    def exit_code(handle, code):
        code._obj.value = 259
        return 1

    win32.kernel.GetExitCodeProcess.side_effect = exit_code
    assert process.poll() == process.wait(.001) == 259


@pytest.mark.parametrize('failure', ['job', 'job_limits', 'assignment', 'resume'])
def test_job_setup_fails_closed_without_running_unowned_reader(library_read, failure):
    case, api = library_read, library_read.win32
    if failure == 'job':
        api.kernel.CreateJobObjectW.return_value = None
    elif failure == 'job_limits':
        api.kernel.SetInformationJobObject.return_value = 0
    elif failure == 'assignment':
        api.kernel.AssignProcessToJobObject.return_value = 0
        api.kernel.TerminateProcess.return_value = 1
        api.kernel.WaitForSingleObject.return_value = 0
        api.kernel.GetExitCodeProcess.return_value = 1
    else:
        api.kernel.ResumeThread.return_value = 0xFFFFFFFF
    with pytest.raises(OSError):
        reader.read_library(case.root, 'Local:Target')
    assert not reader._CACHE
    case.connect.assert_not_called()
    api.user.CloseDesktop.assert_called_once_with(api.handle)
    if failure in ('job', 'job_limits'):
        api.kernel.CreateProcessW.assert_not_called()
    if failure == 'assignment':
        api.kernel.ResumeThread.assert_not_called()
        api.kernel.TerminateProcess.assert_called_once_with(0x234567891, 1)


def test_job_waits_for_owned_helpers_before_closing_handles(library_read):
    case, api = library_read, library_read.win32
    remaining = iter([2, 1, 0])

    def accounting(job, kind, pointer, size, required):
        assert case.close.call_count == 1, 'Reader must be closed naturally first'
        assert not api.user.CloseDesktop.called
        pointer._obj.ActiveProcesses = next(remaining)
        return 1

    api.kernel.QueryInformationJobObject.side_effect = accounting
    reader.read_library(case.root, 'Local:Target')
    assert api.kernel.QueryInformationJobObject.call_count == 3
    api.kernel.TerminateJobObject.assert_called_once_with(0x456789123, 1)
    api.kernel.TerminateProcess.assert_not_called()


def test_more_than_32_targets_do_not_relaunch_on_second_preflight(library_read):
    case = library_read
    assert reader._CACHE_LIMIT == 256
    for number in range(40):
        case.source.with_name(f'Target{number}.kicad_mod').write_bytes(case.source.read_bytes())
    for _ in range(2):
        for number in range(40):
            reader.read_library(case.root, f'Local:Target{number}')
    assert case.win32.kernel.CreateProcessW.call_count == 40


def test_cache_limit_evicts_least_recently_used_target(library_read, monkeypatch):
    case = library_read
    monkeypatch.setattr(reader, '_CACHE_LIMIT', 3)
    for name in ['Second', 'Third', 'Fourth']:
        case.source.with_name(name + '.kicad_mod').write_bytes(case.source.read_bytes())
    for name in ['Target', 'Second', 'Third', 'Target', 'Fourth', 'Target']:
        reader.read_library(case.root, 'Local:' + name)
    assert len(reader._CACHE) == 3
    assert {key[3] for key in reader._CACHE} == {'Local:Target', 'Local:Third', 'Local:Fourth'}
    assert case.win32.kernel.CreateProcessW.call_count == 4


@pytest.mark.parametrize('original_error', [False, True])
def test_locked_scratch_cleanup_is_bounded_and_preserves_original(scratch_project, monkeypatch, original_error):
    work = scratch_project / 'locked-reader'
    work.mkdir()
    removal = Mock(side_effect=PermissionError('owned helper still releasing cwd'))
    with monkeypatch.context() as patcher:
        patcher.setattr(reader.tempfile, 'mkdtemp', lambda **kwargs: str(work))
        patcher.setattr(reader.shutil, 'rmtree', removal)
        patcher.setattr(reader.time, 'sleep', lambda seconds: None)
        with pytest.raises(ValueError if original_error else RuntimeError) as caught:
            with reader._reader_workspace(scratch_project.anchor):
                if original_error:
                    raise ValueError('original IPC error')
        assert removal.call_count == 21
        if original_error:
            assert str(caught.value) == 'original IPC error'
            assert str(work) in '\n'.join(caught.value.__notes__)
        else:
            assert str(work) in str(caught.value)

"""Startup regressions use disposable projects, never a user's editor or backend."""
import sys
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/kicad_plugin'))
import desktop


def test_backend_does_not_http_probe_unbound_port_or_recheck_ready_service(scratch_project):
    tmp_path = scratch_project
    token = 'private-owner'
    info = {'project_root': str(tmp_path), 'config_path': str(tmp_path / 'config.json'),
            'backend_directory': str(desktop.CORE), 'desktop_launcher_version': 3}
    from part_selection_lifecycle import owner_id
    info['desktop_owner_id'] = owner_id(token)
    process = Mock(pid=123)
    process.poll.return_value = None
    bound = iter([True, True, False])  # candidate free, child unbound, then ready
    unbound = True
    def probe(port):
        nonlocal unbound
        unbound = next(bound, False)
        return unbound
    def service(port):
        assert not unbound, 'Do not spend an HTTP timeout on an unbound child port'
        return info
    with patch.object(desktop, 'occupied_ports', return_value=[]), \
            patch.object(desktop, 'port_available', side_effect=probe), \
            patch.object(desktop, 'service_info', side_effect=service) as query, \
            patch.object(desktop.subprocess, 'Popen', return_value=process), \
            patch.object(desktop.time, 'sleep'):
        desktop.ensure_backend(tmp_path, tmp_path / 'config.json', tmp_path, owner_token=token)
    assert query.call_count == 1
    process.terminate.assert_not_called()


@pytest.mark.parametrize('outcome', ['error', 'cancel', 'duplicate', 'success', 'ipc-success', 'webview-error', 'reexec'])
def test_progress_precedes_discovery_and_always_closes(scratch_project, outcome):
    tmp_path = scratch_project
    events = []
    progress = Mock()
    progress.close.side_effect = lambda: events.append('closed')
    def discover():
        events.append('discovery')
        assert events[0] == 'shown'
        if outcome == 'error':
            raise ValueError('unavailable')
        return None if outcome == 'cancel' else tmp_path
    def error(message):
        assert 'closed' in events
    webview = MagicMock()
    loaded_event = webview.create_window.return_value.events.loaded
    webview.start.side_effect = RuntimeError('WebView failed') if outcome == 'webview-error' else None
    if outcome in {'success', 'ipc-success'}:
        def loaded(**kwargs):
            # A real loaded callback must remove the splash before normal exit.
            loaded_event.__iadd__.call_args.args[0]()
            assert 'closed' in events
        webview.start.side_effect = loaded
    lifecycle = Mock(allow_close=outcome != 'webview-error', restart=False, install_update=False)
    modules = {'webview': webview, 'updater': SimpleNamespace(PluginUpdater=Mock())}
    if outcome == 'reexec':
        modules['webview'] = None
    with patch.dict(sys.modules, modules), \
            patch.dict(desktop.os.environ, {'KICAD_API_SOCKET': 'launch-socket' if outcome == 'ipc-success' else ''}), \
            patch.object(desktop, 'StartupProgress', return_value=progress), \
            patch.object(progress, 'start', side_effect=lambda: events.append('shown')), \
            patch.object(desktop, 'choose_project', side_effect=discover), \
            patch.object(desktop, 'resolve_launch_project', side_effect=discover), \
            patch.object(desktop, 'validate_project', side_effect=lambda root: root), \
            patch.object(desktop, 'ensure_config', return_value=tmp_path / 'config.json'), \
            patch('part_selection_overrides.load_config', return_value={}), \
            patch('part_selection_settings.apply_runtime_settings'), \
            patch.object(desktop, 'cache_directory', return_value=tmp_path.parent), \
            patch.object(desktop.logging, 'basicConfig'), \
            patch.object(desktop, 'acquire_window', return_value=(None, outcome != 'duplicate')), \
            patch.object(desktop, 'activate_window', return_value=True), \
            patch.object(desktop, 'ensure_desktop_runtime', return_value=Path(sys.executable)), \
            patch.object(desktop.subprocess, 'Popen'), \
            patch.object(desktop, 'ensure_backend', return_value='http://127.0.0.1:1234/'), \
            patch('part_selection_lifecycle.DesktopLifecycle', return_value=lifecycle), \
            patch.object(desktop, 'enable_browser_shortcuts'), \
            patch.object(desktop, 'show_error', side_effect=error):
        result = desktop.main([])
    assert result == (1 if outcome in {'error', 'webview-error'} else 0)
    assert events[0:2] == ['shown', 'discovery']
    assert 'closed' in events
    if outcome == 'webview-error':
        lifecycle._stop_backend.assert_called_once()
    else:
        lifecycle._stop_backend.assert_not_called()


@pytest.mark.skipif(os.name != 'nt' or os.environ.get('KPS_NATIVE_STARTUP_SMOKE') != '1',
                    reason='opt-in native Windows indicator smoke')
def test_native_indicator_pumps_messages_updates_and_closes():
    import ctypes
    from ctypes import wintypes
    from startup_progress import StartupProgress
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowW.restype = wintypes.HWND
    user.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowExW.restype = wintypes.HWND
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
        wintypes.LPARAM, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    user.SendMessageTimeoutW.restype = wintypes.LPARAM
    title = f'Workbench startup smoke {os.getpid()}'
    progress = StartupProgress(title)
    started = time.perf_counter()
    try:
        progress.start()
        assert progress._ready.wait(2)
        hwnd = user.FindWindowW(None, title)
        assert hwnd
        assert user.GetWindowLongW(hwnd, -20) & 0x8, 'Keep progress above the initializing WebView'
        print(f'native indicator ready_ms={(time.perf_counter()-started)*1000:.1f}')
        # Main thread deliberately waits, just as it does on slow discovery.
        progress.stage('Testing delayed project discovery')
        time.sleep(0.3)
        label = user.FindWindowExW(hwnd, None, 'STATIC', None)
        text = ctypes.create_unicode_buffer(256)
        user.GetWindowTextW(label, text, len(text))
        assert text.value == 'Testing delayed project discovery'
        result = ctypes.c_size_t()
        assert user.SendMessageTimeoutW(hwnd, 0, 0, 0, 2, 500, ctypes.byref(result))
        assert user.FindWindowExW(hwnd, None, 'msctls_progress32', None)
    finally:
        progress.close()
        progress.close()
    assert not progress._thread.is_alive()
    assert not user.FindWindowW(None, title)


def test_package_contains_indicator_and_its_native_controls_manifest():
    from package_plugin import package_files
    files = package_files()
    assert files['plugins/startup_progress.py'] == (desktop.HERE / 'startup_progress.py').read_bytes()
    assert files['plugins/startup_progress.manifest'] == (desktop.HERE / 'startup_progress.manifest').read_bytes()


def test_stage_logs_elapsed_time(caplog):
    import logging
    from startup_progress import StartupProgress
    progress = StartupProgress('Workbench')
    with caplog.at_level(logging.INFO):
        progress.stage('Loading desktop environment')
    assert 'stage=Loading desktop environment elapsed_ms=' in caplog.text


@pytest.mark.skipif(os.name != 'nt' or os.environ.get('KPS_NATIVE_STARTUP_SMOKE') != '1',
                    reason='opt-in native Windows indicator smoke')
def test_indicator_failure_and_early_close_do_not_leave_ui(tmp_path):
    from startup_progress import StartupProgress
    progress = StartupProgress('Unavailable native indicator')
    with patch('startup_progress.Path.with_suffix', return_value=tmp_path / 'missing.manifest'):
        progress.start()
        progress.close()
    assert not progress._thread.is_alive()
    progress = StartupProgress('Already closed native indicator')
    progress.close()
    progress.start()
    progress.close()
    assert not progress._thread.is_alive()

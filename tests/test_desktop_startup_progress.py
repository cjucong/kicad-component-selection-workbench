"""Startup regressions use disposable projects, never a user's editor or backend."""
import sys
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/kicad_plugin'))
import desktop
from startup_progress import StartupProgress


def fake_view():
    view = MagicMock()
    for name in ('loaded', 'closing'):
        event = getattr(view.create_window.return_value.events, name)
        event.__iadd__.return_value = event
    return view


def test_backend_does_not_http_probe_unbound_port_or_recheck_ready_service(scratch_project):
    token = 'private-owner'
    info = {'project_root':str(scratch_project), 'config_path':str(scratch_project / 'config.json'),
            'backend_directory':str(desktop.CORE), 'desktop_launcher_version':3}
    from part_selection_lifecycle import owner_id
    info['desktop_owner_id'] = owner_id(token)
    process = Mock(pid=123)
    process.poll.return_value = None
    bound = iter([True, True, False])
    unbound = True
    def probe(port):
        nonlocal unbound
        unbound = next(bound, False)
        return unbound
    def service(port):
        assert not unbound
        return info
    with patch.object(desktop, 'occupied_ports', return_value=[]), \
            patch.object(desktop, 'port_available', side_effect=probe), \
            patch.object(desktop, 'service_info', side_effect=service) as query, \
            patch.object(desktop.subprocess, 'Popen', return_value=process), \
            patch.object(desktop.time, 'sleep'):
        desktop.ensure_backend(scratch_project, scratch_project / 'config.json', scratch_project, owner_token=token)
    assert query.call_count == 1
    process.terminate.assert_not_called()


@pytest.mark.parametrize('cancel,error', [(False,False), (True,False), (False,True)])
def test_one_window_handoff_error_and_early_close(cancel, error):
    progress = StartupProgress('Workbench')
    view = fake_view()
    lifecycle = Mock(url='http://127.0.0.1:0/', allow_close=False)
    started, release = threading.Event(), threading.Event()
    def loader():
        started.set()
        assert release.wait(3)
        if error:
            raise RuntimeError('synthetic startup failure')
        return 'http://127.0.0.1:1234/'
    ready = Mock()
    window = progress.open(view, 'Workbench', lifecycle, loader, object(), ready)
    loaded = window.events.loaded.__iadd__.call_args.args[0]
    closing = window.events.closing.__iadd__.call_args.args[0]
    try:
        assert 'progress' in view.create_window.call_args.kwargs['html']
        assert not window.load_url.called
        loaded()
        assert started.wait(3)
        if cancel:
            assert closing() is True
            assert lifecycle.allow_close
        release.set()
        progress.close()
        view.create_window.assert_called_once()
        if cancel or error:
            window.load_url.assert_not_called()
            assert not progress.ready
            ready.assert_not_called()
            lifecycle.on_closing.assert_not_called()
        else:
            window.load_url.assert_called_once_with(lifecycle.url)
            window.get_current_url.return_value = lifecycle.url
            loaded()
            assert progress.ready
            ready.assert_called_once()
            closing()
            lifecycle.on_closing.assert_called_once()
        assert bool(progress.error) is error
    finally:
        release.set()
        progress.close()


@pytest.mark.parametrize('outcome', ['error','cancel','duplicate','success','ipc-success','webview-error','backend-error','reexec'])
def test_main_preserves_project_identity_and_cleans_every_startup_exit(scratch_project, outcome):
    events = []
    view = fake_view()
    lifecycle = Mock(allow_close=False, restart=False, install_update=False)
    def discover():
        if outcome == 'error':
            raise ValueError('unavailable')
        return None if outcome == 'cancel' else scratch_project
    def backend(*args, **kwargs):
        assert view.create_window.called, 'Main window must exist before backend starts'
        events.append('backend')
        if outcome == 'backend-error':
            raise RuntimeError('backend failed')
        return 'http://127.0.0.1:1234/'
    def loop(**kwargs):
        if outcome == 'webview-error':
            raise RuntimeError('WebView failed')
        window = view.create_window.return_value
        window.events.loaded.__iadd__.call_args.args[0]()
        # Wait only in the fake loop; the actual GUI thread stays responsive.
        for thread in threading.enumerate():
            if thread.name == 'desktop-startup':
                thread.join(3)
        lifecycle.allow_close = True
    view.start.side_effect = loop
    modules = {'webview':None if outcome == 'reexec' else view,
               'updater':SimpleNamespace(PluginUpdater=Mock())}
    with patch.dict(sys.modules, modules), \
            patch.dict(desktop.os.environ, {'KICAD_API_SOCKET':'launch-socket' if outcome == 'ipc-success' else ''}), \
            patch.object(desktop, 'choose_project', side_effect=discover), \
            patch.object(desktop, 'resolve_launch_project', side_effect=discover), \
            patch.object(desktop, 'validate_project', side_effect=lambda root:root), \
            patch.object(desktop, 'ensure_config', return_value=scratch_project / 'config.json'), \
            patch('part_selection_overrides.load_config', return_value={}), \
            patch('part_selection_settings.apply_runtime_settings'), \
            patch.object(desktop, 'cache_directory', return_value=scratch_project.parent), \
            patch.object(desktop.logging, 'basicConfig'), \
            patch.object(desktop, 'acquire_window', return_value=(None, outcome != 'duplicate')), \
            patch.object(desktop, 'activate_window', return_value=True), \
            patch.object(desktop, 'ensure_desktop_runtime', return_value=Path(sys.executable)), \
            patch.object(desktop.subprocess, 'Popen'), \
            patch.object(desktop, 'ensure_backend', side_effect=backend), \
            patch('part_selection_lifecycle.DesktopLifecycle', return_value=lifecycle), \
            patch.object(desktop, 'enable_browser_shortcuts'), \
            patch.object(desktop, 'show_error') as show_error:
        result = desktop.main([])
    assert result == (1 if outcome in {'error','webview-error','backend-error'} else 0)
    assert view.create_window.call_count == (1 if outcome in {'success','ipc-success','webview-error','backend-error'} else 0)
    if outcome == 'backend-error':
        show_error.assert_not_called()
    if outcome in {'success','ipc-success'}:
        assert events == ['backend']


def test_package_contains_loading_page_without_separate_native_indicator():
    from package_plugin import package_files
    files = package_files()
    assert files['plugins/startup_progress.py'] == (desktop.HERE / 'startup_progress.py').read_bytes()
    assert 'plugins/startup_progress.manifest' not in files


def test_stage_logs_elapsed_time(caplog):
    progress = StartupProgress('Workbench')
    with caplog.at_level(logging.INFO):
        progress.stage('Loading desktop environment')
    assert 'stage=Loading desktop environment elapsed_ms=' in caplog.text

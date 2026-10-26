"""Desktop-owned service shutdown, without using production projects or windows."""
import json
import shutil
import subprocess
import sys
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

import pytest

from part_selection_server import PartSelectionApp, make_handler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/kicad_plugin'))


@pytest.fixture
def app(scratch_project):
    root = Path(scratch_project)
    config = root / 'settings.json'
    config.write_text(json.dumps({'schematic_dir':'.'}))
    instance = PartSelectionApp(config)
    yield instance
    instance.search_jobs.close()
    instance.selection_jobs.close()


def test_only_owner_can_stop_idle_service_and_new_requests_are_blocked(app):
    from part_selection_lifecycle import ServiceLifecycle
    server = Mock()
    stopped = threading.Event()
    server.shutdown.side_effect = stopped.set
    lifecycle = ServiceLifecycle(app, server, 'secret')
    with pytest.raises(ValueError, match='身份'):
        lifecycle.shutdown('other')
    assert not app.settings_gate.stopping
    with patch.object(app.selection_jobs, 'busy', return_value=True):
        with pytest.raises(ValueError, match='尚未完成'):
            lifecycle.shutdown('secret')
    assert not app.settings_gate.stopping
    lifecycle.shutdown('secret')
    assert app.settings_gate.stopping
    assert stopped.wait(2)
    server.shutdown.assert_called_once()


def test_shutdown_does_not_interrupt_other_request(app):
    from part_selection_lifecycle import ServiceLifecycle
    lifecycle = ServiceLifecycle(app, Mock(), 'secret')
    app.settings_gate.active = 2
    from part_selection_lifecycle import DesktopExitPending
    with pytest.raises(DesktopExitPending, match='请求'):
        lifecycle.shutdown('secret')
    assert not app.settings_gate.stopping


def test_exit_cancels_stock_read_waiters_but_not_active_writes(app):
    from concurrent.futures import CancelledError
    from part_selection_lifecycle import DesktopExitPending, ServiceLifecycle
    from part_stock_lookup import _FetchCache, check_stock_cancelled
    started, cancelled, release = threading.Event(), threading.Event(), threading.Event()
    cache = _FetchCache(max_inflight=1)

    def blocked_fetch(code):
        started.set()
        while not release.wait(0.02):
            check_stock_cancelled()
        return {'ok': True, 'c_code': code}

    def request():
        try:
            with patch('part_selection_server.lookup_stock_with_cache', side_effect=lambda path, code:
                       cache.get((code, 1), lambda: blocked_fetch(code))):
                app.stock('C100')
        except CancelledError:
            cancelled.set()

    worker = threading.Thread(target=request)
    worker.start()
    try:
        assert started.wait(2)
        server = Mock()
        lifecycle = ServiceLifecycle(app, server, 'secret')
        app.settings_gate.active = 2
        with pytest.raises(DesktopExitPending):
            lifecycle.shutdown('secret')
        assert cancelled.wait(1), 'Exit is still held hostage by stock network reads'
        assert not app.settings_gate.stopping, 'Unrelated write must still block shutdown'
    finally:
        release.set()
        worker.join(timeout=2)
        app.settings_gate.active = 0


def test_wrong_exit_identity_does_not_cancel_stock_reads(app):
    from part_selection_lifecycle import ServiceLifecycle
    with patch.object(app.stock_queries, 'cancel') as cancel:
        with pytest.raises(ValueError, match='身份'):
            ServiceLifecycle(app, Mock(), 'secret').shutdown('other')
    cancel.assert_not_called()


def test_unmanaged_server_has_no_shutdown_authority(app):
    from part_selection_lifecycle import ServiceLifecycle
    with pytest.raises(ValueError, match='身份'):
        ServiceLifecycle(app, Mock(), '').shutdown('')


def test_http_shutdown_auth_busy_and_real_port_release(app):
    from part_selection_lifecycle import ServiceLifecycle, owner_id
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
    app.lifecycle = ServiceLifecycle(app, server, 'secret')

    def serve():
        try:
            server.serve_forever(poll_interval=0.05)
        finally:
            server.server_close()

    thread = threading.Thread(target=serve)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'

    def stop(token):
        return urlopen(Request(base+'/api/desktop_shutdown', data=b'{}',
            headers={'Content-Type':'application/json','X-Desktop-Token':token}),timeout=3)

    try:
        with urlopen(base+'/api/config') as response:
            info = json.load(response)
        assert info['desktop_launcher_version']==3
        assert info['desktop_owner_id']==owner_id('secret')
        assert 'secret' not in json.dumps(info)
        with pytest.raises(HTTPError):
            stop('wrong')
        with patch.object(app.search_jobs, 'busy', return_value=True):
            with pytest.raises(HTTPError) as busy:
                stop('secret')
            assert json.load(busy.value)['retryable'] is True
        with stop('secret') as response:
            assert json.load(response)['ok']
        thread.join(timeout=4)
        assert not thread.is_alive()
        with pytest.raises(OSError):
            urlopen(base+'/api/config',timeout=0.5)
    finally:
        if thread.is_alive():
            server.shutdown()
        thread.join(timeout=4)
        server.server_close()


def test_http_exit_drains_stock_while_network_is_still_blocked(app):
    from concurrent.futures import ThreadPoolExecutor
    from part_selection_lifecycle import ServiceLifecycle
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
    app.lifecycle = ServiceLifecycle(app, server, 'secret')
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked_network(*args, **kwargs):
        started.set()
        try:
            assert release.wait(4)
            raise OSError('test offline')
        finally:
            finished.set()

    def serve():
        try:
            server.serve_forever(poll_interval=.02)
        finally:
            server.server_close()

    service = threading.Thread(target=serve)
    service.start()
    base = f'http://127.0.0.1:{server.server_port}'

    def stock_request():
        with urlopen(base+'/api/stock?code=C100', timeout=3) as response:
            return json.load(response)

    def shutdown(token):
        with urlopen(Request(base+'/api/desktop_shutdown', data=b'{}',
                headers={'Content-Type':'application/json','X-Desktop-Token':token}), timeout=2) as response:
            return json.load(response)

    try:
        with patch('part_stock_lookup.urllib.request.urlopen', side_effect=blocked_network), \
                ThreadPoolExecutor(max_workers=1) as executor:
            stock = executor.submit(stock_request)
            assert started.wait(2)
            with pytest.raises(HTTPError):
                shutdown('wrong')
            assert not stock.done()
            try:
                shutdown('secret')
            except HTTPError as pending:
                assert json.load(pending)['retryable']
            assert stock.result(timeout=1)['cancelled']
            assert not finished.is_set(), 'Waiter should drain without waiting for network I/O'
            deadline = time.monotonic()+1
            while not app.settings_gate.stopping:
                try:
                    assert shutdown('secret')['ok']
                except HTTPError:
                    assert time.monotonic() < deadline
                    time.sleep(.02)
            service.join(timeout=2)
            assert not service.is_alive()
            assert not app.stock_cache_path.exists()
    finally:
        release.set()
        if started.is_set():
            assert finished.wait(2)
        if service.is_alive():
            server.shutdown()
        service.join(timeout=2)
        server.server_close()


def controller():
    from part_selection_lifecycle import DesktopLifecycle
    window = SimpleNamespace(evaluate_js=Mock(), destroy=Mock(), get_current_url=lambda:'http://127.0.0.1:1234/')
    lifecycle = DesktopLifecycle('http://127.0.0.1:1234/', 'secret')
    lifecycle.window = window
    return lifecycle, window


def test_native_close_is_deferred_until_frontend_saved():
    lifecycle, window = controller()
    dispatched = threading.Event()
    window.evaluate_js.side_effect = lambda script:dispatched.set()
    assert lifecycle.on_closing() is False
    assert dispatched.wait(2)
    assert window.evaluate_js.call_count == 1
    assert 'window.requestDesktopExit(false)' in window.evaluate_js.call_args.args[0]
    window.destroy.assert_not_called()


@pytest.mark.parametrize('empty', [True, False])
def test_uninitialized_close_only_stops_owned_backend_without_editable_rows(empty):
    lifecycle, window = controller()
    evaluated = threading.Event()
    def evaluate(script):
        if 'typeof window.requestDesktopExit' in script:
            return False
        evaluated.set()
        return empty
    window.evaluate_js.side_effect = evaluate
    with patch.object(lifecycle, '_stop_backend') as stop:
        lifecycle.on_closing()
        assert evaluated.wait(2)
        # Wait for the close worker, not merely its last evaluation.
        for worker in threading.enumerate():
            if worker.name == 'desktop-close':
                worker.join(timeout=2)
        assert stop.call_count == int(empty)
        assert window.destroy.call_count == int(empty)
    assert lifecycle.allow_close is empty


def test_uninitialized_close_still_retains_window_when_backend_busy():
    lifecycle, window = controller()
    window.evaluate_js.side_effect = [False, True]
    with patch.object(lifecycle, '_stop_backend', side_effect=ValueError('busy')):
        lifecycle.on_closing()
        for worker in threading.enumerate():
            if worker.name == 'desktop-close':
                worker.join(timeout=2)
    assert not lifecycle.allow_close
    window.destroy.assert_not_called()


def test_restart_stops_owned_service_before_destroying_window():
    lifecycle, window = controller()
    with patch.object(lifecycle, '_stop_backend') as stop:
        result = lifecycle.finish_exit(True)
    assert result['ok']
    stop.assert_called_once()
    assert lifecycle.restart
    assert lifecycle.on_closing() is True
    window.destroy.assert_called_once()


def test_shutdown_failure_keeps_window_and_allows_retry():
    lifecycle, window = controller()
    with patch.object(lifecycle, '_stop_backend', side_effect=ValueError('同步尚未完成')):
        result = lifecycle.finish_exit(False)
    assert not result['ok']
    assert '同步' in result['error']
    assert not lifecycle.allow_close
    window.destroy.assert_not_called()


def test_verified_update_is_applied_only_after_saved_window_and_owned_backend_close():
    lifecycle, window = controller()
    lifecycle.updater = Mock(pending={'verified':True})
    lifecycle.updater.status.return_value = {'mode':'check'}
    with patch.object(lifecycle, '_stop_backend') as stop:
        assert lifecycle.finish_exit(True, True)['ok']
    assert lifecycle.install_update and lifecycle.restart
    stop.assert_called_once()
    lifecycle.updater.apply.assert_not_called()


def test_update_without_verified_package_or_during_busy_shutdown_keeps_window():
    lifecycle, window = controller()
    lifecycle.updater = Mock(pending=None)
    with patch.object(lifecycle, '_stop_backend') as stop:
        assert not lifecycle.finish_exit(True, True)['ok']
    stop.assert_not_called()
    lifecycle.updater.pending={'verified':True}
    with patch.object(lifecycle, '_stop_backend', side_effect=ValueError('busy')):
        assert not lifecycle.finish_exit(True, True)['ok']
    assert not lifecycle.install_update
    window.destroy.assert_not_called()


def test_automatic_update_waits_for_normal_exit_and_does_not_force_restart():
    lifecycle, window = controller()
    lifecycle.updater = Mock(pending={'verified':True})
    lifecycle.updater.status.return_value={'mode':'auto'}
    with patch.object(lifecycle, '_stop_backend'):
        assert lifecycle.finish_exit(False)['ok']
    assert lifecycle.install_update and not lifecycle.restart


def test_other_workbench_keeps_explicit_update_window_and_backend_open():
    lifecycle, window = controller()
    lifecycle.updater = Mock(pending={'verified':True})
    lifecycle.updater.check_installation_sessions.side_effect = ValueError('OtherBoard PID 1234')
    with patch.object(lifecycle, '_stop_backend') as stop:
        result = lifecycle.finish_exit(True, True)
    assert not result['ok'] and 'OtherBoard PID 1234' in result['error']
    assert not lifecycle.allow_close and not lifecycle.install_update
    stop.assert_not_called()
    window.destroy.assert_not_called()


def test_auto_update_with_other_workbench_does_not_prevent_normal_close():
    lifecycle, window = controller()
    lifecycle.updater = Mock(pending={'verified':True})
    lifecycle.updater.status.return_value = {'mode':'auto'}
    lifecycle.updater.check_installation_sessions.side_effect = ValueError('OtherBoard PID 1234')
    with patch.object(lifecycle, '_stop_backend') as stop:
        assert lifecycle.finish_exit(False)['ok']
    assert lifecycle.allow_close and not lifecycle.install_update
    stop.assert_called_once()
    window.destroy.assert_called_once()


def test_foreign_webpage_or_unknown_action_cannot_manage_updates():
    from part_selection_lifecycle import DesktopBridge
    lifecycle, window = controller()
    lifecycle.updater = Mock()
    bridge = DesktopBridge(lifecycle)
    assert not bridge.plugin_update('execute','arbitrary command')['ok']
    window.get_current_url=lambda:'https://example.com/'
    assert not bridge.plugin_update('prepare')['ok']
    lifecycle.updater.prepare.assert_not_called()


def test_foreign_webpage_cannot_call_native_shutdown():
    lifecycle, window = controller()
    window.get_current_url = lambda:'https://example.com/'
    with patch.object(lifecycle, '_stop_backend') as stop:
        assert not lifecycle.finish_exit(True)['ok']
    stop.assert_not_called()


def test_watchdog_waits_for_work_and_does_not_force_terminate(app):
    from part_selection_lifecycle import ServiceLifecycle
    server = Mock()
    lifecycle = ServiceLifecycle(app, server, 'secret')
    with patch.object(app.selection_jobs, 'busy', side_effect=[True, False]), patch(
            'part_selection_lifecycle.time.sleep'):
        lifecycle.owner_exited()
    assert app.settings_gate.stopping


def test_hidden_launcher_no_longer_adds_browser_mode():
    import launch_part_selection
    with patch.object(launch_part_selection.desktop,'main',return_value=0) as main:
        launch_part_selection.main(['--hidden','--project','example'])
    main.assert_called_once_with(['--project','example'])


def test_stale_or_other_owned_service_is_not_silently_reused(app):
    import desktop
    payload = {'project_root':str(app.project_root),'config_path':str(app.config['config_path']),
               'backend_directory':str(desktop.CORE),'desktop_launcher_version':2}
    with patch.object(desktop,'port_available',return_value=False), patch.object(desktop,'service_info',return_value=payload), patch.object(desktop.subprocess,'Popen') as launch:
        with pytest.raises(RuntimeError, match='旧版|其他'):
            desktop.ensure_backend(app.project_root,app.config['config_path'],app.project_root,owner_token='new')
    launch.assert_not_called()


def test_same_project_from_another_installation_is_not_started_twice(app):
    import desktop
    payload = {'project_root':str(app.project_root),'config_path':str(app.config['config_path']),
               'backend_directory':str(app.project_root / 'other-plugin'),'desktop_launcher_version':3}
    with patch.object(desktop,'port_available',return_value=False), patch.object(desktop,'service_info',return_value=payload), patch.object(desktop.subprocess,'Popen') as launch:
        with pytest.raises(RuntimeError, match='旧版|其他'):
            desktop.ensure_backend(app.project_root,app.config['config_path'],app.project_root,owner_token='new')
    launch.assert_not_called()


def test_backend_does_not_health_probe_free_candidate_ports(app):
    import desktop
    with patch.object(desktop, 'port_available', return_value=True) as probe:
        assert desktop.occupied_ports([8765, 19000, 19001]) == []
    assert probe.call_count == 3


def test_stopping_gate_rejects_new_requests(app):
    handler = Mock()
    method = Mock()
    app.settings_gate.stopping = True
    app.settings_gate.request(method)(handler)
    handler.send_error.assert_called_once_with(503, 'Workbench is shutting down')
    method.assert_not_called()
    assert app.settings_gate.active==0


def test_desktop_has_no_browser_flag_or_automatic_fallback():
    import desktop
    source = Path(desktop.__file__).read_text(encoding='utf-8')
    assert 'import webbrowser' not in source
    assert '"--browser"' not in source


def test_restart_relaunch_preserves_explicit_config_and_hides_process(app):
    import desktop
    with patch.object(desktop.subprocess,'Popen') as launch:
        desktop.relaunch_desktop(app.project_root,app.config['config_path'],app.project_root)
    command = launch.call_args.args[0]
    assert command[-4:]==['--project',str(app.project_root),'--config',str(app.config['config_path'])]
    if desktop.os.name=='nt':
        assert launch.call_args.kwargs['creationflags']==desktop.subprocess.CREATE_NO_WINDOW


def test_real_owner_exit_triggers_graceful_service_shutdown(app):
    from part_selection_lifecycle import ServiceLifecycle
    stopped = threading.Event()
    server = Mock()
    server.shutdown.side_effect = stopped.set
    lifecycle = ServiceLifecycle(app, server, 'secret')
    options = {'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}
    with subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(0.3)'], **options) as owner:
        watcher = threading.Thread(target=lifecycle.watch_owner, args=(owner.pid,), daemon=True)
        watcher.start()
        owner.wait(timeout=5)
        watcher.join(timeout=4)
    assert stopped.wait(2)
    assert not watcher.is_alive()
    assert app.settings_gate.stopping


def test_bridge_exposes_only_restricted_desktop_actions():
    from part_selection_lifecycle import DesktopBridge
    bridge = DesktopBridge(Mock())
    assert [name for name in dir(bridge) if not name.startswith('_')]==['candidate_window', 'finish_exit', 'plugin_update', 'tool_window']


def test_frontend_exit_flushes_edits_and_retains_window_on_busy_or_failure():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for frontend lifecycle regression')
    source = Path(__file__).resolve().parents[1] / 'tools/web/desktop-lifecycle.js'
    script = r"""
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
function setup(){
 const calls=[], buttons={};
 const context={bulkSyncRunning:false, composing:false, dirty:false, discard:false,
   document:{body:{inert:false},querySelector:()=>context.composing,
     getElementById:id=>({addEventListener:(_,handler)=>buttons[id]=handler})},
   setState:(...args)=>calls.push(args),closeToolbarMenus:()=>{},
   flushRowEdits:async()=>{calls.push('saved');},
   cancelStockQueries:()=>calls.push('cancel stock'),
   cancelRowStockQueries:()=>calls.push('cancel row stock'),
   window:{confirm:()=>context.discard,hasUnsavedPathSettings:()=>context.dirty,
     pywebview:{api:{finish_exit:async restart=>{calls.push(['exit',restart]);return {ok:true};}}}}};
 vm.runInNewContext(source,context);
 return {context,calls,buttons};
}
(async()=>{
 for(const mode of ['busy','composing','dirty','saveFailure','backendFailure','success']){
  const {context:c,calls}=setup();
  if(mode==='busy')c.bulkSyncRunning=true;
  if(mode==='composing')c.composing=true;
  if(mode==='dirty')c.dirty=true;
  if(mode==='saveFailure')c.flushRowEdits=async()=>{throw Error('save failed');};
  if(mode==='backendFailure')c.window.pywebview.api.finish_exit=async()=>({ok:false,error:'busy backend'});
  await c.window.requestDesktopExit(true);
  assert.strictEqual(c.document.body.inert,false,mode);
  if(mode==='success'){
   assert(calls.includes('saved'));
   assert(calls.includes('cancel stock'));
   assert(calls.includes('cancel row stock'));
   assert(calls.indexOf('saved')<calls.findIndex(call=>Array.isArray(call)&&call[0]==='exit'));
  }
  if(mode==='success')assert(calls.some(call=>Array.isArray(call)&&call[0]==='exit'&&call[1]===true));
  else assert(!calls.some(call=>Array.isArray(call)&&call[0]==='exit'),mode);
  if(mode==='saveFailure'){
   c.flushRowEdits=async()=>calls.push('retry saved');
   await c.window.requestDesktopExit(false);
   assert(calls.some(call=>Array.isArray(call)&&call[0]==='exit'&&call[1]===false));
  }
 }
 const {context:c,calls,buttons}=setup();
 let finish;
 c.flushRowEdits=()=>new Promise(resolve=>finish=resolve);
 const pending=c.window.requestDesktopExit(false);
 await c.window.requestDesktopExit(true);
 finish(); await pending;
 assert.strictEqual(calls.filter(call=>Array.isArray(call)&&call[0]==='exit').length,1);
 assert.deepStrictEqual(Object.keys(buttons).sort(),['exitWorkbench','restartWorkbench']);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    subprocess.run([node,'-e',script,str(source)],check=True,capture_output=True,text=True,timeout=20)

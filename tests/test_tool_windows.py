"""Native window ownership contracts without starting a desktop GUI."""
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest

from part_selection_lifecycle import DesktopBridge, DesktopLifecycle
from test_candidate_native_window import Event


def windows():
    from part_selection_tool_windows import ToolWindows
    lifecycle = DesktopLifecycle('http://127.0.0.1:19010/', 'owner-test')
    lifecycle.window = Mock()
    lifecycle.window.get_current_url.return_value = lifecycle.url
    children = []
    def create(title, url, **kwargs):
        child = Mock()
        child.events = SimpleNamespace(**{name:Event() for name in ('loaded','closing','minimized','restored')})
        child.get_current_url.return_value = url
        child.evaluate_js.return_value = {'ok':True}
        child.bridge = kwargs['js_api']
        children.append(child)
        return child
    factory = Mock(side_effect=create)
    owner = ToolWindows(lifecycle, factory)
    lifecycle.tools = owner
    return owner, lifecycle, children, factory


def request(kind='footprint', rid='one', **payload):
    return {'kind':kind,'request_id':rid,'payload':payload}


def loaded(child):
    for handler in child.events.loaded.handlers:
        handler()


def test_open_shows_before_ready_and_reuses_same_native_window():
    owner, lifecycle, children, factory = windows()
    assert DesktopBridge(lifecycle).tool_window('open', request(refs='R1'))['ok']
    child = children[0]
    child.show.assert_called_once()
    child.evaluate_js.assert_not_called()
    assert factory.call_args.kwargs['frameless'] is False
    loaded(child)
    assert 'receiveNativeToolRequest' in child.evaluate_js.call_args.args[0]
    assert owner.main_action('open', request(refs='R1'))['ok']
    assert len(children) == 1


def test_loading_different_footprint_rows_keeps_independent_requests():
    owner, _, children, _ = windows()
    owner.main_action('open',request(rid='old',refs='R1'))
    owner.main_action('open',request(rid='new',refs='R2'))
    loaded(children[0])
    script = children[0].evaluate_js.call_args.args[0]
    assert 'R1' in script and '"request_id": "old"' in script
    assert len(children) == 2
    loaded(children[1])
    assert 'R2' in children[1].evaluate_js.call_args.args[0]


def test_busy_footprint_on_one_row_does_not_block_another_row():
    owner, lifecycle, children, _ = windows()
    assert owner.main_action('open', request(refs='R1'))['ok']
    loaded(children[0])
    children[0].evaluate_js.return_value = False
    assert owner.main_action('open', request(rid='two', refs='R2'))['ok']
    assert len(children) == 2
    loaded(children[1])
    assert children[0].bridge.tool_window('changed')['ok']
    assert '"refs": "R1"' in lifecycle.window.evaluate_js.call_args.args[0]
    assert children[1].bridge.tool_window('changed')['ok']
    assert '"refs": "R2"' in lifecycle.window.evaluate_js.call_args.args[0]
    assert not children[0].bridge.tool_window('close')['ok']
    children[1].evaluate_js.return_value = True
    assert children[1].bridge.tool_window('close')['ok']
    children[1].destroy.assert_called_once()
    children[0].destroy.assert_not_called()


def test_closed_footprint_reopens_fresh_without_old_mount_or_callbacks():
    owner, _, children, _ = windows()
    owner.main_action('open', request(refs='R1'))
    original = children[0]
    loaded(original)
    original.evaluate_js.return_value = True
    assert original.bridge.tool_window('close')['ok']
    assert owner.main_action('open', request(rid='new', refs='R1'))['ok']
    assert len(children) == 2
    assert not original.bridge.tool_window('changed')['ok'], 'Closed bridge reached a new window'
    loaded(original)
    assert not owner.entries['footprint']['ready'], 'Late load event reached the new window'
    loaded(children[1])
    assert '"request_id": "new"' in children[1].evaluate_js.call_args.args[0]


@pytest.mark.parametrize('value', [None,{},request(kind='arbitrary'),
    request(refs='R1',url='https://evil.invalid'),request(refs=4),request(refs='x'*16001)])
def test_bad_request_cannot_create_window(value):
    owner, _, _, factory = windows()
    assert not owner.main_action('open',value)['ok']
    factory.assert_not_called()


def test_tool_bridge_cannot_control_host_or_other_tools():
    owner, lifecycle, children, _ = windows()
    owner.main_action('open',request(refs='R1'))
    child = children[0]
    loaded(child)
    bridge = child.bridge
    assert [name for name in dir(bridge) if not name.startswith('_')] == ['tool_window']
    assert not bridge.tool_window('finish_exit')['ok']
    assert not bridge.tool_window('open',request(kind='settings'))['ok']
    assert not bridge.tool_window('changed',{'refs':'R2'})['ok']
    assert bridge.tool_window('changed')['ok']
    assert 'nativeToolChanged' in lifecycle.window.evaluate_js.call_args.args[0]
    child.get_current_url.return_value = 'https://evil.invalid/'
    assert not bridge.tool_window('changed')['ok']


def test_busy_or_dirty_tool_prevents_host_shutdown_and_restores_page():
    owner, lifecycle, children, _ = windows()
    owner.main_action('open',request(refs='R1'))
    child = children[0]
    loaded(child)
    child.evaluate_js.return_value = False
    lifecycle._stop_backend = Mock()
    assert not lifecycle.finish_exit()['ok']
    lifecycle._stop_backend.assert_not_called()
    child.destroy.assert_not_called()
    assert 'cancelNativeToolExit' in child.evaluate_js.call_args.args[0]
    child.evaluate_js.return_value = True
    assert lifecycle.finish_exit()['ok']
    child.destroy.assert_called_once()


def test_native_close_does_not_discard_dirty_form():
    owner, _, children, _ = windows()
    owner.main_action('open',request(refs='R1'))
    child = children[0]
    loaded(child)
    child.evaluate_js.return_value = False
    assert not child.bridge.tool_window('close')['ok']
    child.hide.assert_not_called()
    child.evaluate_js.return_value = True
    assert child.bridge.tool_window('minimize')['ok']
    assert owner.main_action('restore',{'kind':'footprint'})['ok']
    child.show.assert_called()


@pytest.mark.parametrize('operation', ['check', 'prepare'])
def test_slow_update_does_not_block_tools_but_still_protects_exit(operation):
    owner, lifecycle, children, _ = windows()
    entered, release = threading.Event(), threading.Event()
    lifecycle.updater = Mock(pending=None)
    lifecycle.updater.status.return_value = {'ok':True}
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'ok':True}
    getattr(lifecycle.updater, operation).side_effect = slow
    bridge = DesktopBridge(lifecycle)
    results = []
    worker = threading.Thread(target=lambda:results.append(bridge.plugin_update(operation)))
    worker.start()
    try:
        assert entered.wait(2)
        assert bridge.tool_window('open', request(refs='R1'))['ok']
        assert bridge.tool_window('open', request(kind='settings', rid='settings'))['ok']
        assert bridge.plugin_update('status')['ok']
        assert not bridge.plugin_update('preferences', 'auto')['ok']
        lifecycle.updater.preferences.assert_not_called()
        lifecycle._stop_backend = Mock()
        result = lifecycle.finish_exit()
        assert not result['ok'] and '更新' in result['error']
        lifecycle._stop_backend.assert_not_called()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and results == [{'ok':True}]
    lifecycle.updater.pending = None
    assert lifecycle.finish_exit()['ok']


def test_update_releases_busy_state_on_failure_and_cannot_start_after_exit():
    _, lifecycle, _, _ = windows()
    lifecycle.updater = Mock(pending=None)
    lifecycle.updater.preferences.return_value = {'ok':True}
    lifecycle.updater.check.side_effect = OSError('network failure')
    bridge = DesktopBridge(lifecycle)
    assert not bridge.plugin_update('check')['ok']
    assert bridge.plugin_update('preferences', 'off')['ok']
    lifecycle.allow_close = True
    assert not bridge.plugin_update('prepare')['ok']
    lifecycle.updater.prepare.assert_not_called()

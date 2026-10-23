"""Native window ownership contracts without starting a desktop GUI."""
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_loading_keeps_only_latest_request_for_that_window():
    owner, _, children, _ = windows()
    owner.main_action('open',request(rid='old',refs='R1'))
    owner.main_action('open',request(rid='new',refs='R2'))
    loaded(children[0])
    script = children[0].evaluate_js.call_args.args[0]
    assert 'R2' in script and '"request_id": "new"' in script
    assert '"refs": "R1"' not in script


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

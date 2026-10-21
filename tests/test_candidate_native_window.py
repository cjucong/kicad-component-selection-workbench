"""Native child ownership and graceful exit; no production windows or files."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from part_selection_candidate_window import CandidateBridge, CandidateWindows
from part_selection_lifecycle import DesktopBridge, DesktopLifecycle


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


@pytest.fixture
def manager():
    lifecycle = DesktopLifecycle('http://127.0.0.1:19000/', 'test-owner')
    lifecycle.window = Mock()
    lifecycle.window.get_current_url.return_value = lifecycle.url
    child = Mock()
    child.events = SimpleNamespace(**{name: Event() for name in ('loaded', 'closing', 'minimized', 'restored')})
    child.get_current_url.return_value = lifecycle.url + '?view=candidate'
    child.evaluate_js.return_value = {'ok': True}
    create = Mock(return_value=child)
    manager = CandidateWindows(lifecycle, create)
    lifecycle.candidates = manager
    # Simulate a loaded native WebView without starting the platform GUI.
    original_ensure = manager._ensure_window

    def ready():
        original_ensure()
        for handler in child.events.loaded.handlers:
            handler()

    manager._ensure_window = ready
    return manager, lifecycle, child, create


def test_reuses_child_and_preserves_native_maximize_on_show(manager):
    owner, lifecycle, child, create = manager
    payload = {'ref': 'C1', 'refs': 'C1,C2', 'comment': 'capacitor', 'value': '100nF'}
    for action, value in [('open', payload), ('minimize', None), ('restore', None), ('open', payload)]:
        assert DesktopBridge(lifecycle).candidate_window(action, value)['ok']
    create.assert_called_once()
    assert create.call_args.kwargs['hidden']
    assert isinstance(create.call_args.kwargs['js_api'], CandidateBridge)
    child.hide.assert_called_once()
    child.restore.assert_not_called()
    child.events.minimized.handlers[0]()
    assert owner.main_action('restore')['ok']
    child.restore.assert_called_once()


def test_child_only_exposes_its_restricted_actions(manager):
    owner, lifecycle, child, _ = manager
    assert owner.main_action('open', {'refs': 'U1'})['ok']
    bridge = CandidateBridge(owner)
    assert [name for name in dir(bridge) if not name.startswith('_')] == ['candidate_window']
    assert not bridge.candidate_window('finish_exit')['ok']
    assert not bridge.candidate_window('changed', {'refs': 'U2'})['ok']
    assert bridge.candidate_window('minimize')['ok']
    assert owner._request['refs'] == 'U1'
    assert bridge.candidate_window('changed')['ok']
    assert 'nativeCandidateSaved' in lifecycle.window.evaluate_js.call_args.args[0]
    assert bridge.candidate_window('close')['ok']
    assert not owner.main_action('restore')['ok']
    lifecycle.window.destroy.assert_not_called()


@pytest.mark.parametrize('payload', [None, {}, {'refs': 4}, {'refs': 'C1', 'url': 'https://example.com'}, {'refs': 'X'*16001}])
def test_invalid_payload_never_creates_child(manager, payload):
    owner, _, _, create = manager
    assert not owner.main_action('open', payload)['ok']
    create.assert_not_called()


def test_other_page_cannot_use_bridge(manager):
    owner, lifecycle, child, create = manager
    lifecycle.window.get_current_url.return_value = 'https://example.com'
    assert not owner.main_action('open', {'refs': 'C1'})['ok']
    create.assert_not_called()
    lifecycle.window.get_current_url.return_value = lifecycle.url
    assert owner.main_action('open', {'refs': 'C1'})['ok']
    child.get_current_url.return_value = 'https://example.com'
    assert not owner.child_action('changed')['ok']
    assert not owner.main_action('open', {'refs': 'C2'})['ok']


def test_busy_child_retains_both_windows_and_backend(manager):
    owner, lifecycle, child, _ = manager
    assert owner.main_action('open', {'refs': 'C1'})['ok']
    lifecycle._stop_backend = Mock()
    child.evaluate_js.return_value = False
    assert not lifecycle.finish_exit()['ok']
    lifecycle._stop_backend.assert_not_called()
    child.destroy.assert_not_called()
    lifecycle.window.destroy.assert_not_called()
    assert not lifecycle.allow_close


def test_shutdown_failure_restores_child_and_success_disposes_it(manager):
    owner, lifecycle, child, _ = manager
    assert owner.main_action('open', {'refs': 'C1'})['ok']
    child.evaluate_js.return_value = True
    lifecycle._stop_backend = Mock(side_effect=ValueError('busy service'))
    assert not lifecycle.finish_exit()['ok']
    assert 'cancelNativeCandidateExit' in child.evaluate_js.call_args.args[0]
    child.destroy.assert_not_called()
    lifecycle._stop_backend.side_effect = None
    assert lifecycle.finish_exit()['ok']
    child.destroy.assert_called_once()
    lifecycle.window.destroy.assert_called_once()
    assert owner._on_closing() is True

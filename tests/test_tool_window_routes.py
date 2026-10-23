"""Fixed utility routes and restricted proposals through the public host bridge."""
import pytest

from test_tool_windows import windows, request, loaded


@pytest.mark.parametrize('kind', ['settings','inventory','list-view','field-mapping','plugin-update'])
def test_project_utility_routes_use_empty_fixed_payload(kind):
    owner, _, children, _ = windows()
    assert owner.main_action('open', request(kind=kind))['ok']
    assert f'tool={kind}' in children[0].get_current_url()
    assert not owner.main_action('open',request(kind=kind,path='arbitrary'))['ok']


def test_update_child_can_propose_only_whitelisted_actions_to_main_host():
    owner, lifecycle, children, _ = windows()
    owner.main_action('open',request(kind='plugin-update'))
    child = children[0]; loaded(child)
    assert not child.bridge.tool_window('propose',{'action':'install','request_id':'one'})['ok']
    assert child.bridge.tool_window('propose',{'action':'update-status','request_id':'one'})['ok']
    assert 'nativeToolProposal' in lifecycle.window.evaluate_js.call_args.args[0]
    assert owner.main_action('proposal-result',{'kind':'plugin-update','request_id':'one',
        'result':{'ok':True,'current_version':'test'}})['ok']
    assert 'receiveNativeToolResult' in child.evaluate_js.call_args.args[0]
    assert not owner.main_action('proposal-result',{'kind':'plugin-update','request_id':'one',
        'result':{'ok':True}})['ok'], 'An already delivered proposal cannot replay'
    owner.main_action('open',request(kind='inventory'))
    loaded(children[1])
    assert not children[1].bridge.tool_window('propose',{'action':'update-status','request_id':'two'})['ok']


def test_utility_forms_and_routes_in_offline_chromium():
    from run_tool_windows_smoke import run_checks
    assert '"ok":true' in run_checks()


def test_invalid_host_reply_does_not_consume_pending_proposal():
    owner, _, children, _ = windows()
    owner.main_action('open', request(kind='plugin-update'))
    loaded(children[0])
    assert children[0].bridge.tool_window('propose', {'action':'update-status','request_id':'one'})['ok']
    reply = {'kind':'plugin-update','request_id':'one','result':None}
    assert not owner.main_action('proposal-result',reply)['ok']
    reply['result'] = {'ok':True,'mode':'off'}
    assert owner.main_action('proposal-result',reply)['ok']

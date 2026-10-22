from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import part_selection_pcb_session as module
from part_selection_pcb_session import PcbSession


@pytest.fixture
def transaction(scratch_project, monkeypatch):
    session = PcbSession(scratch_project, socket='ipc://fixture')
    snapshot = {'session_id': 'fixture', 'parts': {'R1': {'uuid': 'a'}}, 'text': 'board', 'revision': 'a'}
    board = SimpleNamespace(begin_commit=Mock(return_value='commit'), update_items=Mock(return_value=['new']),
                            push_commit=Mock(), drop_commit=Mock(), save=Mock(), revert=Mock())
    session.snapshot = Mock(return_value=deepcopy(snapshot))
    board.push_commit.side_effect = lambda *args: session.snapshot.return_value.update(revision='after')
    session._board = Mock(return_value=(Mock(), board, None))
    session.verify = Mock(return_value={'ok': True})
    monkeypatch.setattr(module, 'decode', lambda value: SimpleNamespace(
        id=SimpleNamespace(value='a'), reference_field=SimpleNamespace(text=SimpleNamespace(value='R1'))))
    monkeypatch.setattr(module, 'encode', lambda value: value)
    monkeypatch.setattr(module, 'validate_item_ids', lambda snapshot, prepared: None)
    return session, snapshot, board


@pytest.mark.parametrize('changes', [[], [{'ref': 'R2', 'proto': 'new'}],
    [{'ref': 'R1', 'proto': 'new'}, {'ref': 'R1', 'proto': 'new'}]])
def test_batch_must_exactly_match_preflight_selection(transaction, tmp_path, changes):
    session, snapshot, board = transaction
    with pytest.raises(ValueError, match='选中'):
        session.apply(snapshot, changes, tmp_path / 'backup')
    board.begin_commit.assert_not_called()


def test_success_is_one_commit_without_save(transaction, tmp_path):
    session, snapshot, board = transaction
    result = session.apply(snapshot, [{'ref': 'R1', 'proto': 'new'}], tmp_path / 'backup')
    assert result['ok'] and result['saved'] is False
    board.begin_commit.assert_called_once()
    board.push_commit.assert_called_once()
    board.save.assert_not_called()
    board.revert.assert_not_called()
    assert (tmp_path / 'backup' / 'before.kicad_pcb').read_text() == 'board'


def test_backup_failure_never_begins_mutation(transaction, tmp_path):
    session, snapshot, board = transaction
    (tmp_path / 'backup').write_text('occupied')
    with pytest.raises(FileExistsError):
        session.apply(snapshot, [{'ref': 'R1', 'proto': 'new'}], tmp_path / 'backup')
    board.begin_commit.assert_not_called()


@pytest.mark.parametrize('failure', ['write', 'staged'])
def test_failed_batch_drops_own_unpublished_commit(transaction, tmp_path, failure):
    session, snapshot, board = transaction
    if failure == 'write':
        board.update_items.side_effect = ValueError('write failed')
    else:
        board.update_items.return_value = ['corrupt']
    with pytest.raises(ValueError, match='已撤回'):
        session.apply(snapshot, [{'ref': 'R1', 'proto': 'new'}], tmp_path / 'backup')
    board.drop_commit.assert_called_once_with('commit')
    board.push_commit.assert_not_called()
    board.save.assert_not_called()


def test_stale_source_never_writes(transaction, tmp_path):
    session, snapshot, board = transaction
    session.snapshot.return_value = {**snapshot, 'revision': 'changed'}
    with pytest.raises(ValueError, match='改变'):
        session.apply(snapshot, [{'ref': 'R1', 'proto': 'new'}], tmp_path / 'backup')
    board.begin_commit.assert_not_called()


def test_prepared_uuid_cannot_touch_an_unselected_device(transaction, tmp_path, monkeypatch):
    session, snapshot, board = transaction
    monkeypatch.setattr(module, 'decode', lambda value: SimpleNamespace(
        id=SimpleNamespace(value='unselected'), reference_field=SimpleNamespace(text=SimpleNamespace(value='R1'))))
    with pytest.raises(ValueError, match='身份'):
        session.apply(snapshot, [{'ref': 'R1', 'proto': 'new'}], tmp_path / 'backup')
    board.begin_commit.assert_not_called()


def test_post_commit_readback_failure_never_claims_rollback(transaction, tmp_path):
    session, snapshot, board = transaction
    session.verify.side_effect = ValueError('unexpected post-commit state')
    with pytest.raises(RuntimeError, match='恢复未确认'):
        session.apply(snapshot, [{'ref': 'R1', 'proto': 'new'}], tmp_path / 'backup')
    board.save.assert_not_called()
    board.drop_commit.assert_not_called()


def test_lost_commit_ack_reconciles_live_result_without_replaying(transaction, tmp_path):
    session, snapshot, board = transaction
    session.snapshot.side_effect = [deepcopy(snapshot), deepcopy(snapshot), {**snapshot, 'revision': 'after'}]
    board.push_commit.side_effect = TimeoutError('native reply lost')
    result = session.apply(snapshot, [{'ref': 'R1', 'proto': 'new'}], tmp_path / 'backup')
    assert result['ok'] and result['saved'] is False
    board.update_items.assert_called_once()
    board.drop_commit.assert_not_called()
    board.save.assert_not_called()

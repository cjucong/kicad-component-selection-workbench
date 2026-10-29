"""PCB completion must follow the project, not one computer's runtime cache."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from _scratch import temporary_project
from part_selection_design_status import DesignStatus
from part_selection_runtime import runtime_directory
from part_selection_server import PartSelectionApp, row_filter_keys
from test_design_status import (status_app, current_board, replacement_record,
                                observation, status)


def certify(app, board):
    replacement_record(app)
    current_board(board)
    target = board.parent / 'Local.pretty' / 'New.kicad_mod'
    target.parent.mkdir(exist_ok=True)
    target.write_text('(footprint "New")')
    (board.parent / 'fp-lib-table').write_text('(fp_lib_table (lib (name "Local") '
        '(type "KiCad") (uri "${KIPRJMOD}/Local.pretty")))')
    state = observation(app, board, targets=[{'ref':'U1', 'path':str(target),
        'hash':hashlib.sha256(target.read_bytes()).hexdigest()}])
    assert app.design_status.observe(state)
    return target, state


def test_receipt_is_project_data_and_uses_relative_library_paths(status_app):
    app, board = status_app
    certify(app, board)
    receipt = app.config['bom_dir'] / 'PCB同步核验记录.json'
    assert receipt.is_file(), 'Completion evidence must be included when sharing project data'
    text = receipt.read_text(encoding='utf-8')
    assert str(app.project_root) not in text
    assert str(app.project_root).replace('\\', '\\\\') not in text
    assert 'Local.pretty/New.kicad_mod' in text


@pytest.mark.parametrize('edit', ['none', 'pad', 'library', 'identity', 'intent'])
def test_project_only_copy_preserves_completion_and_detects_real_changes(status_app, edit):
    app, board = status_app
    target, _ = certify(app, board)
    with temporary_project(prefix='relocated-status-') as directory:
        root = Path(directory)
        shutil.copytree(app.project_root, root, dirs_exist_ok=True)
        assert not (runtime_directory(root) / 'design-status.json').exists()
        copied = PartSelectionApp(root / 'settings.json')
        try:
            copied_board = root / board.name
            if edit == 'pad':
                copied_board.write_text(copied_board.read_text()[:-2] +
                    '(pad "1" smd rect (at 1 0) (size 1 1))) )')
            elif edit == 'library':
                (root / target.relative_to(app.project_root)).write_text('(footprint "Changed")')
            elif edit == 'identity':
                copied_board.write_text(copied_board.read_text().replace('"fp"', '"other"'))
            elif edit == 'intent':
                records = json.loads(copied.overrides_path.read_text(encoding='utf-8'))
                records['U1']['footprint_change']['before'] = 'Local:Different'
                copied.overrides_path.write_text(json.dumps(records), encoding='utf-8')
            row = status(copied)
            assert bool(row['pcb_unverified_refs']) is (edit != 'none')
            assert not row['pcb_changed_refs']
        finally:
            copied.design_sync.close()
            copied.search_jobs.close()
            copied.selection_jobs.close()


def test_uncertainty_is_separate_from_actual_sync_differences(status_app):
    app, board = status_app
    replacement_record(app)
    current_board(board)
    row = status(app)
    assert row['pcb_unverified_refs'] == 'U1'
    assert 'needs_verification' in row_filter_keys(row)
    assert 'needs_sync' not in row_filter_keys(row)
    assert 'sync_done' not in row_filter_keys(row)
    board.write_text(board.read_text().replace('"NEW"', '"OLD"'))
    row = status(app)
    assert 'needs_sync' in row_filter_keys(row)


def test_running_workbench_reloads_pulled_receipt_without_restart(status_app):
    app, board = status_app
    replacement_record(app)
    current_board(board)
    earlier = DesignStatus(app)
    assert status(app)['pcb_unverified_refs']
    certify(app, board)
    app.design_status = earlier
    assert not status(app)['pcb_unverified_refs']
    # A removed or conflicted receipt must not be silently reused from memory.
    earlier.evidence_path.write_text('<<<<<<< conflicting Git records', encoding='utf-8')
    assert status(app)['pcb_unverified_refs'] == 'U1'


@pytest.mark.parametrize('valid', [True, False])
def test_legacy_local_evidence_is_migrated_only_after_current_validation(status_app, valid):
    app, board = status_app
    target, state = certify(app, board)
    evidence = deepcopy(app.design_status.evidence)
    evidence['U1']['targets'] = state['targets']
    shared = app.config['bom_dir'] / 'PCB同步核验记录.json'
    if shared.exists():
        shared.unlink()
    legacy = runtime_directory(app.project_root) / 'design-status.json'
    legacy.write_text(json.dumps(evidence), encoding='utf-8')
    if not valid:
        target.write_text('(footprint "Changed")')
    app.design_status = DesignStatus(app)
    row = status(app)
    assert bool(row['pcb_unverified_refs']) is not valid
    assert shared.exists() is valid
    assert legacy.exists(), 'Do not delete the original recovery evidence'


def test_stale_instances_merge_only_their_own_updates(status_app):
    app, board = status_app
    earlier = DesignStatus(app)
    certify(app, board)
    earlier._store({'U2': {'uncertain': 'save reply unknown'}})
    assert set(json.loads(earlier.evidence_path.read_text(encoding='utf-8'))['records']) == {'U1', 'U2'}
    assert not status(app)['pcb_unverified_refs']


@pytest.mark.parametrize('contents', ['<<<<<<< conflict', '{"schema":2,"records":{}}'])
def test_conflicted_or_unsupported_shared_evidence_is_not_overwritten(status_app, contents):
    app, board = status_app
    _, state = certify(app, board)
    app.design_status.evidence_path.write_text(contents, encoding='utf-8')
    assert not app.design_status.observe(state)
    assert app.design_status.evidence_path.read_text(encoding='utf-8') == contents
    assert status(app)['pcb_unverified_refs'] == 'U1'


def test_escaping_project_targets_and_missing_external_library_are_not_guessed(status_app):
    app, board = status_app
    target, _ = certify(app, board)
    original = deepcopy(app.design_status.evidence['U1'])
    for path, scope in [('../outside.kicad_mod', 'project'), (str(target), 'project'),
                        ('Local.pretty/New.kicad_mod', 'external')]:
        evidence = deepcopy(original)
        evidence['targets'][0].update(path=path, scope=scope)
        assert not app.design_status._valid_targets(evidence)
    evidence = deepcopy(original)
    evidence['targets'][0].update(path=str(target.parent / 'missing' / target.name), scope='external')
    assert not app.design_status._valid_targets(evidence)


def test_uncertain_shared_receipt_overrides_old_local_success(status_app):
    app, board = status_app
    _, state = certify(app, board)
    legacy = runtime_directory(app.project_root) / 'design-status.json'
    legacy.write_text(json.dumps(app.design_status.evidence), encoding='utf-8')
    state['items'][0]['kind'] = 'replace'
    assert app.design_status.uncertain(state, 'save reply unknown')
    app.design_status = DesignStatus(app)
    assert 'save reply unknown' in status(app)['pcb_sync_by_ref']['U1']['reason']


def test_seen_shared_record_never_falls_back_to_old_local_success(status_app):
    app, board = status_app
    _, state = certify(app, board)
    legacy = runtime_directory(app.project_root) / 'design-status.json'
    legacy.write_text(json.dumps(app.design_status.evidence), encoding='utf-8')
    assert not status(app)['pcb_unverified_refs']
    state['items'][0]['kind'] = 'replace'
    assert app.design_status.uncertain(state, 'save reply unknown')
    app.design_status.evidence_path.unlink()
    assert status(app)['pcb_unverified_refs'] == 'U1'
    assert not app.design_status.evidence_path.exists()


def test_failed_receipt_write_retains_uncertainty_in_running_workbench(status_app):
    from unittest.mock import patch
    app, board = status_app
    _, state = certify(app, board)
    state['items'][0]['kind'] = 'replace'
    with patch('part_selection_design_status._load_evidence', side_effect=PermissionError('read only')):
        assert not app.design_status.uncertain(state, 'save reply unknown')
    assert 'save reply unknown' in status(app)['pcb_sync_by_ref']['U1']['reason']


@pytest.mark.parametrize('move_directory', [False, True])
def test_settings_save_retains_unpersisted_uncertainty(status_app, move_directory):
    from unittest.mock import patch
    from test_path_settings import payload
    app, board = status_app
    _, state = certify(app, board)
    state['items'][0]['kind'] = 'replace'
    with patch('part_selection_design_status._load_evidence', side_effect=PermissionError('read only')):
        assert not app.design_status.uncertain(state, 'save reply unknown')
    values = {'bom_dir': 'moved'} if move_directory else {'project_name': 'renamed project'}
    app.save_path_settings(payload(app, **values))
    assert 'save reply unknown' in status(app)['pcb_sync_by_ref']['U1']['reason']


def test_legacy_migration_cannot_replace_unpersisted_uncertainty(status_app):
    from unittest.mock import patch
    app, board = status_app
    _, state = certify(app, board)
    legacy = runtime_directory(app.project_root) / 'design-status.json'
    evidence = deepcopy(app.design_status.evidence)
    evidence['U1']['targets'] = state['targets']
    legacy.write_text(json.dumps(evidence), encoding='utf-8')
    app.design_status.evidence_path.unlink()
    app.design_status = DesignStatus(app)
    state['items'][0]['kind'] = 'replace'
    with patch('part_selection_design_status._load_evidence', side_effect=PermissionError('read only')):
        assert not app.design_status.uncertain(state, 'save reply unknown')
    assert 'save reply unknown' in status(app)['pcb_sync_by_ref']['U1']['reason']
    assert not app.design_status.evidence_path.exists()


def test_migrating_other_refs_preserves_remaining_volatile_overlay(status_app):
    from unittest.mock import patch
    app, board = status_app
    _, state = certify(app, board)
    source = deepcopy(state['source']['design'])
    for name in ('fields', 'properties', 'records'):
        source[name]['U2'] = deepcopy(source[name]['U1'])
    source['identity']['paths']['U2'] = source['identity']['paths']['U1']
    legacy_item = deepcopy(app.design_status.evidence['U1'])
    legacy_item.update(source=app.design_status._key(source, 'U2'), targets=state['targets'])
    legacy = runtime_directory(app.project_root) / 'design-status.json'
    legacy.write_text(json.dumps({'U2': legacy_item}), encoding='utf-8')
    app.design_status.evidence_path.unlink()
    app.design_status = DesignStatus(app)
    state['items'][0]['kind'] = 'fields'
    with patch('part_selection_design_status._load_evidence', side_effect=PermissionError('read only')):
        assert not app.design_status.uncertain(state, 'save reply unknown')
    _, parts = app.design_status._read(board.read_bytes())
    parts = {**parts, 'U2':deepcopy(parts['U1'])}
    app.design_status._refresh_evidence(source, parts)
    assert 'U2' in json.loads(app.design_status.evidence_path.read_text(encoding='utf-8'))['records']
    assert app.design_status.evidence['U1']['uncertain'] == 'save reply unknown'


def test_corrupt_receipt_cannot_hide_known_field_and_name_differences(status_app):
    app, board = status_app
    certify(app, board)
    app.design_status.evidence_path.write_text('<<<<<<< conflict', encoding='utf-8')
    board.write_text(board.read_text().replace('"NEW"', '"OLD"'))
    row = status(app)
    assert row['pcb_changed_refs'] == 'U1'
    assert 'needs_sync' in row_filter_keys(row)


def test_relocated_receipt_checks_current_library_binding(status_app):
    app, board = status_app
    target, _ = certify(app, board)
    table = board.parent / 'fp-lib-table'
    table.write_text(table.read_text().replace('${KIPRJMOD}/Local.pretty', target.parent.as_posix()))
    state = observation(app, board, targets=[{'ref':'U1', 'path':str(target),
        'hash':hashlib.sha256(target.read_bytes()).hexdigest()}])
    assert app.design_status.observe(state)
    with temporary_project(prefix='absolute-library-') as directory:
        root = Path(directory)
        shutil.copytree(app.project_root, root, dirs_exist_ok=True)
        target.write_text('(footprint "Changed")')
        copied = PartSelectionApp(root / 'settings.json')
        try:
            assert status(copied)['pcb_unverified_refs'] == 'U1'
        finally:
            copied.design_sync.close()
            copied.search_jobs.close()
            copied.selection_jobs.close()

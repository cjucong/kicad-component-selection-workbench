"""Opt-in real KiCad grouped rename/conversion QA; synthetic conversion input.

Reuses the disposable native probe and never accepts a production project.
No network converter is claimed: only its input module is a fixture; library
publication, schematic sync, live IPC PCB update, and undo are real.
"""
import argparse
from contextlib import ExitStack
import ctypes
from pathlib import Path
import time
from unittest.mock import patch

from run_native_pcb_in_place_probe import run_probe, footprint_map, close_owned_schematic, sync_fixture_adapter, windows
from part_selection_paths import resolve_config
from part_selection_server import PartSelectionApp
from part_selection_pcb_session import encode, decode, footprint_bytes, metadata_replacement
from part_selection_footprint_geometry import _outside_project


def saved_rename_checks(root, report, session, client, board, results):
    """Real save/reopen and service restart must accept legacy/new stroke evidence."""
    from part_selection_footprint_changes import board_copper_hashes
    from part_selection_overrides import load_overrides, save_overrides
    from part_selection_pcb import PcbUpdates
    from part_selection_pcb_session import persisted_footprint_matches
    app = PartSelectionApp(resolve_config(root))
    app.pcb_updates.session = session
    refs = ['R1', 'U1']
    for ref, part in session.snapshot(root, refs)['parts'].items():
        app.save_field_override({'refs':ref,'bom':'__schematic__',
                                'field_overrides':{'footprint':part['footprint']}})
    sync_fixture_adapter(app, refs)
    close_owned_schematic(results['pid'])
    before = session.snapshot(root, refs)
    tracks = [t.proto.SerializeToString() for t in board.get_tracks()]
    try:
        for ref in refs:
            preview = app.row_footprint({'refs':ref,'bom':'__schematic__','mode':'rename'})['preview']
            reference = preview.get('default_reference_id') or preview['default_id']
            app.footprint_choices.library_detail(preview['preview_id'], reference)
            app.row_footprint({'footprint_choice':{'preview_id':preview['preview_id'],
                'source':'rename','existing_id':reference,'target_mode':'existing',
                'target_library':'Local','name':'SavedRename_'+ref,'acknowledged':True,'scope_acknowledged':True}})
        records = load_overrides(app.overrides_path)
        disk = (root/'Probe.kicad_pcb').read_text(encoding='utf-8-sig')
        canonical = board_copper_hashes(disk, refs, include_instance=True, stroke_style='solid')
        assert all(records[ref]['footprint_change']['source']['instance_source_hash'] == canonical[ref] for ref in refs)
        # Recreate evidence issued by old versions; only the hash spelling changes.
        legacy = board_copper_hashes(disk, refs, include_instance=True, stroke_style='default')
        records['R1']['footprint_change']['source']['instance_source_hash'] = legacy['R1']
        save_overrides(app.overrides_path, records)
        sync_fixture_adapter(app, refs)
        close_owned_schematic(results['pid'])
        renamed = session.snapshot(root, refs)
        for ref in refs:
            expected = metadata_replacement(before['parts'][ref], footprint='Local:SavedRename_'+ref)
            assert footprint_bytes(decode(renamed['parts'][ref]['proto'])) == footprint_bytes(decode(expected))
        assert tracks == [t.proto.SerializeToString() for t in board.get_tracks()]
        saved = session.save_verified(renamed, report/'rename-save-verified')
        assert saved['ok'], saved
        for handle, title in windows(results['pid']):
            if 'Probe' in title and 'PCB' in title:
                ctypes.windll.user32.PostMessageW(handle, 0x0010, 0, 0)
        for _ in range(60):
            if not any('Probe' in title and 'PCB' in title for _, title in windows(results['pid'])):
                break
            time.sleep(.25)
        else:
            raise AssertionError('Owned renamed fixture PCB did not close')
        assert session.connection.ensure(open_if_closed=True)['state'] == 'connected'
        reopened = session.snapshot(root, refs)
        for ref in refs:
            assert persisted_footprint_matches(decode(renamed['parts'][ref]['proto']),
                                               decode(reopened['parts'][ref]['proto']))
        restarted = PcbUpdates(app, session=session)
        check = restarted.preview(refs, '__schematic__', alignment='rigid')
        assert not check['blocked'] and all(item['kind'] == 'none' for item in check['items']), check
        assert restarted.apply(check['preview_id'], True)['no_changes']
        assert session.snapshot(root, refs) == reopened, 'Repeated rename modified the reopened fixture'
        results['checks']['legacy_and_canonical_rename_save_reopen_restart_noop'] = True
    finally:
        app.search_jobs.close()
        app.selection_jobs.close()
        app.design_sync.close()


def same_parts(first, second):
    return first.keys()==second.keys() and all(
        footprint_bytes(first[ref])==footprint_bytes(second[ref]) for ref in first)


def locked_checks(root, report, session, client, board, results):
    original = footprint_map(board)
    locked = decode(encode(original['R1']))
    locked.locked = True
    published = None
    baseline = None
    commit = board.begin_commit()
    board.update_items(locked)
    board.push_commit(commit, 'Synthetic locked instance')
    try:
        baseline = footprint_map(board)
        snapshot = session.snapshot(root, ['R1', 'R2'])
        assert snapshot['parts']['R1']['locked']
        fields = {'Value':'locked metadata QA', 'Brand':'synthetic QA'}
        renamed = metadata_replacement(snapshot['parts']['R1'], footprint='Local:LockedRename', fields=fields)
        changes = [{'ref':'R1','proto':renamed,'fields':fields,'kind':'rename'},
                   {'ref':'R2','proto':snapshot['parts']['R2']['proto'],'fields':{},'kind':'none'}]
        result = session.apply(snapshot, changes, report/'locked-rename-transaction', schematic_close_verified=True)
        assert result['ok']
        published = footprint_map(board)
        assert published['R1'].locked
        assert footprint_bytes(published['R1']) == footprint_bytes(decode(renamed))
        assert all(footprint_bytes(published[r]) == footprint_bytes(baseline[r]) for r in baseline if r != 'R1')
        results['checks']['locked_read_rename_fields_and_unchanged_batch_real_ipc'] = True
        current = session.snapshot(root,['R1'])
        bad = decode(current['parts']['R1']['proto'])
        bad.proto.position.x_nm += 1
        try:
            session.apply(current,[{'ref':'R1','proto':encode(bad),'fields':{},'kind':'fields'}],
                report/'must-not-write-locked',schematic_close_verified=True)
        except ValueError as exc:
            assert 'R1' in str(exc)
        else:
            raise AssertionError('Locked movement was accepted')
        assert session.snapshot(root,['R1']) == current
        assert not (report/'must-not-write-locked').exists()
        results['checks']['locked_geometry_rejected_before_native_mutation'] = True
    finally:
        current = footprint_map(board)
        if published is not None and same_parts(current, published):
            client.run_action('common.Interactive.undo');time.sleep(.25)
            current = footprint_map(board)
        if baseline is not None and same_parts(current, baseline):
            client.run_action('common.Interactive.undo');time.sleep(.25)
        assert same_parts(footprint_map(board),original), 'Locked QA did not restore fixture'
    results['checks']['locked_rename_single_undo_preserves_previous_edits'] = True


def grouped_checks(root, report, session, client, board, results):
    close_owned_schematic(results['pid'])
    locked_checks(root, report, session, client, board, results)
    schematic = root/'Parts.kicad_sch'
    schematic.write_text(schematic.read_text(encoding='utf-8').replace(
        'Local:Original_R2', 'Local:Original_R1'), encoding='utf-8')
    app = PartSelectionApp(resolve_config(root))
    app.pcb_updates.session = session
    paths = [root/'Parts.kicad_sch', root/'Probe.kicad_pcb', app.overrides_path, root/'fp-lib-table']
    files = {p: p.read_bytes() if p.exists() else None for p in paths}
    aliases = [root/'Local.pretty'/(name+'.kicad_mod') for name in ('GroupedRename','GroupedConverted')]
    original = footprint_map(board)
    changed = decode(encode(original['R2']))
    changed.definition.pads[1].proto.position.x_nm += 10
    assert changed.proto!=original['R2'].proto
    baseline = None
    published = None
    tracks = [t.proto.SerializeToString() for t in board.get_tracks()]

    def restore_files():
        try:
            close_owned_schematic(results['pid'])
        finally:
            for path, content in files.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)

    def restore_board():
        current = footprint_map(board)
        if same_parts(current, original):
            return
        if published is not None and same_parts(current, published):
            client.run_action('common.Interactive.undo');time.sleep(.25)
            current = footprint_map(board)
        if baseline is None or not same_parts(current, baseline):
            (report/'uncertain-cleanup.kicad_pcb').write_text(board.get_as_string(),encoding='utf-8')
            raise AssertionError('Unknown fixture state; did not blindly undo another batch')
        client.run_action('common.Interactive.undo');time.sleep(.25)
        assert same_parts(footprint_map(board), original), 'Coordinate injection undo failed'

    def remove_aliases():
        for alias in aliases:
            alias.unlink(missing_ok=True)

    def convert(importer, code, progress=None):
        folder = importer.root/'Libraries/DSMU/LCSC'/code
        pretty = folder/f'LCSC_{code}.pretty'
        pretty.mkdir(parents=True)
        (pretty/'Converted.kicad_mod').write_bytes((root/'Local.pretty/Standard_R1.kicad_mod').read_bytes())
        (folder/f'LCSC_{code}.kicad_sym').write_text('(kicad_symbol_lib (version 20241209) '
            f'(generator test) (symbol "Part" (property "Footprint" "LCSC_{code}:Converted")))', encoding='utf-8')
        return {'footprint':f'LCSC_{code}:Converted','symbol':f'LCSC_{code}:Part','c_code':code}

    with ExitStack() as cleanup:
        cleanup.callback(remove_aliases)
        cleanup.callback(restore_files)
        cleanup.callback(restore_board)
        cleanup.callback(app.search_jobs.close)
        cleanup.callback(app.selection_jobs.close)
        cleanup.callback(app.design_sync.close)
        commit = board.begin_commit()
        try:
            board.update_items(changed)
            board.push_commit(commit, 'Synthetic ten nanometer coordinate history')
        except BaseException:
            board.drop_commit(commit)
            raise
        baseline = footprint_map(board)
        assert not same_parts(baseline, original), 'Coordinate injection did not reach live PCB'
        # Saved PCB extraction must see the tiny delta, not the schematic library.
        board.save()
        for mode, name in [('rename','GroupedRename'), ('convert','GroupedConverted')]:
            # Restore only fixture workbench/schematic data between independent paths.
            # Keep the intentionally rounded saved PCB as the extraction baseline.
            for path in paths:
                if path.suffix == '.kicad_pcb':
                    continue
                content = files[path]
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            with patch('part_selection_footprints.LibraryImporter.convert', autospec=True, side_effect=convert):
                preview = app.row_footprint({'refs':'R1','bom':'__schematic__','mode':mode})['preview']
            assert preview['scope']['refs'] == ['R1','R2'], preview['scope']
            reference = preview.get('default_reference_id') or preview['default_id']
            if mode == 'convert':
                detail = app.footprint_choices.comparison(preview['preview_id'], reference)
                assert detail['alignment']['ok'], detail
            else:
                app.footprint_choices.library_detail(preview['preview_id'], reference)
            saved = app.row_footprint({'footprint_choice':{'preview_id':preview['preview_id'],
                'source':'converted' if mode=='convert' else 'rename','existing_id':reference,
                'target_mode':'existing','target_library':'Local','name':name,'acknowledged':True,
                'scope_acknowledged':True}})
            assert saved['ok'], saved
            sync_fixture_adapter(app, ['R1','R2'])
            after = footprint_map(board)
            published = after
            for ref in ('R1','R2'):
                old, new = baseline[ref], after[ref]
                assert str(new.definition.id)=='Local:'+name
                assert new.id==old.id and new.proto.symbol_path==old.proto.symbol_path
                assert new.layer==old.layer
                old_pads = {p.number:p for p in old.definition.pads}
                for pad in new.definition.pads:
                    prior = old_pads[pad.number]
                    assert pad.proto.net==prior.proto.net and pad.id==prior.id
                    assert max(abs(pad.position.x-prior.position.x), abs(pad.position.y-prior.position.y))<=20
                if mode=='rename':
                    expected=decode(encode(old))
                    expected.definition.id.library='Local';expected.definition.id.name=name
                    assert footprint_bytes(new)==footprint_bytes(expected), 'Rename changed instance geometry'
            assert all(after[r].proto==baseline[r].proto for r in baseline if r not in ('R1','R2'))
            assert tracks==[t.proto.SerializeToString() for t in board.get_tracks()]
            results['checks']['grouped_'+mode+'_ten_nm_real_adapter_sync_and_pcb']=True
            if mode == 'convert':
                from part_selection_pcb import PcbUpdates
                close_owned_schematic(results['pid'])
                lock_copy = decode(encode(after['R1']))
                lock_copy.locked = True
                commit = board.begin_commit()
                board.update_items(lock_copy)
                board.push_commit(commit, 'Synthetic completed locked replacement')
                try:
                    current = footprint_map(board)
                    restarted = PcbUpdates(app, session=session)
                    check = restarted.preview(['R1','R2'], '__schematic__', alignment='rigid')
                    assert not check['blocked'], check
                    assert all(item['kind'] == 'none' for item in check['items']), check
                    assert restarted.apply(check['preview_id'], True)['no_changes']
                    assert same_parts(footprint_map(board), current)
                    results['checks']['locked_completed_geometry_after_restart_is_noop'] = True
                finally:
                    client.run_action('common.Interactive.undo');time.sleep(.25)
                    assert same_parts(footprint_map(board), after)
            client.run_action('common.Interactive.undo');time.sleep(.25)
            assert same_parts(footprint_map(board), baseline)
            published = None
            close_owned_schematic(results['pid'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir',type=Path,required=True)
    args=parser.parse_args()
    report=args.report_dir.resolve()
    if not args.report_dir.is_absolute() or report.is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error('Report directory must be absolute and outside source')
    try:
        _outside_project(report)
    except ValueError as exc:
        parser.error(str(exc))
    report.mkdir(parents=True,exist_ok=True)
    raise SystemExit(run_probe(report,extra_checks=grouped_checks,after_reopen_checks=saved_rename_checks))

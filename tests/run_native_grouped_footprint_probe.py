"""Opt-in real KiCad grouped rename/conversion QA; synthetic conversion input.

Reuses the disposable native probe and never accepts a production project.
No network converter is claimed: only its input module is a fixture; library
publication, schematic sync, live IPC PCB update, and undo are real.
"""
import argparse
from contextlib import ExitStack
from pathlib import Path
import time
from unittest.mock import patch

from run_native_pcb_in_place_probe import run_probe, footprint_map, close_owned_schematic
from run_native_design_sync_probe import finished
from part_selection_paths import resolve_config
from part_selection_server import PartSelectionApp
from part_selection_pcb_session import encode, decode, footprint_bytes
from part_selection_footprint_geometry import _outside_project


def same_parts(first, second):
    return first.keys()==second.keys() and all(
        footprint_bytes(first[ref])==footprint_bytes(second[ref]) for ref in first)


def grouped_checks(root, report, session, client, board, results):
    close_owned_schematic(results['pid'])
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
                preview = app.row_footprint({'refs':'R1,R2','bom':'__schematic__','mode':mode})['preview']
            reference = preview.get('default_reference_id') or preview['default_id']
            if mode == 'convert':
                detail = app.footprint_choices.comparison(preview['preview_id'], reference)
                assert detail['alignment']['ok'], detail
            else:
                app.footprint_choices.library_detail(preview['preview_id'], reference)
            saved = app.row_footprint({'footprint_choice':{'preview_id':preview['preview_id'],
                'source':'converted' if mode=='convert' else 'rename','existing_id':reference,
                'target_mode':'existing','target_library':'Local','name':name,'acknowledged':True}})
            assert saved['ok'], saved
            preview = app.design_sync.preview({'refs':['R1','R2'],'destination':'both','alignment':'rigid'})
            acknowledgments = {'geometry':True}
            job = finished(app.design_sync, app.design_sync.start(preview['preview_id'], acknowledgments))
            assert job['stage']=='awaiting_pcb_confirmation', job
            assert job['phase_results']['schematic']['schematic_verify']['ok'], job
            done = finished(app.design_sync, app.design_sync.continue_pcb(
                job['job_id'], job['pcb_preview']['preview_id'], acknowledgments))
            assert done['stage']=='complete', done
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
            results['checks']['grouped_'+mode+'_ten_nm_real_sync_and_pcb']=True
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
    raise SystemExit(run_probe(report,extra_checks=grouped_checks))

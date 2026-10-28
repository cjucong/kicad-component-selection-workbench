"""Real disposable KiCad cycle plus annotation-only completion without writes."""
import argparse
from pathlib import Path
from uuid import uuid4
from run_native_pcb_in_place_probe import run_probe, footprint_map, sync_fixture_adapter, close_owned_schematic
from part_selection_paths import resolve_config
from part_selection_server import PartSelectionApp
from part_selection_pcb_session import encode, decode
from part_selection_overrides import load_overrides, save_overrides


def completion_checks(root, report, session, client, board, results):
    from kipy.board_types import BoardShape
    from kipy.proto.board.board_types_pb2 import BoardLayer
    app=PartSelectionApp(resolve_config(root))
    app.pcb_updates.session=session
    refs=['R1','U1']
    try:
        current=session.snapshot(root,refs)
        for ref,part in current['parts'].items():
            app.save_field_override({'refs':ref,'bom':'__schematic__',
                'field_overrides':{'footprint':part['footprint']}})
        records=load_overrides(app.overrides_path)
        for ref in refs:
            records[ref]['footprint_change']={'schema':1,'operation':'replace',
                'before':'Local:Previous','after':current['parts'][ref]['footprint'],'source':{},'target':{}}
        save_overrides(app.overrides_path,records)
        sync_fixture_adapter(app,refs)
        close_owned_schematic(results['pid'])
        changed=[]
        for ref,fp in footprint_map(board).items():
            if ref not in refs:continue
            fp=decode(encode(fp))
            shape=BoardShape();shape.proto.id.value=str(uuid4());shape.layer=BoardLayer.BL_F_SilkS
            shape.proto.shape.segment.start.x_nm=fp.position.x
            shape.proto.shape.segment.start.y_nm=fp.position.y
            shape.proto.shape.segment.end.x_nm=fp.position.x+500000
            shape.proto.shape.segment.end.y_nm=fp.position.y
            fp.definition.add_item(shape);changed.append(fp)
        commit=board.begin_commit()
        board.update_items(changed);board.push_commit(commit,'Synthetic board silk customization')
        board.save()
        before=session.snapshot(root,refs)
        disk=(root/'Probe.kicad_pcb').read_bytes()
        preview=app.pcb_updates.preview(refs,'__schematic__',alignment='center')
        assert all(item['kind']=='none' for item in preview['items']),preview
        assert all(item.get('retained_board_annotations') for item in preview['items']),preview
        assert app.pcb_updates.apply(preview['preview_id'],True)['no_changes']
        assert session.snapshot(root,refs)==before
        assert (root/'Probe.kicad_pcb').read_bytes()==disk
        results['checks']['annotation_only_completion_keeps_full_live_and_disk_content']=True
    finally:
        app.selection_jobs.close();app.search_jobs.close();app.design_sync.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir',type=Path,required=True)
    args=parser.parse_args()
    report=args.report_dir.resolve()
    if not args.report_dir.is_absolute() or report.is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error('Report directory must be absolute and outside the source tree')
    report.mkdir(parents=True,exist_ok=True)
    raise SystemExit(run_probe(report,after_reopen_checks=completion_checks))

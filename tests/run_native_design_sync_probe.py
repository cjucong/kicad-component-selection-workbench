"""Real typed design-sync metadata and rename gate, followed by native geometry QA."""
import argparse
from copy import deepcopy
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import time

from run_native_pcb_in_place_probe import run_probe, footprint_map, windows, close_owned_schematic
from part_selection_paths import resolve_config
from part_selection_server import PartSelectionApp


def finished(sync, job):
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        result=sync.status(job['job_id'])
        if result['done']: return result
        time.sleep(.1)
    raise AssertionError('Design sync timed out: '+json.dumps(result,ensure_ascii=False))


def open_owned_pcb_update_dialog(pid):
    user = ctypes.windll.user32
    user.GetMenu.argtypes = [wintypes.HWND]
    user.GetMenu.restype = wintypes.HMENU
    user.GetSubMenu.argtypes = [wintypes.HMENU, ctypes.c_int]
    user.GetSubMenu.restype = wintypes.HMENU
    user.GetMenuItemCount.argtypes = [wintypes.HMENU]
    user.GetMenuItemID.argtypes = [wintypes.HMENU, ctypes.c_int]
    user.GetMenuItemID.restype = wintypes.UINT
    user.GetMenuStringW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT]
    def find(menu):
        for index in range(user.GetMenuItemCount(menu)):
            child = user.GetSubMenu(menu,index)
            if child:
                result = find(child)
                if result is not None: return result
            label = ctypes.create_unicode_buffer(512)
            user.GetMenuStringW(menu,index,label,len(label),0x400)
            if '原理图' in label.value and 'PCB' in label.value and '更新' in label.value:
                return user.GetMenuItemID(menu,index)
        return None
    editors = [(handle,title) for handle,title in windows(pid) if 'PCB' in title and '编辑器' in title]
    assert len(editors)==1, editors
    handle,_ = editors[0]
    command = find(user.GetMenu(handle))
    assert command is not None, 'Native PCB update command missing'
    assert user.PostMessageW(handle,0x0111,command,0)


def metadata_checks(root, report, session, client, board, results):
    from kipy.client import KiCadClient
    send=KiCadClient.send
    transport_events=[]
    def traced_send(native, command, response_type):
        started=time.perf_counter()
        event={'command':type(command).__name__,'socket':native._socket_path,
               'transport':id(getattr(native,'_conn',None))}
        try:
            return send(native,command,response_type)
        except Exception as exc:
            event['error']=repr(exc)
            event['windows']=windows(results['pid'])
            raise
        finally:
            event['ms']=round((time.perf_counter()-started)*1000,1)
            transport_events.append(event)
    KiCadClient.send=traced_send
    app=PartSelectionApp(resolve_config(root))
    app.pcb_updates.session=session
    apply=app.apply_design_pcb
    def traced_apply(token):
        try:
            return apply(token)
        except Exception:
            import traceback
            (report/'pcb-apply-error.log').write_text(traceback.format_exc(),encoding='utf-8')
            raise
    app.apply_design_pcb=traced_apply
    paths=[root/'Parts.kicad_sch',app.overrides_path,root/'fp-lib-table']
    original_files={p:p.read_bytes() if p.exists() else None for p in paths}
    alias=root/'Local.pretty'/'Renamed_R1.kicad_mod'
    before=footprint_map(board)
    tracks=[t.proto.SerializeToString() for t in board.get_tracks()]
    read_library=session.read_library
    calls=[]
    def counted(*args,**kwargs):
        calls.append(args)
        return read_library(*args,**kwargs)
    session.read_library=counted
    modal=None
    try:
        app.save_override({'refs':'R1','field_overrides':{
            'value':'22k, 1%','comment':'resistor','description':'probe material','description_c_code':'C12345'}})
        started=time.perf_counter()
        preview=app.design_sync.preview({'refs':['R1'],'destination':'both'})
        job=finished(app.design_sync,app.design_sync.start(preview['preview_id'],{}))
        assert job['stage']=='awaiting_pcb_confirmation',job
        assert job['phase_results']['schematic']['schematic_verify']['ok']
        assert all(item['kind']=='fields' for item in job['pcb_preview']['items']),job
        assert calls==[], 'Field-only preflight loaded library geometry'
        done=finished(app.design_sync,app.design_sync.continue_pcb(job['job_id'],job['pcb_preview']['preview_id'],{}))
        assert done['stage']=='complete',done
        after=footprint_map(board)
        assert after['R1'].value_field.text.value=='22k, 1%'
        assert after['R1'].description_field.text.value=='probe material'
        from kipy.board_types import Field
        from part_selection_pcb_session import footprint_bytes, decode, encode
        # The parser can reorder top-level definition items; compare their full multiset.
        expected_instance=decode(encode(before['R1']))
        expected_instance.value_field.text.value='22k, 1%'
        expected_instance.description_field.text.value='probe material'
        for item in expected_instance.definition.items:
            if isinstance(item,Field) and item.name=='Comment': item.text.value='resistor'
        if footprint_bytes(after['R1'])!=footprint_bytes(expected_instance):
            from google.protobuf.json_format import MessageToDict
            (report/'metadata-comparison.json').write_text(json.dumps({
                'before':MessageToDict(before['R1'].proto),
                'expected':MessageToDict(expected_instance.proto),
                'actual':MessageToDict(after['R1'].proto),
            },ensure_ascii=False,indent=2),encoding='utf-8')
        assert footprint_bytes(after['R1'])==footprint_bytes(expected_instance)
        assert all(after[ref].proto==before[ref].proto for ref in before if ref!='R1')
        assert tracks==[t.proto.SerializeToString() for t in board.get_tracks()]
        client.run_action('common.Interactive.undo');time.sleep(.2)
        assert footprint_bytes(footprint_map(board)['R1'])==footprint_bytes(before['R1'])
        results['checks']['native_fields_only_and_single_undo']=True
        results.setdefault('timings_ms',{})['fields_both']=round((time.perf_counter()-started)*1000,1)
        close_owned_schematic(results['pid'])
        for path,content in original_files.items():
            if content is None: path.unlink(missing_ok=True)
            else: path.write_bytes(content)

        # Actual service-issued rename evidence comes from the saved PCB instance.
        preview=app.row_footprint({'refs':'R1','bom':'__schematic__','mode':'rename'})['preview']
        reference=preview['default_id']
        app.footprint_choices.library_detail(preview['preview_id'],reference)
        app.row_footprint({'footprint_choice':{'preview_id':preview['preview_id'],'source':'rename',
            'existing_id':reference,'acknowledged':True,'target_mode':'existing',
            'target_library':'Local','name':'Renamed_R1'}})
        assert alias.is_file()
        preview=app.design_sync.preview({'refs':['R1'],'destination':'both'})
        job=finished(app.design_sync,app.design_sync.start(preview['preview_id'],{}))
        assert job['stage']=='awaiting_pcb_confirmation',job
        assert job['pcb_preview']['items'][0]['kind']=='rename',job
        assert calls==[], 'Rename loaded library geometry'
        done=finished(app.design_sync,app.design_sync.continue_pcb(job['job_id'],job['pcb_preview']['preview_id'],{}))
        assert done['stage']=='complete',done
        renamed=footprint_map(board)['R1']
        assert str(renamed.definition.id)=='Local:Renamed_R1'
        expected_instance=decode(encode(before['R1']))
        expected_instance.definition.id.library='Local';expected_instance.definition.id.name='Renamed_R1'
        assert footprint_bytes(renamed)==footprint_bytes(expected_instance),'Rename changed instance geometry/presentation'
        client.run_action('common.Interactive.undo');time.sleep(.2)
        assert footprint_bytes(footprint_map(board)['R1'])==footprint_bytes(before['R1'])
        results['checks']['native_rename_keeps_complete_instance']=True

        # Hold an actual KiCad modal open: schematic success must not replay on retry.
        # Old KiCad RunAction has no document routing either: dispatch the real
        # menu command only to this owned fixture PCB, never the user's editor.
        open_owned_pcb_update_dialog(results['pid'])
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            pid=results['pid']
            matches=[handle for handle,title in windows(pid) if '原理图' in title and 'PCB' in title and '编辑器' not in title]
            if matches: modal=matches[0];break
            time.sleep(.1)
        assert modal, 'Real KiCad update dialog did not open'
        preview=app.design_sync.preview({'refs':['R1'],'destination':'both'})
        job=finished(app.design_sync,app.design_sync.start(preview['preview_id'],{}))
        assert job['stage']=='partial' and job['phase_results']['schematic']['ok'],job
        source_after={p:p.read_bytes() for p in root.glob('*.kicad_sch')}
        ctypes.windll.user32.PostMessageW(modal,0x0010,0,0);modal=None
        time.sleep(.4)
        retry=app.design_sync.preview({'refs':['R1'],'destination':'pcb'})
        done=finished(app.design_sync,app.design_sync.continue_pcb(job['job_id'],retry['preview_id'],{}))
        assert done['stage']=='complete',done
        assert source_after=={p:p.read_bytes() for p in root.glob('*.kicad_sch')}, 'PCB retry rewrote schematic'
        assert done['phase_results']['schematic']==job['phase_results']['schematic']
        client.run_action('common.Interactive.undo');time.sleep(.2)
        results['checks']['native_modal_partial_and_pcb_only_retry']=True
        assert tracks==[t.proto.SerializeToString() for t in board.get_tracks()]
    finally:
        KiCadClient.send=send
        (report/'metadata-transport.json').write_text(json.dumps(transport_events,ensure_ascii=False,indent=2),encoding='utf-8')
        if modal: ctypes.windll.user32.PostMessageW(modal,0x0010,0,0)
        session.read_library=read_library
        app.design_sync.close();app.search_jobs.close();app.selection_jobs.close()
        close_owned_schematic(results['pid'])
        for path,content in original_files.items():
            if content is None: path.unlink(missing_ok=True)
            else: path.write_bytes(content)
        alias.unlink(missing_ok=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir',type=Path,required=True)
    args=parser.parse_args()
    report=args.report_dir.resolve()
    if report.is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error('Report directory must be outside source')
    report.mkdir(parents=True,exist_ok=True)
    raise SystemExit(run_probe(report,extra_checks=metadata_checks))

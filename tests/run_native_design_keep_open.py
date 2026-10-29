"""Disposable real design-sync run: save verified PCB without restarting it."""
import argparse
from contextlib import ExitStack
from copy import deepcopy
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import traceback
from unittest.mock import patch
from uuid import uuid4

from _scratch import temporary_project
from run_native_project_cycle_probe import owned_managers, OwnedProcess, FLAGS, finished
from run_native_pcb_in_place_probe import ROOT, close_fixture, close_fixture_helpers, windows, check_replacement


def run(report, *, with_footprints=False):
    from part_selection_footprint_geometry import _find_kicad_python
    from part_selection_paths import resolve_config
    from part_selection_server import PartSelectionApp
    from part_selection_pcb_session import decode, footprint_bytes, outside_parts
    result = {'passed':False}
    app = None
    owned = None
    processes = {}
    def own(pid):
        process = OwnedProcess(pid)
        from ctypes import wintypes
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        if not kernel.GetProcessTimes(process.handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise ctypes.WinError(ctypes.get_last_error())
        process.probe_started = ((created.dwHighDateTime << 32) | created.dwLowDateTime) / 10000000 - 11644473600
        return process
    with ExitStack() as stack:
        root = Path(stack.enter_context(temporary_project(prefix='pcb-probe-')))
        ipc = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='keep-ipc-',dir=root.anchor)))
        settings = root.parent/'isolated-config'/'10.0'
        settings.mkdir(parents=True)
        for source in (Path(os.environ['APPDATA'])/'kicad/10.0').iterdir():
            if source.is_file() and (source.suffix=='.json' or source.name.endswith('lib-table')):
                shutil.copy2(source, settings/source.name)
        common = settings/'kicad_common.json'
        config = json.loads(common.read_text(encoding='utf-8'))
        config.setdefault('api',{})['enable_server']=True
        config.setdefault('auto_backup',{})['enabled']=False
        config.setdefault('session',{})['remember_open_files']=False
        common.write_text(json.dumps(config),encoding='utf-8')
        stack.enter_context(patch.dict(os.environ,{'KICAD_CONFIG_HOME':str(settings.parent),
            'KICAD_PART_SELECTION_CACHE':str(ipc/'cache'),'TEMP':str(ipc),'TMP':str(ipc)}))
        native = _find_kicad_python()
        subprocess.run([str(native),'-B',str(ROOT/'tests/run_native_pcb_in_place_probe.py'),
                        '--build',str(root),'--save-batch'],check=True,capture_output=True,timeout=60,**FLAGS)
        for lock in root.glob('~Probe.*.lck'):
            lock.unlink()
        startup=subprocess.STARTUPINFO()
        startup.dwFlags=subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow=1
        subprocess.Popen([str(native.parent/'kicad.exe'),str(root/'Probe.kicad_pro')],startupinfo=startup,**FLAGS)
        try:
            deadline=time.monotonic()+40
            while time.monotonic()<deadline:
                pids=owned_managers(root)
                if len(pids)==1:
                    if owned is None:
                        owned=own(pids[0])
                        processes[owned.pid]=owned
                    window_list=windows(pids[0])
                    warnings=[handle for handle,title in window_list if title=='文件打开警告']
                    for handle in warnings:
                        ctypes.windll.user32.PostMessageW(handle,0x0111,5104,0)
                    if not warnings and any(title.startswith('Probe ') and 'KiCad' in title for _,title in window_list):
                        break
                time.sleep(.3)
            pids=owned_managers(root)
            assert len(pids)==1,pids
            assert owned is not None
            result['owned_pid']=owned.pid
            result['initial_windows']=[{'handle':handle,'title':title,
                'visible':bool(ctypes.windll.user32.IsWindowVisible(handle))} for handle,title in windows(owned.pid)]
            print(json.dumps(result['initial_windows'],ensure_ascii=False),flush=True)
            print('fixture manager PID '+str(owned.pid),flush=True)
            app=PartSelectionApp(resolve_config(root))
            run_script=app.run_kicad_script
            def tracked_script(*args, **kwargs):
                try:
                    return run_script(*args, **kwargs)
                finally:
                    for pid in owned_managers(root):
                        if pid not in processes:
                            processes[pid]=own(pid)
            app.run_kicad_script=tracked_script
            session=app.pcb_updates.session
            session.socket='ipc://'+str(ipc/'kicad/api.sock')
            refs=['R1','R2','U1']+[f'R{i}' for i in range(3,23)]
            before=session.snapshot(root,refs)
            board=session._board()[1]
            dirty=next(fp for fp in board.get_footprints() if fp.reference_field.text.value=='R1')
            dirty=deepcopy(dirty)
            dirty.value_field.text.value='stale PCB value'
            from kipy.board_types import BoardShape, FootprintInstance
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from part_selection_pcb_session import encode
            polygon=BoardShape();polygon.proto.id.value=str(uuid4())
            polygon.proto.parent.value=dirty.id.value
            polygon.proto.layer=BoardLayer.BL_Dwgs_User
            polygon.proto.shape.attributes.CopyFrom(next(item for item in dirty.definition.items
                if isinstance(item,BoardShape)).proto.shape.attributes)
            outline=polygon.proto.shape.polygon.polygons.add().outline
            outline.closed=True
            for x,y in [(100000,100000),(200000,100000),(200000,200000),(100000,200000)]:
                point=outline.nodes.add().point;point.x_nm=x;point.y_nm=y
            dirty.definition.add_item(polygon)
            board.update_items(dirty)
            live_before=session.snapshot(root,refs)
            import part_selection_pcb as updates
            original_metadata=updates.metadata_replacement
            def redundant_closure(*args, **kwargs):
                prototype=original_metadata(*args, **kwargs)
                if args[0]['ref']!='R1':return prototype
                native=deepcopy(decode(prototype).proto)
                wrapped=FootprintInstance(native)
                drawing=next(item for item in wrapped.definition.items
                    if isinstance(item,BoardShape) and item.proto.shape.HasField('polygon'))
                line=drawing.proto.shape.polygon.polygons[0].outline
                if line.nodes[-1]!=line.nodes[0]:line.nodes.add().CopyFrom(line.nodes[0])
                return encode(wrapped)
            with patch.object(updates,'metadata_replacement',redundant_closure):
                preview=app.design_sync.preview({'refs':refs,'destination':'design'})
            assert not preview['schematic_changes'],preview['schematic_changes']
            assert preview['automatic'] and not preview['blocked'],preview
            job=app.design_sync.start(preview['preview_id'],{})
            deadline=time.monotonic()+180
            last=''
            while time.monotonic()<deadline:
                state=app.design_sync.status(job['job_id'])
                if state['stage']!=last:
                    print(state['stage']+': '+state['message'],flush=True)
                    last=state['stage']
                if state['done']:break
                time.sleep(.2)
            result['job']=state
            assert state['stage']=='complete',state
            pcb=state['phase_results']['pcb']
            assert pcb['saved'] and pcb['editors_preserved'] and not pcb['reopened'],pcb
            after=session.snapshot(root,refs)
            assert after['session_id']==live_before['session_id']
            assert owned_managers(root)==pids
            changed=app.pcb_updates.previews[preview['pcb_preview']['preview_id']]
            assert changed['refs']==['R1'],changed['refs']
            prepared=decode(changed['replacements'][0]['proto'])
            staged=decode(after['parts']['R1']['proto'])
            def polygon_nodes(fp):
                return next(item for item in fp.definition.items if isinstance(item,BoardShape)
                    and item.proto.shape.HasField('polygon')).proto.shape.polygon.polygons[0].outline.nodes
            assert len(polygon_nodes(prepared))==5 and len(polygon_nodes(staged))==4
            assert encode(prepared)!=encode(staged) and footprint_bytes(prepared)==footprint_bytes(staged)
            result['native_closed_polygon_roundtrip']=True
            for ref,part in before['parts'].items():
                current=after['parts'][ref]
                for key in ('uuid','footprint','position_mm','rotation_deg','back','pads','symbol_path'):
                    assert current[key]==part[key],(ref,key)
            assert 'stale PCB value' not in after['text']
            result.update(functional_passed=True, same_native_session=True,
                          actual_changed_refs=changed['refs'], production_written=False)
            if with_footprints:
                saves=[]
                transaction_snapshots=[]
                native_sync=app.sync_design_pcb
                def keep_open_sync(*args, **kwargs):
                    original=session.snapshot(root,['R1'])
                    transaction_snapshots.append(deepcopy(app.pcb_updates.previews[args[0]]['snapshot']))
                    saved=native_sync(*args, **kwargs)
                    current=session.snapshot(root,['R1'])
                    assert original['session_id']==current['session_id']
                    assert saved['saved'] and saved['editors_preserved'] and not saved['reopened']
                    saves.append(saved)
                    return saved
                app.sync_design_pcb=keep_open_sync
                for mode, target in [('rename','Local:Renamed_R1'),('library','Local:Standard_R1'),
                                     ('manual','Local:Managed_R1')]:
                    original=session.snapshot(root,['R1'])
                    if mode=='manual':
                        (report/'manual-before-live.kicad_pcb').write_text(original['text'],encoding='utf-8')
                        shutil.copy2(root/'Probe.kicad_pcb',report/'manual-before-disk.kicad_pcb')
                    choice=app.row_footprint({'refs':'R1','bom':'__schematic__',
                                             'mode':'library' if mode=='manual' else mode})['preview']
                    reference=choice['default_id'] if mode=='rename' else 'Local:Standard_R1'
                    app.footprint_choices.library_detail(choice['preview_id'],reference)
                    selection={'preview_id':choice['preview_id'], 'existing_id':reference,
                               'source':'rename' if mode=='rename' else 'existing','acknowledged':True}
                    if mode=='rename':
                        selection.update(target_mode='existing',target_library='Local',name='Renamed_R1')
                    elif mode=='manual':
                        selection.update(target_mode='existing',target_library='Local',name='Managed_R1',
                                         mapping={'schema':1,'pairs':[{'source':n,'target':n,'align':True}
                                                                      for n in ['1','2']], 'dropped':[]},
                                         alignment_transform={'rotation_deg':17.0,'offset_mm':[.75,-1.25]})
                    app.row_footprint({'footprint_choice':selection})
                    table = app.rows('R1', '__schematic__', include_stock=False)
                    assert table[0].get('field_changed_refs') and table[0].get('pcb_changed_refs'), table
                    preview=app.design_sync.preview({'refs':['R1'],'destination':'design'})
                    assert preview['schematic_changes'] and preview['automatic']
                    native_preview=app.pcb_updates.preview
                    preview_attempts=[]
                    def interrupted_preview(*args, **kwargs):
                        preview_attempts.append(True)
                        if len(preview_attempts)==1:
                            raise ValueError('Injected preflight connection interruption; no PCB write')
                        return native_preview(*args, **kwargs)
                    with patch.object(app.pcb_updates,'preview',interrupted_preview if mode=='library' else native_preview):
                        completed=finished(app.design_sync,app.design_sync.start(preview['preview_id'],{}))
                        if mode=='library':
                            assert completed['stage']=='partial' and completed['phase_results']['schematic']['ok'],completed
                            table = app.rows('R1', '__schematic__', include_stock=False)
                            assert not table[0].get('field_changed_refs') and table[0].get('pcb_changed_refs'), table
                            retry=app.design_sync.preview({'refs':['R1'],'destination':'pcb'})
                            assert retry['pcb_automatic'] and not retry['schematic_changes'],retry
                            completed=finished(app.design_sync,app.design_sync.continue_pcb(
                                completed['job_id'],retry['preview_id'],{}))
                            result['retry_automatically_eligible']=True
                    result[mode+'_job']=completed
                    if mode=='manual':
                        from part_selection_overrides import load_overrides
                        (report/'manual-record.json').write_text(json.dumps(load_overrides(app.overrides_path)['R1'],
                            ensure_ascii=False,indent=2),encoding='utf-8')
                        (report/'manual-after-live.kicad_pcb').write_text(session.snapshot(root,['R1'])['text'],encoding='utf-8')
                        shutil.copy2(root/'Probe.kicad_pcb',report/'manual-after-disk.kicad_pcb')
                    assert completed['stage']=='complete',completed
                    from part_selection_design_status import DesignStatus
                    app.design_status = DesignStatus(app)
                    table = app.rows('R1', '__schematic__', include_stock=False)
                    assert not table[0].get('field_changed_refs') and not table[0].get('pcb_changed_refs') and not table[0].get('pcb_unverified_refs'), table
                    result['main_table_restart_status_verified'] = True
                    # A second checkout receives project data, never this host's cache.
                    with temporary_project(prefix='portable-native-') as directory:
                        copied_root = Path(directory)
                        shutil.copytree(root, copied_root, dirs_exist_ok=True)
                        from part_selection_runtime import runtime_directory
                        assert not (runtime_directory(copied_root) / 'design-status.json').exists()
                        copied_app = PartSelectionApp(resolve_config(copied_root))
                        try:
                            before_copy = (copied_root / 'Probe.kicad_pcb').read_bytes()
                            copied_table = copied_app.rows('R1', '__schematic__', include_stock=False)
                            assert copied_table and not any(row.get(key) for row in copied_table
                                for key in ('field_changed_refs', 'pcb_changed_refs', 'pcb_unverified_refs')), copied_table
                            assert (copied_root / 'Probe.kicad_pcb').read_bytes() == before_copy
                            result.setdefault('project_only_copy_verified', []).append(mode)
                        finally:
                            copied_app.design_sync.close()
                            copied_app.search_jobs.close()
                            copied_app.selection_jobs.close()
                    current=session.snapshot(root,['R1'])
                    assert current['parts']['R1']['footprint']==target
                    token=(app.design_sync.previews[retry['preview_id']]['source'] if mode=='library' else
                           app.design_sync.jobs[completed['job_id']]['_pcb_source'])
                    assert token['fields']['R1']['footprint']==target
                    if mode=='rename':
                        expected=decode(original['parts']['R1']['proto'])
                        expected.definition.id.library='Local'
                        expected.definition.id.name='Renamed_R1'
                        assert footprint_bytes(expected)==footprint_bytes(decode(current['parts']['R1']['proto']))
                    else:
                        updated=next(state for state in reversed(app.pcb_updates.previews.values())
                                     if state.get('replacements'))
                        check_replacement(decode(original['parts']['R1']['proto']),
                            decode(current['parts']['R1']['proto']),decode(updated['replacements'][0]['proto']),
                            persisted=True,allow_pad_motion=mode=='manual')
                        if mode=='manual':
                            item=next(item for item in updated['items'] if item['ref']=='R1')
                            assert item['manual_adaptation'] and item['max_error_mm']>.5,item
                            import math
                            from part_selection_footprint_geometry import _parse_module, _pad_positions
                            local=_pad_positions(_parse_module((root/'Local.pretty/Managed_R1.kicad_mod').read_text(encoding='utf-8')))
                            standard=_pad_positions(_parse_module((root/'Local.pretty/Standard_R1.kicad_mod').read_text(encoding='utf-8')))
                            delta=math.radians(17.0)
                            for number,(x,y) in standard.items():
                                expected=(math.cos(delta)*x+math.sin(delta)*y+.75,
                                          -math.sin(delta)*x+math.cos(delta)*y-1.25)
                                assert math.dist(local[number],expected)<.000002
                            pose=item['position_mm']
                            assert math.dist(current['parts']['R1']['position_mm'],pose)<.000002
                            assert abs(current['parts']['R1']['rotation_deg']-item['rotation_deg'])<.0001
                            assert not current['parts']['R1']['back']
                            angle=math.radians(item['rotation_deg'])
                            for pad in current['parts']['R1']['pads']:
                                x,y=local[pad['number']]
                                expected=(math.cos(angle)*x+math.sin(angle)*y+pose[0],
                                          -math.sin(angle)*x+math.cos(angle)*y+pose[1])
                                assert math.dist(pad['position_mm'],expected)<.000002
                            result['management_transform_preserved']=True
                    # The schematic cycle reloads PCB before the transaction;
                    # KiCad may round rotated unselected geometry by one nm.
                    # Verify exact outside contents against the transaction's
                    # post-reload baseline, not a different native session.
                    transaction=transaction_snapshots[-1]
                    selected={transaction['parts']['R1']['uuid']}
                    outside_before=outside_parts(transaction['text'],selected)
                    outside_after=outside_parts(current['text'],selected)
                    if outside_before!=outside_after:
                        (report/(mode+'-outside-before.json')).write_text(json.dumps(outside_before,indent=2),encoding='utf-8')
                        (report/(mode+'-outside-after.json')).write_text(json.dumps(outside_after,indent=2),encoding='utf-8')
                    assert outside_before==outside_after
                    repeated=app.design_sync.preview({'refs':refs,'destination':'design'})
                    assert not repeated['schematic_changes']
                    assert all(item['kind']=='none' for item in repeated['pcb_preview']['items'])
                result['footprint_workflows_passed']=len(saves)==3
        except BaseException:
            result['error']=traceback.format_exc()
            log=ipc/'kicad_schematic_search.log'
            if log.exists():
                shutil.copy2(log,report/'kicad_schematic_search.log')
        finally:
            if app:
                app.design_sync.close();app.search_jobs.close();app.selection_jobs.close()
            for pid in owned_managers(root):
                process=processes.get(pid) or own(pid)
                close_fixture(process);process.wait(15)
            for process in processes.values():
                if process.poll() is not None:
                    close_fixture_helpers(process.pid, process.probe_started)
                subprocess._winapi.CloseHandle(process.handle)
            result['owned_pids']=list(processes)
            result['process_cleanup']=not owned_managers(root)
            result['cleanup_cwd']=os.getcwd()
            os.chdir(ROOT)
            import gc
            gc.collect()
            (report/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    result['directory_cleanup']=not root.exists()
    result['passed']=bool(result.get('functional_passed') and not result.get('error') and
        (not with_footprints or result.get('footprint_workflows_passed')) and
        result['process_cleanup'] and result['directory_cleanup'])
    (report/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:value for key,value in result.items() if not key.endswith('_job') and key!='job'},
                     ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir',type=Path,required=True)
    parser.add_argument('--with-footprints',action='store_true')
    args=parser.parse_args()
    report=args.report_dir.resolve()
    assert not report.is_relative_to(ROOT)
    report.mkdir(parents=True,exist_ok=True)
    raise SystemExit(run(report,with_footprints=args.with_footprints))

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
                        owned=OwnedProcess(pids[0])
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
                            processes[pid]=OwnedProcess(pid)
            app.run_kicad_script=tracked_script
            session=app.pcb_updates.session
            session.socket='ipc://'+str(ipc/'kicad/api.sock')
            refs=['R1','R2','U1']+[f'R{i}' for i in range(3,23)]
            before=session.snapshot(root,refs)
            board=session._board()[1]
            dirty=next(fp for fp in board.get_footprints() if fp.reference_field.text.value=='R1')
            dirty=deepcopy(dirty)
            dirty.value_field.text.value='stale PCB value'
            board.update_items(dirty)
            live_before=session.snapshot(root,refs)
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
                for mode, target in [('rename','Local:Renamed_R1'),('library','Local:Standard_R1')]:
                    original=session.snapshot(root,['R1'])
                    choice=app.row_footprint({'refs':'R1','bom':'__schematic__','mode':mode})['preview']
                    reference=choice['default_id'] if mode=='rename' else target
                    app.footprint_choices.library_detail(choice['preview_id'],reference)
                    selection={'preview_id':choice['preview_id'], 'existing_id':reference,
                               'source':'rename' if mode=='rename' else 'existing','acknowledged':True}
                    if mode=='rename':
                        selection.update(target_mode='existing',target_library='Local',name='Renamed_R1')
                    app.row_footprint({'footprint_choice':selection})
                    preview=app.design_sync.preview({'refs':['R1'],'destination':'design','alignment':'rigid'})
                    assert preview['schematic_changes'] and preview['automatic']
                    completed=finished(app.design_sync,app.design_sync.start(preview['preview_id'],{}))
                    result[mode+'_job']=completed
                    assert completed['stage']=='complete',completed
                    current=session.snapshot(root,['R1'])
                    assert current['parts']['R1']['footprint']==target
                    token=app.design_sync.jobs[completed['job_id']]['_pcb_source']
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
                            decode(current['parts']['R1']['proto']),decode(updated['replacements'][0]['proto']),persisted=True)
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
                result['footprint_workflows_passed']=len(saves)==2
        except BaseException:
            result['error']=traceback.format_exc()
            log=ipc/'kicad_schematic_search.log'
            if log.exists():
                shutil.copy2(log,report/'kicad_schematic_search.log')
        finally:
            if app:
                app.design_sync.close();app.search_jobs.close();app.selection_jobs.close()
            for pid in owned_managers(root):
                process=processes.get(pid) or OwnedProcess(pid)
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
    print(json.dumps(result,ensure_ascii=False,indent=2))
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

"""Opt-in real long-path manager, stale-entry, save/close/reopen design cycle QA."""
import argparse
from contextlib import ExitStack
from copy import deepcopy
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from _scratch import temporary_project
from run_native_pcb_in_place_probe import ROOT, windows, footprint_map, close_fixture, close_fixture_helpers

sys.path.insert(0, str(ROOT / 'tools'))
FLAGS = {'creationflags':subprocess.CREATE_NO_WINDOW}


def owned_managers(root):
    output = subprocess.check_output(['powershell.exe','-NoProfile','-NonInteractive','-Command',
        '[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); '
        'Get-CimInstance Win32_Process -Filter "Name=\'kicad.exe\'" | '
        'Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress'], encoding='utf-8', **FLAGS)
    items = json.loads(output or '[]')
    if isinstance(items,dict): items=[items]
    target = str(root / 'Probe.kicad_pro').lower()
    return [item['ProcessId'] for item in items if target in (item['CommandLine'] or '').lower()]


class OwnedProcess:
    def __init__(self,pid):
        self.pid=pid
        self.probe_started=time.time()-1
        self.handle=subprocess._winapi.OpenProcess(0x101000,False,pid)

    def poll(self):
        code=subprocess._winapi.GetExitCodeProcess(self.handle)
        return None if code==259 else code

    def wait(self,timeout):
        if subprocess._winapi.WaitForSingleObject(self.handle,round(timeout*1000))==258:
            raise subprocess.TimeoutExpired(str(self.pid),timeout)
        return self.poll()


def child_launch(root):
    sys.path.insert(0,str(ROOT/'tools/kicad_plugin'))
    import desktop
    import webview
    original=webview.create_window
    failures=[]
    def create(*args,**kwargs):
        kwargs['hidden']=True
        window=original(*args,**kwargs)
        def exercise():
            try:
                deadline=time.monotonic()+35
                while not window.evaluate_js("document.querySelectorAll('#rows tr').length>=3 && !!window.pywebview?.api?.finish_exit"):
                    if time.monotonic()>deadline: raise AssertionError('Actual workbench did not load')
                    time.sleep(.1)
                from urllib.request import urlopen
                with urlopen(window.evaluate_js('location.origin')+'/api/config',timeout=5) as response:
                    actual=json.load(response)['project_root']
                assert Path(actual).resolve()==root.resolve(),actual
                window.evaluate_js("document.getElementById('exitWorkbench').click();null")
            except BaseException as exc:
                failures.append(repr(exc))
                kwargs['js_api']._lifecycle.allow_close=True
                window.destroy()
        window.events.loaded+=lambda:threading.Thread(target=exercise,daemon=True).start()
        return window
    desktop.choose_project=lambda:(_ for _ in ()).throw(AssertionError('Unexpected project picker'))
    desktop.show_error=lambda message:(_ for _ in ()).throw(AssertionError(message))
    webview.create_window=create
    result=desktop.main([])
    assert result==0 and not failures,failures


def finished(sync,job):
    deadline=time.monotonic()+150
    last=None
    while time.monotonic()<deadline:
        value=sync.status(job['job_id'])
        if value['stage']!=last:
            print(value['stage']+': '+value['message'],flush=True)
            last=value['stage']
        if value['done']: return value
        time.sleep(.1)
    raise AssertionError(value)


def run(report):
    from part_selection_footprint_geometry import _find_kicad_python
    from part_selection_paths import resolve_config
    from part_selection_server import PartSelectionApp
    from part_selection_pcb_session import footprint_bytes,decode,encode
    results={'passed':False,'checks':{}}
    app=None
    started=time.time()
    observed=set()
    with ExitStack() as stack:
        root=Path(stack.enter_context(temporary_project(prefix='pcb-probe-')))
        results['fixture']=str(root)
        settings=root.parent/'isolated-config'/'10.0'
        settings.mkdir(parents=True)
        for source in (Path(os.environ['APPDATA'])/'kicad'/'10.0').iterdir():
            if source.is_file() and (source.suffix=='.json' or source.name.endswith('lib-table')):
                shutil.copy2(source,settings/source.name)
        common=settings/'kicad_common.json'
        config=json.loads(common.read_text(encoding='utf-8'))
        config.setdefault('api',{})['enable_server']=True
        config.setdefault('auto_backup',{})['enabled']=False
        config.setdefault('session',{})['remember_open_files']=False
        common.write_text(json.dumps(config),encoding='utf-8')
        # Keep the transport address short; the project path remains long.
        ipc=Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='cycle-ipc-',dir=root.anchor)))
        stack.enter_context(patch.dict(os.environ,{'KICAD_CONFIG_HOME':str(settings.parent),
            'KICAD_PART_SELECTION_HOME':str(root.parent/'desktop'),'KIPRJMOD':str(root),
            'TEMP':str(ipc),'TMP':str(ipc)}))
        native=_find_kicad_python()
        subprocess.run([str(native),'-B',str(ROOT/'tests/run_native_pcb_in_place_probe.py'),
            '--build',str(root)],check=True,capture_output=True,timeout=60,**FLAGS)
        for lock in root.glob('~Probe.*.lck'): lock.unlink()
        subprocess.Popen([str(native.parent/'kicad.exe'),str(root/'Probe.kicad_pro')],**FLAGS)
        try:
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                pids=owned_managers(root)
                if len(pids)==1:
                    owned=windows(pids[0])
                    warnings=[handle for handle,title in owned if title=='文件打开警告']
                    if warnings:
                        for handle in warnings:ctypes.windll.user32.PostMessageW(handle,0x0111,5104,0)
                        results['fixture_lock_acknowledged']=True
                        time.sleep(.5)
                    elif any('KiCad' in title for _,title in owned):break
                time.sleep(.25)
            assert len(pids)==1,pids
            app=PartSelectionApp(resolve_config(root))
            observed.update(pids)
            run_script=app.run_kicad_script
            def tracked_script(*args,**kwargs):
                try:return run_script(*args,**kwargs)
                finally:observed.update(owned_managers(root))
            app.run_kicad_script=tracked_script
            session=app.pcb_updates.session
            session.socket='ipc://'+str(ipc/'kicad/api.sock')
            manager=next(h for h,t in windows(pids[0]) if 'KiCad' in t)
            ctypes.windll.user32.ShowWindow(manager,9)
            ctypes.windll.user32.SetWindowPos(manager,0,0,0,620,600,0x16)
            time.sleep(1)
            closed=app.prepare_kicad_for_external_schematic_write(['R1'])
            assert closed['ok'] and 'ProjectClosed=1' in closed['message'],closed
            assert not owned_managers(root),'Manager-only close left a process'
            closed=app.prepare_kicad_for_external_schematic_write(['R1'])
            assert closed['ok'] and 'ProjectClosed=0' in closed['message'],closed
            results['checks']['manager_only_and_already_closed']=True
            url=app.kicad_url_for_refs(['R1'])
            opened=app.run_kicad_script(url,['-NotifyOnly'])
            assert opened['ok'],opened
            pids=owned_managers(root)
            assert len(pids)==1,pids
            manager=next(h for h,t in windows(pids[0]) if 'KiCad' in t)
            ctypes.windll.user32.SetWindowPos(manager,0,0,0,620,600,0x16)
            opened=app.run_kicad_script(url,['-NotifyOnly'])
            assert opened['ok'],opened
            results['checks']['long_narrow_manager_opens_schematic']=True
            child=subprocess.run([sys.executable,'-B',str(Path(__file__)),'--launch-child',str(root)],
                env={**os.environ,'KICAD_API_SOCKET':'ipc://'+str(ipc/'expired.sock'),
                     'KICAD_API_TOKEN':'expired-test-token'},capture_output=True,text=True,
                encoding='utf-8',timeout=65,**FLAGS)
            (report/'stale-launch.log').write_text(child.stdout+child.stderr,encoding='utf-8')
            assert child.returncode==0,child.stdout+child.stderr
            results['checks']['actual_stale_entry_webview_no_picker']=True
            app.save_override({'refs':'R1','field_overrides':{'value':'10k, 1%'}})
            preview=app.design_sync.preview({'refs':['R1'],'destination':'schematic'})
            done=finished(app.design_sync,app.design_sync.start(preview['preview_id'],{}))
            assert done['stage']=='complete',done
            results['checks']['schematic_only_saved_reopened_located']=True
            guard=app.run_kicad_script(url,['-Mode','FindDialog','-SaveActiveFrame','-CloseAfterSave','-SaveOnly','-ExistingOnly'])
            assert guard['ok'],guard
            client,board,_=session._board()
            before=footprint_map(board)
            dirty=deepcopy(before['T_R1']);dirty.value_field.text.value='prior unsaved PCB edit'
            board.update_items(dirty)
            assert footprint_map(board)['T_R1'].value_field.text.value=='prior unsaved PCB edit'
            assert app.run_kicad_script(url,['-NotifyOnly'])['ok']
            app.save_override({'refs':'R1','field_overrides':{'value':'22k, 1%','comment':'resistor'}})
            preview=app.design_sync.preview({'refs':['R1'],'destination':'both'})
            state=finished(app.design_sync,app.design_sync.start(preview['preview_id'],{}))
            assert state['stage']=='awaiting_pcb_confirmation',state
            assert not any(pid in owned_managers(root) for pid in pids),'Initial manager never exited'
            results['checks']['dirty_pcb_saved_before_whole_project_close']=True
            done=finished(app.design_sync,app.design_sync.continue_pcb(state['job_id'],state['pcb_preview']['preview_id'],{}))
            assert done['stage']=='complete' and done['result']['saved'],done
            client,board,_=session._board()
            after=footprint_map(board)
            assert after['R1'].value_field.text.value=='22k, 1%'
            assert after['T_R1'].value_field.text.value=='prior unsaved PCB edit'
            for ref in ('R2','U1'):
                assert footprint_bytes(after[ref])==footprint_bytes(before[ref]),ref
            results['checks']['fields_saved_reopened_located_prior_edit_preserved']=True
            baseline=decode(encode(after['R1']))
            preview=app.row_footprint({'refs':'R1','bom':'__schematic__','mode':'rename'})['preview']
            reference=preview['default_id']
            app.footprint_choices.library_detail(preview['preview_id'],reference)
            app.row_footprint({'footprint_choice':{'preview_id':preview['preview_id'],'source':'rename',
                'existing_id':reference,'acknowledged':True,'target_mode':'existing',
                'target_library':'Local','name':'Renamed_R1'}})
            preview=app.design_sync.preview({'refs':['R1'],'destination':'both'})
            state=finished(app.design_sync,app.design_sync.start(preview['preview_id'],{}))
            assert state['stage']=='awaiting_pcb_confirmation',state
            assert state['pcb_preview']['items'][0]['kind']=='rename',state
            done=finished(app.design_sync,app.design_sync.continue_pcb(state['job_id'],state['pcb_preview']['preview_id'],{}))
            assert done['stage']=='complete',done
            client,board,_=session._board()
            renamed=footprint_map(board)['R1']
            baseline.definition.id.library='Local';baseline.definition.id.name='Renamed_R1'
            from google.protobuf.json_format import MessageToDict
            for name,fp in [('rename-before',baseline),('rename-after',renamed)]:
                (report/(name+'.json')).write_text(json.dumps(MessageToDict(fp.proto,preserving_proto_field_name=True),indent=2),encoding='utf-8')
            assert footprint_bytes(renamed)==footprint_bytes(baseline),'Rename changed complete instance'
            assert done['phase_results']['pcb']['cycle_complete']
            results['checks']['rename_geometry_pads_nets_saved_reopened']=True
            preview=app.design_sync.preview({'refs':['R1'],'destination':'pcb'})
            done=finished(app.design_sync,app.design_sync.start(preview['preview_id'],{}))
            assert done['stage']=='complete' and done['result']['saved'],done
            assert footprint_bytes(footprint_map(session._board()[1])['R1'])==footprint_bytes(renamed)
            results['checks']['pcb_only_saved_reopened_located']=True
            before_replace=session.snapshot(root,['R1'])
            preview=app.row_footprint({'refs':'R1','bom':'__schematic__','mode':'library'})['preview']
            app.footprint_choices.library_detail(preview['preview_id'],'Local:Standard_R1')
            app.row_footprint({'footprint_choice':{'preview_id':preview['preview_id'],
                'source':'existing','existing_id':'Local:Standard_R1','acknowledged':True}})
            preview=app.design_sync.preview({'refs':['R1'],'destination':'both','alignment':'rigid'})
            state=finished(app.design_sync,app.design_sync.start(preview['preview_id'],{}))
            assert state['stage']=='awaiting_pcb_confirmation',state
            token=state['pcb_preview']['preview_id']
            replacements=app.pcb_updates.previews[token]['replacements']
            done=finished(app.design_sync,app.design_sync.continue_pcb(state['job_id'],token,{'geometry':True}))
            assert done['stage']=='complete' and done['result']['saved'],done
            from run_native_pcb_in_place_probe import check_replacement
            after_replace=session.snapshot(root,['R1'])
            target=decode(replacements[0]['proto'])
            check_replacement(decode(before_replace['parts']['R1']['proto']),
                decode(after_replace['parts']['R1']['proto']),target,persisted=True)
            from part_selection_pcb_session import outside_parts
            selected={before_replace['parts']['R1']['uuid']}
            assert outside_parts(before_replace['text'],selected)==outside_parts(after_replace['text'],selected)
            results['checks']['standard_library_geometry_pads_nets_tracks_saved_reopened']=True
            results['timings']=done['timings']
            results['functional_passed']=True
        except BaseException as exc:
            import traceback
            results['error']=repr(exc)
            (report/'error.log').write_text(traceback.format_exc(),encoding='utf-8')
            from part_selection_runtime import runtime_directory
            for directory in runtime_directory(root).glob('pcb-save-*'):
                shutil.copytree(directory, report/directory.name, dirs_exist_ok=True)
        finally:
            for name in ('kicad_schematic_search.log',):
                if (ipc/name).is_file():shutil.copy2(ipc/name,report/name)
            if app:
                app.design_sync.close();app.selection_jobs.close();app.search_jobs.close()
            failures=[]
            remaining=owned_managers(root)
            observed.update(remaining)
            for pid in remaining:
                process=OwnedProcess(pid)
                try: close_fixture(process);process.wait(10)
                except BaseException as exc: failures.append(repr(exc))
                finally: subprocess._winapi.CloseHandle(process.handle)
            for pid in observed:
                try:close_fixture_helpers(pid, started)
                except BaseException as exc:failures.append(repr(exc))
            results['cleanup_errors']=failures
            results['process_cleanup']=not owned_managers(root)
            results['passed']=bool(results.get('functional_passed') and results['process_cleanup'] and not failures)
            (report/'capabilities.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    results['directory_cleanup']=not root.exists()
    results['passed']=results['passed'] and results['directory_cleanup']
    (report/'capabilities.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(results,ensure_ascii=False,indent=2))
    return 0 if results['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir',type=Path)
    parser.add_argument('--launch-child',type=Path)
    args=parser.parse_args()
    if args.launch_child:
        assert args.launch_child.parent.name.startswith('pcb-probe-') and args.launch_child.name=='project'
        child_launch(args.launch_child)
    else:
        assert args.report_dir and args.report_dir.is_absolute() and not args.report_dir.resolve().is_relative_to(ROOT)
        args.report_dir.mkdir(parents=True,exist_ok=True)
        raise SystemExit(run(args.report_dir))

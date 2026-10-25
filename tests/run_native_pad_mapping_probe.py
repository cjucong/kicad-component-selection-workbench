"""Opt-in real KiCad manual pad mapping, staged sync and save/reopen probe.

Uses only a disposable synthetic project. The converter's upstream download is
synthetic; native rendering, geometry publication, IPC and CAD sync are real.
"""
import argparse
from collections import Counter
from contextlib import ExitStack
import ctypes
import json
import math
import os
from pathlib import Path
import shutil
import re
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from _scratch import temporary_project
from run_native_pcb_in_place_probe import ROOT, native_build, close_fixture, close_fixture_helpers, windows
from run_native_project_cycle_probe import owned_managers, OwnedProcess, finished

sys.path.insert(0, str(ROOT/'tools'))
FLAGS = {'creationflags':subprocess.CREATE_NO_WINDOW}


def native_schematic_update(app, root, report):
    """Invoke the real F8 dialog in the owned fixture, using UIA button names."""
    pid, = owned_managers(root)
    client, _, _ = app.pcb_updates.session._board()
    from kipy.errors import ConnectionError as KiCadConnectionError
    action_timeout = None
    try:
        client.run_action('common.Control.updatePcbFromSchematic')
    except KiCadConnectionError as exc:
        # KiCad handles this action modally and may not reply until the dialog
        # closes. Only observing the owned dialog below confirms it launched.
        action_timeout = str(exc)
    deadline = time.monotonic()+20
    while time.monotonic() < deadline:
        dialogs = [(handle,title) for handle,title in windows(pid)
                   if 'PCB' in title and ('从原理图更新' in title or 'Update PCB from Schematic' in title)]
        if len(dialogs) == 1:
            break
        time.sleep(.1)
    assert len(dialogs) == 1, windows(pid)
    handle, title = dialogs[0]
    prefix = ('[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); '
        'Add-Type -AssemblyName UIAutomationClient; Add-Type -AssemblyName UIAutomationTypes; '
        f'$dialog=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{handle}); '
        '$controls=$dialog.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition); ')
    def invoke(name=None):
        command = prefix
        if name is None:
            command += '@($controls | ForEach-Object { [pscustomobject]@{Name=$_.Current.Name;Type=$_.Current.ControlType.ProgrammaticName;Enabled=$_.Current.IsEnabled;Handle=$_.Current.NativeWindowHandle;Patterns=@($_.GetSupportedPatterns() | ForEach-Object { $_.ProgrammaticName })} }) | ConvertTo-Json -Compress'
        else:
            quoted = name.replace("'", "''")
            command += f"$buttons=@($controls | Where-Object {{ $_.Current.Name -eq '{quoted}' }}); "
            command += "if($buttons.Count -ne 1 -or -not $buttons[0].Current.IsEnabled){throw 'Fixture button missing or disabled'}; "
            command += ('$pattern=$null; if($buttons[0].TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)) '
                '{ $pattern.Invoke() } else { '
                'Add-Type -TypeDefinition \'using System; using System.Text; using System.Runtime.InteropServices; public static class FixtureButton {'
                '[DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h,StringBuilder s,int n);'
                '[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);'
                '[DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h,uint m,IntPtr w,IntPtr l); }\'; '
                '$h=[IntPtr]$buttons[0].Current.NativeWindowHandle; $class=[Text.StringBuilder]::new(256); $owner=[uint32]0; '
                '[void][FixtureButton]::GetClassName($h,$class,256); [void][FixtureButton]::GetWindowThreadProcessId($h,[ref]$owner); '
                f'if($owner -ne {pid} -or $class.ToString() -ne "Button"){{throw "Not an owned native button: $class / $owner"}}; '
                'if(-not [FixtureButton]::PostMessage($h,0x00F5,[IntPtr]::Zero,[IntPtr]::Zero)){throw "Native fixture button failed"} }')
        completed = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                                   capture_output=True,text=True,encoding='utf-8-sig',timeout=25,**FLAGS)
        assert completed.returncode == 0, completed.stdout+completed.stderr
        return json.loads(completed.stdout or 'null')
    controls = invoke()
    (report/'native-f8-controls.json').write_text(json.dumps(controls,ensure_ascii=False,indent=2),encoding='utf-8')
    def button(pattern):
        found = [c['Name'] for c in controls if re.fullmatch(pattern,c['Name'].replace('&',''))]
        assert len(found) == 1, controls
        return found[0]
    invoke(button(r'(?:更新\s*PCB|Update PCB)(?:\([A-Za-z]\))?'))
    time.sleep(.5)
    (report/'native-f8-result.json').write_text(json.dumps(invoke(),ensure_ascii=False,indent=2),encoding='utf-8')
    invoke(button(r'(?:关闭|Close)(?:\([A-Za-z]\))?'))
    deadline = time.monotonic()+10
    while time.monotonic() < deadline and any(h == handle for h,_ in windows(pid)):
        time.sleep(.1)
    assert not any(h == handle for h,_ in windows(pid)), 'Native F8 dialog did not close'
    return {'title':title,'native_update_clicked':True,'modal_action_reply':action_timeout}


def run(report):
    from part_selection_footprint_geometry import _find_kicad_python
    from part_selection_paths import resolve_config
    from part_selection_server import PartSelectionApp
    from part_selection_pcb import PcbUpdates
    from part_selection_pcb_session import decode, outside_parts, describe
    from part_selection_pad_mapping import adapt_numbers
    result = {'passed':False, 'checks':{}, 'upstream_conversion':'synthetic'}
    app = None
    observed = set()
    started = time.time()
    with ExitStack() as stack:
        root = Path(stack.enter_context(temporary_project(prefix='pcb-probe-', short_paths=True)))
        ipc = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='pad-ipc-', dir=root.anchor)))
        result['fixture'] = str(root)
        settings = ipc/'settings'/'10.0'
        settings.mkdir(parents=True)
        for source in (Path(os.environ['APPDATA'])/'kicad'/'10.0').iterdir():
            if source.is_file() and (source.suffix == '.json' or source.name.endswith('lib-table')):
                shutil.copy2(source, settings/source.name)
        common = settings/'kicad_common.json'
        config = json.loads(common.read_text(encoding='utf-8'))
        config.setdefault('api', {})['enable_server'] = True
        config.setdefault('auto_backup', {})['enabled'] = False
        config.setdefault('session', {})['remember_open_files'] = False
        common.write_text(json.dumps(config), encoding='utf-8')
        stack.enter_context(patch.dict(os.environ, {'KICAD_CONFIG_HOME':str(settings.parent),
            'KICAD_PART_SELECTION_HOME':str(ipc/'desktop'), 'KIPRJMOD':str(root),
            'KICAD_PART_SELECTION_CACHE':str(ipc/'cache'), 'TEMP':str(ipc), 'TMP':str(ipc)}))
        native = _find_kicad_python()
        built = subprocess.run([str(native), '-B', str(Path(__file__)), '--build', str(root)],
                               capture_output=True, text=True, timeout=60, **FLAGS)
        (report/'build.log').write_text(built.stdout+built.stderr, encoding='utf-8')
        built.check_returncode()
        exported = subprocess.run([str(native.parent/'kicad-cli.exe'),'sch','export','netlist',
            '-o',str(ipc/'fixture.net'),str(root/'Probe.kicad_sch')], capture_output=True,text=True,timeout=45,**FLAGS)
        (report/'schematic-validation.log').write_text(exported.stdout+exported.stderr,encoding='utf-8')
        exported.check_returncode()
        from run_native_footprint_workflow import validate_netlist
        validate_netlist((ipc/'fixture.net').read_text(encoding='utf-8'),
            [{'ref':ref,'pin_numbers':[str(n) for n in range(1,(9 if ref in ('R1','U1') else 11))]}
             for ref in ('R1','R2','U1','U2')], prefix='')
        for lock in root.glob('~Probe.*.lck'):
            lock.unlink()
        try:
            process = subprocess.Popen([str(native.parent/'kicad.exe'), str(root/'Probe.kicad_pro')], **FLAGS)
            observed.add(process.pid)
            deadline = time.monotonic()+30
            while time.monotonic() < deadline:
                owned = windows(process.pid)
                warnings = [handle for handle,title in owned if title == '文件打开警告']
                for handle in warnings:
                    ctypes.windll.user32.PostMessageW(handle,0x0111,5104,0)
                if not warnings and any('Probe' in title and 'KiCad' in title for _,title in owned):
                    break
                time.sleep(.25)
            assert process.poll() is None and owned_managers(root) == [process.pid], windows(process.pid)
            app = PartSelectionApp(resolve_config(root))
            run_script = app.run_kicad_script
            def tracked(*args, **kwargs):
                try:
                    return run_script(*args, **kwargs)
                finally:
                    observed.update(owned_managers(root))
            app.run_kicad_script = tracked
            session = app.pcb_updates.session
            session.socket = 'ipc://'+str(ipc/'kicad/api.sock')
            refs = ['R1','R2','U1','U2']
            before = session.snapshot(root, refs)
            (report/'initial.json').write_text(json.dumps(before,ensure_ascii=False),encoding='utf-8')
            original_files = {path:path.read_bytes() for path in (root/'Local.pretty').glob('*.kicad_mod')}
            choices = {}

            def converted_fixture(importer, code, progress=None):
                ref = 'U2' if code == 'C12346' else 'U1'
                folder = importer.root/'Libraries'/'DSMU'/'LCSC'/code
                pretty = folder/f'LCSC_{code}.pretty'
                pretty.mkdir(parents=True)
                source = (root/'Local.pretty'/f'Mapped_{ref}.kicad_mod').read_text(encoding='utf-8')
                source = adapt_numbers(source, {'number_map':{str(n):'A'+str(n) for n in range(1,9)}})
                (pretty/'Converted.kicad_mod').write_text(source, encoding='utf-8')
                (folder/f'LCSC_{code}.kicad_sym').write_text('(kicad_symbol_lib (version 20241209) '
                    f'(generator test) (symbol "Part" (property "Footprint" "LCSC_{code}:Converted")))', encoding='utf-8')
                return {'footprint':f'LCSC_{code}:Converted','symbol':f'LCSC_{code}:Part','c_code':code}

            for ref in refs:
                converted = ref.startswith('U')
                with patch('part_selection_footprints.LibraryImporter.convert', autospec=True, side_effect=converted_fixture):
                    preview = app.row_footprint({'refs':ref,'bom':'__schematic__',
                                                'mode':'convert' if converted else 'library'})['preview']
                identifier = preview['default_reference_id']
                options = {}
                if not converted:
                    app.footprint_choices.library_detail(preview['preview_id'],'Local:Mapped_'+ref)
                    options['target_identifier'] = 'Local:Mapped_'+ref
                detail = app.footprint_choices.comparison(preview['preview_id'], identifier, **options)
                count = 8 if ref in ('R1','U1') else 10
                assert len(detail['existing_pads']) == count
                assert len(detail['converted_pads']) == (10 if count == 8 else 8)
                mapping = {'schema':1,'pairs':[{'source':str(n),'target':('A' if converted else '')+str(n),
                    'align':n != 8} for n in range(1,9)],'dropped':['9','10'] if count == 10 else []}
                staged = app.footprint_choices.previews[preview['preview_id']]
                selected = None if converted else next(e for e in staged['existing'] if e['id'] == 'Local:Mapped_'+ref)
                aligned, _ = app.footprint_choices._alignment(staged, identifier, mapping=mapping, target=selected)
                initial = aligned['alignment']
                assert initial['ok'], initial
                transform = {'rotation_deg':initial['rotation_deg']+2.0,
                             'offset_mm':[initial['offset_mm'][0]+.1,initial['offset_mm'][1]-.15]}
                # Keep an intentional manual rotation/offset. PCB application
                # must not fit this approved pose back onto the original pads.
                choice = {'preview_id':preview['preview_id'],'source':'converted' if converted else 'existing',
                    'existing_id':identifier if converted else 'Local:Mapped_'+ref,'reference_id':identifier,
                    'mapping':mapping,'alignment_transform':transform,
                    'dropped_acknowledged':True,'acknowledged':True,'target_mode':'existing',
                    'target_library':'Local','name':'Adapted_'+ref}
                saved = app.row_footprint({'footprint_choice':choice})
                assert saved['ok'], saved
                choices[ref] = choice
            assert all(path.read_bytes() == data for path,data in original_files.items())
            result['checks']['both_sources_real_preview_and_project_copy'] = True
            preview = app.design_sync.preview({'refs':refs,'destination':'both','alignment':'center'})
            state = finished(app.design_sync, app.design_sync.start(preview['preview_id'], {}))
            assert state['stage'] == 'awaiting_pcb_confirmation', state
            items = {item['ref']:item for item in state['pcb_preview']['items']}
            assert items['R1']['extra_pads'] == ['10','9'] or set(items['R1']['extra_pads']) == {'9','10'}
            assert all(item['net_assigned'] for item in items['R2']['dropped_terminals'])
            token = state['pcb_preview']['preview_id']
            try:
                app.pcb_updates.preview(refs, '__schematic__', alignment='rigid')
            except ValueError as exc:
                assert '刚体容差' in str(exc), exc
            else:
                raise AssertionError('Manual offsets bypassed rigid-mode tolerance')
            result['checks']['rigid_mode_rejects_manual_pad_movement'] = True
            prototypes = {item['ref']:item['proto'] for item in app.pcb_updates.previews[token]['replacements']}
            for incomplete in ({'geometry':True,'pad_movement':True},
                               {'geometry':True,'pad_movement':True,'dropped_terminals':True}):
                try:
                    app.design_sync.continue_pcb(state['job_id'], token, incomplete)
                except ValueError:
                    pass
                else:
                    raise AssertionError('Missing drop/connected confirmation was accepted')
            result['checks']['separate_connected_terminal_acknowledgment'] = True
            done = finished(app.design_sync, app.design_sync.continue_pcb(state['job_id'], token,
                {'geometry':True,'pad_movement':True,'dropped_terminals':True,'connected_terminals':True}))
            assert done['stage'] == 'complete' and done['result']['saved'], done
            after = session.snapshot(root, refs)
            (report/'completed.json').write_text(json.dumps(after,ensure_ascii=False),encoding='utf-8')
            selected = {part['uuid'] for part in before['parts'].values()}
            # Saving/reopening can reorder top-level objects; compare complete
            # nodes as a multiset so values and duplicate counts stay strict.
            unchanged = [Counter(json.dumps(node, sort_keys=True) for node in outside_parts(s['text'], selected))
                         for s in (before, after)]
            assert unchanged[0] == unchanged[1], 'Tracks or other objects changed'
            for ref in refs:
                old, actual = decode(before['parts'][ref]['proto']), decode(after['parts'][ref]['proto'])
                expected = decode(prototypes[ref])
                assert old.id == actual.id and old.proto.symbol_path == actual.proto.symbol_path
                assert old.layer == actual.layer
                assert actual.position == old.position and actual.orientation == old.orientation, 'Manual pose was refitted'
                assert describe(actual)['footprint'] == 'Local:Adapted_'+ref
                prior = {pad.number:pad for pad in old.definition.pads}
                desired = {pad.number:pad for pad in expected.definition.pads}
                for pad in actual.definition.pads:
                    wanted = desired[pad.number]
                    assert max(abs(pad.position.x-wanted.position.x), abs(pad.position.y-wanted.position.y)) <= 1
                    if pad.number in prior:
                        assert pad.id == prior[pad.number].id and pad.proto.net == prior[pad.number].proto.net
                        assert pad.proto.symbol_pin == prior[pad.number].proto.symbol_pin
                    else:
                        assert not pad.proto.net.name
                        assert pad.id.value not in {p.id.value for p in prior.values()}
                assert {pad.number for pad in actual.definition.pads} == ({str(n) for n in range(1,11)} if ref in ('R1','U1') else {str(n) for n in range(1,9)})
                assert any(math.dist(pad['position_mm'],before['parts'][ref]['pads'][0]['position_mm']) > .01
                           for pad in after['parts'][ref]['pads']), 'Intentional manual placement vanished'
            result['checks']['front_back_8_to_10_and_10_to_8_saved_reopened'] = True
            result['checks']['mapped_uuid_nets_linkage_and_routing_preserved'] = True
            # An independent updater has no in-memory completion cache.
            app.pcb_updates = PcbUpdates(app, session=session)
            repeat = app.pcb_updates.preview(refs, '__schematic__', alignment='center')
            assert all(item['kind'] in ('none','fields') for item in repeat['items']), repeat
            result['checks']['restart_repeat_no_relocation'] = True
            result['native_f8'] = native_schematic_update(app, root, report)
            f8 = session.snapshot(root, refs)
            for ref in ('R1','U1'):
                before_pads = {p['number']:p for p in after['parts'][ref]['pads']}
                for pad in f8['parts'][ref]['pads']:
                    assert pad['position_mm'] == before_pads[pad['number']]['position_mm'], 'Native F8 moved the adapted module'
                    assert pad['net'] == before_pads[pad['number']]['net'], 'Native F8 assigned an incorrect net'
            session.save_verified(f8, report/'f8-save')
            closed = app.prepare_kicad_for_external_schematic_write(refs)
            assert closed['ok'], closed
            reopened = session.snapshot(root, refs)
            for ref in ('R1','U1'):
                assert reopened['parts'][ref]['pads'] == f8['parts'][ref]['pads']
            result['checks']['real_f8_different_numbers_extras_no_net_saved_reopened'] = True
            result['functional_passed'] = True
        except BaseException as exc:
            import traceback
            result['error'] = repr(exc)
            (report/'error.log').write_text(traceback.format_exc(), encoding='utf-8')
            from part_selection_runtime import runtime_directory
            for directory in runtime_directory(root).glob('pcb-*'):
                if directory.is_dir():
                    shutil.copytree(directory, report/directory.name, dirs_exist_ok=True)
        finally:
            if (ipc/'kicad_schematic_search.log').is_file():
                shutil.copy2(ipc/'kicad_schematic_search.log',report/'kicad_schematic_search.log')
            if app:
                app.design_sync.close(); app.selection_jobs.close(); app.search_jobs.close()
            failures = []
            observed.update(owned_managers(root))
            for pid in owned_managers(root):
                process = OwnedProcess(pid)
                try:
                    close_fixture(process); process.wait(10)
                except BaseException as exc:
                    failures.append(repr(exc))
                finally:
                    subprocess._winapi.CloseHandle(process.handle)
            for pid in observed:
                try:
                    close_fixture_helpers(pid, started)
                except BaseException as exc:
                    failures.append(repr(exc))
            result['cleanup_errors'] = failures
            result['process_cleanup'] = not owned_managers(root)
            result['passed'] = bool(result.get('functional_passed') and result['process_cleanup'] and not failures)
            (report/'capabilities.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    result['directory_cleanup'] = not root.exists()
    result['passed'] = result['passed'] and result['directory_cleanup']
    (report/'capabilities.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build',type=Path)
    parser.add_argument('--report-dir',type=Path)
    args = parser.parse_args()
    if args.build:
        assert args.build.parent.name.startswith('pcb-probe-') and args.build.name == 'project'
        native_build(args.build, manual_mapping=True)
    else:
        assert args.report_dir and args.report_dir.is_absolute() and not args.report_dir.resolve().is_relative_to(ROOT)
        args.report_dir.mkdir(parents=True,exist_ok=True)
        raise SystemExit(run(args.report_dir))

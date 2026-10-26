"""Opt-in 23-item native save/lost-reply QA, without synthetic keyboard input."""
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
from run_native_project_cycle_probe import owned_managers, OwnedProcess, FLAGS
from run_native_pcb_in_place_probe import ROOT, windows, close_fixture


def run(report):
    from kipy.board import Board
    from part_selection_footprint_geometry import _find_kicad_python
    from part_selection_pcb_session import PcbSession, metadata_replacement, persisted_footprint_matches, decode
    result = {'passed':False, 'checks':{}}
    owned = None
    with ExitStack() as stack:
        root = Path(stack.enter_context(temporary_project(prefix='pcb-probe-')))
        ipc = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='save-ipc-', dir=root.anchor)))
        settings = root.parent/'isolated-config'/'10.0'
        settings.mkdir(parents=True)
        for source in (Path(os.environ['APPDATA'])/'kicad/10.0').iterdir():
            if source.is_file() and (source.suffix == '.json' or source.name.endswith('lib-table')):
                shutil.copy2(source, settings/source.name)
        common = settings/'kicad_common.json'
        config = json.loads(common.read_text(encoding='utf-8'))
        config.setdefault('api', {})['enable_server'] = True
        config.setdefault('auto_backup', {})['enabled'] = False
        config.setdefault('session', {})['remember_open_files'] = False
        common.write_text(json.dumps(config), encoding='utf-8')
        stack.enter_context(patch.dict(os.environ, {'KICAD_CONFIG_HOME':str(settings.parent),
            'KICAD_PART_SELECTION_CACHE':str(ipc/'cache'), 'TEMP':str(ipc), 'TMP':str(ipc)}))
        native = _find_kicad_python()
        subprocess.run([str(native), '-B', str(ROOT/'tests/run_native_pcb_in_place_probe.py'),
            '--build', str(root), '--save-batch'], check=True, capture_output=True, timeout=60, **FLAGS)
        for lock in root.glob('~Probe.*.lck'): lock.unlink()
        subprocess.Popen([str(native.parent/'kicad.exe'), str(root/'Probe.kicad_pro')], **FLAGS)
        try:
            deadline = time.monotonic()+30
            while time.monotonic() < deadline:
                pids = owned_managers(root)
                if len(pids) == 1:
                    owned = OwnedProcess(pids[0])
                    for handle, title in windows(pids[0]):
                        if title == '文件打开警告': ctypes.windll.user32.PostMessageW(handle, 0x0111, 5104, 0)
                    if any('KiCad' in title for _,title in windows(pids[0])): break
                time.sleep(.25)
            assert owned is not None
            session = PcbSession(root, socket='ipc://'+str(ipc/'kicad/api.sock'))
            refs = ['R1','R2','U1']+[f'R{index}' for index in range(3,23)]
            before = session.snapshot(root, refs)
            assert len(before['parts']) == 23
            all_before = {fp.reference_field.text.value:deepcopy(fp) for fp in session._board()[1].get_footprints()}
            replacements = [{'ref':ref, 'kind':'fields', 'fields':{'Value':'save QA '+ref},
                'proto':metadata_replacement(part, fields={'Value':'save QA '+ref})}
                for ref,part in before['parts'].items()]
            assert not any('原理图编辑器' in title for _,title in windows(owned.pid))
            session.apply(before, replacements, report/'transaction', schematic_close_verified=True)
            expected = session.snapshot(root, refs)
            session.verify(before, expected, replacements)
            result['checks']['23_native_updates_complete_readback'] = True
            original_save = Board.save
            calls, stages = [], []
            def lost_reply(board):
                calls.append(True)
                time.sleep(5.2)
                original_save(board)
                raise TimeoutError('Error receiving reply from KiCad: Timed out (injected after native save)')
            with patch.object(Board, 'save', lost_reply):
                saved = session.save_verified(expected, report/'save',
                    progress=lambda stage,message:stages.append({'stage':stage,'message':message}))
            assert calls == [True] and saved['saved'] and saved['reply_timeout_recovered']
            assert any(item['stage'] == 'save_reconcile' for item in stages)
            assert any('不会重复提交' in item['message'] for item in stages)
            result['checks']['slow_save_lost_reply_recovered_once'] = True
            result['stages'] = stages
            close_fixture(owned)
            owned.wait(15)
            subprocess._winapi.CloseHandle(owned.handle)
            owned = None
            reopened = session.snapshot(root, refs)
            for ref in refs:
                assert persisted_footprint_matches(decode(expected['parts'][ref]['proto']), decode(reopened['parts'][ref]['proto'])),ref
            from part_selection_pcb_session import outside_parts
            all_after = {fp.reference_field.text.value:fp for fp in session._board()[1].get_footprints()}
            assert set(all_before) == set(all_after)
            for ref,fp in all_before.items():
                if ref not in refs: assert persisted_footprint_matches(fp, all_after[ref]),ref
            selected = {fp.id.value for fp in all_before.values()}
            for name,snapshot in (('before-reopen',expected),('after-reopen',reopened)):
                (report/(name+'.json')).write_text(json.dumps(snapshot,ensure_ascii=False),encoding='utf-8')
            assert outside_parts(expected['text'], selected) == outside_parts(reopened['text'], selected)
            result['checks']['23_saved_reopened_fields_geometry_nets_tracks'] = True
            result['functional_passed'] = True
        except BaseException:
            result['error'] = traceback.format_exc()
        finally:
            for pid in owned_managers(root):
                process = owned if owned is not None and owned.pid == pid else OwnedProcess(pid)
                close_fixture(process)
                process.wait(15)
                subprocess._winapi.CloseHandle(process.handle)
            result['process_cleanup'] = not owned_managers(root)
    result['directory_cleanup'] = not root.exists()
    result['passed'] = bool(result.get('functional_passed') and result['process_cleanup'] and result['directory_cleanup'])
    (report/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, required=True)
    report = parser.parse_args().report_dir.resolve()
    assert not report.is_relative_to(ROOT)
    report.mkdir(parents=True, exist_ok=True)
    raise SystemExit(run(report))

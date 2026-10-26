"""Opt-in real rename/source-identity save/reopen QA without library conversion.

Builds an automatically cleaned synthetic project; never accepts user CAD paths.
Run with the desktop Python runtime (kipy required).
"""
import argparse
from contextlib import ExitStack
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from unittest.mock import patch

from _scratch import temporary_project
from run_native_pcb_in_place_probe import ROOT, close_fixture, windows
from run_native_project_cycle_probe import OwnedProcess, owned_managers
from run_native_grouped_footprint_probe import saved_rename_checks


def run(report):
    from part_selection_footprint_geometry import _find_kicad_python
    from part_selection_pcb_session import PcbSession
    results = {'passed': False, 'checks': {}}
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW}
    with ExitStack() as stack:
        root = Path(stack.enter_context(temporary_project(prefix='pcb-probe-', short_paths=True)))
        work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='rename-source-', dir=root.anchor)))
        results['fixture'] = str(root)
        settings = work / 'config' / '10.0'
        settings.mkdir(parents=True)
        for source in (Path(os.environ['APPDATA']) / 'kicad' / '10.0').iterdir():
            if source.is_file() and (source.suffix == '.json' or source.name.endswith('lib-table')):
                shutil.copy2(source, settings / source.name)
        common = settings / 'kicad_common.json'
        config = json.loads(common.read_text(encoding='utf-8'))
        config.setdefault('api', {})['enable_server'] = True
        config.setdefault('auto_backup', {})['enabled'] = False
        config.setdefault('session', {})['remember_open_files'] = False
        common.write_text(json.dumps(config), encoding='utf-8')
        stack.enter_context(patch.dict(os.environ, {
            'KICAD_CONFIG_HOME': str(settings.parent), 'KIPRJMOD': str(root),
            'KICAD_PART_SELECTION_HOME': str(work / 'desktop'),
            'KICAD_PART_SELECTION_CACHE': str(work / 'cache'),
            'TEMP': str(work), 'TMP': str(work)}))
        native = _find_kicad_python()
        subprocess.run([str(native), '-B', str(ROOT / 'tests/run_native_pcb_in_place_probe.py'),
            '--build', str(root)], check=True, capture_output=True, timeout=60, **flags)
        for lock in root.glob('~Probe.*.lck'):
            lock.unlink()
        process = subprocess.Popen([str(native.parent / 'kicad.exe'), str(root / 'Probe.kicad_pro')])
        process.probe_started = time.time() - 1
        results['pid'] = process.pid
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                owned = windows(process.pid)
                warning = next((handle for handle, title in owned if title == '文件打开警告'), None)
                if warning:
                    ctypes.windll.user32.PostMessageW(warning, 0x0111, 5104, 0)
                elif any(title.startswith('Probe') and 'KiCad' in title for _, title in owned):
                    break
                time.sleep(.25)
            else:
                raise AssertionError('Synthetic project manager did not become ready')
            from part_selection_pcb_connection import launch_editor
            launch_editor(root / 'Probe.kicad_pcb')
            session = PcbSession(root, socket='ipc://' + str(work / 'kicad/api.sock'))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                connected = session.connection.ensure(open_if_closed=False)
                if connected['state'] == 'connected':
                    break
                time.sleep(.25)
            else:
                raise AssertionError(connected)
            client, board, _ = session._board()
            saved_rename_checks(root, report, session, client, board, results)
            results['functional_passed'] = True
        except BaseException as exc:
            import traceback
            results['error'] = repr(exc)
            (report / 'error.log').write_text(traceback.format_exc(), encoding='utf-8')
        finally:
            failures = []
            for pid in owned_managers(root):
                owned = OwnedProcess(pid)
                owned.probe_started = process.probe_started
                try:
                    close_fixture(owned)
                    owned.wait(10)
                except BaseException as exc:
                    failures.append(repr(exc))
                finally:
                    subprocess._winapi.CloseHandle(owned.handle)
            results['cleanup_errors'] = failures
            results['process_cleanup'] = not owned_managers(root)
    results['directory_cleanup'] = not root.exists()
    results['passed'] = bool(results.get('functional_passed') and results['process_cleanup']
                             and results['directory_cleanup'] and not failures)
    (report / 'capabilities.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps(results, indent=2))
    return 0 if results['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, required=True)
    args = parser.parse_args()
    assert args.report_dir.is_absolute() and not args.report_dir.resolve().is_relative_to(ROOT)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    raise SystemExit(run(args.report_dir))

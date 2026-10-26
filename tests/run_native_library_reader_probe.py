"""Real, opt-in private reader/IPC/desktop probe; accepts no CAD project path.

Run with the plugin desktop Python (kipy). Reports and every CAD write stay
outside the repository. Windows owned by other processes are never changed.
"""
import argparse
from base64 import b64decode
from copy import deepcopy
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from _scratch import temporary_project
import part_selection_pcb_library as reader
from part_selection_pcb_session import decode, describe, geometry_signature


FOOTPRINT = '''(footprint "Target" (version 20241229) (generator "pcbnew") (layer "F.Cu")
 (property "Reference" "REF**" (at 0 -3) (layer "F.SilkS") (effects (font (size 1 1) (thickness .15))))
 (property "Value" "Target" (at 0 3) (layer "F.Fab") (effects (font (size 1 1) (thickness .15))))
 (property "ProbeField" "preserve full definition" (at 0 4) (layer "F.Fab") (effects (font (size 1 1))))
 (fp_text user "geometry marker" (at 0 5) (layer "F.SilkS") (effects (font (size 1 1) (thickness .15))))
 (fp_rect (start -3 -2) (end 3 2) (stroke (width .15) (type default)) (fill none) (layer "F.SilkS"))
 (fp_poly (pts (xy -2 -1) (xy -1 -1) (xy -1 0)) (stroke (width .1) (type default)) (fill solid) (layer "F.Fab"))
 (pad "1" smd custom (at -1.2 0 30) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")
  (options (clearance outline) (anchor rect))
  (primitives (gr_poly (pts (xy -.5 -.5) (xy .7 -.5) (xy .5 .6)) (width 0) (fill yes))))
 (pad "2" thru_hole oval (at 1.2 0 15) (size 1.8 1.3) (drill oval .8 .5) (layers "*.Cu" "*.Mask"))
 (pad "" np_thru_hole circle (at 0 1.5) (size .7 .7) (drill .7) (layers "*.Cu" "*.Mask")))
'''


class Monitor:
    def __init__(self):
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        bind = reader._bind
        self.enum_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        self.event_type = ctypes.WINFUNCTYPE(None, w.HANDLE, w.DWORD, w.HWND,
                                           ctypes.c_long, ctypes.c_long, w.DWORD, w.DWORD)
        bind(self.user, 'EnumDesktopWindows', w.BOOL, w.HANDLE, self.enum_type, w.LPARAM)
        bind(self.user, 'GetWindowThreadProcessId', w.DWORD, w.HWND, ctypes.POINTER(w.DWORD))
        bind(self.user, 'GetThreadDesktop', w.HANDLE, w.DWORD)
        bind(self.user, 'GetUserObjectInformationW', w.BOOL, w.HANDLE, ctypes.c_int,
             w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD))
        bind(self.user, 'OpenInputDesktop', w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
        bind(self.user, 'CloseDesktop', w.BOOL, w.HANDLE)
        bind(self.user, 'SetWinEventHook', w.HANDLE, w.DWORD, w.DWORD, w.HANDLE,
             self.event_type, w.DWORD, w.DWORD, w.DWORD)
        bind(self.user, 'UnhookWinEvent', w.BOOL, w.HANDLE)
        bind(self.user, 'PeekMessageW', w.BOOL, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
        bind(self.user, 'DispatchMessageW', w.LPARAM, ctypes.POINTER(w.MSG))
        bind(self.user, 'IsWindowVisible', w.BOOL, w.HWND)
        bind(self.user, 'GetAncestor', w.HWND, w.HWND, w.UINT)
        bind(self.kernel, 'GetCurrentThreadId', w.DWORD)
        self.desktop = self.user.GetThreadDesktop(self.kernel.GetCurrentThreadId())
        self.name = self.desktop_name(self.desktop)
        interactive = self.user.OpenInputDesktop(0, False, 0x41)
        if not interactive:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            assert self.desktop_name(interactive) == self.name, 'Probe is not on the interactive desktop'
        finally:
            self.user.CloseDesktop(interactive)
        self.records, self.events, self.interactive_windows, self.errors = [], [], [], []
        self.window_desktops = {}
        self.stop, self.ready = threading.Event(), threading.Event()

    def desktop_name(self, handle):
        buffer, needed = ctypes.create_unicode_buffer(256), w.DWORD()
        if not self.user.GetUserObjectInformationW(handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value

    def __enter__(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        assert self.ready.wait(5), 'Desktop monitor did not start'
        assert not self.errors, self.errors
        # A hidden native sentinel proves this hook actually observes creations
        # on the interactive desktop, without showing a test window to the user.
        reader._bind(self.user, 'CreateWindowExW', w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR,
                     w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                     w.HWND, w.HANDLE, w.HANDLE, w.LPVOID)
        reader._bind(self.user, 'DestroyWindow', w.BOOL, w.HWND)
        sentinel = self.user.CreateWindowExW(0, 'STATIC', 'kw-reader-probe-sentinel',
                                            0, 0, 0, 1, 1, None, None, None, None)
        assert sentinel, 'Native hook sentinel could not be created'
        try:
            deadline = time.monotonic() + 5
            while not any(event['hwnd'] == sentinel for event in self.events) and time.monotonic() < deadline:
                time.sleep(.01)
            assert any(event['hwnd'] == sentinel and event['desktop'] == self.name
                       for event in self.events), 'WinEvent hook missed/classified its sentinel incorrectly'
        finally:
            self.user.DestroyWindow(sentinel)
        return self

    def locate_window(self, hwnd, thread_id):
        root = self.user.GetAncestor(hwnd, 2) or hwnd
        desktops = [(self.desktop, self.name)] + [
            (record['desktop'].handle, record['name']) for record in list(self.records)]
        for handle, name in desktops:
            if not handle:
                continue
            found = []
            def visit(candidate, unused):
                if candidate == root or candidate == hwnd:
                    found.append(True)
                return True
            self.user.EnumDesktopWindows(handle, self.enum_type(visit), 0)
            if found:
                self.window_desktops[hwnd, thread_id] = name
                return name
        return self.window_desktops.get((hwnd, thread_id))

    def run(self):
        hook = None
        try:
            def event(hook, event, hwnd, object_id, child_id, thread_id, timestamp):
                if hwnd and object_id == 0 and child_id == 0:
                    pid = w.DWORD()
                    self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    self.events.append({'event': event, 'hwnd': hwnd, 'pid': pid.value,
                        'thread_id': thread_id, 'desktop': self.locate_window(hwnd, thread_id)})
            callback = self.event_type(event)
            hook = self.user.SetWinEventHook(0x8000, 0x8002, None, callback, 0, 0, 0)
            if not hook:
                raise ctypes.WinError(ctypes.get_last_error())
            self.ready.set()
            message = w.MSG()
            while not self.stop.is_set():
                while self.user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    self.user.DispatchMessageW(ctypes.byref(message))
                for record in list(self.records):
                    def visit(hwnd, unused):
                        pid = w.DWORD()
                        tid = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        if pid.value == record['pid']:
                            self.interactive_windows.append((pid.value, hwnd, tid))
                        return True
                    self.user.EnumDesktopWindows(self.desktop, self.enum_type(visit), 0)
                    private = record['desktop'].handle
                    if private:
                        def private_visit(hwnd, unused):
                            pid = w.DWORD()
                            tid = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                            if pid.value == record['pid']:
                                record['windows'].add(hwnd)
                                self.window_desktops[hwnd, tid] = record['name']
                                record['visible_windows'].add(hwnd) if self.user.IsWindowVisible(hwnd) else None
                                # Successful EnumDesktopWindows membership is the
                                # proof; GetThreadDesktop on a foreign process can
                                # be denied even when this desktop is readable.
                                record['thread_desktops'].add(record['name'])
                            return True
                        self.user.EnumDesktopWindows(private, self.enum_type(private_visit), 0)
                self.stop.wait(.002)
        except BaseException:
            self.errors.append(traceback.format_exc())
            self.ready.set()
        finally:
            if hook:
                self.user.UnhookWinEvent(hook)

    def __exit__(self, *exc):
        time.sleep(.1)
        self.stop.set()
        self.thread.join(5)
        assert not self.thread.is_alive(), 'Desktop monitor did not exit'
        assert not self.errors, self.errors


def run(report):
    reader._CACHE.clear()
    records, ipc_definitions = [], []
    original_start, original_exit = reader._ReaderDesktop.start, reader._ReaderDesktop.__exit__

    with Monitor() as monitor:
        def start(desktop, args, env, cwd):
            assert 'KICAD_API_SOCKET' not in env and 'KICAD_API_TOKEN' not in env
            record = {'pid': None, 'thread_id': None,
                      'name': desktop.name, 'desktop': desktop, 'windows': set(),
                      'visible_windows': set(), 'thread_desktops': set(),
                      'config': env['KICAD_CONFIG_HOME'], 'ipc': env['TEMP'],
                      'work': str(Path(cwd).parent)}
            records.append(record)
            monitor.records.append(record)
            process = original_start(desktop, args, env, cwd)
            record.update(pid=process.pid, thread_id=process.thread_id)
            return process

        def exit_reader(desktop, *exc):
            record = next(record for record in records if record['desktop'] is desktop)
            record['natural_reader_exit'] = desktop.process.poll() is not None
            accounting = reader._JobAccounting()
            assert desktop.kernel.QueryInformationJobObject(desktop.job, 1,
                ctypes.byref(accounting), ctypes.sizeof(accounting), None)
            record['job_total_processes'] = accounting.TotalProcesses
            # Capture handles while the Job still proves membership. Do not
            # enumerate or act on similarly named user/old helper processes.
            ids = ctypes.create_string_buffer(8192)
            assert desktop.kernel.QueryInformationJobObject(desktop.job, 3, ids, ctypes.sizeof(ids), None)
            count = ctypes.c_uint32.from_buffer(ids, 4).value
            owned = (ctypes.c_size_t * count).from_buffer(ids, 8)
            reader._bind(desktop.kernel, 'OpenProcess', w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
            handles = [desktop.kernel.OpenProcess(0x100000, False, pid) for pid in owned]
            assert all(handles), 'Could not verify Job-owned helper lifetime'
            try:
                original_exit(desktop, *exc)
                record['all_job_children_exited'] = all(
                    desktop.kernel.WaitForSingleObject(handle, 5000) == 0 for handle in handles)
                assert record['all_job_children_exited']
            finally:
                for handle in handles:
                    desktop.kernel.CloseHandle(handle)

        def capture(fp):
            result = describe(fp)
            assert b64decode(result['proto']) == fp.proto.SerializeToString()
            ipc_definitions.append(result['proto'])
            return result

        with temporary_project(prefix='reader-probe-', short_paths=True) as temporary:
            root = Path(temporary)
            library = root / 'Local.pretty'
            library.mkdir()
            source = library / 'Target.kicad_mod'
            source.write_text(FOOTPRINT, encoding='utf-8')
            (root / 'fp-lib-table').write_text('(fp_lib_table ' + ''.join(
                '(lib (name "' + name + '") (type "KiCad") (uri "${KIPRJMOD}/Local.pretty") '
                '(options "") (descr ""))' for name in ['Local', 'Alias']) + ')', encoding='utf-8')
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            with patch.object(reader._ReaderDesktop, 'start', start), \
                 patch.object(reader._ReaderDesktop, '__exit__', exit_reader), \
                 patch('part_selection_pcb_session.describe', capture):
                front = reader.read_library(root, 'Local:Target')
                assert front['source_hash'] == source_hash and not front['back']
                fp = decode(front['proto'])
                assert len(fp.definition.pads) == 3
                pads = {pad['number']: pad for pad in front['pads']}
                custom = pads['1']['geometry']['copper_layers'][0]
                assert custom['shape'] == 'PSS_CUSTOM' and custom['custom_shapes']
                assert pads['2']['geometry']['drill']['diameter'] == {'x_nm': '800000', 'y_nm': '500000'}
                assert pads['2']['geometry']['drill']['shape'] == 'DS_OBLONG'
                assert pads['']['geometry']['drill']['diameter'] == {'x_nm': '700000', 'y_nm': '700000'}
                kinds = [type(item).__name__ for item in fp.definition.items]
                assert 'BoardShape' in kinds and 'BoardText' in kinds and 'Field' in kinds
                assert any(field.name == 'ProbeField' for field in fp.definition.items if type(field).__name__ == 'Field')
                report['geometry_items'] = kinds
                report['pads'] = deepcopy(front['pads'])
                print('Native front read/IPC geometry passed', flush=True)
                front['pads'][0]['number'] = 'tampered'
                front['proto'] = 'tampered'
                cached = reader.read_library(root, 'Local:Target')
                assert cached['proto'] == ipc_definitions[0] and len(records) == 1
                back = reader.read_library(root, 'Local:Target', back=True)
                assert back['back'] and len(back['pads']) == 3
                alias = reader.read_library(root, 'Alias:Target')
                assert alias['footprint'] == 'Alias:Target'
                assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
                source.write_text(FOOTPRINT.replace('(size 1.8 1.3)', '(size 1.8 1.4)'), encoding='utf-8')
                edited = reader.read_library(root, 'Local:Target')
                assert edited['source_hash'] != source_hash
                assert geometry_signature(edited) != geometry_signature(cached)
                assert len(records) == 4
                print('Native back/alias/hash cache invalidation passed', flush=True)
                reader._CACHE.clear()
                with patch('kipy.KiCad', side_effect=RuntimeError('probe IPC construction failure')):
                    try:
                        reader.read_library(root, 'Local:Target')
                    except RuntimeError as exc:
                        assert 'probe IPC construction failure' in str(exc)
                    else:
                        raise AssertionError('Injected IPC failure did not fail closed')
                assert not reader._CACHE
                assert len(records) == 5
                print('Native early IPC failure/reader/helper cleanup passed', flush=True)
                native_dll = reader.ctypes.WinDLL

                def denied(*args):
                    ctypes.set_last_error(5)
                    return None

                denied_user = SimpleNamespace(CreateDesktopW=Mock(side_effect=denied),
                                              CloseDesktop=monitor.user.CloseDesktop)
                def dll(name, **options):
                    return denied_user if name == 'user32' else native_dll(name, **options)

                with patch.object(reader.ctypes, 'WinDLL', dll):
                    try:
                        reader.read_library(root, 'Local:Target')
                    except OSError as exc:
                        assert exc.winerror == 5
                    else:
                        raise AssertionError('Private desktop denial fell back to an interactive reader')
                assert len(records) == 5 and not reader._CACHE
                report['private_desktop_denial_failed_closed'] = True
                print('Native private desktop denial/no interactive fallback passed', flush=True)
    # The monitor consumes these same records. Stop/join it before formatting
    # records for JSON, including removal of its live desktop reference.
    for record in records:
        assert record['windows'], 'No private reader window observed; proof is incomplete'
        assert record['thread_desktops'] == {record['name']}
        assert record['natural_reader_exit'] and record['all_job_children_exited']
        assert not Path(record['work']).exists(), 'Reader scratch workspace was not removed'
        record['window_count'] = len(record.pop('windows'))
        record['visible_window_count'] = len(record.pop('visible_windows'))
        record['thread_desktops'] = sorted(record['thread_desktops'])
        record.pop('desktop')
    assert not monitor.interactive_windows, monitor.interactive_windows
    owned_pids, owned_threads = {r['pid'] for r in records}, {r['thread_id'] for r in records}
    owned_events = [event for event in monitor.events
                    if event['pid'] in owned_pids or event['thread_id'] in owned_threads]
    for event in owned_events:
        if event['desktop'] is None:
            event['desktop'] = monitor.window_desktops.get((event['hwnd'], event['thread_id']))
    unresolved = [event for event in owned_events if event['desktop'] is None]
    assert not unresolved, ('Reader window events lacked actual desktop membership evidence', unresolved)
    private_names = {record['name'] for record in records}
    assert all(event['desktop'] in private_names | {monitor.name} for event in owned_events)
    interactive_events = [event for event in owned_events if event['desktop'] == monitor.name]
    assert not interactive_events, interactive_events
    report.update(passed=True, interactive_desktop=monitor.name, hook_sentinel_verified=True,
                  event_desktops_verified_by_window_enumeration=True, native_readers=records,
                  interactive_reader_windows=monitor.interactive_windows,
                  interactive_reader_create_show_events=interactive_events,
                  reader_window_events=owned_events,
                  native_full_protobuf_reads=len(ipc_definitions), production_written=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    target = args.report.resolve()
    if not target.is_absolute() or target == ROOT or ROOT in target.parents:
        raise SystemExit('Report must be outside the repository')
    target.parent.mkdir(parents=True, exist_ok=True)
    report = {'passed': False}
    try:
        run(report)
    except BaseException:
        report['error'] = traceback.format_exc()
        raise
    finally:
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'report': str(target)}))


if __name__ == '__main__':
    main()

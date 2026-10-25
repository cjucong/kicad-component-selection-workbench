"""Opt-in capability gate for scoped live PCB footprint replacement.

Run with the desktop runtime Python (kipy required). All writes are confined to
an automatically cleaned fixture. Never accepts a production project path.
"""
import argparse
from contextlib import ExitStack
from copy import deepcopy
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))


def native_build(root, *, manual_mapping=False):
    import pcbnew as p
    root = Path(root)
    board = p.BOARD()
    origin = p.VECTOR2I(0, 0)
    directory = root / 'Local.pretty'
    directory.mkdir()
    sheet, new_root, top_sheet, out_sheet = (str(uuid4()) for _ in range(4))
    symbols, definitions, defined, labels = [], [], set(), []
    from kicad_sexpr import parse_sexpr
    cases = [('R1', 37, False, 8), ('R2', -25, False, 10), ('U1', 90, True, 8), ('U2', -17, True, 10)] if manual_mapping else [('R1', 37, False, 2), ('R2', -25, False, 2), ('U1', 90, True, 4)]
    for index, (ref, angle, back, count) in enumerate(cases):
        def raw(name, size, pad_count=count):
            pad_xy = ([(-1, 0), (1, 0)] if pad_count == 2 else
                      [(-1, -1), (1, -1), (1, 1), (-1, 1)] if pad_count == 4 else
                      [(i//5*8-4, (i%5-2)*2.5) for i in range(pad_count)])
            pads = ''.join(f'(pad "{i}" smd rect (at {x} {y}) (size {size} {size}) '
                           '(layers "F.Cu" "F.Paste" "F.Mask"))'
                           for i, (x, y) in enumerate(pad_xy, 1))
            return p.PCB_IO_KICAD_SEXPR().Parse(f'(footprint "{name}" (version 20241229) '
                '(generator "pcbnew") (layer "F.Cu") '
                '(fp_rect (start -2 -1.6) (end 2 1.6) (stroke (width .15) (type default)) '
                '(fill none) (layer "F.SilkS"))' + pads + ')').Cast()
        old = raw('Original_' + ref, .8)
        old.SetFPID(p.LIB_ID('Local', 'Original_' + ref))
        for child in [*old.Pads(), *old.GraphicalItems(), *old.GetFields()]:
            child.Rotate(origin, p.EDA_ANGLE(90, p.DEGREES_T))
            child.Move(p.VECTOR2I(p.FromMM(4), p.FromMM(-3)))
        p.PCB_IO_KICAD_SEXPR().FootprintSave(str(directory), old)
        board.Add(old)
        old.SetReference(ref)
        old.SetValue('fixture value')
        c_code = 'C12346' if manual_mapping and ref == 'U2' else 'C12345'
        old.SetField('LCSC', c_code)
        old.SetField('LCSC_URL', 'https://so.szlcsc.com/global.html?k=C12345')
        old.SetField('Comment', 'fixture comment ' + ref)
        symbol_id = str(uuid4())
        old.SetPath(p.KIID_PATH('/' + top_sheet + '/' + out_sheet + '/' + symbol_id))
        library_file, entry_name = ('Connector_Generic', f'Conn_01x{count:02d}') if manual_mapping else (('Device', 'R') if ref.startswith('R') else ('Connector_Generic', 'Conn_01x04'))
        library_text = (Path(p.__file__).parents[3] / 'share' / 'kicad' / 'symbols' / (library_file + '.kicad_sym')).read_text(encoding='utf-8')
        definition = next(node for node in parse_sexpr(library_text, 'kicad_symbol_lib').children_named('symbol') if node.arg() == entry_name)
        block = library_text[definition.start:definition.end]
        atom = definition.children[1]
        start, end = atom.start-definition.start, atom.end-definition.start
        lib_id = library_file + ':' + entry_name
        if lib_id not in defined:
            definitions.append(block[:start] + json.dumps(lib_id) + block[end:])
            defined.add(lib_id)
        sx = 50 + index*40 if manual_mapping else 50
        if manual_mapping:
            from run_native_footprint_workflow import symbol_pins
            for pin in symbol_pins(definition):
                number, at = pin.child('number').arg(), pin.child('at')
                x, y = sx+float(at.arg()), 50-float(at.arg(2))
                labels.append(f'(global_label "{ref}_{number}" (shape input) (at {x:.6f} {y:.6f} 0) '
                    f'(effects (font (size 1.27 1.27))) (uuid "{uuid4()}"))')
        symbols.append(f'(symbol (lib_id "{lib_id}") (at {sx} 50 0) (unit 1) (in_bom yes) '
            f'(on_board yes) (dnp no) (uuid "{symbol_id}") '
            f'(property "Reference" "{ref}" (at 50 50 0) (effects (font (size 1.27 1.27)))) '
            '(property "Value" "fixture value" (at 50 52 0) (effects (font (size 1.27 1.27)))) '
            f'(property "Footprint" "Local:Original_{ref}" (at 50 54 0) (effects (font (size 1.27 1.27)) (hide yes))) '
            f'(property "Comment" "fixture comment {ref}" (at 50 56 0) (effects (font (size 1.27 1.27)) (hide yes))) '
            f'(property "LCSC" "{c_code}" (at 50 58 0) (effects (font (size 1.27 1.27)) (hide yes))) '
            '(property "LCSC_URL" "https://so.szlcsc.com/global.html?k=C12345" (at 50 60 0) (effects (font (size 1.27 1.27)) (hide yes))) '
            f'(instances (project "Probe" (path "/{sheet}/{top_sheet}/{out_sheet}" (reference "{ref}") (unit 1)))))')
        if back:
            old.Flip(origin, False)
        old.SetOrientationDegrees(angle)
        old.SetPosition(p.VECTOR2I(p.FromMM(100 if ref == 'U1' else 80), p.FromMM(100 if ref == 'R2' else 80)))
        old.Reference().SetPosition(old.GetPosition() + p.VECTOR2I(p.FromMM(3), p.FromMM(5)))
        for pad in old.Pads():
            net = p.NETINFO_ITEM(board, ('' if manual_mapping else '/') + ref + '_' + pad.GetNumber())
            board.Add(net)
            pad.SetNet(net)
            track = p.PCB_TRACK(board)
            track.SetStart(pad.GetPosition())
            track.SetEnd(pad.GetPosition() + p.VECTOR2I(p.FromMM(1), 0))
            track.SetWidth(p.FromMM(.15))
            track.SetLayer(p.B_Cu if back else p.F_Cu)
            track.SetNet(net)
            board.Add(track)
        target = raw('Standard_' + ref, .9)
        target.SetFPID(p.LIB_ID('Local', 'Standard_' + ('R1' if ref == 'R2' else ref)))
        p.PCB_IO_KICAD_SEXPR().FootprintSave(str(directory), target)
        if manual_mapping:
            mapped = raw('Mapped_' + ref, .9, 10 if count == 8 else 8)
            mapped.SetFPID(p.LIB_ID('Local', 'Mapped_' + ref))
            p.PCB_IO_KICAD_SEXPR().FootprintSave(str(directory), mapped)
        if ref != 'R2':
            centered = raw('Center_' + ref, .9)
            centered.SetFPID(p.LIB_ID('Local', 'Center_' + ref))
            for pad in centered.Pads():
                pad.SetPosition(p.VECTOR2I(round(pad.GetPosition().x*.8), round(pad.GetPosition().y*.8)))
            p.PCB_IO_KICAD_SEXPR().FootprintSave(str(directory), centered)
        board.Add(target)
        if back:
            target.Flip(origin, False)
        target.SetOrientationDegrees(angle + (-90 if back else 90))
        old_pads = {pad.GetNumber(): pad for pad in old.Pads()}
        first = next(iter(target.Pads()))
        target.Move(old_pads[first.GetNumber()].GetPosition() - first.GetPosition())
        for pad in target.Pads():
            delta = pad.GetPosition() - old_pads[pad.GetNumber()].GetPosition()
            assert abs(delta.x) <= 5 and abs(delta.y) <= 5, (ref, 'Bad probe fixture transform', delta)
        target.SetReference('T_' + ref)
    p.SaveBoard(str(root / 'Probe.kicad_pcb'), board)
    (root / 'Probe.kicad_pro').write_text('{}', encoding='utf-8')
    (root / 'Probe.kicad_sch').write_text(f'''(kicad_sch (version 20250114) (generator "eeschema")
        (uuid "{new_root}") (paper "A4") (lib_symbols)
        (sheet (at 30 30) (size 40 30) (uuid "{top_sheet}")
          (property "Sheetname" "Top" (at 30 29 0) (effects (font (size 1.27 1.27))))
          (property "Sheetfile" "Top.kicad_sch" (at 30 62 0) (effects (font (size 1.27 1.27))))
          (instances (project "Probe" (path "/{sheet}" (page "2"))))))''', encoding='utf-8')
    (root / 'Top.kicad_sch').write_text(f'''(kicad_sch (version 20250114) (generator "eeschema")
        (uuid "{uuid4()}") (paper "A4") (lib_symbols)
        (sheet (at 30 30) (size 40 30) (uuid "{out_sheet}")
          (property "Sheetname" "Parts" (at 30 29 0) (effects (font (size 1.27 1.27))))
          (property "Sheetfile" "Parts.kicad_sch" (at 30 62 0) (effects (font (size 1.27 1.27))))
          (instances (project "Probe" (path "/{sheet}/{top_sheet}" (page "3"))))))''', encoding='utf-8')
    (root / 'Parts.kicad_sch').write_text(f'(kicad_sch (version 20250114) (generator "eeschema") '
        f'(uuid "{uuid4()}") (paper "A4") (lib_symbols ' + ''.join(definitions) + ') ' + ''.join(symbols) + ''.join(labels) + ')', encoding='utf-8')
    (root / 'fp-lib-table').write_text('(fp_lib_table (lib (name "Local") (type "KiCad") '
        '(uri "${KIPRJMOD}/Local.pretty") (options "") (descr "")))', encoding='utf-8')


def windows(pid):
    user = ctypes.windll.user32
    result = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            text = ctypes.create_unicode_buffer(1024)
            user.GetWindowTextW(hwnd, text, len(text))
            if text.value:
                result.append((hwnd, text.value))
        return True
    callback = callback_type(visit)
    user.EnumWindows(callback, 0)
    return result


def close_fixture_helpers(pid, since):
    """Only reap this probe's orphaned helper after its owned KiCad exits."""
    command = (f"$since=[DateTimeOffset]::FromUnixTimeSeconds({int(since)}).UtcDateTime; "
        f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 0 }}; "
        f"Get-CimInstance Win32_Process -Filter \"ParentProcessId={pid}\" | "
        "Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*kicad-mcp-client*' "
        "-and $_.CreationDate.ToUniversalTime() -ge $since } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction Stop }")
    subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
        check=True,capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)


def close_fixture(process):
    # This process was created by this probe; never enumerate or close user KiCad.
    user = ctypes.windll.user32
    sent = set()
    for _ in range(60):
        if process.poll() is not None:
            # This KiCad distribution can leave its Copilot helper orphaned.
            # Only verified children born after this owned fixture may be ended.
            since = getattr(process, 'probe_started', None)
            if since is not None:
                close_fixture_helpers(process.pid, since)
            return
        owned = windows(process.pid)
        dialogs = [(h, t) for h, t in owned if t in ('保存变更?', '文件打开警告') or '错误' in t]
        if dialogs:
            for hwnd, title in dialogs:
                # These dialogs belong only to this synthetic fixture.
                user.PostMessageW(hwnd, 0x0111, 5104 if title == '保存变更?' else 5103 if title == '文件打开警告' else 5100, 0)
            time.sleep(.25)
            continue
        editors = [(h, t) for h, t in owned if 'Probe' in t and ('PCB' in t or '原理图' in t)]
        for hwnd, title in owned:
            if title == '文件打开警告':
                user.PostMessageW(hwnd, 0x0111, 5103, 0)
            elif '错误' in title:
                user.PostMessageW(hwnd, 0x0111, 5100, 0)
            elif hwnd not in sent and ((hwnd, title) in editors or (not editors and ('KiCad' in title or title.startswith('Probe')))):
                user.PostMessageW(hwnd, 0x0010, 0, 0)
                sent.add(hwnd)
        time.sleep(.25)
    raise RuntimeError('Fixture editor did not exit normally: ' + str(process.pid))


def footprint_map(board):
    return {fp.reference_field.text.value: fp for fp in board.get_footprints()}


def close_owned_schematic(pid):
    for handle,title in windows(pid):
        if 'Probe' in title and '原理图编辑器' in title:
            ctypes.windll.user32.PostMessageW(handle,0x0010,0,0)
    for _ in range(40):
        if not any('原理图编辑器' in title for _,title in windows(pid)):
            return
        time.sleep(.25)
    raise AssertionError('Owned fixture schematic did not close normally')


def replace_proto(original, target):
    from kipy.board_types import FootprintInstance
    proto = deepcopy(original.proto)
    proto.definition.CopyFrom(target.proto.definition)
    proto.position.CopyFrom(target.proto.position)
    proto.orientation.CopyFrom(target.proto.orientation)
    result = FootprintInstance(proto)
    old_pads = {pad.number: pad for pad in original.definition.pads}
    for pad in result.definition.pads:
        old = old_pads[pad.number]
        pad.proto.id.CopyFrom(old.proto.id)
        pad.proto.net.CopyFrom(old.proto.net)
        pad.proto.symbol_pin.CopyFrom(old.proto.symbol_pin)
    return result


def check_replacement(before, after, target, *, persisted=False, allow_pad_motion=False):
    assert before.id == after.id, 'Footprint UUID changed'
    assert before.proto.symbol_path == after.proto.symbol_path, 'Schematic link changed'
    assert before.layer == after.layer, 'Board side changed'
    for field in ('reference_field', 'value_field', 'datasheet_field', 'description_field'):
        first, second = deepcopy(getattr(before.proto, field)), deepcopy(getattr(after.proto, field))
        if persisted:
            first.ClearField('id')
            second.ClearField('id')
            # KiCad's file-coordinate round trip can quantize text by 1 nm.
            a, b = first.text.text.position, second.text.text.position
            assert max(abs(a.x_nm-b.x_nm), abs(a.y_nm-b.y_nm)) <= 5, field + ' moved on reopen'
            a.CopyFrom(b)
        assert first == second, field + ' changed: ' + str(first) + ' / ' + str(second)
    assert after.definition.id.proto == target.definition.id.proto, 'Standard library binding not applied'
    old_pads = {p.number: p for p in before.definition.pads}
    target_pads = {p.number: p for p in target.definition.pads}
    assert set(old_pads) == {p.number for p in after.definition.pads}, 'Pad set changed'
    for pad in after.definition.pads:
        old, wanted = old_pads[pad.number], target_pads[pad.number]
        assert pad.id == old.id, 'Pad UUID changed'
        assert pad.proto.net == old.proto.net, 'Pad net changed'
        if not allow_pad_motion:
            assert max(abs(pad.position.x - old.position.x), abs(pad.position.y - old.position.y)) <= 5, 'Pad moved'
        actual_stack, expected_stack = deepcopy(pad.proto.pad_stack), deepcopy(wanted.proto.pad_stack)
        delta = (actual_stack.angle.value_degrees - expected_stack.angle.value_degrees + 180) % 360 - 180
        assert abs(delta) <= 1e-5, f'Standard pad angle changed: {actual_stack.angle.value_degrees} / {expected_stack.angle.value_degrees}'
        actual_stack.ClearField('angle')
        expected_stack.ClearField('angle')
        assert actual_stack == expected_stack, 'Standard pad geometry not replaced: ' + str(actual_stack) + ' / ' + str(expected_stack)


WORKBENCH = r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:980}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(60000);
  await page.goto(process.env.PCB_WORKBENCH_URL);
  const center=process.env.PCB_WORKBENCH_CENTER==='1',refs=center?['R1','U1']:['R1','R2','U1'];
  const prefix=center?'Center_':'Standard_';
  for(const ref of refs) {
   const target=ref==='R2'?'R1':ref;
   const row=page.locator(`#rows tr[data-refs="${ref}"]`);await row.waitFor();
   await row.getByRole('button',{name:'预览／管理封装'}).click();
   const library=page.locator('[data-fp-page="library"]');
   await library.locator('[data-fp-items] option[value="Local:'+prefix+target+'"]').waitFor({state:'attached'});
   await library.locator('[data-fp-items]').selectOption('Local:'+prefix+target);
   await library.getByText('Local:'+prefix+target+' · 焊盘 '+(ref.startsWith('R')?2:4)+' 个 · 编号 '+(ref.startsWith('R')?'1, 2':'1, 2, 3, 4'),{exact:true}).waitFor();
   await library.locator('[data-fp-ack]').check();await library.locator('.fp-apply').click();
   await page.waitForFunction(({ref,target,prefix})=>currentRows.find(row=>row.ref===ref)?.footprint==='Local:'+prefix+target,{ref,target,prefix});
  }
  for(const width of [1440,800,390]) {
   await page.setViewportSize({width,height:980});
   await page.screenshot({path:process.env.PCB_WORKBENCH_REPORT+'/workbench-'+width+'.png'});
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({ok:true,ui_library_selection:true,refs,alignment:center?'center':'rigid'}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


def sync_fixture_adapter(app, refs, *, alignment='rigid'):
    """Test adapter undo with a persistent session, never full project restart."""
    from unittest.mock import patch
    with patch.object(app, 'prepare_kicad_for_external_schematic_write',
                      side_effect=AssertionError('Adapter probe must not close the whole project')):
        closed = app.run_kicad_script(app.kicad_url_for_refs(refs),
            ['-Mode','FindDialog','-SaveActiveFrame','-CloseAfterSave','-SaveOnly','-ExistingOnly'])
        assert closed['ok'], closed
        synced = app.bulk_sync_bom_state_to_schematic('__schematic__', selected_refs=refs,
                                                     prepare=False, notify=False)
        assert synced['ok'] and synced['schematic_verify']['ok'], synced
        preview = app.pcb_updates.preview(refs, '__schematic__', alignment=alignment)
        assert not preview['blocked'], preview
        applied = app.apply_design_pcb(preview['preview_id'])
        assert applied['ok'] and not applied['saved'], applied
        return applied


def workbench_flow(root, report, session, *, center=False):
    import threading
    from http.server import ThreadingHTTPServer
    from part_selection_paths import resolve_config
    from part_selection_server import PartSelectionApp, make_handler
    from part_selection_pcb_session import PcbSession
    app = PartSelectionApp(resolve_config(root))
    # The workbench has no PCB launch transport, as with a separate schematic
    # entry. Discover and verify the already-open fixture board on demand.
    app.pcb_updates.session = PcbSession(root)
    app.pcb_updates.session.socket = ''
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = dict(os.environ, PCB_WORKBENCH_URL=f'http://127.0.0.1:{server.server_port}/',
                   PCB_WORKBENCH_REPORT=str(report), PCB_WORKBENCH_CENTER='1' if center else '0')
        result = subprocess.run(['node', '-e', WORKBENCH], capture_output=True,
                                text=True, encoding='utf-8', env=env, timeout=240,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        (report / 'workbench.log').write_text(result.stdout + result.stderr, encoding='utf-8')
        assert result.returncode == 0, result.stderr
        outcome = json.loads(result.stdout)
        # This probe owns a persistent native session to test one-step undo.
        # Exercise the adapter explicitly, not the user save/restart cycle.
        refs = outcome['refs']
        sync_fixture_adapter(app, refs, alignment=outcome['alignment'])
        assert app.pcb_updates.session.socket == session.socket
        return {**outcome, 'adapter_only':True, 'pcb_connection_discovered_on_demand': True}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.selection_jobs.close()
        app.search_jobs.close()
        app.design_sync.close()


def run_probe(report, extra_checks=None, *, long_path=False, manager_width=None):
    from _scratch import temporary_project
    from part_selection_footprint_geometry import _find_kicad_python
    from kipy import KiCad
    from kipy.board import Board
    from kipy.proto.common.types import DocumentType
    native = _find_kicad_python()
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW}
    results = {'passed': False, 'checks': {}, 'stage': 'build'}
    process = None
    stack = ExitStack()
    try:
        root = Path(stack.enter_context(temporary_project(prefix='pcb-probe-', short_paths=not long_path)))
        cache = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='pcb-ipc-', dir=root.anchor)))
        results['fixture'] = str(root)
        try:
            built = subprocess.run([str(native), '-X', 'faulthandler', '-B', str(Path(__file__)), '--build', str(root)],
                                   capture_output=True, text=True, timeout=60, **flags)
            (report / 'build.log').write_text(built.stdout + built.stderr, encoding='utf-8')
            built.check_returncode()
            shutil.copy2(root / 'Probe.kicad_sch', report / 'fixture.kicad_sch')
            exported = subprocess.run([str(native.parent / 'kicad-cli.exe'), 'sch', 'export', 'netlist',
                '-o', str(root / 'fixture.net'), str(root / 'Probe.kicad_sch')],
                capture_output=True, text=True, timeout=45, **flags)
            (report / 'schematic-validation.log').write_text(exported.stdout + exported.stderr, encoding='utf-8')
            exported.check_returncode()
            # SWIG's project singleton can leave a fixture lock after the worker
            # exits. No GUI has been launched for this new, unique root yet.
            for lock in root.glob('~Probe.*.lck'):
                lock.unlink()
            settings = cache / 'config' / '10.0'
            settings.mkdir(parents=True)
            user_settings = Path(os.environ['APPDATA']) / 'kicad' / '10.0'
            for source in user_settings.iterdir():
                if source.is_file() and (source.suffix == '.json' or source.name.endswith('lib-table')):
                    shutil.copy2(source, settings / source.name)
            common = settings / 'kicad_common.json'
            config = json.loads(common.read_text(encoding='utf-8'))
            config.setdefault('api', {})['enable_server'] = True
            config.setdefault('auto_backup', {})['enabled'] = False
            config.setdefault('session', {})['remember_open_files'] = False
            common.write_text(json.dumps(config), encoding='utf-8')
            socket_dir = cache / 'ipc'
            socket_dir.mkdir()
            env = dict(os.environ, KICAD_CONFIG_HOME=str(settings.parent), TEMP=str(socket_dir), TMP=str(socket_dir))
            process = subprocess.Popen([str(native.parent / 'kicad.exe'), str(root / 'Probe.kicad_pro')], env=env)
            process.probe_started = time.time() - 1
            results.update(pid=process.pid, stage='connect')
            socket = 'ipc://' + str(socket_dir / 'kicad' / 'api.sock')
            client = KiCad(socket_path=socket, client_name='workbench-capability-probe', timeout_ms=1000)
            documents = []
            opened = False
            lock_acknowledged = False
            for attempt in range(45):
                try:
                    documents = list(client.get_open_documents(DocumentType.DOCTYPE_PCB))
                    if documents:
                        break
                except Exception as exc:
                    results['connect_error'] = str(exc)
                if not lock_acknowledged:
                    for hwnd, title in windows(process.pid):
                        if title == '文件打开警告':
                            # A brand-new synthetic root and this sole owned GUI
                            # process: never acknowledge a production lock.
                            ctypes.windll.user32.PostMessageW(hwnd, 0x0111, 5104, 0)
                            lock_acknowledged = True
                            results['fixture_lock_acknowledged'] = True
                if not opened:
                    for hwnd, title in windows(process.pid):
                        if title.startswith('Probe') and 'KiCad' in title:
                            if manager_width is not None:
                                user = ctypes.windll.user32
                                user.ShowWindow(hwnd, 9)
                                user.SetWindowPos(hwnd, 0, 0, 0, manager_width, 600, 0x16)
                                time.sleep(2)
                                results['narrow_manager_width'] = manager_width
                                inspection = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                    '[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); '
                                    'Add-Type -AssemblyName UIAutomationClient; Add-Type -AssemblyName UIAutomationTypes; '
                                    '$e=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]' + str(hwnd) + '); '
                                    '$e.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition) | '
                                    'ForEach-Object { $c=$_.Current; '
                                    '$value=$null;try {$value=$_.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value}catch{}; '
                                    '[pscustomobject]@{Name=$c.Name;Value=$value;Help=$c.HelpText;Id=$c.AutomationId} } | ConvertTo-Json'],
                                    capture_output=True, text=True, encoding='utf-8', timeout=10, **flags)
                                (report / 'manager-accessibility.json').write_text(inspection.stdout + inspection.stderr, encoding='utf-8')
                            from part_selection_pcb_connection import launch_editor
                            started=time.perf_counter()
                            launch_editor(root / 'Probe.kicad_pcb')
                            results['manager_open_seconds']=time.perf_counter()-started
                            opened = True
                time.sleep(1)
            if len(documents) != 1:
                results['windows'] = [title for _, title in windows(process.pid)]
                raise RuntimeError('No unique fixture PCB API document: ' + results.get('connect_error', ''))
            document = documents[0]
            assert Path(document.project.path).resolve() == root.resolve(), 'Wrong API project'
            assert document.board_filename == 'Probe.kicad_pcb', 'Wrong API board'
            board = Board(client._client, document)
            results['version'] = str(client.get_version())
            results['checks']['project_identity'] = True
            disk = (root / 'Probe.kicad_pcb').read_bytes()
            libraries = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / 'Local.pretty').glob('*')}
            before = footprint_map(board)
            results['stage'] = 'unsaved_edit'
            unsaved = deepcopy(before['T_R1'])
            unsaved.value_field.text.value = 'prior unsaved edit'
            board.update_items(unsaved)
            before = footprint_map(board)
            tracks_before = [t.proto.SerializeToString() for t in board.get_tracks()]
            (report / 'before.kicad_pcb').write_text(board.get_as_string(), encoding='utf-8')
            replacements = [replace_proto(before[ref], before['T_' + ref]) for ref in ('R1', 'U1')]
            results['stage'] = 'replace'
            commit = board.begin_commit()
            try:
                board.update_items(replacements)
                board.push_commit(commit, 'Workbench capability probe')
            except BaseException:
                board.drop_commit(commit)
                raise
            results['stage'] = 'verify'
            after = footprint_map(board)
            (report / 'after.kicad_pcb').write_text(board.get_as_string(), encoding='utf-8')
            for ref in ('R1', 'U1'):
                check_replacement(before[ref], after[ref], before['T_' + ref])
            for ref in ('T_R1', 'T_R2', 'R2', 'T_U1'):
                assert before[ref].proto == after[ref].proto, 'Unselected item changed'
            assert tracks_before == [t.proto.SerializeToString() for t in board.get_tracks()], 'Tracks changed'
            assert disk == (root / 'Probe.kicad_pcb').read_bytes(), 'PCB automatically saved'
            assert libraries == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / 'Local.pretty').glob('*')}, 'Library changed'
            results['checks']['replacement_identity_geometry_nets'] = True
            results['checks']['unselected_unsaved_disk_and_library'] = True
            results['stage'] = 'undo'
            undo_result = client.run_action('common.Interactive.undo')
            results['undo_response'] = str(undo_result)
            time.sleep(.25)
            undone = footprint_map(board)
            for ref in before:
                assert before[ref].proto == undone[ref].proto, 'Undo did not restore only this batch: ' + ref
            results['checks']['single_undo_preserves_prior_edit'] = True
            results['stage'] = 'production_adapter'
            from part_selection_pcb_session import PcbSession, replacement, encode, decode
            from part_selection_pcb_geometry import solve_placement
            session = PcbSession(root, socket=socket)
            snapshot = session.snapshot(root, ['R1', 'R2', 'U1'])
            changes = []
            for ref, original in snapshot['parts'].items():
                target = session.read_library('Local:Standard_' + ('R1' if ref == 'R2' else ref), back=original['back'])
                placement = solve_placement(original, target)
                assert placement['ok'], placement
                updated = replacement(original, target, placement)
                changes.append({'ref': ref, 'proto': encode(updated)})
            result = session.apply(snapshot, changes, report / ('transaction-' + str(uuid4())),
                                   schematic_close_verified=True)
            assert result['ok'] and result['saved'] is False
            assert disk == (root / 'Probe.kicad_pcb').read_bytes(), 'Adapter auto-saved PCB'
            client.run_action('common.Interactive.undo')
            time.sleep(.25)
            assert session.snapshot(root, ['R1', 'R2', 'U1']) == snapshot, 'Adapter single undo failed'
            results['checks']['production_library_reader_and_transaction'] = True
            if extra_checks is not None:
                results['stage'] = 'design_sync_metadata'
                extra_checks(root, report, session, client, board, results)
            results['stage'] = 'workbench_flow'
            # Exercise the existing save/close sync guard on an actually open
            # schematic in the same verified manager, not an absent-window shortcut.
            for hwnd, title in windows(process.pid):
                if title.startswith('Probe') and 'KiCad' in title:
                    ctypes.windll.user32.PostMessageW(hwnd, 0x0111, 20010, 0)
            for _ in range(60):
                if any('原理图编辑器' in title for _, title in windows(process.pid)):
                    break
                time.sleep(.25)
            else:
                raise RuntimeError('Fixture schematic did not open in its manager')
            results['checks']['workbench_select_sync_update'] = workbench_flow(root, report, session)
            live = footprint_map(board)
            for ref in ('R1', 'R2', 'U1'):
                check_replacement(before[ref], live[ref], before['T_' + ref])
            assert disk == (root / 'Probe.kicad_pcb').read_bytes(), 'Workbench auto-saved PCB'
            from part_selection_paths import resolve_config
            from part_selection_server import PartSelectionApp
            completed_app = PartSelectionApp(resolve_config(root))
            completed_app.pcb_updates.session = session
            reads = session.read_library
            library_calls = []
            def counted_library(*args, **kwargs):
                library_calls.append(args)
                return reads(*args, **kwargs)
            session.read_library = counted_library
            try:
                completed = completed_app.pcb_updates.preview(['R1','R2','U1'], '__schematic__', alignment='rigid')
                assert all(item['kind'] == 'none' and not item['needs_ack'] for item in completed['items']), completed
                count = len(library_calls)
                repeated = completed_app.pcb_updates.preview(['R1','R2','U1'], '__schematic__', alignment='rigid')
                assert all(item['kind'] == 'none' for item in repeated['items'])
                assert len(library_calls) == count, 'Completed replacements reloaded library geometry'
                assert completed_app.pcb_updates.apply(repeated['preview_id'], True)['no_changes']
                results['checks']['completed_replacement_no_repeat_or_library_reload'] = True
            finally:
                session.read_library = reads
                completed_app.selection_jobs.close()
                completed_app.search_jobs.close()
                completed_app.design_sync.close()
            results['stage'] = 'center_workbench_flow'
            center_before = session.snapshot(root, ['R1', 'U1'])
            center_changes = []
            for ref, original in center_before['parts'].items():
                target = session.read_library('Local:Center_' + ref, back=original['back'])
                placement = solve_placement(original, target, alignment='center')
                assert placement['ok'] and placement['max_error_mm'] > .1
                center_changes.append({'ref': ref, 'proto': encode(replacement(original, target, placement))})
            center_report = report / 'center'
            center_report.mkdir(exist_ok=True)
            results['checks']['center_workbench_update'] = workbench_flow(root, center_report, session, center=True)
            centered = session.snapshot(root, ['R1', 'U1'])
            # Runtime writes generate fresh graphic UUIDs. Independent golden
            # checks compare placement/geometry and preserved instance identity,
            # not those intentionally new random identifiers.
            from part_selection_pcb_session import outside_parts
            selected_ids = {p['uuid'] for p in center_before['parts'].values()}
            assert outside_parts(center_before['text'], selected_ids) == outside_parts(centered['text'], selected_ids)
            for change in center_changes:
                ref = change['ref']
                expected = decode(change['proto'])
                actual = decode(centered['parts'][ref]['proto'])
                check_replacement(decode(center_before['parts'][ref]['proto']), actual, expected, allow_pad_motion=True)
                expected_pads = {pad.number: pad for pad in expected.definition.pads}
                for pad in actual.definition.pads:
                    assert max(abs(pad.position.x-expected_pads[pad.number].position.x),
                               abs(pad.position.y-expected_pads[pad.number].position.y)) <= 5
            def pad_center(part):
                return [(min(p['position_mm'][i] for p in part['pads']) +
                         max(p['position_mm'][i] for p in part['pads']))/2 for i in (0, 1)]
            for ref in ('R1', 'U1'):
                assert math.dist(pad_center(center_before['parts'][ref]), pad_center(centered['parts'][ref])) <= .000005
                old = {p['number']: p for p in center_before['parts'][ref]['pads']}
                assert max(math.dist(p['position_mm'], old[p['number']]['position_mm']) for p in centered['parts'][ref]['pads']) > .1
            assert tracks_before == [t.proto.SerializeToString() for t in board.get_tracks()], 'Center update moved tracks'
            assert disk == (root / 'Probe.kicad_pcb').read_bytes(), 'Center update auto-saved PCB'
            assert libraries == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / 'Local.pretty').glob('*')}, 'Center update changed library'
            client.run_action('common.Interactive.undo')
            time.sleep(.25)
            assert session.snapshot(root, ['R1', 'U1']) == center_before, 'Center update was not one undo step'
            close_owned_schematic(process.pid)
            session.apply(center_before, center_changes, report / ('center-reapply-' + str(uuid4())),
                          schematic_close_verified=True)
            live = footprint_map(board)
            results['checks']['center_world_coordinates_tracks_and_undo'] = True
            results['stage'] = 'save_and_reopen'
            board.save()  # Only the disposable fixture: explicit persistence verification.
            saved = (root / 'Probe.kicad_pcb').read_text(encoding='utf-8')
            assert 'Standard_R1' in saved and 'prior unsaved edit' in saved
            results['checks']['fixture_save'] = True
            for hwnd,title in windows(process.pid):
                if 'PCB' in title and 'Probe' in title:
                    ctypes.windll.user32.PostMessageW(hwnd,0x0010,0,0)
            for _ in range(60):
                if not any('PCB' in title and 'Probe' in title for _,title in windows(process.pid)):
                    break
                time.sleep(.25)
            else:
                raise RuntimeError('Fixture PCB did not close normally')
            results['stage'] = 'reopen'
            events=[]
            started=time.perf_counter()
            connection = session.connection.ensure(open_if_closed=True,
                progress=lambda stage,message:events.append({'stage':stage,'message':message,'seconds':time.perf_counter()-started}))
            assert connection['state']=='connected', connection
            assert process.poll() is None
            assert any('PCB' in title for _,title in windows(process.pid)), 'PCB not hosted by original manager'
            results['manager_reopen_progress']=events
            results['checks']['manager_hosted_open_and_reopen']=True
            results['checks']['absent_only_native_auto_open'] = True
            reopened = None
            for _ in range(25):
                try:
                    reopened = session.snapshot(root, ['R1', 'R2', 'U1'])
                    break
                except Exception:
                    time.sleep(.25)
            assert reopened is not None, 'Saved fixture did not reopen'
            (report / 'reopened.json').write_text(json.dumps(reopened, indent=2), encoding='utf-8')
            for ref in ('R1', 'R2', 'U1'):
                check_replacement(live[ref], decode(reopened['parts'][ref]['proto']), live[ref], persisted=True)
            results['checks']['saved_and_reopened'] = True
            results['functional_passed'] = True
            results['stage'] = 'complete'
        except Exception as exc:
            results['error'] = repr(exc)
            if process is not None:
                results['failure_windows'] = windows(process.pid)
            import traceback
            (report / 'error.log').write_text(traceback.format_exc(), encoding='utf-8')
        finally:
            if process is not None:
                # Discard only probe edits by undoing its batches before normal close.
                try:
                    board.save()  # Fixture cleanup only; never a production save path.
                except Exception:
                    pass
                try:
                    close_fixture(process)
                    process.wait(timeout=10)
                    time.sleep(1)
                    results['process_cleanup'] = True
                except Exception as exc:
                    results['process_cleanup'] = False
                    results['cleanup_error'] = repr(exc)
            else:
                results['process_cleanup'] = True
            results['cleanup_cwd'] = os.getcwd()
            os.chdir(ROOT)
            import gc
            gc.collect()
    finally:
        try:
            stack.close()
            results['directory_cleanup'] = True
        except OSError as exc:
            results['directory_cleanup'] = False
            results['directory_cleanup_error'] = repr(exc)
        results['passed'] = bool(results.get('functional_passed') and results.get('process_cleanup') and results['directory_cleanup'])
        (report / 'capabilities.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps(results, indent=2))
    return 0 if results['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path)
    parser.add_argument('--report-dir', type=Path)
    parser.add_argument('--long-path', action='store_true')
    parser.add_argument('--manager-width', type=int)
    args = parser.parse_args()
    if args.build:
        if not args.build.parent.name.startswith('pcb-probe-') or args.build.name != 'project':
            parser.error('Only a disposable capability fixture may be built')
        native_build(args.build)
        return 0
    if not args.report_dir or not args.report_dir.is_absolute():
        parser.error('--report-dir requires an external absolute directory')
    report = args.report_dir.resolve()
    if report.is_relative_to(ROOT):
        parser.error('Reports must stay outside source')
    report.mkdir(parents=True, exist_ok=True)
    return run_probe(report, long_path=args.long_path, manager_width=args.manager_width)


if __name__ == '__main__':
    raise SystemExit(main())

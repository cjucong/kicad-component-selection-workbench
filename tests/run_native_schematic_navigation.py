"""Exercise hidden Search panel and cross-sheet jumps in a disposable KiCad project."""
import argparse
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import traceback
from unittest.mock import patch

from _scratch import temporary_project
from run_native_pcb_in_place_probe import ROOT, windows, close_fixture
from run_native_project_cycle_probe import OwnedProcess, owned_managers, FLAGS


def child_windows(pid):
    result = []
    user = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user.IsWindowVisible(hwnd):
            cls, text = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(512)
            user.GetClassNameW(hwnd, cls, len(cls)); user.GetWindowTextW(hwnd, text, len(text))
            rect = wintypes.RECT(); user.GetWindowRect(hwnd, ctypes.byref(rect))
            result.append({'class': cls.value, 'text': text.value,
                           'rect': [rect.left, rect.top, rect.right, rect.bottom]})
        return True
    callback = callback_type(visit)
    for hwnd, title in windows(pid):
        if '原理图编辑器' in title: user.EnumChildWindows(hwnd, callback, 0)
    return result


def run(report):
    from part_selection_footprint_geometry import _find_kicad_python
    from part_selection_paths import resolve_config
    from part_selection_server import PartSelectionApp
    result = {'passed': False, 'jumps': []}
    app = None
    with ExitStack() as stack:
        scratch = temporary_project(prefix='pcb-probe-')
        root = Path(scratch.__enter__())
        def safe_cleanup():
            if owned_managers(root):
                raise RuntimeError('Fixture is still open; preserve its project and configuration: ' + str(root))
            scratch.cleanup()
        stack.callback(safe_cleanup)
        settings = root.parent / 'isolated-config' / '10.0'
        settings.mkdir(parents=True)
        for source in (Path(os.environ['APPDATA']) / 'kicad/10.0').iterdir():
            if source.is_file() and (source.suffix == '.json' or source.name.endswith('lib-table')):
                shutil.copy2(source, settings / source.name)
        eeschema = settings / 'eeschema.json'
        config = json.loads(eeschema.read_text(encoding='utf-8'))
        # Existing versions store this setting under different parents.
        def hide_search(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == 'show_search': value[key] = False
                    else: hide_search(item)
        hide_search(config)
        config.setdefault('search_pane', {})['selection_zoom'] = 0
        eeschema.write_text(json.dumps(config), encoding='utf-8')
        common = settings / 'kicad_common.json'
        config = json.loads(common.read_text(encoding='utf-8'))
        config.setdefault('auto_backup', {})['enabled'] = False
        config.setdefault('session', {})['remember_open_files'] = False
        common.write_text(json.dumps(config), encoding='utf-8')
        stack.enter_context(patch.dict(os.environ, {
            'KICAD_CONFIG_HOME': str(settings.parent), 'TEMP': str(root.parent),
            'TMP': str(root.parent), 'KICAD_PART_SELECTION_CACHE': str(root.parent / 'cache'),
        }))
        native = _find_kicad_python()
        subprocess.run([str(native), '-B', str(ROOT / 'tests/run_native_pcb_in_place_probe.py'),
                        '--build', str(root)], check=True, capture_output=True, timeout=60, **FLAGS)
        # Put R2 on the root page and rename the intermediate sheet; navigation
        # must not assume every symbol lives below a sheet literally named Top.
        from kicad_sexpr import parse_sexpr
        main_path, parts_path = root/'Probe.kicad_sch', root/'Parts.kicad_sch'
        main_text, parts_text = main_path.read_text(encoding='utf-8'), parts_path.read_text(encoding='utf-8')
        main_node, parts_node = parse_sexpr(main_text), parse_sexpr(parts_text)
        symbol = next(s for s in parts_node.children_named('symbol') if any(
            p.arg() == 'Reference' and p.arg(2) == 'R2' for p in s.children_named('property')))
        instance = symbol.child('instances').child('project').child('path').children[1]
        block = parts_text[symbol.start:instance.start] + json.dumps('/'+main_node.child('uuid').arg()) + parts_text[instance.end:symbol.end]
        libraries = parts_node.child('lib_symbols')
        empty_libraries = main_node.child('lib_symbols')
        main_text = (main_text[:empty_libraries.start] + parts_text[libraries.start:libraries.end]
                     + main_text[empty_libraries.end:])
        main_node = parse_sexpr(main_text)
        name = next(p for p in main_node.child('sheet').children_named('property') if p.arg() == 'Sheetname').children[2]
        main_text = main_text[:name.start] + json.dumps('Module') + main_text[name.end:]
        main_text = main_text.rstrip()[:-1] + block + ')'
        main_path.write_text(main_text, encoding='utf-8')
        parts_path.write_text(parts_text[:symbol.start]+parts_text[symbol.end:], encoding='utf-8')
        # Mark the placed U1 as unit A of a two-unit symbol, so a base U1
        # search cannot succeed without resolving the saved display suffix.
        parts_text = parts_path.read_text(encoding='utf-8')
        parts_node = parse_sexpr(parts_text)
        u1 = next(s for s in parts_node.children_named('symbol') if any(
            p.arg() == 'Reference' and p.arg(2) == 'U1' for p in s.children_named('property')))
        definition = next(s for s in parts_node.child('lib_symbols').children_named('symbol')
                          if s.arg() == u1.child('lib_id').arg())
        unit = next(s for s in definition.children_named('symbol') if s.arg().endswith('_1_1'))
        extra = parts_text[unit.start:unit.end].replace('_1_1"', '_2_1"', 1)
        parts_path.write_text(parts_text[:definition.end-1] + extra + parts_text[definition.end-1:], encoding='utf-8')
        for lock in root.glob('~Probe.*.lck'): lock.unlink()
        protected = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.kicad_sch')}
        protected.update({p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.kicad_pcb')})
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 1
        subprocess.Popen([str(native.parent / 'kicad.exe'), str(root / 'Probe.kicad_pro')],
                         startupinfo=startup, **FLAGS)
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                pids = owned_managers(root)
                if len(pids) == 1:
                    items = windows(pids[0])
                    for handle, title in items:
                        if title == '文件打开警告':
                            ctypes.windll.user32.PostMessageW(handle, 0x0111, 5104, 0)
                    if any(title.startswith('Probe ') and 'KiCad' in title for _, title in items): break
                time.sleep(.2)
            assert len(owned_managers(root)) == 1
            app = PartSelectionApp(resolve_config(root))
            opened = app.run_kicad_script(app.kicad_url_for_refs(['R1']), ['-Mode', 'SearchPanel', '-NotifyOnly'])
            assert opened['ok'], opened
            pid = owned_managers(root)[0]
            initial = windows(pid)
            assert any('Probe' in title and '原理图编辑器' in title and 'Parts' not in title
                       for _, title in initial), initial
            result['initial_root_sheet'] = True
            result['initial_controls'] = child_windows(pid)
            # Set and verify the actual panel state, independent of persisted
            # AUI layout restoration in the installed KiCad build.
            handle = next(h for h, title in initial if '原理图编辑器' in title)
            hide = r'''
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:TEST_SEARCH_SCRIPT,[ref]$tokens,[ref]$errors)
foreach($n in $ast.FindAll({param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) {
    if($n.Name -in @('Ensure-NativeMethods','Get-ChildWindowInfos','Find-KiCadMenuCommand')) {
        . ([scriptblock]::Create($n.Extent.Text))
    }
}
Ensure-NativeMethods
$hwnd=[IntPtr]::new([long]$env:TEST_SEARCH_FRAME)
if (@(Get-ChildWindowInfos $hwnd | Where-Object { $_.Text -eq 'searchCtrl' }).Count) {
    $label=-join ([char]0x641C,[char]0x7D22)
    $command=Find-KiCadMenuCommand -Menu ([NativeMethods]::GetMenu($hwnd)) -Labels @('Search',$label)
    if ($null -eq $command) { throw 'Missing fixture search menu' }
    [NativeMethods]::PostMessage($hwnd,0x111,[IntPtr]$command,[IntPtr]::Zero) | Out-Null
    Start-Sleep -Milliseconds 300
}
if (@(Get-ChildWindowInfos $hwnd | Where-Object { $_.Text -eq 'searchCtrl' }).Count) { throw 'Fixture panel is still visible' }
'''
            hidden = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', hide],
                env={**os.environ, 'TEST_SEARCH_SCRIPT': str(ROOT/'tools/kicad_schematic_search.ps1'),
                     'TEST_SEARCH_FRAME': str(handle)}, capture_output=True, timeout=20, **FLAGS)
            assert hidden.returncode == 0, hidden.stderr
            assert not any(c['text'] == 'searchCtrl' for c in child_windows(pid))
            result['hidden_panel_verified'] = True
            assert app.design_sync.sources.navigation_target('U1').search_name == 'U1A'
            result['multiunit_display_verified'] = True
            for ref in ['R1', 'R2', 'U1', 'R1']:
                if ref == 'R2':
                    # Simulate a user leaving Search on Text/Labels rather
                    # than Symbols; the next jump must restore Symbols.
                    switch = hide[:hide.index("if (@(Get-ChildWindowInfos")] + r'''
$tabs=@(Get-ChildWindowInfos $hwnd | Where-Object { $_.ClassName -match 'SysTabCtl32|SysTabControl32' })
if ($tabs.Count -ne 1) { throw 'Fixture search tabs not found' }
[UIntPtr]$result=[UIntPtr]::Zero
[NativeMethods]::SendMessageTimeout($tabs[0].Hwnd,0x1330,[IntPtr]1,[IntPtr]::Zero,0x22,500,[ref]$result) | Out-Null
[NativeMethods]::SendMessageTimeout($tabs[0].Hwnd,0x130b,[IntPtr]::Zero,[IntPtr]::Zero,0x22,500,[ref]$result) | Out-Null
if ($result.ToUInt64() -ne 1) { throw 'Fixture search tab did not change' }
'''
                    changed = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',switch],
                        env={**os.environ,'TEST_SEARCH_SCRIPT':str(ROOT/'tools/kicad_schematic_search.ps1'),
                             'TEST_SEARCH_FRAME':str(handle)}, capture_output=True,timeout=20,**FLAGS)
                    assert changed.returncode == 0, changed.stderr
                    result['non_symbol_tab_verified'] = True
                started = time.monotonic()
                answer = app.jump_to_schematic(app.kicad_url_for_refs([ref]))
                entry = {'ref': ref, 'elapsed': round(time.monotonic() - started, 3), 'answer': answer}
                result['jumps'].append(entry)
                result['last_controls'] = child_windows(pid)
                assert answer['ok'], entry
                titles = [title for _, title in windows(pid)]
                assert '符号属性' not in titles, titles
                expected = 'Probe — 原理图编辑器' if ref == 'R2' else 'Parts [Probe/Module/Parts]'
                assert any(expected in title for title in titles), titles
                print(json.dumps(entry, ensure_ascii=False), flush=True)
            assert all(hashlib.sha256(p.read_bytes()).hexdigest() == expected
                       for p, expected in protected.items()), 'Navigation rewrote CAD'
            result['cad_unchanged'] = True
            result['functional_passed'] = True
        except BaseException:
            result['error'] = traceback.format_exc()
        finally:
            log = root.parent / 'kicad_schematic_search.log'
            if log.exists(): shutil.copy2(log, report / log.name)
            if app:
                app.design_sync.close(); app.search_jobs.close(); app.selection_jobs.close()
            for pid in owned_managers(root):
                process = OwnedProcess(pid)
                try:
                    for handle, title in windows(pid):
                        if title == '符号属性':
                            ctypes.windll.user32.PostMessageW(handle, 0x0010, 0, 0)
                    close_fixture(process); process.wait(15)
                finally:
                    subprocess._winapi.CloseHandle(process.handle)
            result['process_cleanup'] = not owned_managers(root)
            os.chdir(ROOT)
    result['directory_cleanup'] = not root.exists()
    result['passed'] = bool(result.get('functional_passed') and not result.get('error')
                            and result['process_cleanup'] and result['directory_cleanup'])
    (report / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if not k.endswith('_controls')}, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, required=True)
    args = parser.parse_args()
    assert args.report_dir.is_absolute() and not args.report_dir.resolve().is_relative_to(ROOT)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    raise SystemExit(run(args.report_dir))

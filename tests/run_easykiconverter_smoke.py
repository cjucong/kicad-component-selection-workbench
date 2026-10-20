"""Opt-in full-package conversion smoke test; writes disposable projects only."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import zipfile
from _scratch import temporary_project

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from part_selection_runtime import runtime_directory


def console_windows():
    if os.name != 'nt':
        return set()
    handles = set()
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    @callback_type
    def collect(hwnd, _):
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(ctypes.c_void_p(hwnd), name, 256)
        if name.value == 'ConsoleWindowClass' and user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
            handles.add(int(hwnd))
        return True
    user32.EnumWindows(collect, None)
    return handles


def run_smoke(args, output):
    runtime = runtime_directory(ROOT)
    package = args.package or runtime / 'part-selection-full-0.1.2.zip'
    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None
        archive.extractall(output / 'installed')
    prefix = output / 'installed/plugins/third_party/EasyKiConverter'
    manifest = json.loads((prefix / 'bundle-manifest.json').read_text())
    for name, checksum in manifest['sha256'].items():
        assert hashlib.sha256((prefix / name).read_bytes()).hexdigest() == checksum, name
    sys.path.insert(0, str(output / 'installed/plugins/app'))
    from part_selection_library import LibraryImporter, find_converter
    assert find_converter().is_relative_to(prefix), 'Converter must come from the extracted package'
    project = output / 'project with spaces'
    project.mkdir()
    (project / 'DOC').mkdir()
    schematic = project / 'DOC/test.kicad_sch'
    schematic.write_text('(kicad_sch (symbol (lib_id "Device:R") (at 0 0 0) (property "Reference" "R1" (at 0 0 0)) (property "Comment" "Resistor" (at 0 0 0)) (property "Value" "10k" (at 0 0 0)) (property "Footprint" "DSMU:Original" (at 0 0 0))))')
    before = schematic.read_bytes()
    initial = console_windows()
    new_consoles = set()
    stopped = threading.Event()
    def monitor():
        while not stopped.wait(.02):
            new_consoles.update(console_windows() - initial)
    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    started = time.monotonic()
    try:
        result = LibraryImporter(project).convert('C4283613', progress=lambda s, m: print(s, flush=True))
    finally:
        stopped.set()
        watcher.join()
    assert not new_consoles, f'Unexpected visible converter console: {new_consoles}'
    assert schematic.read_bytes() == before
    assert LibraryImporter(project).convert('C4283613')['cached']
    from part_selection_server import PartSelectionApp, row_schematic_sync_fields
    from part_selection_helper import update_schematic_symbol_fields, read_schematic_symbol_field_values
    bom = project / 'BOM'
    bom.mkdir()
    config = bom / 'config.json'
    config.write_text(json.dumps({'schematic_dir':'DOC', 'source_bom':'__schematic__'}))
    app = PartSelectionApp(config)
    selected = app.select_candidate({'refs':'R1', 'candidate':{'c_code':'C4283613', 'model':'P2TC0805D5002LNTA', 'package':'0805', 'param_detail':'Resistance: 50kOhm'}, 'footprint_mode':'convert'})
    assert selected['library']['footprint'] == result['footprint']
    assert schematic.read_bytes() == before, 'Choosing a candidate must not auto-write schematic'
    row = app.rows('R1', '__schematic__', include_stock=False)[0]
    fields = row_schematic_sync_fields(row)
    assert fields['footprint'] == 'LCSC_C4283613:R0805'
    updated = update_schematic_symbol_fields(project / 'DOC', ['R1'], fields)
    assert updated['updated_refs'] == ['R1']
    actual = read_schematic_symbol_field_values(project / 'DOC', ['R1'], tuple(fields))
    assert actual['R1'] == fields
    assert not app.rows('R1', '__schematic__', include_stock=False)[0]['field_changed_refs']
    library = project / 'Libraries/DSMU/LCSC/C4283613'
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    for kind, source in (('sym', library / 'LCSC_C4283613.kicad_sym'), ('fp', library / 'LCSC_C4283613.pretty')):
        (output / kind).mkdir(exist_ok=True)
        completed = subprocess.run([str(args.cli), kind, 'export', 'svg', '-o', str(output / kind), str(source)], capture_output=True, timeout=60, **flags)
        assert completed.returncode == 0, completed.stderr
        assert list((output / kind).rglob('*.svg')), 'KiCad CLI produced no SVG'
    report = {'ok': True, 'package': str(args.package), 'source_commit': manifest['commit'],
              'converter': str(find_converter()), 'library': result, 'visible_new_consoles': len(new_consoles),
              'elapsed_seconds': round(time.monotonic() - started, 3), 'kicad_cli': 'symbol and footprint SVG export passed',
              'formal_cad_modified': False, 'temporary_schematic_round_trip':'passed', 'output': str(output)}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path)
    parser.add_argument('--cli', type=Path, default=Path('C:/Program Files/KiCad/10.0/bin/kicad-cli.exe'))
    args = parser.parse_args()
    with temporary_project(prefix='easyki-package-qa-') as directory:
        run_smoke(args, Path(directory))


if __name__ == '__main__':
    main()

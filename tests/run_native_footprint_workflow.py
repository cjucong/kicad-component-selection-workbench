"""Opt-in real desktop workflow fixture; never opens or edits production CAD.

Keep this process alive while driving the real workbench and KiCad. Stdin
accepts snapshot, verify, and finish. The temporary project is removed on exit.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from kicad_sexpr import parse_sexpr


def uid():
    return str(uuid4())


def verify_snapshot(root, baseline, current):
    schematic = parse_sexpr((root / 'FlowQA.kicad_sch').read_text(encoding='utf-8'), 'kicad_sch')
    expected = {}
    for symbol in schematic.children_named('symbol'):
        properties = {p.arg(): p.arg(2) for p in symbol.children_named('property')}
        expected[properties['Reference']] = properties['Footprint']
    assert current['tracks'] == baseline['tracks'], 'Existing tracks changed'
    assert set(current['parts']) == set(baseline['parts']), 'Footprints added/deleted'
    for ref, old in baseline['parts'].items():
        new = current['parts'][ref]
        assert new['id'] == expected[ref], (ref, 'PCB does not match schematic', new['id'], expected[ref])
        assert new['id'] != old['id'], (ref, 'Footprint was never replaced')
        for key in ('position_nm', 'angle', 'back', 'path'):
            assert new[key] == old[key], (ref, key, old[key], new[key])
        assert set(new['pads']) == set(old['pads']), (ref, 'pad numbers')
        for number, pad in old['pads'].items():
            actual = new['pads'][number]
            assert max(abs(a-b) for a,b in zip(actual['position_nm'], pad['position_nm'])) <= 5, (ref, number, 'position')
            assert actual['net'] == pad['net'], (ref, number, 'net')


def native(payload):
    from part_selection_footprint_geometry import _find_kicad_python
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    done = subprocess.run([str(_find_kicad_python()), '-B', str(Path(__file__)), '--worker'],
                          input=json.dumps(payload), text=True, encoding='utf-8',
                          capture_output=True, timeout=60, **flags)
    if done.returncode:
        raise RuntimeError(done.stderr)
    return json.loads(done.stdout)


def launch_suite(root, cache, kicad_python):
    settings = Path(cache) / 'config' / '10.0'
    settings.mkdir(parents=True, exist_ok=True)
    user_settings = Path(os.environ['APPDATA']) / 'kicad' / '10.0'
    for source in user_settings.iterdir():
        if source.is_file() and (source.suffix == '.json' or source.name.endswith('lib-table')):
            shutil.copy2(source, settings / source.name)
    common = settings / 'kicad_common.json'
    data = json.loads(common.read_text(encoding='utf-8'))
    data.setdefault('session', {})['remember_open_files'] = True
    common.write_text(json.dumps(data), encoding='utf-8')
    local = root / 'FlowQA.kicad_prl'
    data = json.loads(local.read_text(encoding='utf-8')) if local.exists() else {}
    data.setdefault('project', {})['files'] = [
        {'name': str(root / ('FlowQA.' + ext)), 'open': True,
         'window': {'size_x': 1100, 'size_y': 800, 'pos_x': 40, 'pos_y': 40,
                    'maximized': False, 'display': 0}}
        for ext in ('kicad_pcb', 'kicad_sch')]
    local.write_text(json.dumps(data), encoding='utf-8')
    ipc = Path(cache) / 'suite-ipc'
    ipc.mkdir(exist_ok=True)
    env = dict(os.environ, KICAD_CONFIG_HOME=str(settings.parent), TEMP=str(ipc), TMP=str(ipc))
    process = subprocess.Popen([str(kicad_python.parent / 'kicad.exe'), str(root / 'FlowQA.kicad_pro')], env=env)
    print('SUITE ' + json.dumps({'pid': process.pid, 'ipc': str(ipc / 'kicad' / 'api.sock')}), flush=True)


def worker(payload):
    import pcbnew
    root = Path(payload['root'])
    if payload['operation'] == 'build':
        board = pcbnew.BOARD()
        for item in payload['parts']:
            source = Path(item['module']).read_text(encoding='utf-8-sig')
            fp = pcbnew.PCB_IO_KICAD_SEXPR().Parse(source).Cast()
            board.Add(fp)
            origin = pcbnew.VECTOR2I(0, 0)
            for child in [*fp.Pads(), *fp.GraphicalItems(), *fp.GetFields(), *fp.Zones()]:
                child.Rotate(origin, pcbnew.EDA_ANGLE(90, pcbnew.DEGREES_T))
                child.Move(pcbnew.VECTOR2I(pcbnew.FromMM(4), pcbnew.FromMM(-3)))
            fp.SetFPID(pcbnew.LIB_ID('Local', 'Original_' + item['ref']))
            directory = root / 'Local.pretty'
            directory.mkdir(exist_ok=True)
            pcbnew.PCB_IO_KICAD_SEXPR().FootprintSave(str(directory), fp)
            fp.SetReference(item['ref'])
            fp.SetValue(item['value'])
            fp.SetPath(pcbnew.KIID_PATH('/' + payload['sheet_uuid'] + '/' + item['uuid']))
            if item['back']:
                fp.Flip(origin, False)
            fp.SetOrientationDegrees(item['angle'])
            fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(item['x']), pcbnew.FromMM(item['y'])))
            for pad in fp.Pads():
                name = '/' + item['ref'] + '_' + pad.GetNumber()
                net = pcbnew.NETINFO_ITEM(board, name)
                board.Add(net)
                pad.SetNet(net)
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(pad.GetPosition())
                track.SetEnd(pad.GetPosition() + pcbnew.VECTOR2I(pcbnew.FromMM(1), 0))
                track.SetWidth(pcbnew.FromMM(.15))
                track.SetLayer(pcbnew.B_Cu if item['back'] else pcbnew.F_Cu)
                track.SetNet(net)
                board.Add(track)
        pcbnew.SaveBoard(str(root / 'FlowQA.kicad_pcb'), board)
    board = pcbnew.LoadBoard(str(root / 'FlowQA.kicad_pcb'))
    def xy(v):
        return [v.x, v.y]
    result = {'parts': {}, 'tracks': []}
    for fp in board.GetFootprints():
        result['parts'][fp.GetReference()] = {
            'id': fp.GetFPID().GetLibNickname().c_str() + ':' + fp.GetFPID().GetLibItemName().c_str(),
            'position_nm': xy(fp.GetPosition()), 'angle': fp.GetOrientationDegrees(), 'back': fp.IsFlipped(),
            'path': fp.GetPath().AsString(),
            'pads': {p.GetNumber(): {'position_nm': xy(p.GetPosition()), 'size_nm': xy(p.GetSize()),
                                    'angle': p.GetOrientationDegrees(), 'net': p.GetNetname()}
                     for p in fp.Pads()}}
    for t in board.GetTracks():
        result['tracks'].append([xy(t.GetStart()), xy(t.GetEnd()), t.GetWidth(), t.GetLayer(), t.GetNetname()])
    result['tracks'].sort(key=str)
    print(json.dumps(result))


def make_project(root, converter):
    from part_selection_library import LibraryImporter
    parts = []
    for ref, code, value, back, angle, x, y in [
        ('R1', 'C25804', '10k', False, 37, 80, 80),
        ('U1', 'C461888', 'SI8641BA-C-IUR', True, 90, 100, 80),
    ]:
        item = LibraryImporter(root, converter).convert(code, progress=lambda s, m: print(ref, s, flush=True))
        folder = root / 'Libraries/DSMU/LCSC' / code
        nickname, name = item['footprint'].split(':', 1)
        parts.append({'ref': ref, 'code': code, 'value': value, 'comment': 'resistor' if ref[0] == 'R' else value,
                      'back': back, 'angle': angle, 'x': x, 'y': y, 'uuid': uid(),
                      'symbol': str(folder / (nickname + '.kicad_sym')),
                      'module': str(folder / (nickname + '.pretty') / (name + '.kicad_mod'))})
    sheet = uid()
    libs, instances, labels = [], [], []
    for index, item in enumerate(parts):
        text = Path(item['symbol']).read_text(encoding='utf-8-sig')
        definition = parse_sexpr(text, 'kicad_symbol_lib').children_named('symbol')[0]
        lib_id = 'Fixture:' + item['ref']
        block = text[definition.start:definition.end]
        atom = definition.children[1]
        start, end = atom.start - definition.start, atom.end - definition.start
        libs.append(block[:start] + json.dumps(lib_id) + block[end:])
        sx, sy = 70 + index * 70, 80
        pins = [p for unit in definition.children_named('symbol') for p in unit.children_named('pin')]
        pin_blocks = []
        for p in pins:
            number = p.child('number').arg()
            pin_blocks.append(f'(pin {json.dumps(number)} (uuid {uid()}))')
            at = p.child('at')
            px, py = sx + float(at.arg()), sy - float(at.arg(2))
            labels.append(f'(label "{item["ref"]}_{number}" (at {px:.6f} {py:.6f} 0) '
                          f'(effects (font (size 1.27 1.27)) (justify left bottom)) (uuid {uid()}))')
        properties = ''.join(f'(property {json.dumps(k)} {json.dumps(v)} (at {sx} {sy + n * 3} 0) '
                             '(effects (font (size 1.27 1.27)) (hide yes)))'
                             for n, (k, v) in enumerate({'Reference': item['ref'], 'Value': item['value'],
                                 'Footprint': 'Local:Original_' + item['ref'], 'Datasheet': '',
                                 'Comment': item['comment']}.items()))
        instances.append(f'(symbol (lib_id {json.dumps(lib_id)}) (at {sx} {sy} 0) (unit 1) '
                         f'(in_bom yes) (on_board yes) (dnp no) (uuid {item["uuid"]}) {properties}'
                         + ''.join(pin_blocks) + f'(instances (project "FlowQA" (path "/{sheet}" '
                         f'(reference "{item["ref"]}") (unit 1)))))')
    schematic = f'(kicad_sch (version 20250114) (generator "eeschema") (uuid {sheet}) (paper "A4") '
    schematic += '(lib_symbols ' + ''.join(libs) + ')' + ''.join(instances + labels)
    schematic += '(sheet_instances (path "/" (page "1"))))'
    (root / 'FlowQA.kicad_sch').write_text(schematic, encoding='utf-8')
    (root / 'FlowQA.kicad_pro').write_text('{}', encoding='utf-8')
    table = root / 'fp-lib-table'
    from part_selection_library import table_text
    table.write_text(table_text(table, 'Local', '${KIPRJMOD}/Local.pretty', 'fp_lib_table'), encoding='utf-8')
    return {'operation': 'build', 'root': str(root), 'sheet_uuid': sheet, 'parts': parts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--report-dir', type=Path)
    parser.add_argument('--launch-session', type=Path)
    parser.add_argument('--verify-session', type=Path)
    parser.add_argument('--stage', choices=('rename', 'converted'))
    args = parser.parse_args()
    if args.worker:
        worker(json.load(sys.stdin))
        return
    if args.verify_session:
        if not args.stage:
            parser.error('--verify-session requires --stage')
        session = json.loads(args.verify_session.read_text(encoding='utf-8'))
        root, report = Path(session['root']), Path(session['report'])
        baseline = json.loads((report / 'baseline.json').read_text(encoding='utf-8'))
        current = native({'operation': 'snapshot', 'root': str(root)})
        verify_snapshot(root, baseline, current)
        (report / (args.stage + '.json')).write_text(json.dumps(current, indent=2), encoding='utf-8')
        print('VERIFIED ' + args.stage)
        return
    if args.launch_session:
        from part_selection_footprint_geometry import _find_kicad_python
        session = json.loads(args.launch_session.read_text(encoding='utf-8'))
        root = Path(session['root'])
        # Only an active harness owns this deliberately short temporary root.
        cache = next((p for p in root.parents if p.name.startswith('kq-')), None)
        if cache is None or not (root / 'FlowQA.kicad_pro').is_file():
            parser.error('Not an active temporary workflow fixture')
        launch_suite(root, cache, _find_kicad_python())
        return
    if not args.report_dir or not args.report_dir.is_absolute():
        parser.error('--report-dir requires an external absolute directory')
    report = args.report_dir.resolve()
    if report.is_relative_to(ROOT):
        parser.error('Report directory must be outside the source repository')
    report.mkdir(parents=True, exist_ok=True)
    from _scratch import temporary_project
    from part_selection_footprint_geometry import _find_kicad_python
    installed = Path.home() / 'Documents/KiCad/10.0/plugins/com_smu_part-selection'
    converter = installed / 'third_party/EasyKiConverter/runtime/easykiconverter.exe'
    with ExitStack() as stack:
        # Keep real converter output below Windows' legacy path length limit.
        cache = stack.enter_context(tempfile.TemporaryDirectory(prefix='kq-', dir=ROOT.anchor))
        previous_cache = os.environ.get('KICAD_PART_SELECTION_CACHE')
        def restore_cache():
            if previous_cache is None:
                os.environ.pop('KICAD_PART_SELECTION_CACHE', None)
            else:
                os.environ['KICAD_PART_SELECTION_CACHE'] = previous_cache
        stack.callback(restore_cache)
        os.environ['KICAD_PART_SELECTION_CACHE'] = cache
        directory = stack.enter_context(temporary_project(prefix='flow-'))
        root = Path(directory)
        payload = make_project(root, str(converter))
        baseline = native(payload)
        (report / 'baseline.json').write_text(json.dumps(baseline, indent=2), encoding='utf-8')
        from part_selection_paths import resolve_config
        config = resolve_config(root)
        raw = json.loads(config.read_text(encoding='utf-8'))
        raw['open_lcsc_search_on_replace'] = 'false'
        config.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
        manifest = {'root': str(root), 'config': str(config), 'installed': str(installed), 'report': str(report)}
        (report / 'session.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        print('READY ' + json.dumps(manifest), flush=True)
        for line in sys.stdin:
            command = line.strip()
            if command == 'finish':
                break
            try:
                if command == 'launch-suite':
                    launch_suite(root, cache, _find_kicad_python())
                    continue
                current = native({'operation': 'snapshot', 'root': str(root)})
                if command.startswith('verify '):
                    stage = command.split()[1]
                    verify_snapshot(root, baseline, current)
                    (report / (stage + '.json')).write_text(json.dumps(current, indent=2), encoding='utf-8')
                    print('VERIFIED ' + stage, flush=True)
                else:
                    print(json.dumps(current), flush=True)
            except Exception as exc:
                print('FAILED ' + repr(exc), flush=True)
        print('CLEANUP temporary project', flush=True)


if __name__ == '__main__':
    main()

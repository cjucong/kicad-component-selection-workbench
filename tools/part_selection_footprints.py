"""Preview staged conversions and publish only explicitly selected library additions."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from kicad_sexpr import parse_sexpr
from part_selection_footprint_geometry import reference_layout_difference
from part_selection_library import IMPORT_LOCK, LibraryImporter, checked_path, digest, table_text
from part_selection_runtime import runtime_directory


def item_name(value):
    if not isinstance(value, str) or not value or len(value) > 120 or value != value.strip():
        raise ValueError('Invalid library/item name')
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', value) or value.endswith(('.', ' ')) or value in ('.', '..'):
        raise ValueError('Invalid library/item name')
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value, re.I):
        raise ValueError('Reserved Windows name')
    return value


def tree_data(node):
    return [tree_data(n) for n in node.children] if node.children else node.value


def rename_content(source):
    return tree_data(parse_sexpr(source, 'footprint'))[2:]


def geometry(source):
    tree = parse_sexpr(source, 'footprint')
    # Conservative equality: ignore labels/IDs, retain courtyard, pad technology and settings.
    ignored = {'version', 'generator', 'generator_version', 'uuid', 'tstamp', 'path', 'descr', 'tags'}
    def clean(node):
        if node.head in ignored or node.head == 'property' and node.arg() in ('Reference', 'Value'):
            return None
        if node.head == 'fp_text' and node.arg() in ('reference', 'value'):
            return None
        if not node.children:
            return node.value
        return [part for child in node.children if (part := clean(child)) is not None]
    return [clean(n) for n in tree.children[2:] if clean(n) is not None]


def footprint_info(path):
    source = path.read_text(encoding='utf-8-sig')
    tree = parse_sexpr(source, 'footprint')
    pads = tree.children_named('pad')
    numbers = sorted({pad.arg() for pad in pads if pad.arg()})
    sizes = sorted({(pad.child('size').arg(), pad.child('size').arg(2)) for pad in pads if pad.child('size')})
    drills = sorted({json.dumps(tree_data(pad.child('drill')), ensure_ascii=False) for pad in pads if pad.child('drill')})
    bounds = []
    for pad in pads:
        size, at = pad.child('size'), pad.child('at')
        if size is None or at is None:
            continue
        x, y = float(at.arg()), float(at.arg(2))
        angle = math.radians(float(at.arg(3) or 0))
        w, h = float(size.arg()), float(size.arg(2))
        dx = (abs(w * math.cos(angle)) + abs(h * math.sin(angle))) / 2
        dy = (abs(w * math.sin(angle)) + abs(h * math.cos(angle))) / 2
        bounds.append((x - dx, y - dy, x + dx, y + dy))
    envelope = ('%.3g × %.3g mm' % (max(b[2] for b in bounds) - min(b[0] for b in bounds),
                                    max(b[3] for b in bounds) - min(b[1] for b in bounds))) if bounds else ''
    return {'pads': len(pads), 'numbers': numbers, 'pad_size': ' / '.join(' × '.join(s) + ' mm' for s in sizes),
            'drills': drills, 'pad_envelope': envelope,
            'geometry_hash': hashlib.sha256(json.dumps(geometry(source)).encode()).hexdigest()}


def render_previews(existing, converted, output):
    cli = shutil.which('kicad-cli')
    if not cli:
        candidate = Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'KiCad/10.0/bin/kicad-cli.exe'
        cli = str(candidate) if candidate.is_file() else None
    if not cli:
        raise FileNotFoundError('KiCad CLI is required for real footprint previews')
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    result = {}
    for key, path in [('converted', converted), *[(str(index), path) for index, path in enumerate(existing)]]:
        folder = output / ('svg-' + key)
        folder.mkdir()
        # Hide placement labels in a render-only copy; preserve all real pad/body geometry.
        render_lib = folder / 'Preview.pretty'
        render_lib.mkdir()
        source = path.read_text(encoding='utf-8-sig')
        tree = parse_sexpr(source, 'footprint')
        patches = [n for n in tree.children if
                   n.head == 'property' and n.arg() in ('Reference', 'Value') or
                   n.head == 'fp_text' and (n.arg() in ('reference', 'value') or n.arg(2) in ('${REFERENCE}', '%R', '${VALUE}', '%V'))]
        for node in sorted(patches, key=lambda n: n.start, reverse=True):
            source = source[:node.start] + source[node.end:]
        (render_lib / path.name).write_text(source, encoding='utf-8')
        proc = subprocess.run([cli, 'fp', 'export', 'svg', '-o', str(folder),
                               '-l', 'F.Cu,B.Cu,F.SilkS,F.Fab,F.CrtYd,Edge.Cuts', '--sp', '--fp', path.stem, str(render_lib)],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=60, **flags)
        files = list(folder.rglob('*.svg'))
        if proc.returncode or len(files) != 1:
            raise ValueError('KiCad footprint preview failed: ' + proc.stderr.decode(errors='replace')[-800:])
        result[key] = files[0].read_text(encoding='utf-8')
    result['converted_svg'] = result.pop('converted')
    result['existing'] = [result[str(i)] for i in range(len(existing))]
    return result


class FootprintChoices:
    def __init__(self, root, converter=None):
        self.root = Path(root).resolve()
        self.converter = converter
        self.previews = OrderedDict()
        self.staging_root = None
        self.conversion_lock = threading.RLock()
        self.conversion_cache = OrderedDict()

    def change_evidence(self, choice, refs, before, result):
        """Capture only validated server-side preview evidence, never client metadata."""
        from part_selection_footprint_changes import make_footprint_change, board_copper_hashes
        state = self.previews[choice['preview_id']]
        nickname, name = result['footprint'].split(':', 1)
        target = checked_path(self.root, self.libraries()[nickname] / (item_name(name) + '.kicad_mod'))
        target_info = {'path': str(target), 'hash': digest(target)}
        operation = 'rename' if result.get('renamed') else 'replace'
        board_hashes = (board_copper_hashes(Path(state['board']['path']).read_text(encoding='utf-8-sig'), refs)
                        if state.get('board') else {})
        entries = state['existing']
        originals = state.get('baseline_ids', state['original_ids'])
        evidence = {}
        for ref in refs:
            entry = next((e for e in entries if e.get('placement', {}).get('ref') == ref), None)
            if entry is None:
                entry = next((e for e in entries if e['id'] == before[ref]), None)
            if entry is None:
                entry = next((e for e in entries if e['id'] in originals), None)
            source = {key: deepcopy(entry[key]) for key in ('geometry_hash', 'hash', 'source', 'placement')
                      if entry and key in entry}
            if entry and not state.get('board'):
                source['path'] = str(entry['path'])
            if state.get('board'):
                source['board'] = {k: str(v) for k, v in state['board'].items()}
                if ref in board_hashes:
                    source['instance_geometry_hash'] = board_hashes[ref]
            original_id = entry.get('placement', {}).get('footprint', before[ref]) if entry else before[ref]
            evidence[ref] = make_footprint_change(operation=operation, before=original_id,
                after=result['footprint'], source=source, target=target_info)
        return evidence

    def libraries(self):
        table = checked_path(self.root, self.root / 'fp-lib-table')
        if not table.exists():
            return {}
        result = {}
        for node in parse_sexpr(table.read_text(encoding='utf-8-sig'), 'fp_lib_table').children_named('lib'):
            if not node.child('name') or not node.child('uri') or not node.child('type') or node.child('type').arg() != 'KiCad':
                continue
            name, uri = node.child('name').arg(), node.child('uri').arg()
            if uri.startswith('${KIPRJMOD}/'):
                path = self.root / uri[len('${KIPRJMOD}/'):]
            elif Path(uri).is_absolute():
                path = Path(uri)
            else:
                continue
            try:
                item_name(name)
                path = checked_path(self.root, path)
                if path.is_dir() and path.suffix == '.pretty':
                    result[name] = path
            except ValueError:
                continue
        return result

    def _expire(self):
        for key, state in list(self.previews.items()):
            if time.monotonic() - state['time'] > 1800 or len(self.previews) >= 8:
                if state.get('work'):
                    shutil.rmtree(state['work'], ignore_errors=True)
                del self.previews[key]

    def prepare_library(self, existing_ids, package=''):
        with IMPORT_LOCK:
            self._expire()
            libs = self.libraries()
            catalog = {}
            for nickname, pretty in libs.items():
                for path in sorted(pretty.glob('*.kicad_mod')):
                    try:
                        item_name(path.stem)
                        catalog[nickname + ':' + path.stem] = checked_path(self.root, path)
                    except ValueError:
                        continue
            originals = list(dict.fromkeys(existing_ids))
            existing = []
            for identifier in originals:
                path = catalog.get(identifier)
                if path is None:
                    continue
                try:
                    existing.append({'id': identifier, 'path': path, 'hash': digest(path), **footprint_info(path)})
                except (ValueError, OSError):
                    continue
            key = uuid4().hex
            self.previews[key] = {
                'kind': 'library', 'time': time.monotonic(), 'work': None,
                'catalog': catalog, 'existing': existing, 'original_ids': originals,
                'tables': {n: (self.root / n).read_bytes() if (self.root / n).exists() else None
                           for n in ('fp-lib-table', 'sym-lib-table')},
            }
            default = next((i for i in originals if i in catalog), '')
            return {'ok': True, 'kind': 'library', 'preview_id': key, 'libraries': list(libs),
                    'original_ids': originals, 'default_id': default, 'package': package,
                    'items': [{'id': i, 'library': i.split(':', 1)[0]} for i in catalog]}

    def library_detail(self, preview_id, identifier, render=False):
        with IMPORT_LOCK:
            self.context(preview_id)
            state = self.previews[preview_id]
            if state.get('kind') not in ('library', 'rename') or identifier not in state['catalog']:
                raise ValueError('请选择工程库列表中的封装')
            self._verify(state)
            entry = next((e for e in state['existing'] if e['id'] == identifier), None)
            if entry is None:
                path = checked_path(self.root, state['catalog'][identifier])
                entry = {'id': identifier, 'path': path, 'hash': digest(path), **footprint_info(path)}
                state['existing'].append(entry)
            original = [e for e in state['existing'] if e['id'] in state['original_ids']]
            compatible = (bool(original) and len(original) == len(state['original_ids']) and
                          bool(entry['numbers']) and all(e['numbers'] == entry['numbers'] for e in original))
            entry['loaded'] = True
            entry['compatible'] = compatible
            if render and 'svg' not in entry:
                temp = runtime_directory(self.root)
                if state['work'] is None:
                    state['work'] = Path(tempfile.mkdtemp(prefix='library-footprint-preview-', dir=temp))
                output = state['work'] / uuid4().hex
                output.mkdir()
                entry['svg'] = render_previews([], entry['path'], output)['converted_svg']
            self._verify(state)
            return {k: v for k, v in entry.items() if k not in ('path', 'hash', 'loaded', 'svg')} | (
                {'svg': entry['svg']} if render else {})

    def _pcb_references(self, refs, work):
        if not refs:
            return None, []
        from part_selection_footprint_geometry import board_references
        boards = sorted(self.root.glob('*.kicad_pcb'))
        preferred = self.root/(self.root.name+'.kicad_pcb')
        if preferred in boards:
            boards = [preferred]
        if len(boards) > 1:
            raise ValueError('工程中存在多个 PCB，无法确定配准基准，请保留唯一目标 PCB')
        if not boards:
            return None, []
        board = checked_path(self.root, boards[0])
        state = {'path':board,'hash':digest(board)}
        entries = []
        for record in board_references(board,refs,work/'board-reference'):
            path = checked_path(work,Path(record['path']))
            entries.append({'id':'PCB:'+record['ref'],'path':path,'hash':digest(path),
                **footprint_info(path),'source':'pcb','placement':record})
        if entries and len(entries) != len(set(refs)):
            raise ValueError('该行只有部分位号存在于 PCB，请先拆行，再分别更换封装')
        if digest(board) != state['hash']:
            raise ValueError('读取期间 PCB 发生变化，请保存后重新预览')
        return state, entries

    def prepare_rename(self, existing_ids, package='', code='', *, refs=()):
        if code and not re.fullmatch(r'C[0-9]+', str(code)):
            raise ValueError('Invalid C code')
        existing_ids = list(existing_ids)
        with IMPORT_LOCK:
            self._expire()
            libs, originals, existing = self.libraries(), list(dict.fromkeys(existing_ids)), []
            work, board = None, None
            if refs:
                work = Path(tempfile.mkdtemp(prefix='footprint-rename-', dir=runtime_directory(self.root)))
                try:
                    board, existing = self._pcb_references(refs, work)
                except Exception:
                    shutil.rmtree(work, ignore_errors=True)
                    raise
            library_default = next((i.split(':',1)[0] for i in originals if ':' in i and i.split(':',1)[0] in libs),
                                   next(iter(libs), ''))
            if existing:
                originals = [e['id'] for e in existing]
            for identifier in ([] if existing else originals):
                if not isinstance(identifier, str) or ':' not in identifier:
                    continue
                nickname, name = identifier.split(':', 1)
                if nickname not in libs:
                    continue
                try:
                    path = checked_path(self.root, libs[nickname] / (item_name(name) + '.kicad_mod'))
                    existing.append({'id': identifier, 'path': path, 'hash': digest(path), **footprint_info(path)})
                except (ValueError, OSError):
                    continue
            if not existing or len(existing) != len(originals):
                if work:
                    shutil.rmtree(work, ignore_errors=True)
                raise ValueError('原绑定封装不可读取，不能仅改名')
            key, default = uuid4().hex, originals[0]
            self.previews[key] = {'kind': 'rename', 'code': code, 'time': time.monotonic(), 'work': work, 'board':board,
                'original_ids': originals, 'existing': existing, 'catalog': {e['id']: e['path'] for e in existing},
                'tables': {n: (self.root / n).read_bytes() if (self.root / n).exists() else None
                           for n in ('fp-lib-table', 'sym-lib-table')}}
            try:
                suggested = item_name(str(package).strip())
            except ValueError:
                suggested = existing[0].get('placement',{}).get('footprint',default).split(':', 1)[1]
            return {'ok': True, 'preview_id': key, 'kind': 'rename', 'c_code': code, 'suggested_name': suggested,
                    'default_id': default, 'libraries': list(libs), 'original_ids': originals, 'package': package,
                    'current_footprints': list(existing_ids),
                    'default_library': library_default,
                    'items': [{'id': e['id'], 'library': e['id'].split(':', 1)[0],
                               'label':('PCB 已保存 · '+e['placement']['ref']+' · '+e['placement']['footprint'])
                                       if e.get('source')=='pcb' else e['id']} for e in existing]}

    def prepare(self, code, existing_ids, package='', progress=None, *, refs=()):
        if not re.fullmatch(r'C[0-9]+', str(code)):
            raise ValueError('Invalid C code')
        with IMPORT_LOCK:
            self._expire()
        work = Path(tempfile.mkdtemp(prefix='footprint-preview-', dir=runtime_directory(self.root)))
        try:
            # Network conversion owns only its isolated staging lock, never the project lock.
            with self.conversion_lock:
                self.staging_root = work
                cached = self.conversion_cache.get(code)
                cache_valid = cached and time.monotonic() - cached['time'] <= 1800
                if cache_valid:
                    converted = deepcopy(cached['converted'])
                else:
                    converted = LibraryImporter(work, self.converter, import_lock=self.conversion_lock).convert(code, progress=progress)
                lib, name = converted['footprint'].split(':', 1)
                folder = work / 'Libraries/DSMU/LCSC' / code
                module = checked_path(work, folder / (lib + '.pretty') / (item_name(name) + '.kicad_mod'))
                symbol = checked_path(work, folder / (lib + '.kicad_sym'))
                if cache_valid:
                    module.parent.mkdir(parents=True, exist_ok=True)
                    module.write_bytes(cached['module'])
                    symbol.write_bytes(cached['symbol'])
                info = footprint_info(module)
                self.conversion_cache[code] = {'time': time.monotonic(), 'converted': deepcopy(converted),
                                               'module': module.read_bytes(), 'symbol': symbol.read_bytes()}
                while len(self.conversion_cache) > 8:
                    self.conversion_cache.popitem(last=False)
            with IMPORT_LOCK:
                libs = self.libraries()
                existing = []
                ids = list(dict.fromkeys(existing_ids))
                # Also find likely matching project library names, not a fixed 0402/0603 mapping.
                normalized = re.sub(r'[^A-Z0-9]', '', str(package).upper())
                if normalized:
                    for nickname, pretty in libs.items():
                        for path in pretty.glob('*.kicad_mod'):
                            if re.sub(r'[^A-Z0-9]', '', path.stem.upper()) in (normalized, 'R' + normalized, 'C' + normalized, 'L' + normalized):
                                ids.append(nickname + ':' + path.stem)
                for identifier in dict.fromkeys(ids):
                    if ':' not in identifier:
                        continue
                    nickname, part = identifier.split(':', 1)
                    if nickname not in libs:
                        continue
                    try:
                        path = checked_path(self.root, libs[nickname] / (item_name(part) + '.kicad_mod'))
                        if path.is_file():
                            details = footprint_info(path)
                            existing.append({'id': identifier, 'path': path, 'hash': digest(path), **details})
                    except (ValueError, OSError):
                        continue
                    if len(existing) >= 24:
                        break
                notify = progress or (lambda *args: None)
                notify('previewing', 'Preparing footprint alignment and comparison')
                board_state, board_entries = self._pcb_references(refs, work)
                board_ids = [e['id'] for e in board_entries]
                existing.extend(board_entries)
                # Explicit recovery references: don't infer the old package by name.
                known = {e['id'] for e in existing}
                for nickname,pretty in libs.items():
                    for path in sorted(pretty.glob('*.kicad_mod')):
                        identifier = nickname+':'+path.stem
                        if identifier in known:
                            continue
                        try:
                            path = checked_path(self.root,path)
                            details = footprint_info(path)
                            if details['numbers'] != info['numbers']:
                                continue
                            existing.append({'id':identifier,'path':path,'hash':digest(path),**details})
                            known.add(identifier)
                        except (ValueError,OSError):
                            continue
                for entry in existing:
                    entry['svg'] = ''
                    entry['compatible'] = entry['numbers'] == info['numbers']
                baseline_ids = board_ids or list(existing_ids)
                originals = [entry for entry in existing if entry['id'] in baseline_ids]
                compatible = bool(originals) and len(originals) == len(set(baseline_ids)) and all(e['compatible'] for e in originals)
                alignment_ids = [e['id'] for e in existing if e['compatible']]
                suggested = str(package).strip() if re.fullmatch(r'[A-Za-z0-9_.()+,-]{1,100}', str(package).strip()) else name
                try:
                    item_name(suggested)
                except ValueError:
                    suggested = name
                preview_id = uuid4().hex
                state = {'time': time.monotonic(), 'work': work, 'code': code, 'converted': converted,
                         'module': module, 'symbol': symbol, 'module_hash': digest(module), 'symbol_hash': digest(symbol),
                         'existing': existing, 'original_ids': list(existing_ids), 'baseline_ids':baseline_ids,
                         'alignment_ids':alignment_ids,'board':board_state,'compatible': compatible, 'info': info,
                         'tables': {n: (self.root / n).read_bytes() if (self.root / n).exists() else None for n in ('fp-lib-table', 'sym-lib-table')}}
                self.previews[preview_id] = state
                return {'ok': True, 'preview_id': preview_id, 'c_code': code, 'suggested_name': suggested,
                        'original_ids': list(existing_ids),
                        'rename_ids':baseline_ids,
                        'alignment_ids':alignment_ids,'default_reference_id':baseline_ids[0] if baseline_ids else '',
                        'default_library': 'DSMU' if 'DSMU' in libs else next(iter(libs), ''),
                        'libraries': list(libs), 'existing': [{k: v for k, v in e.items() if k not in ('path', 'hash')} for e in existing],
                        'compatible': compatible, 'converted_svg': '', 'converted_info': info, 'comparison_available': True,
                        'metrics': ['焊盘数量', '焊盘编号', '单焊盘尺寸', '钻孔'],
                        'warning': '' if compatible else '原封装缺失或焊盘编号不同，不能直接采用转换封装；请先人工核对。'}
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise

    def _alignment(self, state, identifier):
        from part_selection_footprint_geometry import align_module
        entry = next((e for e in state['existing'] if e['id'] == identifier
                      and e['id'] in state.get('alignment_ids',state['original_ids'])), None)
        if entry is None:
            raise ValueError('请选择预览中的兼容封装作为配准基准')
        baseline_ids = state.get('baseline_ids',state['original_ids'])
        originals = [e for e in state['existing'] if e['id'] in baseline_ids]
        if not originals or len(originals) != len(set(baseline_ids)):
            missing = sorted(set(baseline_ids) - {e['id'] for e in originals})
            raise ValueError('该行缺少原封装：' + '、'.join(missing) + '；请先拆行后分别配准')
        baseline = originals[0]['path'].read_text(encoding='utf-8-sig')
        for original in originals[1:]:
            difference = reference_layout_difference(original['path'].read_text(encoding='utf-8-sig'), baseline)
            if difference:
                raise ValueError(original['id'] + ' 与 ' + originals[0]['id'] + '：' + difference + '；请先拆行后分别配准')
        cache = state.setdefault('aligned', {})
        if identifier not in cache:
            index = len(cache)
            folder = checked_path(state['work'], state['work'] / ('v' + str(index)))
            folder.mkdir()
            output = checked_path(state['work'], state['work'] / ('a' + str(index) + '.kicad_mod'))
            result = align_module(state['module'], entry['path'], output, method='center')
            cache[identifier] = {'alignment': result, 'path': output if result.get('ok') else None,
                                 'hash': digest(output) if result.get('ok') else None, 'work': folder}
        return cache[identifier], entry

    def comparison(self, preview_id, identifier, target_identifier=None):
        from part_selection_footprint_geometry import render_comparison
        with IMPORT_LOCK:
            self.context(preview_id)
            state = self.previews[preview_id]
            if target_identifier is not None:
                if state.get('kind') != 'library' or identifier not in state['original_ids']:
                    raise ValueError('请选择原绑定库封装作为比较基准')
                self._verify(state)
                reference = next((e for e in state['existing'] if e['id'] == identifier), None)
                target = next((e for e in state['existing'] if e['id'] == target_identifier), None)
                if (reference is None or target_identifier not in state['catalog'] or target is None
                        or not target.get('loaded') or not target.get('compatible')):
                    raise ValueError('请先读取兼容的工程库目标封装')
                cache = state.setdefault('library_comparisons', {})
                pair = (identifier, target_identifier)
                if pair not in cache:
                    if state['work'] is None:
                        state['work'] = Path(tempfile.mkdtemp(prefix='local-footprint-', dir=runtime_directory(self.root)))
                    output = state['work'] / uuid4().hex
                    output.mkdir()
                    # Compare native library coordinates without rigid alignment or publication.
                    rendered = render_comparison(reference['path'], target['path'], output)
                    if rendered.get('ok') is False:
                        raise ValueError('库封装比较预览失败：' + str(rendered.get('reason') or '渲染不可用'))
                    self._verify(state)
                    cache[pair] = {**rendered, 'library_comparison': True,
                                   'reference_id': identifier, 'target_id': target_identifier}
                self._verify(state)
                return deepcopy(cache[pair])
            if state.get('kind') in ('library', 'rename'):
                self.library_detail(preview_id, identifier)
                entry = next(e for e in state['existing'] if e['id'] == identifier)
                if state['work'] is None:
                    state['work'] = Path(tempfile.mkdtemp(prefix='local-footprint-', dir=runtime_directory(self.root)))
                if 'comparison' not in entry:
                    output = state['work'] / uuid4().hex
                    output.mkdir()
                    rendered = render_comparison(entry['path'], entry['path'], output)
                    if rendered.get('ok') is False:
                        raise ValueError('本地封装预览失败：' + str(rendered.get('reason') or '渲染不可用'))
                    entry['comparison'] = {**rendered, 'converted_layers': {}, 'local_only': True}
                self._verify(state)
                return deepcopy(entry['comparison'])
            self._verify(state)
            aligned, entry = self._alignment(state, identifier)
            if 'comparison' not in aligned:
                rendered = render_comparison(entry['path'], aligned['path'] or state['module'], aligned['work'])
                if rendered.get('ok') is False:
                    raise ValueError('封装预览失败：' + str(rendered.get('reason') or 'KiCad 原生渲染不可用'))
                aligned['comparison'] = rendered
            self._verify(state)
            return deepcopy({**aligned['comparison'], 'reference_id': identifier,
                             'alignment': aligned['alignment']})

    def bind(self, preview_id, context):
        with IMPORT_LOCK:
            self.previews[preview_id]['context'] = deepcopy(context)

    def context(self, preview_id):
        state = self.previews.get(preview_id)
        if not state or time.monotonic() - state['time'] > 1800:
            raise ValueError('封装预览已过期，请重新转换')
        return deepcopy(state.get('context'))

    def rollback(self, preview_id):
        """Undo this publication only, without overwriting concurrent library edits."""
        with IMPORT_LOCK:
            state = self.previews[preview_id]
            writes = state.get('rollback', {})
            for path, before in writes.items():
                if not path.exists() or digest(path) != state['published'].get(path):
                    raise RuntimeError('库文件已被其他操作修改，请从事务备份恢复：' + str(path))
            for path, before in writes.items():
                if before is None:
                    path.unlink()
                else:
                    path.write_bytes(before)
            state.pop('result', None)
            state.pop('published', None)
            state.pop('rollback', None)

    def changes_geometry(self, choice):
        """Compare selected geometry with the preview's frozen baseline, not its name."""
        with IMPORT_LOCK:
            self.context(choice.get('preview_id'))
            state = self.previews[choice['preview_id']]
            self._verify(state)
            if choice.get('source') == 'rename':
                return False
            baseline_ids = state.get('baseline_ids', state['original_ids'])
            originals = [e for e in state['existing'] if e['id'] in baseline_ids]
            if choice.get('source') == 'existing':
                entry = next(e for e in state['existing'] if e['id'] == choice.get('existing_id'))
                target = entry['path']
            else:
                aligned, _ = self._alignment(state, choice.get('existing_id'))
                target = aligned['path'] or state['module']
            selected = geometry(target.read_text(encoding='utf-8-sig'))
            return len(originals) != len(set(baseline_ids)) or any(
                geometry(e['path'].read_text(encoding='utf-8-sig')) != selected for e in originals)

    def _verify(self, state):
        files = [(state['work'] if e.get('source')=='pcb' else self.root, e['path'], e['hash'])
                 for e in state['existing']]
        if state.get('board'):
            files.append((self.root,state['board']['path'],state['board']['hash']))
        if state.get('kind') not in ('library', 'rename'):
            files += [(state['work'], state['module'], state['module_hash']),
                      (state['work'], state['symbol'], state['symbol_hash'])]
            files += [(state['work'], value['path'], value['hash']) for value in state.get('aligned', {}).values() if value['path']]
        for boundary, path, expected in files:
            checked_path(boundary, path)
            if not path.exists() or digest(path) != expected:
                raise ValueError('封装文件发生变化 (changed)，请重新预览')
        for name, data in state['tables'].items():
            path = checked_path(self.root, self.root / name)
            if (path.read_bytes() if path.exists() else None) != data:
                raise ValueError('工程库表发生变化，请重新预览')
        for folder in {e['path'].parent for e in state['existing']}:
            if list(folder.glob('*.lck')) or list(folder.glob('*.lock')):
                raise ValueError('封装库存在活动锁，请保存并关闭库编辑器后重试')

    def commit(self, choice):
        with IMPORT_LOCK:
            key = choice.get('preview_id')
            self.context(key)
            state = self.previews[key]
            if state.get('result'):
                if state['choice'] != choice:
                    raise ValueError('该预览已确认，请重新预览后选择其他目标')
                for path, expected in state['published'].items():
                    if not path.exists() or digest(path) != expected:
                        raise ValueError('已保存封装发生变化，请重新预览')
                return deepcopy(state['result'])
            self._verify(state)
            if state.get('kind') == 'library':
                entry = next((e for e in state['existing'] if e['id'] == choice.get('existing_id')), None)
                if choice.get('source') != 'existing' or not entry or not entry.get('loaded'):
                    raise ValueError('请先选择并读取库封装')
                if choice.get('acknowledged') is not True:
                    raise ValueError('请确认已核对尺寸、焊盘编号和引脚功能')
                if not entry['compatible']:
                    raise ValueError('焊盘编号不一致或原封装不可读取，禁止直接替换')
                return {'footprint': entry['id'], 'reused': True}
            if choice.get('source') == 'existing' and state.get('kind') != 'rename':
                entry = next((e for e in state['existing'] if e['id'] == choice.get('existing_id')), None)
                if entry is None or entry.get('source')=='pcb':
                    raise ValueError('请选择预览中的现有封装')
                original = [e for e in state['existing'] if e['id'] in state['original_ids']]
                if entry['id'] not in state['original_ids'] and (not original or any(e['numbers'] != entry['numbers'] for e in original)):
                    raise ValueError('该库封装与原封装焊盘编号不同，请先人工核对')
                return {'footprint': entry['id'], 'reused': True, 'c_code': state['code']}
            renaming = choice.get('source') == 'rename'
            if choice.get('source') not in ('converted', 'rename') or choice.get('acknowledged') is not True:
                raise ValueError('请确认已核对尺寸、焊盘编号和引脚功能')
            if state.get('kind') == 'rename' and not renaming:
                raise ValueError('仅改名预览不能用于替换几何')
            if renaming:
                rename_ids = state.get('baseline_ids',state['original_ids'])
                entry = next((e for e in state['existing'] if e['id'] == choice.get('existing_id')
                              and e['id'] in rename_ids), None)
                if entry is None or state.get('kind') == 'rename' and not entry.get('loaded'):
                    raise ValueError('仅改名必须先读取当前绑定的原封装')
                originals = [e for e in state['existing'] if e['id'] in rename_ids]
                content = geometry if entry.get('source')=='pcb' else rename_content
                if len(originals) != len(set(rename_ids)):
                    missing = sorted(set(rename_ids) - {e['id'] for e in originals})
                    raise ValueError('该行缺少原封装：' + '、'.join(missing) + '；请先拆行再仅改名')
                baseline_text = entry['path'].read_text(encoding='utf-8-sig')
                differences = []
                visual_differences = []
                for original in originals:
                    original_text = original['path'].read_text(encoding='utf-8-sig')
                    if content(original_text) == content(baseline_text):
                        continue
                    pad_difference = reference_layout_difference(original_text, baseline_text)
                    if pad_difference:
                        differences.append(original['id'] + '：' + pad_difference)
                    else:
                        visual_differences.append(original['id'])
                if differences:
                    raise ValueError('该行原封装不能共用一个仅改名库条目；' + '；'.join(differences) +
                                     '。请先拆行，铜皮焊盘布局一致后才能仅改名')
                source = baseline_text
                if state['work'] is None:
                    state['work'] = Path(tempfile.mkdtemp(prefix='footprint-rename-', dir=runtime_directory(self.root)))
            elif not state['compatible']:
                raise ValueError('焊盘编号不一致或原封装不可读取，禁止直接替换')
            else:
                aligned, _ = self._alignment(state, choice.get('existing_id'))
                if not aligned['alignment'].get('ok'):
                    raise ValueError('封装配准失败：' + str(aligned['alignment'].get('reason') or '无法可靠匹配焊盘'))
                source = aligned['path'].read_text(encoding='utf-8-sig')
            if choice.get('collision', 'ask') not in ('ask', 'overwrite'):
                raise ValueError('请选择覆盖或另存新名称')
            name = item_name(choice.get('name'))
            mode = choice.get('target_mode')
            libs = self.libraries()
            nickname = item_name(choice.get('target_library'))
            if mode == 'existing':
                if nickname not in libs:
                    raise ValueError('请选择工程已注册的本地封装库')
                if nickname.startswith('LCSC_') and not renaming:
                    raise ValueError('受管理的旧转换库不能作为合并目标，请选择 DSMU 或新建库')
                pretty = libs[nickname]
            elif mode in ('new', 'independent'):
                if mode == 'independent':
                    nickname = 'LCSC_' + state['code'] + '_Selected'
                    folder = 'Libraries/DSMU/LCSC/selected'
                else:
                    folder = choice.get('target_folder', 'Libraries/DSMU')
                if not isinstance(folder, str) or PureWindowsPath(folder).is_absolute() or Path(folder).is_absolute() or ':' in folder:
                    raise ValueError('新库目录必须是工程内的相对路径')
                pretty = checked_path(self.root, self.root / folder / (nickname + '.pretty'))
                reusable = mode == 'independent' and libs.get(nickname) == pretty and pretty.is_dir()
                if not reusable and (nickname in libs or pretty.exists()):
                    raise ValueError('新封装库已经存在，请选择合并或更换库名')
            else:
                raise ValueError('Invalid target library mode')
            pretty = checked_path(self.root, pretty)
            target = checked_path(self.root, pretty / (name + '.kicad_mod'))
            def equivalent(path):
                actual = path.read_text(encoding='utf-8-sig')
                if renaming:
                    return rename_content(actual) == rename_content(source)
                return geometry(actual) == geometry(source)
            target_before = target.read_bytes() if target.exists() else None
            overwrite = target_before is not None and not equivalent(target)
            if overwrite:
                # Approval is bound to both the exact choice and the observed file version.
                identity = {k: v for k, v in choice.items() if k not in ('collision', 'collision_token')}
                offer = state.get('collision_offer')
                approved = (choice.get('collision') == 'overwrite' and offer
                    and choice.get('collision_token') == offer['token'] and identity == offer['choice'])
                if choice.get('collision') == 'overwrite' and not approved:
                    raise ValueError('覆盖确认无效，请重新确认同名封装')
                current_hash = hashlib.sha256(target_before).hexdigest()
                if not approved or current_hash != offer['hash']:
                    token = uuid4().hex
                    state['collision_offer'] = {'token': token, 'choice': deepcopy(identity), 'hash': current_hash}
                    return {'conflict': {'footprint': nickname + ':' + name, 'name': name, 'token': token,
                        'changed': bool(approved)}}
            footprint = nickname + ':' + name
            atom = parse_sexpr(source, 'footprint').children[1]
            source = source[:atom.start] + json.dumps(name) + source[atom.end:]
            writes = {target: source.encode('utf-8')} if target_before is None or overwrite else {}
            symbol_path, symbol_lib = None, None
            tables = [('fp-lib-table', nickname, pretty, 'fp_lib_table')]
            if not renaming:
                symbol_source = state['symbol'].read_text(encoding='utf-8-sig')
                tree = parse_sexpr(symbol_source, 'kicad_symbol_lib')
                props = [p for s in tree.children_named('symbol') for p in s.children_named('property') if p.arg() == 'Footprint']
                if len(props) != 1:
                    raise ValueError('转换符号缺少唯一封装引用')
                atom = props[0].children[2]
                symbol_source = symbol_source[:atom.start] + json.dumps(footprint) + symbol_source[atom.end:]
                symbol_lib = 'LCSC_' + state['code'] + '_' + hashlib.sha256(footprint.encode()).hexdigest()[:8]
                symbol_path = checked_path(self.root, self.root / 'Libraries/DSMU/LCSC/symbols' / (symbol_lib + '.kicad_sym'))
                if symbol_path.exists() and symbol_path.read_bytes() != symbol_source.encode('utf-8'):
                    raise ValueError('转换符号已有人修改，拒绝覆盖')
                if not symbol_path.exists():
                    writes[symbol_path] = symbol_source.encode('utf-8')
                tables.append(('sym-lib-table', symbol_lib, symbol_path, 'sym_lib_table'))
            for table, lib, path, kind in tables:
                table_path = checked_path(self.root, self.root / table)
                text = table_text(table_path, lib, '${KIPRJMOD}/' + path.relative_to(self.root).as_posix(), kind)
                if state['tables'][table] is None or state['tables'][table].decode('utf-8-sig') != text:
                    writes[table_path] = text.encode('utf-8')
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            temp = runtime_directory(self.root)
            backup = checked_path(temp, temp / 'part_selection_library_backups' / stamp)
            backup.mkdir(parents=True)
            original = {p: p.read_bytes() if p.exists() else None for p in writes}
            if target in original and original[target] != target_before:
                raise ValueError('目标封装发生变化，请重新确认')
            for index, (path, data) in enumerate(original.items()):
                if data is not None:
                    (backup / str(index)).write_bytes(data)
            (backup / 'transaction.json').write_text(json.dumps({'c_code': state['code'], 'footprint': footprint,
                'symbol': symbol_lib, 'files': [str(p.relative_to(self.root)) for p in writes]}, indent=2), encoding='utf-8')
            touched = []
            self._verify(state)
            if (target.read_bytes() if target.exists() else None) != target_before:
                raise ValueError('目标封装发生变化，请重新确认')
            try:
                for index, (path, data) in enumerate(writes.items()):
                    checked_path(self.root, path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if (path.read_bytes() if path.exists() else None) != original[path]:
                        raise ValueError('文件发生并发变化，拒绝覆盖')
                    if original[path] is None:
                        with path.open('xb') as stream:
                            stream.write(data)
                    else:
                        staged = state['work'] / ('publish-' + str(index))
                        staged.write_bytes(data)
                        os.replace(staged, path)
                    touched.append(path)
            except Exception as exc:
                conflicts = []
                for path in reversed(touched):
                    if not path.exists() or path.read_bytes() != writes[path]:
                        conflicts.append(str(path))
                        continue
                    if original[path] is None:
                        path.unlink()
                    else:
                        path.write_bytes(original[path])
                if conflicts:
                    raise RuntimeError('并发编辑已保留；请根据备份恢复库表：' + str(backup)) from exc
                raise
            result = {'footprint': footprint,
                      'target_path': pretty.relative_to(self.root).as_posix(), 'c_code': state['code'],
                      'reused': target not in writes, 'overwritten': overwrite, 'backup': str(backup.resolve())}
            published = [target, self.root / 'fp-lib-table']
            if renaming:
                result.update(renamed=True, geometry_source=entry['id'],
                              copper_matched=True, visual_differences=visual_differences)
            else:
                result['symbol'] = symbol_lib + ':' + state['converted']['symbol'].split(':', 1)[1]
                result['alignment'] = aligned['alignment']
                reference = next(e for e in state['existing'] if e['id']==choice['existing_id'])
                result['alignment_reference'] = {'id':reference['id'],
                    'source':reference.get('source','library'),
                    'refs':[reference['placement']['ref']] if reference.get('placement') else [],
                    'placement':{k:v for k,v in reference.get('placement',{}).items() if k!='path'}}
                published.extend([symbol_path, self.root / 'sym-lib-table'])
            state.update(result=result, choice=deepcopy(choice), published={p: digest(p) for p in published}, rollback=original)
            return deepcopy(result)

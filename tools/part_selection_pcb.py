"""Scoped PCB update preflights and idempotent execution, separate from selection."""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
import tempfile
import time
from uuid import uuid4

from part_selection_design_source import DesignSource
from part_selection_footprint_changes import classify_footprint_change, board_copper_hashes
from part_selection_pcb_geometry import solve_placement
from part_selection_pcb_session import PcbSession, replacement, encode, metadata_replacement, metadata_values
from part_selection_runtime import runtime_directory


def schematic_identity(root, directory, refs, *, read_page=None, visit_symbol=None):
    from kicad_sexpr import parse_sexpr
    root, directory = Path(root).resolve(), Path(directory).resolve()
    projects = {path.stem for path in root.glob('*.kicad_pro')}
    parsed, hashes = {}, {}

    def read(path):
        path = path.resolve()
        if path not in parsed:
            if read_page:
                parsed[path], hashes[str(path)] = read_page(path)
                return parsed[path]
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise ValueError('无法读取原理图层级页：' + str(path)) from exc
            parsed[path] = parse_sexpr(content.decode('utf-8-sig'), 'kicad_sch')
            hashes[str(path)] = hashlib.sha256(content).hexdigest()
        return parsed[path]

    def sheet_file(path, sheet):
        prop = next((prop for prop in sheet.children_named('property')
                     if prop.arg().lower() == 'sheetfile'), None)
        if not prop or not prop.arg(2):
            raise ValueError('原理图层级页缺少 Sheetfile：' + str(path))
        return (path.parent / prop.arg(2).replace('${KIPRJMOD}', str(root))).resolve()

    mains = [root / (name + '.kicad_sch') for name in projects
             if (root / (name + '.kicad_sch')).is_file()]
    if not mains:
        files = set(directory.glob('*.kicad_sch')) | set(root.glob('*.kicad_sch'))
        referenced = set()
        for path in files:
            node = read(path)
            for sheet in node.children_named('sheet'):
                referenced.add(sheet_file(path, sheet))
        mains = [path for path in files if path.resolve() not in referenced]
    if len(mains) != 1:
        raise ValueError('无法唯一核验原理图根页与 PCB 关联')
    main = mains[0].resolve()
    main_node = read(main)
    root_id = main_node.child('uuid')
    if not root_id or not root_id.arg():
        raise ValueError('原理图根页缺少 UUID：' + str(main))
    root_uuid = root_id.arg()

    def instances(node):
        group = node.child('instances')
        if group:
            for project in group.children_named('project'):
                if project.arg() in projects:
                    yield from project.children_named('path')

    # Copied/imported projects can retain a historical root in their own
    # top-level instance records. Verify it there, not by trusting a leaf suffix.
    anchors = {root_uuid}
    for child in main_node.children_named('sheet') + main_node.children_named('symbol'):
        for instance in instances(child):
            parts = tuple(filter(None, instance.arg().split('/')))
            if len(parts) == 1:
                anchors.add(parts[0])
    paths = {ref: set() for ref in refs}

    def walk(path, chain, ancestors):
        path = path.resolve()
        if path in ancestors or len(ancestors) >= 64:
            raise ValueError('原理图层级循环或过深：' + str(path))
        node = read(path)
        for symbol in node.children_named('symbol'):
            identifier = symbol.child('uuid')
            if not identifier or not identifier.arg():
                continue
            for instance in instances(symbol):
                ref_node = instance.child('reference')
                ref = ref_node.arg().upper() if ref_node else ''
                prefix = tuple(filter(None, instance.arg().split('/')))
                rooted = len(prefix) == len(chain) + 1 and prefix[0] in anchors and prefix[1:] == chain
                if ref in paths and (prefix == chain or rooted):
                    if visit_symbol:
                        visit_symbol(ref, symbol, path)
                    relative = '/' + '/'.join((*chain, identifier.arg()))
                    paths[ref].add(relative)
                    paths[ref].add('/' + root_uuid + relative)
                    if rooted:
                        paths[ref].add('/' + prefix[0] + relative)
        for sheet in node.children_named('sheet'):
            identifier = sheet.child('uuid')
            if not identifier or not identifier.arg():
                raise ValueError('原理图层级页缺少 UUID：' + str(path))
            walk(sheet_file(path, sheet), (*chain, identifier.arg()), (*ancestors, path))

    walk(main, (), ())
    missing = [ref for ref, values in paths.items() if not values]
    if missing:
        raise ValueError('原理图缺少可核验的位号 UUID / 实例关联路径：' + ', '.join(missing))
    return {'paths': {ref: sorted(values) for ref, values in paths.items()}, 'hashes': hashes}


class PcbUpdates:
    def __init__(self, app, session=None, build=None):
        self.app = app
        self.sources = DesignSource(app)
        self.session = session or PcbSession(app.project_root)
        self.build = build or (lambda original, target, placement: encode(replacement(original, target, placement)))
        self.lock = threading.RLock()
        self.previews = OrderedDict()

    def _source(self, refs, source=None):
        source = source or self.sources.capture(refs)
        if source['refs'] != refs or not self.sources.validate(source):
            raise ValueError('源快照已改变，请重新预检')
        actual, records = source['fields'], source['records']
        targets = {}
        for ref in refs:
            if ref not in actual or records[ref].get('decision') == 'DNP':
                raise ValueError('原理图缺少位号或标记为不装配：' + ref)
            bound = str(actual[ref].get('footprint') or '')
            selected = str(records[ref].get('field_overrides', {}).get('footprint', bound))
            if bound != selected:
                raise ValueError(ref + ' 封装尚未同步原理图，请先同步原理图后再更新 PCB')
            if ':' not in bound:
                raise ValueError(ref + ' 缺少有效的完整库封装标识')
            targets[ref] = bound
        return {'actual': actual, 'records': records, 'targets': targets,
                'identity': source['identity'], 'design':source}

    @staticmethod
    def _fields(source, ref):
        fields = {}
        for key, value in source['actual'][ref].items():
            name = source['design']['properties'][ref][key]
            builtin = {'value':'Value','description':'Description'}.get(key)
            if builtin:
                fields[builtin] = value
                if name.casefold() == builtin.casefold():
                    continue
            if key == 'footprint' and name.casefold() == 'footprint':
                continue
            fields[name] = value
        return fields

    def preview(self, refs: list[str], bom: str, *, alignment='rigid', source=None) -> dict:
        if alignment not in ('rigid', 'center'):
            raise ValueError('未知的封装对齐方式')
        if not refs or len(refs) > 10000 or len(set(refs)) != len(refs):
            raise ValueError('请选择 1 至 10000 个不重复的位号')
        with self.app.selection_save_lock:
            source = self._source(refs, source)
        snapshot = self.session.snapshot(self.app.project_root, refs)
        copper_refs = [ref for ref in refs if source['records'][ref].get('footprint_change', {}).get(
            'source', {}).get('instance_geometry_hash')]
        copper = board_copper_hashes(snapshot['text'], copper_refs) if copper_refs else {}
        replacements, targets, items = [], [], []
        libraries = {}
        for ref in refs:
            original = snapshot['parts'][ref]
            if original.get('symbol_path') not in source['identity']['paths'][ref]:
                raise ValueError(ref + ' 原理图实例与 PCB 关联不一致，请先核对 UUID / 重新关联，不能只凭同位号替换')
            desired = self._fields(source, ref)
            current_fields = metadata_values(original) if desired else {}
            folded = {name.casefold():name for name in current_fields}
            field_changes = [{'name':folded.get(name.casefold(), name),
                              'before':current_fields.get(folded.get(name.casefold(), name), ''), 'after':value}
                             for name, value in desired.items()
                             if current_fields.get(folded.get(name.casefold(), name)) != value]
            fields = {item['name']:item['after'] for item in field_changes}
            record = deepcopy(source['records'][ref])
            record.setdefault('field_overrides', {})['footprint'] = source['targets'][ref]
            change = classify_footprint_change(record, {**original, 'instance_geometry_hash':copper.get(ref)})
            kind = change['kind']
            common = {'ref':ref, 'kind':kind if kind != 'none' else 'fields' if fields else 'none',
                      'before':original['footprint'], 'after':source['targets'][ref],
                      'reason':change['reason'], 'field_changes':field_changes, 'needs_ack':kind == 'replace'}
            if kind == 'unknown':
                items.append(common)
                continue
            if kind in ('rename','none'):
                prototype = metadata_replacement(original, footprint=source['targets'][ref] if kind == 'rename' else None,
                                                 fields=fields) if kind == 'rename' or fields else original['proto']
                replacements.append({'ref':ref, 'proto':prototype, 'fields':fields, 'kind':common['kind']})
                evidence_target = record.get('footprint_change', {}).get('target', {}) if kind == 'rename' else {}
                if evidence_target.get('path'):
                    target_hash = hashlib.sha256(Path(evidence_target['path']).read_bytes()).hexdigest()
                    if target_hash != evidence_target.get('hash'):
                        raise ValueError('改名后的目标库封装已改变，请重新预检')
                    targets.append({'ref':ref, 'path':evidence_target['path'], 'hash':target_hash})
                items.append(common)
                continue
            key = (source['targets'][ref], original['back'])
            if key not in libraries:
                libraries[key] = self.session.read_library(key[0], back=key[1])
            target = libraries[key]
            if target['footprint'] != source['targets'][ref]:
                raise ValueError('库读取返回了不同的完整封装标识，请重新预检')
            placement = solve_placement(original, target, alignment=alignment)
            if not placement['ok']:
                raise ValueError(ref + ' 不能原位替换：' + placement['reason'])
            prototype = self.build(original, target, placement)
            if fields:
                prototype = metadata_replacement({'proto':prototype}, fields=fields)
            replacements.append({'ref': ref, 'proto': prototype, 'fields':fields, 'kind':'replace'})
            targets.append({'ref': ref, 'path': target['source_path'], 'hash': target['source_hash']})
            items.append({**common,
                'old_position_mm': original['position_mm'], 'old_rotation_deg': original['rotation_deg'],
                'position_mm': placement['position_mm'], 'rotation_deg': placement['rotation_deg'],
                'back': original['back'], 'max_error_mm': placement['max_error_mm'],
                'center_error_mm': placement['center_error_mm'], 'pad_errors': placement['pad_errors'],
                'differences': placement['differences']})
        with self.app.selection_save_lock:
            if not self.sources.validate(source['design']):
                raise ValueError('读取期间原理图或工作台记录已改变，请重新预检')
        token = uuid4().hex
        with self.lock:
            now = time.monotonic()
            for key in list(self.previews):
                if now - self.previews[key]['time'] > 600 or len(self.previews) >= 16:
                    del self.previews[key]
            self.previews[token] = {'refs': refs, 'snapshot': snapshot, 'source': source,
                'targets': targets, 'replacements': replacements, 'time': now, 'started': False,
                'alignment': alignment, 'items':items}
        return {'ok': True, 'preview_id': token, 'items': items,
                'blocked':any(item['kind'] == 'unknown' for item in items),
                'alignment': alignment,
                'basis': '当前 PCB 焊盘位置，不是历史位置恢复', 'saved': False}

    def apply(self, preview_id: str, acknowledged: bool) -> dict:
        with self.lock, self.app.selection_save_lock:
            state = self.previews.get(preview_id)
            if not state or time.monotonic() - state['time'] > 600:
                raise ValueError('预检已失效，请重新预检')
            if acknowledged is not True:
                raise ValueError('请确认已核对焊盘、尺寸、钻孔和引脚功能')
            if state.get('result'):
                # A lost response can be polled again; never repeat the mutation.
                return deepcopy(state['result'])
            if state['started']:
                raise ValueError('此预检已执行或结果未能确认，请检查 PCB 后重新预检，不能重复执行')
            if any(item['kind'] == 'unknown' for item in state['items']):
                raise ValueError('封装改动类型待核对，请先明确选择封装操作')
            if not self.sources.validate(state['source']['design']):
                raise ValueError('工作台记录或原理图已改变，请重新预检')
            for target in state['targets']:
                if hashlib.sha256(Path(target['path']).read_bytes()).hexdigest() != target['hash']:
                    raise ValueError('目标封装库已改变，请重新预检')
            if self.session.snapshot(self.app.project_root, state['refs']) != state['snapshot']:
                raise ValueError('PCB 在预检后发生变化，请重新预检')
            state['started'] = True
            if all(item['kind'] == 'none' for item in state['items']):
                state['result'] = {'ok':True, 'saved':False, 'refs':state['refs'], 'no_changes':True}
                return deepcopy(state['result'])
            directory = runtime_directory(self.app.project_root) / ('pcb-update-' + preview_id)
            result = self.session.apply(state['snapshot'], state['replacements'], directory)
            state['result'] = {key: value for key, value in result.items() if key != 'snapshot'}
            # Evidence is not a durable "synced" flag: undo/reopen is checked live on every preflight.
            try:
                evidence = runtime_directory(self.app.project_root) / 'PCB更新记录.jsonl'
                with evidence.open('a', encoding='utf-8') as output:
                    output.write(json.dumps({'preview_id': preview_id, 'time': time.time(),
                        'editor_only': True, 'requires_live_recheck': True, **state['result']}, ensure_ascii=False) + '\n')
            except OSError as exc:
                state['result']['warning'] = 'PCB 编辑器已更新，但运行记录保存失败：' + str(exc)
            return deepcopy(state['result'])

    def start(self, preview_id, acknowledged, jobs):
        with self.lock:
            state = self.previews.get(preview_id)
            if not state or time.monotonic() - state['time'] > 600 or acknowledged is not True:
                raise ValueError('请重新预检并明确确认更新 PCB')
            if state.get('job_id'):
                return jobs.status(state['job_id'])
            result = jobs.start({'operation': 'pcb-update', 'preview_id': preview_id, 'acknowledged': True})
            state['job_id'] = result['job_id']
            return result

    def job_id(self, preview_id):
        with self.lock:
            return self.previews.get(preview_id, {}).get('job_id', '')

    def comparison(self, preview_id, ref):
        from part_selection_footprint_geometry import board_references, align_module, render_comparison
        from kicad_sexpr import parse_sexpr
        with self.lock:
            state = self.previews.get(preview_id)
            if not state or ref not in state['refs'] or state['started'] or time.monotonic() - state['time'] > 600:
                raise ValueError('请重新预检后查看所选器件对比')
            if not self.sources.validate(state['source']['design']):
                raise ValueError('原理图或工作台记录已改变，请重新预检')
            if self.session.snapshot(self.app.project_root, state['refs']) != state['snapshot']:
                raise ValueError('PCB 已改变，请重新预检')
            if next(item for item in state['items'] if item['ref'] == ref)['kind'] != 'replace':
                raise ValueError('此器件没有需要渲染的几何替换')
            target = next(item for item in state['targets'] if item['ref'] == ref)
            content = Path(target['path']).read_bytes()
            source = content.decode('utf-8-sig')
            if hashlib.sha256(content).hexdigest() != target['hash']:
                raise ValueError('库文件已改变，请重新预检')
            cached = state.setdefault('comparisons', {})
            if ref in cached:
                return deepcopy(cached[ref])
            with tempfile.TemporaryDirectory(prefix='pcb-comparison-', dir=runtime_directory(self.app.project_root)) as directory:
                work = Path(directory)
                board = work / 'current.kicad_pcb'
                board.write_text(state['snapshot']['text'], encoding='utf-8')
                records = board_references(board, [ref], work)
                if len(records) != 1:
                    raise ValueError('当前 PCB 实例预览不可读取')
                # 3D models do not participate in a 2D preview; never strip them
                # from the formal library or the actual replacement definition.
                tree = parse_sexpr(source, 'footprint')
                for node in reversed(tree.children_named('model')):
                    source = source[:node.start] + source[node.end:]
                selected = work / 'target.kicad_mod'
                selected.write_text(source, encoding='utf-8')
                aligned = work / 'aligned.kicad_mod'
                result = align_module(selected, Path(records[0]['path']), aligned, method=state['alignment'])
                if not result.get('ok'):
                    raise ValueError('比较配准失败：' + str(result.get('reason')))
                rendered = render_comparison(Path(records[0]['path']), aligned, work / 'svg')
                if not rendered.get('ok'):
                    raise ValueError('封装图读取失败：' + str(rendered.get('reason')))
                if (self.session.snapshot(self.app.project_root, state['refs']) != state['snapshot'] or
                        not self.sources.validate(state['source']['design']) or
                        hashlib.sha256(Path(target['path']).read_bytes()).hexdigest() != target['hash']):
                    raise ValueError('预览生成期间 PCB 已改变，请重新预检')
                cached[ref] = {**rendered, 'alignment': result, 'pcb_comparison': True}
                return deepcopy(cached[ref])

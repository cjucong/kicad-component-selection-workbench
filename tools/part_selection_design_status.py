"""Read-only table status; native verification evidence never authorizes writes."""
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import threading
from uuid import uuid4

from kicad_sexpr import parse_sexpr
from part_selection_design_source import DesignSource
from part_selection_overrides import PCB_SYNC_EVIDENCE_NAME
from part_selection_runtime import runtime_directory

_EVIDENCE_LOCK = threading.RLock()


def _load_evidence(path, *, legacy=False):
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError('PCB 核验记录格式错误')
    if not legacy:
        if data.get('schema') != 1 or not isinstance(data.get('records'), dict):
            raise ValueError('PCB 核验记录版本或内容不支持')
        data = data['records']
    if not all(isinstance(ref, str) and isinstance(item, dict) for ref, item in data.items()):
        raise ValueError('PCB 核验记录条目格式错误')
    return data


def pcb_fields(values, properties):
    fields = {}
    for key, value in values.items():
        name = properties[key]
        builtin = {'value':'Value', 'description':'Description'}.get(key)
        if builtin:
            fields[builtin] = value
            if name.casefold() == builtin.casefold():
                continue
        if key == 'footprint' and name.casefold() == 'footprint':
            continue
        fields[name] = value
    return fields


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


_NUMERIC_NODES = {'at', 'start', 'end', 'mid', 'center', 'xy', 'xyz', 'size',
                  'width', 'thickness', 'offset', 'rotate', 'scale'}


def _pad_relevant(node):
    if node.head in ('property', 'model'):
        return False
    layer = node.child('layer')
    return not (node.head in ('fp_line','fp_arc','fp_rect','fp_circle','fp_poly','fp_text','fp_text_box')
                and layer and layer.arg() in {'F.SilkS','B.SilkS','F.Fab','B.Fab',
                    'F.CrtYd','B.CrtYd','Dwgs.User','Cmts.User'})


def _tree(node, content):
    if not node.children:
        return node.value
    values = [_tree(child, content) for child in node.children]
    if node.head in _NUMERIC_NODES:
        for index, child in enumerate(node.children[1:], 1):
            if child.children or content[child.start:child.start+1] == '"':
                continue
            try:
                number = Decimal(child.value)
                if number.is_finite():
                    exact = format(number, 'f')
                    values[index] = exact.rstrip('0').rstrip('.') if '.' in exact else exact
                    if not number:
                        values[index] = '0'
            except InvalidOperation:
                pass
    return values


class DesignStatus:
    def __init__(self, app):
        self.app = app
        self.sources = DesignSource(app)
        self.lock = threading.RLock()
        self.source = None
        self.board_hash = None
        self.parts = {}
        self.evidence_path = Path(app.config['bom_dir']) / PCB_SYNC_EVIDENCE_NAME
        self.legacy_evidence_path = runtime_directory(app.project_root) / 'design-status.json'
        self._migration_checked = False
        self._volatile = {}
        self.evidence = {}
        try:
            self.evidence = _load_evidence(self.evidence_path)
        except (OSError, ValueError):
            pass

    def settings_changed(self):
        with self.lock:
            self.evidence_path = Path(self.app.config['bom_dir']) / PCB_SYNC_EVIDENCE_NAME
            self.source = None

    def _board(self):
        root = Path(self.app.project_root)
        candidates = list(root.glob('*.kicad_pcb'))
        if not candidates:
            return None
        if len(candidates) != 1:
            raise ValueError('工程存在多个 PCB，请通过同步设计核验目标')
        return candidates[0]

    @staticmethod
    def _key(source, ref):
        versions = source['versions']
        table = str(Path(versions['configuration']['root']) / 'fp-lib-table')
        return _digest([source['fields'][ref], source['properties'][ref],
                        source['records'].get(ref, {}).get('footprint_change'),
                        source['identity']['paths'][ref], versions['hashes'].get(table)])

    @staticmethod
    def _geometry_key(source, ref):
        versions = source['versions']
        table = str(Path(versions['configuration']['root']) / 'fp-lib-table')
        return _digest([source['fields'][ref].get('footprint', ''),
                        source['records'].get(ref, {}).get('footprint_change'),
                        source['identity']['paths'][ref], versions['hashes'].get(table)])

    def _source_matches(self, evidence, source, ref):
        if 'geometry_source' in evidence:
            return evidence['geometry_source'] == self._geometry_key(source, ref)
        return evidence.get('source') == self._key(source, ref)

    def _read(self, content, *, cache=True):
        digest = hashlib.sha256(content).hexdigest()
        if digest == self.board_hash:
            return digest, self.parts
        parts = {}
        text = content.decode('utf-8-sig')
        for fp in parse_sexpr(text, 'kicad_pcb').children_named('footprint'):
            fields = {}
            for prop in fp.children_named('property'):
                name = prop.arg().casefold()
                if name in fields:
                    raise ValueError('PCB 存在重复属性：' + prop.arg())
                fields[name] = prop.arg(2)
            for prop in fp.children_named('fp_text'):
                if prop.arg() in ('reference', 'value'):
                    fields.setdefault(prop.arg(), prop.arg(2))
            ref = fields.get('reference', '').upper()
            if not ref:
                continue
            if ref in parts:
                raise ValueError('PCB 存在重复位号：' + ref)
            # IPC adds library headers which the saved board omits.
            instance = [child for child in fp.children if not child.children
                        or child.head not in ('version', 'generator', 'generator_version')]
            # Native reading reorders footprint objects and trims decimal zeroes.
            # Keep every UUID/property and nested order; accept no coordinate drift.
            data = [_tree(child, text) for child in instance]
            atoms = [value for value in data if not isinstance(value, list)]
            objects = sorted((value for value in data if isinstance(value, list)),
                             key=lambda value:json.dumps(value, sort_keys=True))
            parts[ref] = {'footprint':fp.arg(), 'fields':fields,
                         'digest':_digest([atoms, objects]),
                         'pad_digest':_digest([atoms, sorted(
                             [_tree(child, text) for child in instance
                              if child.children and _pad_relevant(child)],
                             key=lambda value:json.dumps(value, sort_keys=True))]),
                         'path':fp.child('path').arg() if fp.child('path') else ''}
        if cache:
            self.board_hash, self.parts = digest, parts
        return digest, parts

    def _portable_targets(self, targets):
        root = Path(self.app.project_root).resolve()
        result = []
        for item in targets:
            path = Path(item['path']).resolve()
            local = path.is_relative_to(root)
            result.append({**item, 'path':path.relative_to(root).as_posix() if local else str(path),
                           'scope':'project' if local else 'external'})
        return result

    def _valid_targets(self, evidence, *, legacy=False, footprint=None):
        try:
            root = Path(self.app.project_root).resolve()
            targets = evidence.get('targets', [])
            if not isinstance(targets, list):
                return False
            for item in targets:
                path = Path(item['path'])
                if not legacy and item.get('scope') == 'project':
                    if path.is_absolute():
                        return False
                    path = (root / path).resolve()
                    if not path.is_relative_to(root):
                        return False
                elif not path.is_absolute() or (not legacy and item.get('scope') != 'external'):
                    return False
                if footprint:
                    from part_selection_footprints import FootprintChoices, item_name
                    nickname, name = (item_name(value) for value in footprint.split(':'))
                    library = FootprintChoices(root).libraries().get(nickname)
                    if library is None or (library / (name + '.kicad_mod')).resolve() != path.resolve():
                        return False
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item['hash']:
                    return False
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _store(self, updates):
        # Merge only this operation's refs; another window may have newer receipts.
        # A conflicted/unsupported shared file must never be silently overwritten.
        with _EVIDENCE_LOCK:
            self._volatile.update(updates)
            evidence = {**_load_evidence(self.evidence_path), **updates}
            self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.evidence_path.with_suffix('.' + uuid4().hex + '.tmp')
            try:
                temporary.write_text(json.dumps({'schema':1, 'records':evidence},
                    sort_keys=True, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                temporary.replace(self.evidence_path)
                for ref in updates:
                    self._volatile.pop(ref, None)
                self.evidence = {**evidence, **self._volatile}
            finally:
                temporary.unlink(missing_ok=True)

    def _refresh_evidence(self, source, parts):
        with _EVIDENCE_LOCK:
            if self.evidence_path.exists():
                self._migration_checked = True
            self.evidence = _load_evidence(self.evidence_path)
            self.evidence.update(self._volatile)
            if self.evidence_path.exists() or self._migration_checked:
                return
            self._migration_checked = True
            legacy = _load_evidence(self.legacy_evidence_path, legacy=True)
            migrated = {}
            for ref, item in legacy.items():
                if (ref in self._volatile or ref not in source['fields'] or ref not in parts
                        or not self._source_matches(item, source, ref)
                        or not self._valid_targets(item, legacy=True,
                            footprint=source['fields'][ref].get('footprint'))):
                    continue
                same = (item.get('instance') == parts[ref]['digest']
                        or item.get('pad_instance') == parts[ref]['pad_digest'])
                if not same and not item.get('uncertain'):
                    continue
                migrated[ref] = {**item, 'targets':self._portable_targets(item.get('targets', []))}
            if migrated and self.sources.validate(source):
                path = self._board()
                if path and hashlib.sha256(path.read_bytes()).hexdigest() == self.board_hash:
                    self._store(migrated)

    def observe(self, state, *, saved=False, verified=None):
        """Certify geometry only for the exact saved board and validated libraries."""
        with self.lock:
            try:
                self._observe(state, saved, verified)
            except (OSError, ValueError, KeyError, TypeError):
                # Table evidence failure must never turn a successful CAD save
                # into an apparently failed transaction. Fall back to unverified.
                return False
        return True

    def _observe(self, state, saved, verified):
        path = self._board()
        if not path:
            return
        content = path.read_bytes()
        expected = verified if saved else state['snapshot']
        _, expected_parts = self._read(expected['text'].encode('utf-8'), cache=False)
        source = state['source']['design']
        # A save can follow a field-only transaction scoped to a subset of
        # the preflight. Certify only actually verified instances.
        refs = state['refs'] if saved else state['requested_refs']
        items = {item['ref']:item for item in state['items']}
        _, parts = self._read(content)
        updates = {}
        for ref in refs:
            if (ref not in parts or ref not in expected_parts
                    or parts[ref]['digest'] != expected_parts[ref]['digest']):
                continue
            item = items[ref]
            evidence = {'instance':parts[ref]['digest'], 'pad_instance':parts[ref]['pad_digest'],
                        'source':self._key(source, ref), 'geometry_source':self._geometry_key(source, ref)}
            if item.get('retained_board_annotations'):
                evidence['retained_board_annotations'] = True
            if not saved and item['kind'] not in ('none', 'fields'):
                evidence.update(pending_kind=item['kind'], reason=item.get('blocked_reason') or item.get('reason', ''))
            else:
                evidence['targets'] = self._portable_targets(
                    [target for target in state['targets'] if target['ref'] == ref])
                if not self._valid_targets(evidence):
                    continue
            updates[ref] = evidence
        if updates and self.sources.validate(source):
            self._store(updates)

    def uncertain(self, state, reason):
        with self.lock:
            try:
                source = state['source']['design']
                updates = {}
                for item in state['items']:
                    if item['kind'] != 'none':
                        ref = item['ref']
                        updates[ref] = {'source':self._key(source, ref),
                                        'geometry_source':self._geometry_key(source, ref), 'uncertain':reason}
                if updates:
                    self._store(updates)
            except (OSError, ValueError, KeyError, TypeError):
                return False
        return True

    def pending_refs(self, source, desired):
        rows = [{'refs_list':source['refs']}]
        self.enrich(rows, '__schematic__', source=source, desired=desired)
        states = rows[0].get('pcb_sync_by_ref', {})
        return [ref for ref in source['refs'] if states.get(ref, {}).get('status') in ('pending', 'unverified')
                or (ref not in states and (source['records'].get(ref, {}).get('footprint_change') or {}
                                          ).get('operation') == 'replace')
                or source['fields'][ref].get('footprint', '') != desired[ref].get(
                    'footprint', source['fields'][ref].get('footprint', ''))]

    def enrich(self, rows, selected_bom, *, source=None, desired=None):
        with self.lock:
            try:
                path = self._board()
                if path is None:
                    return rows
                _, parts = self._read(path.read_bytes())
                if source is None:
                    if self.source is None or not self.sources.validate(self.source):
                        self.source = self.sources.capture(allow_legacy=True, include_terminals=False)
                    source = self.source
                evidence_error = ''
                try:
                    self._refresh_evidence(source, parts)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    self.evidence = dict(self._volatile)
                    evidence_error = 'PCB 核验记录不可用：' + str(exc)
                if desired is None:
                    desired = self.app.design_sync_fields(source, '__schematic__')
                for row in rows:
                    states = {}
                    for ref in row.get('refs_list', []):
                        if ref not in desired:
                            states[ref] = {'status':'unverified', 'reason':'原理图未找到位号'}
                            continue
                        values = {**source['fields'][ref], **desired[ref]}
                        if not self.app.is_schematic_source(selected_bom):
                            values.update(row.get('per_ref_sync_fields', {}).get(ref, {}))
                        target = values.get('footprint', '')
                        if not target:
                            states[ref] = {'status':'not_applicable'}
                            continue
                        part = parts.get(ref)
                        if part is None:
                            states[ref] = {'status':'pending', 'reason':'已保存 PCB 中未找到器件'}
                            continue
                        if part['path'] not in source['identity']['paths'][ref]:
                            states[ref] = {'status':'unverified', 'reason':'PCB 与原理图实例关联待核验'}
                            continue
                        fields = pcb_fields(values, source['properties'][ref])
                        changes = [{'name':name, 'before':part['fields'].get(name.casefold(), ''), 'after':value}
                                   for name, value in fields.items()
                                   if part['fields'].get(name.casefold(), '') != value]
                        intent = source['records'][ref].get('footprint_change') or {}
                        status = {'status':'synced', 'field_changes':changes,
                                  'before':part['footprint'], 'after':target}
                        evidence = self.evidence.get(ref, {})
                        same_instance = ((evidence.get('instance') == part['digest']
                                          or evidence.get('pad_instance') == part['pad_digest'])
                                         and self._source_matches(evidence, source, ref))
                        certified = (same_instance and not evidence.get('pending_kind')
                                     and self._valid_targets(evidence, footprint=target))
                        if part['footprint'] != target:
                            status['status'] = 'pending'
                        elif intent.get('operation') == 'replace' and not certified:
                            status.update(status='unverified', reason='封装名称已一致，几何仍需 PCB 实时核验')
                        if evidence_error and status['status'] == 'synced':
                            status.update(status='unverified', reason=evidence_error)
                        if same_instance and evidence.get('pending_kind'):
                            status.update(status='unverified' if evidence['pending_kind'] == 'unknown' else 'pending',
                                          reason=evidence.get('reason') or 'PCB 预检差异尚未完成')
                        if evidence.get('uncertain') and self._source_matches(evidence, source, ref):
                            status.update(status='unverified', reason='上次 PCB 结果待核验：' + evidence['uncertain'])
                        if status['status'] == 'synced' and certified and evidence.get('retained_board_annotations'):
                            status.update(retained_board_annotations=True, reason='焊盘已核验，保留板级定制')
                        states[ref] = status
                    row['pcb_sync_by_ref'] = states
                    self._summarize(row)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                for row in rows:
                    row['pcb_sync_by_ref'] = {ref:{'status':'unverified', 'reason':str(exc)}
                                              for ref in row.get('refs_list', [])}
                    self._summarize(row)
        return rows

    @staticmethod
    def _summarize(row):
        states = row['pcb_sync_by_ref']
        row['pcb_changed_refs'] = ','.join(ref for ref, item in states.items() if item['status'] == 'pending')
        row['pcb_unverified_refs'] = ','.join(ref for ref, item in states.items() if item['status'] == 'unverified')

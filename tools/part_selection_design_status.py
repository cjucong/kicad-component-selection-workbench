"""Read-only table status; native verification evidence never authorizes writes."""
import hashlib
import json
from pathlib import Path
import threading
from uuid import uuid4

from kicad_sexpr import parse_sexpr
from part_selection_design_source import DesignSource
from part_selection_runtime import runtime_directory


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


def _tree(node):
    return [_tree(child) for child in node.children] if node.children else node.value


class DesignStatus:
    def __init__(self, app):
        self.app = app
        self.sources = DesignSource(app)
        self.lock = threading.RLock()
        self.source = None
        self.board_hash = None
        self.parts = {}
        self.evidence_path = runtime_directory(app.project_root) / 'design-status.json'
        try:
            self.evidence = json.loads(self.evidence_path.read_text(encoding='utf-8'))
            if not isinstance(self.evidence, dict):
                self.evidence = {}
            self.evidence = {ref:item for ref, item in self.evidence.items() if isinstance(item, dict)}
        except (OSError, ValueError):
            self.evidence = {}

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

    def _read(self, content, *, cache=True):
        digest = hashlib.sha256(content).hexdigest()
        if digest == self.board_hash:
            return digest, self.parts
        parts = {}
        for fp in parse_sexpr(content.decode('utf-8-sig'), 'kicad_pcb').children_named('footprint'):
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
            parts[ref] = {'footprint':fp.arg(), 'fields':fields,
                         'digest':_digest([_tree(child) for child in instance]),
                         'path':fp.child('path').arg() if fp.child('path') else ''}
        if cache:
            self.board_hash, self.parts = digest, parts
        return digest, parts

    def _store(self):
        temporary = self.evidence_path.with_suffix('.' + uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(self.evidence, ensure_ascii=False), encoding='utf-8')
            temporary.replace(self.evidence_path)
        finally:
            temporary.unlink(missing_ok=True)

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
        for ref in refs:
            if (ref not in parts or ref not in expected_parts
                    or parts[ref]['digest'] != expected_parts[ref]['digest']):
                continue
            item = items[ref]
            evidence = {'instance':parts[ref]['digest'], 'source':self._key(source, ref)}
            if not saved and item['kind'] not in ('none', 'fields'):
                evidence.update(pending_kind=item['kind'], reason=item.get('blocked_reason') or item.get('reason', ''))
            else:
                evidence['targets'] = [target for target in state['targets'] if target['ref'] == ref]
            self.evidence[ref] = evidence
        self._store()

    def uncertain(self, state, reason):
        with self.lock:
            try:
                source = state['source']['design']
                for item in state['items']:
                    if item['kind'] != 'none':
                        ref = item['ref']
                        self.evidence[ref] = {'source':self._key(source, ref), 'uncertain':reason}
                self._store()
            except (OSError, ValueError, KeyError, TypeError):
                return False
        return True

    def enrich(self, rows, selected_bom):
        with self.lock:
            try:
                path = self._board()
                if path is None:
                    return rows
                _, parts = self._read(path.read_bytes())
                if self.source is None or not self.sources.validate(self.source):
                    self.source = self.sources.capture(allow_legacy=True, include_terminals=False)
                source = self.source
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
                        same_instance = (evidence.get('instance') == part['digest']
                                         and evidence.get('source') == self._key(source, ref))
                        certified = (same_instance and not evidence.get('pending_kind')
                            and all(Path(item['path']).is_file() and hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()
                                    == item['hash'] for item in evidence.get('targets', [])))
                        if part['footprint'] != target or changes:
                            status['status'] = 'pending'
                        elif intent.get('operation') == 'replace' and not certified:
                            status.update(status='unverified', reason='封装名称已一致，几何仍需 PCB 实时核验')
                        if same_instance and evidence.get('pending_kind'):
                            status.update(status='unverified' if evidence['pending_kind'] == 'unknown' else 'pending',
                                          reason=evidence.get('reason') or 'PCB 预检差异尚未完成')
                        if evidence.get('uncertain') and evidence.get('source') == self._key(source, ref):
                            status.update(status='unverified', reason='上次 PCB 结果待核验：' + evidence['uncertain'])
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

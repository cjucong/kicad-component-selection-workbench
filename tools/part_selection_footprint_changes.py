"""Server-issued footprint operation evidence, separate from selection fields."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json


def board_copper_hashes(text, refs):
    """Pose and complete pad definitions from the PCB's own saved/live serialization."""
    from kicad_sexpr import parse_sexpr
    ignored = {'uuid','tstamp','net','pinfunction','pintype'}
    def data(node):
        if node.head in ignored:
            return None
        if node.children:
            return [value for child in node.children if (value := data(child)) is not None]
        if text[node.start:node.start+1] != '"':
            try:
                number = Decimal(node.value)
                if number.is_finite():
                    return str(number.normalize()) if number else '0'
            except InvalidOperation:
                pass
        return node.value
    result = {}
    for fp in parse_sexpr(text, 'kicad_pcb').children_named('footprint'):
        prop = next((p for p in fp.children_named('property') if p.arg() == 'Reference'), None)
        old_ref = next((p for p in fp.children_named('fp_text') if p.arg() == 'reference'), None)
        ref = prop.arg(2) if prop else old_ref.arg(2) if old_ref else ''
        if ref not in refs:
            continue
        if ref in result:
            raise ValueError('PCB 存在重复位号：' + ref)
        values = sorted(json.dumps(data(n), ensure_ascii=True) for n in fp.children
                        if n.head in ('pad','at','layer'))
        result[ref] = hashlib.sha256(json.dumps(values).encode()).hexdigest()
    return result


def make_footprint_change(*, operation: str, before: str, after: str,
                          source: dict, target: dict) -> dict:
    if operation not in ('rename', 'replace') or ':' not in before or ':' not in after:
        raise ValueError('Invalid footprint operation evidence')
    if not isinstance(source, dict) or not isinstance(target, dict):
        raise ValueError('Invalid footprint evidence sources')
    return {'schema': 1, 'operation': operation, 'before': before, 'after': after,
            'source': deepcopy(source), 'target': deepcopy(target)}


def footprint_intent_hint(record, target):
    """Display recorded intent only; actual PCB geometry is verified at PCB preflight."""
    evidence = record.get('footprint_change') or {}
    if (evidence.get('schema') == 1 and evidence.get('after') == target
            and evidence.get('operation') in ('rename', 'replace')):
        return evidence['operation']
    return 'unknown'


def classify_footprint_change(record: dict, live: dict) -> dict:
    current = str(live.get('footprint') or '')
    target = str(record.get('field_overrides', {}).get('footprint', current))
    evidence = record.get('footprint_change')
    valid = (isinstance(evidence, dict) and evidence.get('schema') == 1
             and evidence.get('operation') in ('rename', 'replace')
             and evidence.get('after') == target
             and current in (evidence.get('before'), target))
    if not valid:
        return {'kind': 'none' if target == current else 'unknown',
                'reason': '' if target == current else '缺少可信的封装操作记录，请核对改动类型'}
    if evidence['operation'] == 'replace':
        return {'kind': 'replace', 'reason': '明确选择使用目标封装几何'}
    source = evidence.get('source', {})
    key = 'instance_geometry_hash' if source.get('instance_geometry_hash') else 'geometry_hash'
    source_hash = source.get(key)
    if not source_hash or live.get(key) != source_hash:
        return {'kind': 'unknown', 'reason': '原封装几何与改名来源不一致，请重新预检'}
    return {'kind': 'none' if target == current else 'rename', 'reason': '保留当前实例几何'}

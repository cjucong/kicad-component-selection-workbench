"""Server-issued footprint operation evidence, separate from selection fields."""
from copy import deepcopy


def make_footprint_change(*, operation: str, before: str, after: str,
                          source: dict, target: dict) -> dict:
    if operation not in ('rename', 'replace') or ':' not in before or ':' not in after:
        raise ValueError('Invalid footprint operation evidence')
    if not isinstance(source, dict) or not isinstance(target, dict):
        raise ValueError('Invalid footprint evidence sources')
    return {'schema': 1, 'operation': operation, 'before': before, 'after': after,
            'source': deepcopy(source), 'target': deepcopy(target)}


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
    source_hash = evidence.get('source', {}).get('geometry_hash')
    if not source_hash or live.get('geometry_hash') != source_hash:
        return {'kind': 'unknown', 'reason': '原封装几何与改名来源不一致，请重新预检'}
    return {'kind': 'none' if target == current else 'rename', 'reason': '保留当前实例几何'}

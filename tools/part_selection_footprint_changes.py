"""Server-issued footprint operation evidence, separate from selection fields."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json


def _board_hashes(text, refs, *, include_instance, stroke_styles):
    from kicad_sexpr import parse_sexpr
    ignored = {'uuid','tstamp','net','pinfunction','pintype'}
    refs = set(refs)
    def data(node, style, parent=''):
        if node.head in ignored:
            return None
        # KiCad serializes its default solid stroke as either spelling.
        # Do not normalize unrelated type tokens or any dashed/dotted stroke.
        if (style and parent == 'stroke' and node.head == 'type'
                and len(node.children) == 2 and node.arg() in ('default', 'solid')):
            return ['type', style]
        if node.children:
            return [value for child in node.children if (value := data(child, style, node.head)) is not None]
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
        heads = {'pad','at','layer'}
        if include_instance:
            heads.update(('fp_line','fp_arc','fp_circle','fp_rect','fp_poly','zone','model','path'))
        hashes = []
        for style in stroke_styles:
            values = sorted(json.dumps(data(n, style), ensure_ascii=True) for n in fp.children if n.head in heads)
            if include_instance:
                identity = fp.child('uuid') or fp.child('tstamp')
                values.append('instance:' + (identity.arg() if identity else ''))
            hashes.append(hashlib.sha256(json.dumps(values).encode()).hexdigest())
        result[ref] = hashes
    return result


def board_copper_hashes(text, refs, *, include_instance=False, stroke_style=None):
    """Pose and complete pad definitions; the default retains legacy exact hashes."""
    if stroke_style not in (None, 'default', 'solid'):
        raise ValueError('Unknown canonical stroke style')
    return {ref: hashes[0] for ref, hashes in _board_hashes(text, refs,
        include_instance=include_instance, stroke_styles=(stroke_style,)).items()}


def board_source_hash_variants(text, refs):
    """Legacy source identities and canonical stroke aliases, with one PCB parse."""
    return {ref: list(dict.fromkeys(hashes)) for ref, hashes in _board_hashes(text, refs,
        include_instance=True, stroke_styles=(None, 'default', 'solid')).items()}


def recover_legacy_source_hashes(sources, live_hashes, candidates, cache):
    """Recover mixed legacy spellings only from hash-verified original PCB bytes.

    A legacy hash cannot be canonicalized by itself. Never guess arbitrary
    combinations: verify the original board and instance hash before comparing.
    """
    pending = {}
    for ref, source in sources.items():
        source_hash = source.get('instance_source_hash')
        hashes = live_hashes.get(ref, [])
        board_hash = source.get('board', {}).get('hash')
        key = (board_hash, ref, source_hash)
        if source_hash and source_hash not in hashes and board_hash:
            if key in cache:
                if cache[key] in hashes:
                    hashes.append(source_hash)
            else:
                pending[ref] = (board_hash, source_hash)
    if not pending:
        return
    seen = set()
    for path in candidates:
        try:
            path = path.resolve()
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            content = path.read_bytes()
        except OSError:
            continue
        board_hash = hashlib.sha256(content).hexdigest()
        refs = [ref for ref, (expected, _) in pending.items() if expected == board_hash]
        if not refs:
            continue
        try:
            original = _board_hashes(content.decode('utf-8-sig'), refs,
                                     include_instance=True, stroke_styles=(None, 'solid'))
        except (UnicodeError, ValueError):
            continue
        for ref in refs:
            source_hash = pending[ref][1]
            # Raw must match, not just another alias or a same-named instance.
            if ref in original and original[ref][0] == source_hash:
                canonical = original[ref][-1]
                cache[(board_hash, ref, source_hash)] = canonical
                if canonical in live_hashes.get(ref, ()):
                    live_hashes[ref].append(source_hash)
                del pending[ref]
        if not pending:
            break


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
    if source.get('board') and not source.get('instance_source_hash'):
        return {'kind':'unknown', 'reason':'旧改名记录缺少原实例身份，请重新预览并确认改名'}
    key = ('instance_source_hash' if source.get('instance_source_hash') else
           'instance_geometry_hash' if source.get('instance_geometry_hash') else 'geometry_hash')
    source_hash = source.get(key)
    matches = live.get(key) == source_hash
    if key == 'instance_source_hash':
        matches = matches or source_hash in live.get('instance_source_hash_variants', ())
    if not source_hash or not matches:
        return {'kind': 'unknown', 'reason': '原封装几何与改名来源不一致，请重新预检'}
    return {'kind': 'none' if target == current else 'rename', 'reason': '保留当前实例几何'}

"""Validated manual correspondence between two numbered footprint pad sets.

The correspondence is deliberately separate from the subset used to solve a
rigid transform.  A pad may be mapped for electrical identity but excluded
from alignment; a source pad may only be omitted through an explicit drop.
"""

from __future__ import annotations

from kicad_sexpr import parse_sexpr


SCHEMA = 1


def _unique(values, label):
    if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) for v in values):
        raise ValueError(f'{label} must contain string pad numbers')
    values = list(values)
    if any(not value for value in values):
        raise ValueError(f'{label} contains an empty pad number')
    if len(values) != len(set(values)):
        raise ValueError(f'{label} contains duplicate pad numbers')
    return values


def validate_mapping(plan, source_numbers, target_numbers):
    """Return a normalized mapping or reject an incomplete/ambiguous plan.

    ``source`` is the original PCB/schematic terminal number and ``target``
    is the selected footprint's number.  ``dropped`` is intentionally a
    separate list so an alignment exclusion can never silently drop a net.
    """
    if not isinstance(plan, dict) or type(plan.get('schema')) is not int or plan['schema'] != SCHEMA:
        raise ValueError('manual pad mapping schema is unsupported')
    source = _unique(source_numbers, 'source_numbers')
    target = _unique(target_numbers, 'target_numbers')
    source_set, target_set = set(source), set(target)
    pairs = plan.get('pairs')
    dropped = plan.get('dropped', [])
    if not isinstance(pairs, list) or not isinstance(dropped, list):
        raise ValueError('manual pad mapping requires pairs and dropped lists')
    if any(not isinstance(value, str) for value in dropped):
        raise ValueError('dropped must contain string pad numbers')
    if len(dropped) != len(set(dropped)) or not set(dropped) <= source_set:
        raise ValueError('dropped contains an unknown or repeated source pad')
    seen_source, seen_target, normalized = set(), set(), []
    for item in pairs:
        if not isinstance(item, dict):
            raise ValueError('each manual pad mapping pair must be an object')
        left, right = item.get('source'), item.get('target')
        if not isinstance(left, str) or not isinstance(right, str) or type(item.get('align')) is not bool:
            raise ValueError('pad pairs require string numbers and a boolean align flag')
        if left not in source_set or right not in target_set:
            raise ValueError('manual pad mapping contains an unknown pad')
        if left in seen_source or left in dropped or right in seen_target:
            raise ValueError('manual pad mapping contains a one-to-many or many-to-one pair')
        seen_source.add(left)
        seen_target.add(right)
        normalized.append({'source': left, 'target': right, 'align': item.get('align', True) is True})
    unaccounted = source_set - seen_source - set(dropped)
    if unaccounted:
        raise ValueError('unaccounted source pads: ' + ', '.join(sorted(unaccounted)))
    extras = target_set - seen_target
    if extras & set(dropped):
        raise ValueError('extra target numbers reuse dropped schematic terminals: ' + ', '.join(sorted(extras & set(dropped))))
    align = [(item['source'], item['target']) for item in normalized if item['align']]
    if not align:
        raise ValueError('at least one mapped pad must participate in alignment')
    number_map = {item['target']: item['source'] for item in normalized}
    final_numbers = set(number_map.values()) | extras
    if len(final_numbers) != len(target_set):
        raise ValueError('mapped target pad numbers collide')
    return {
        'schema': SCHEMA,
        'pairs': normalized,
        'dropped': sorted(dropped),
        'extras': sorted(extras),
        'alignment_pairs': align,
        'number_map': number_map,
    }


def adapt_numbers(source, mapping):
    """Return a copy whose mapped target pads use original terminal numbers."""
    root = parse_sexpr(source, 'footprint')
    number_map = mapping.get('number_map', {})
    if any(old != new for old, new in number_map.items()) and any(
            root.children_named(head) for head in ('net_tie_pad_groups', 'jumper_pad_groups')):
        raise ValueError('目标封装包含网络连接或跳线焊盘分组，暂不支持自动改号；请在库编辑器核对分组后另存封装')
    edits = []
    for pad in root.children_named('pad'):
        old = pad.arg(1)
        new = number_map.get(old, old)
        if new != old:
            token = pad.children[1]
            replacement = '"' + new.replace('\\', '\\\\').replace('"', '\\"') + '"'
            edits.append((token.start, token.end, replacement))
    result = source
    for start, end, replacement in reversed(edits):
        result = result[:start] + replacement + result[end:]
    return result


def symbol_terminals(symbol, page):
    """Read embedded symbol pins; missing definitions stay unknown, never empty."""
    library = page.child('lib_symbols')
    lib_id = symbol.child('lib_id')
    if not library or not lib_id:
        return None
    definitions = {entry.arg():entry for entry in library.children_named('symbol')}
    unit = symbol.child('unit')
    unit = unit.arg() if unit else '1'
    convert = symbol.child('convert')
    convert = convert.arg() if convert else '1'

    def collect(name, ancestors):
        definition = definitions.get(name)
        if definition is None or name in ancestors:
            return None
        values = set()
        parent = definition.child('extends')
        if parent:
            inherited = collect(parent.arg(), ancestors | {name})
            if inherited is None:
                return None
            values.update(inherited)
        for group in definition.children_named('symbol'):
            suffix = group.arg().rsplit('_', 2)
            if len(suffix) != 3 or suffix[1] not in ('0', unit) or suffix[2] not in ('0', convert):
                continue
            for pin in group.children_named('pin'):
                number = pin.child('number')
                if not number or not number.arg():
                    return None
                values.add(number.arg())
        return values

    values = collect(lib_id.arg(), set())
    return sorted(values) if values is not None else None


def verify_extra_terminals(extras, terminals):
    if not extras:
        return
    if terminals is None:
        raise ValueError('无法核验原理图引脚编号；额外焊盘不能保证未分配网络，请补全原理图符号后重试')
    conflicts = set(extras) & set(terminals)
    if conflicts:
        raise ValueError('额外焊盘与原理图已有端子同号，不能视为未分配网络：' + ', '.join(sorted(conflicts)))


def dropped_terminal_report(board_text, ref, numbers):
    """Read net membership, without claiming membership proves copper contact."""
    if not numbers:
        return []
    board = parse_sexpr(board_text, 'kicad_pcb')
    footprints = []
    for fp in board.children_named('footprint'):
        prop = next((p for p in fp.children_named('property') if p.arg() == 'Reference'), None)
        old_ref = next((p for p in fp.children_named('fp_text') if p.arg() == 'reference'), None)
        if (prop.arg(2) if prop else old_ref.arg(2) if old_ref else '') == ref:
            footprints.append(fp)
    if len(footprints) != 1:
        raise ValueError('无法核验将放弃的 PCB 端子：' + ref)
    pads = {pad.arg():pad for pad in footprints[0].children_named('pad') if pad.arg()}
    report = []
    net_names = {net.arg():net.arg(2) for net in board.children_named('net')}
    for number in numbers:
        if number not in pads:
            raise ValueError('PCB 中缺少将放弃的端子：' + number)
        net = pads[number].child('net')
        code = net.arg() if net else '0'
        # KiCad 10 IPC emits (net "name"); saved legacy boards can use
        # (net code "name"). Preserve the actual name in either format.
        if net is not None and len(net.children) == 2:
            name = code
            assigned = bool(name)
        else:
            name = (net.arg(2) if net else '') or net_names.get(code, '')
            assigned = bool(name) or code not in ('', '0')
        members = {head:sum(1 for item in board.children_named(head)
                            if assigned and item.child('net') and item.child('net').arg() == code)
                   for head in ('segment', 'arc', 'via', 'zone')}
        report.append({'number':number, 'net_code':code, 'net_name':name,
                       'net_assigned':assigned, 'same_net_objects':members,
                       'physical_connection':'未核验物理接触；同网对象数量不代表直接连接',
                       'warning':'此端子将移除；走线、过孔和区域保持原样，可能留下悬空线端或接触新铜皮。请检查连接与 DRC。'})
    return report

"""Pure numbered-pad world-coordinate placement; no CAD writes or library copies."""
import json
import math

from part_selection_footprint_geometry import estimate_alignment, estimate_mapped_alignment
from part_selection_pad_mapping import validate_mapping


def _pad_geometry_fields(before, after):
    fields=[]
    for key in sorted(set(before) | set(after)):
        if key in ('number','position_mm','uuid','net','padstack'):
            continue
        a,b=before.get(key),after.get(key)
        # Preflight only: native nm-coordinate fitting can perturb angles.
        # Complete staged/readback protobuf comparisons remain unchanged.
        if key=='rotation_deg' and isinstance(a,(int,float)) and isinstance(b,(int,float)):
            if abs(math.remainder(a-b,360))<=.0001:
                continue
        if a!=b:
            fields.append(key)
    return fields


def mapped_pad_changes(source, target, mapping):
    """Report only electrically mapped pads; unnamed mechanical pads stay out."""
    before = {p['number']:p for p in source['pads'] if p['number']}
    after = {p['number']:p for p in target['pads'] if p['number']}
    errors, differences, centers = [], [], [[], []]
    for pair in mapping['pairs']:
        a, b = before[pair['source']], after[pair['target']]
        errors.append({'number':pair['source'], 'mapped_number':pair['target'],
                       'error_mm':math.dist(a['position_mm'], b['position_mm'])})
        fields = _pad_geometry_fields(a,b)
        if fields:
            differences.append({'number':pair['source'],'fields':fields,
                'before':{key:a.get(key) for key in fields},'after':{key:b.get(key) for key in fields}})
        if pair['align']:
            centers[0].append(a['position_mm']);centers[1].append(b['position_mm'])
    def center(points):
        return [(min(p[i] for p in points)+max(p[i] for p in points))/2 for i in (0,1)]
    return {'pad_errors':errors,'max_error_mm':max((p['error_mm'] for p in errors),default=0.0),
            'differences':differences,'center_error_mm':math.dist(*(center(p) for p in centers))}


def adapted_placement(source, target, evidence):
    """Restore the approved reference pose without solving away manual offsets.

    The published module already contains the user's full local transform and
    final terminal numbers.  The PCB operation only restores its baseline pose.
    """
    adaptation = evidence['adaptation']
    if adaptation.get('schema') != 1 or adaptation.get('pose') != 'reference-local':
        raise ValueError('封装适配记录格式不支持，请重新预览')
    mapping = adaptation['mapping']
    raw = adaptation['raw_target']
    normalized = validate_mapping(mapping, [p['source'] for p in mapping['pairs']] + mapping['dropped'],
                                  [p['target'] for p in mapping['pairs']] + mapping['extras'])
    pose = evidence['source'].get('placement')
    if not pose or pose.get('back') is not source['back'] or source['back'] != target['back']:
        raise ValueError('手动适配需要已保存 PCB 实例的板面与放置基准，请重新预览')
    if not all(math.isfinite(v) for v in [*pose['position_mm'],pose['rotation_deg']]):
        raise ValueError('无效的适配放置基准')
    numbers = [pad['number'] for pad in target['pads'] if pad['number']]
    expected = {pair['source'] for pair in normalized['pairs']} | set(normalized['extras'])
    if len(numbers) != len(set(numbers)) or set(numbers) != expected:
        raise ValueError('适配目标焊盘编号已改变，请重新预览')
    old = {pad['number']:pad for pad in source['pads'] if pad['number']}
    if len(old) != sum(bool(pad['number']) for pad in source['pads']):
        raise ValueError('原实例存在重复端子，不能手动替换')
    final_mapping = {'schema':1, 'pairs':[{'source':p['source'],'target':p['source'],'align':p['align']}
                                      for p in normalized['pairs']], 'dropped':normalized['dropped']}
    first = source['footprint'] == evidence['before']
    if not first and source['footprint'] != evidence['after']:
        raise ValueError('当前 PCB 封装与适配来源不一致')
    if first:
        if set(old) != {p['source'] for p in normalized['pairs']} | set(normalized['dropped']):
            raise ValueError('原 PCB 端子集合已改变，请重新预览')
    else:
        if set(old) != expected:
            raise ValueError('适配实例端子已改变，请重新预览')
        # Repeated updates must preserve extra pads if the user wired them later.
        final_mapping['pairs'].extend({'source':n,'target':n,'align':False} for n in normalized['extras'])
        final_mapping['dropped'] = []
    effective = validate_mapping(final_mapping, list(old), numbers)
    return {'ok':True,'position_mm':pose['position_mm'], 'rotation_deg':pose['rotation_deg'],
            'back':source['back'], 'mapping':effective, 'method':'manual', 'max_error_mm':0.0,
            'center_error_mm':0.0, 'pad_errors':[], 'differences':[], 'dropped':normalized['dropped'],
            'extras':normalized['extras'], 'raw_target':raw}


def solve_placement(source: dict, target: dict, tolerance_mm: float = .001, *, alignment='rigid', mapping=None) -> dict:
    try:
        if alignment not in ('rigid', 'center'):
            raise ValueError('未知的封装对齐方式')
        if source['back'] != target['back']:
            raise ValueError('目标封装必须先由 KiCad 转换到相同板面，禁止猜测镜像')
        if not math.isfinite(tolerance_mm) or not 0 < tolerance_mm <= .001:
            raise ValueError('无效的原位匹配容差')

        def pads(part):
            result = {}
            for pad in part['pads']:
                number = str(pad['number'])
                if not number or number in result:
                    raise ValueError('焊盘编号缺失或重复，无法自动匹配')
                if len(pad['position_mm']) != 2 or not all(math.isfinite(x) for x in pad['position_mm']):
                    raise ValueError('焊盘坐标无效')
                result[number] = pad
            return result

        original, selected = pads(source), pads(target)

        mapping = (validate_mapping(mapping, list(original), list(selected))
                   if mapping is not None else None)

        def module(values):
            return '(footprint "alignment" ' + ''.join(
                '(pad %s smd rect (at %.12f %.12f))' % (json.dumps(number), *pad['position_mm'])
                for number, pad in values.items()) + ')'

        result = (estimate_mapped_alignment(module(selected), module(original), mapping, method=alignment)
                  if mapping is not None else estimate_alignment(module(selected), module(original), method=alignment))
        if not result['ok']:
            if result.get('max_error_mm') is not None:
                result['reason'] = ('焊盘间距不一致，最大偏差 %.6f mm；可改用中心对齐并检查走线'
                                    % result['max_error_mm'])
            return result
        if alignment == 'rigid' and result['max_error_mm'] > tolerance_mm + 1e-12:
            return {**result, 'ok': False, 'reason':
                    '焊盘间距不一致，最大偏差 %.6f mm，超过 %.3f mm；可改用中心对齐并检查走线' % (result['max_error_mm'], tolerance_mm)}
        angle = math.radians(result['rotation_deg'])
        c, s = math.cos(angle), math.sin(angle)
        x, y = target['position_mm']
        dx, dy = result['offset_mm']
        differences, errors, placed = [], [], []
        for number, pad in selected.items():
            x1, y1 = pad['position_mm']
            source_number = mapping['number_map'].get(number) if mapping else number
            before = original.get(source_number)
            position = [c*x1+s*y1+dx, -s*x1+c*y1+dy]
            placed.append(position)
            if before is not None:
                errors.append({'number': number, 'mapped_number': source_number,
                               'error_mm': math.dist(position, before['position_mm'])})
            pad = dict(pad)
            if 'rotation_deg' in pad:
                pad['rotation_deg'] = math.remainder(pad['rotation_deg'] + result['rotation_deg'], 360)
            fields = _pad_geometry_fields(before,pad) if before is not None else []
            if fields:
                differences.append({'number': number, 'fields': sorted(fields),
                                    'before': {key: before.get(key) for key in fields},
                                    'after': {key: pad.get(key) for key in fields}})
        def center(points):
            return [(min(p[i] for p in points) + max(p[i] for p in points))/2 for i in (0, 1)]
        aligned_original = [original[source_number]['position_mm'] for source_number, _ in (mapping['alignment_pairs'] if mapping else [(n,n) for n in original])]
        center_error = math.dist(center(placed), center(aligned_original)) if placed and aligned_original else 0.0
        return {**result, 'center_error_mm': center_error,
                'position_mm': [c*x+s*y+dx, -s*x+c*y+dy],
                'rotation_deg': target['rotation_deg'] + result['rotation_deg'],
                'back': source['back'], 'pad_errors': errors, 'differences': differences,
                'mapping': mapping}
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        return {'ok': False, 'reason': str(exc), 'max_error_mm': None}

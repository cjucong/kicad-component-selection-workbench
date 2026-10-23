"""Pure numbered-pad world-coordinate placement; no CAD writes or library copies."""
import json
import math

from part_selection_footprint_geometry import estimate_alignment


def solve_placement(source: dict, target: dict, tolerance_mm: float = .001, *, alignment='rigid') -> dict:
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

        def module(values):
            return '(footprint "alignment" ' + ''.join(
                '(pad %s smd rect (at %.12f %.12f))' % (json.dumps(number), *pad['position_mm'])
                for number, pad in values.items()) + ')'

        result = estimate_alignment(module(selected), module(original), method=alignment)
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
            before = original[number]
            position = [c*x1+s*y1+dx, -s*x1+c*y1+dy]
            placed.append(position)
            errors.append({'number': number, 'error_mm': math.dist(position, before['position_mm'])})
            fields = [key for key in set(pad) | set(before)
                      if key not in ('number', 'position_mm', 'uuid', 'net', 'padstack') and pad.get(key) != before.get(key)]
            if fields:
                differences.append({'number': number, 'fields': sorted(fields),
                                    'before': {key: before.get(key) for key in fields},
                                    'after': {key: pad.get(key) for key in fields}})
        def center(points):
            return [(min(p[i] for p in points) + max(p[i] for p in points))/2 for i in (0, 1)]
        center_error = math.dist(center(placed), center([p['position_mm'] for p in original.values()]))
        return {**result, 'center_error_mm': center_error,
                'position_mm': [c*x+s*y+dx, -s*x+c*y+dy],
                'rotation_deg': target['rotation_deg'] + result['rotation_deg'],
                'back': source['back'], 'pad_errors': errors, 'differences': differences}
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        return {'ok': False, 'reason': str(exc), 'max_error_mm': None}

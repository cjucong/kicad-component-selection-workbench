"""Numbered-pad rigid alignment and isolated native KiCad footprint previews.

Positive angles follow KiCad: (x, y) -> (cos(a)*x+sin(a)*y,
-sin(a)*x+cos(a)*y). Offsets are applied after rotation, in millimeters.
Two-point/collinear data cannot distinguish a reflection that is itself
identical to a proper rotation on those points; outlines never break ties.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from kicad_sexpr import parse_sexpr
from part_selection_pad_mapping import adapt_numbers, validate_mapping


_TOLERANCE = 0.025
# Group equivalence only: absorb historical 10 nm coordinates plus native
# integer transform rounding, without accepting micrometer-scale layout edits.
_REFERENCE_CENTER_TOLERANCE = 0.00002
_ROOT = Path(__file__).resolve().parents[1]
_SUPPORTED = {
    'version', 'generator', 'generator_version', 'layer', 'descr', 'tags', 'attr',
    'autoplace_cost90', 'autoplace_cost180', 'solder_mask_margin', 'solder_paste_margin',
    'solder_paste_ratio', 'clearance', 'zone_connect', 'thermal_width', 'thermal_gap',
    'path', 'sheetname', 'sheetfile', 'uuid', 'tedit', 'tstamp', 'private_layers', 'net_tie_pad_groups',
    'embedded_fonts', 'duplicate_pad_numbers_are_jumpers', 'jumper_pad_groups',
    'at', 'property', 'fp_text', 'fp_text_box', 'pad', 'zone',
    'fp_line', 'fp_rect', 'fp_arc', 'fp_circle', 'fp_poly', 'fp_curve',
}


def _parse_module(source):
    source = source.lstrip('\ufeff')
    try:
        return parse_sexpr(source, 'footprint')
    except ValueError:
        return parse_sexpr(source, 'module')


def _number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Nonfinite geometry coordinate')
    return number


def _pad_positions(root):
    pads = {}
    for pad in root.children_named('pad'):
        number = pad.arg(1)
        if not number:
            continue
        if number in pads:
            raise ValueError(f'Duplicate pad number is ambiguous: {number}')
        at = pad.child('at')
        pads[number] = (_number(at.arg(1)), _number(at.arg(2))) if at else (0.0, 0.0)
    return pads


def reference_layout_difference(source, reference):
    """A shared replacement needs the same local numbered-pad centers, not text."""
    try:
        pads, baseline = _pad_positions(_parse_module(source)), _pad_positions(_parse_module(reference))
        if set(pads) != set(baseline):
            return '焊盘编号不同：' + ', '.join(sorted(set(pads) ^ set(baseline)))
        if len(pads) < 2:
            return '缺少至少两个唯一编号焊盘，无法核对方向'
        for number in sorted(pads):
            distance = math.dist(pads[number], baseline[number])
            if distance > _REFERENCE_CENTER_TOLERANCE + 1e-12:
                return ('焊盘 %s 局部中心不同：(%.9f, %.9f) / (%.9f, %.9f) mm；'
                        '偏差 %.9f mm，允许 %.9f mm（不比较丝印）' %
                        (number, *pads[number], *baseline[number], distance,
                         _REFERENCE_CENTER_TOLERANCE))
        return ''
    except (ValueError, TypeError, OverflowError) as exc:
        return '焊盘坐标不可识别：' + str(exc)


def _failure(reason):
    return {'ok': False, 'rotation_deg': 0.0, 'offset_mm': [0.0, 0.0],
            'max_error_mm': None, 'reason': str(reason)}


def estimate_alignment(converted_source: str, reference_source: str, *, method='rigid') -> dict:
    """Least-squares proper rigid transform from identical, unique pad numbers.

    No scaling, reflection, outline matching, or pad renumbering is allowed.
    Rigid mode requires a maximum numbered-pad residual <= 0.025 mm.
    Center mode aligns the bounds of numbered-pad centers after orienting
    by matching numbers, and reports residuals for explicit human review.
    Empty-number mechanical pads are not alignment correspondences.
    """
    try:
        if method not in ('rigid', 'center'):
            return _failure('Unknown alignment method')
        converted = _pad_positions(_parse_module(converted_source))
        reference = _pad_positions(_parse_module(reference_source))
        if set(converted) != set(reference):
            return _failure('Pad number sets do not match exactly')
        if len(converted) < 2:
            return _failure('At least two numbered correspondence points are required')
        keys = sorted(converted)
        points, targets = [converted[n] for n in keys], [reference[n] for n in keys]
        count = len(keys)
        cx, cy = (math.fsum(p[axis] for p in points) / count for axis in (0, 1))
        rx, ry = (math.fsum(p[axis] for p in targets) / count for axis in (0, 1))
        p = [(x-cx, y-cy) for x,y in points]
        q = [(x-rx, y-ry) for x,y in targets]
        if (max(math.hypot(x,y) for x,y in p) < 1e-9
                or max(math.hypot(x,y) for x,y in q) < 1e-9):
            return _failure('Coincident correspondence points do not determine direction')
        dot = math.fsum(x*u+y*v for (x,y),(u,v) in zip(p,q))
        cross = math.fsum(y*u-x*v for (x,y),(u,v) in zip(p,q))
        if math.hypot(dot, cross) < 1e-12:
            return _failure('Pad correspondences do not determine a rotation')
        angle = math.atan2(cross, dot)
        c, s = math.cos(angle), math.sin(angle)
        dx, dy = rx-c*cx-s*cy, ry+s*cx-c*cy
        rotated = [(c*x+s*y, -s*x+c*y) for x,y in points]
        def center(values):
            return [(min(v[i] for v in values)+max(v[i] for v in values))/2 for i in (0,1)]
        target_center = center(targets)
        if method == 'center':
            converted_center = center(rotated)
            dx,dy = (target_center[i]-converted_center[i] for i in (0,1))
        error = max(math.hypot(c*x+s*y+dx-u, -s*x+c*y+dy-v)
                    for (x,y),(u,v) in zip(points,targets))
        matches = error <= _TOLERANCE + 1e-12
        accepted = matches or method == 'center'
        return {'ok': accepted, 'rotation_deg': math.degrees(angle),
                'offset_mm': [dx,dy], 'max_error_mm': error,
                'method': method, 'requires_review': not matches,
                'reference_center_mm': target_center,
                'reason': ('Numbered-pad envelope centers aligned; inspect pitch and geometry differences'
                           if method == 'center' else
                           'Numbered pads admit a rigid rotation and translation' if matches else
                           'Numbered pads do not overlap within the allowed tolerance; pitch or geometry differs')}
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        return _failure(f'Invalid footprint geometry: {exc}')


def estimate_mapped_alignment(converted_source: str, reference_source: str, mapping: dict, *, method='center') -> dict:
    """Estimate a transform from an explicitly selected subset of pad pairs.

    The target may contain extra pads and the original may contain explicitly
    dropped pads.  Only ``alignment_pairs`` determine the pose; the transform
    is applied to the complete target footprint afterwards.
    """
    try:
        if method not in ('rigid','center'):
            return _failure('Unknown alignment method')
        converted = _pad_positions(_parse_module(converted_source))
        reference = _pad_positions(_parse_module(reference_source))
        pairs = mapping.get('alignment_pairs')
        if not pairs:
            return _failure('至少选择一个参与对齐的焊盘')
        points = [converted[target] for source, target in pairs]
        targets = [reference[source] for source, target in pairs]
        count = len(points)
        cx, cy = (math.fsum(p[axis] for p in points) / count for axis in (0, 1))
        rx, ry = (math.fsum(p[axis] for p in targets) / count for axis in (0, 1))
        p = [(x-cx, y-cy) for x,y in points]
        q = [(x-rx, y-ry) for x,y in targets]
        if count > 1:
            dot = math.fsum(x*u+y*v for (x,y),(u,v) in zip(p,q))
            cross = math.fsum(y*u-x*v for (x,y),(u,v) in zip(p,q))
            if math.hypot(dot, cross) < 1e-12:
                return _failure('对应焊盘无法确定方向')
            angle = math.atan2(cross, dot)
        else:
            angle = 0.0
        c, s = math.cos(angle), math.sin(angle)
        rotated = [(c*x+s*y, -s*x+c*y) for x,y in points]
        if method == 'center':
            def center(values):
                return [(min(v[i] for v in values)+max(v[i] for v in values))/2 for i in (0,1)]
            a, b = center(rotated), center(targets)
            dx, dy = b[0]-a[0], b[1]-a[1]
        else:
            dx, dy = rx-c*cx-s*cy, ry+s*cx-c*cy
        error = max(math.hypot(c*x+s*y+dx-u, -s*x+c*y+dy-v)
                    for (x,y),(u,v) in zip(points,targets))
        matches = error <= _TOLERANCE + 1e-12
        return {'ok': matches or method == 'center', 'rotation_deg': math.degrees(angle),
                'offset_mm': [dx,dy], 'max_error_mm': error, 'method': method,
                'requires_review': not matches,
                'mapping': {'alignment_pairs': [list(pair) for pair in pairs]},
                'reason': '手动焊盘对应已配准，请检查未参与对齐的焊盘' if method == 'center' else
                          '手动焊盘对应满足刚体对齐' if matches else
                          '手动焊盘对应的间距超过刚体容差'}
    except (ValueError, TypeError, OverflowError, KeyError) as exc:
        return _failure(f'Invalid mapped footprint geometry: {exc}')


def validate_alignment_transform(transform):
    """Copy a bounded, absolute raw-converted-to-reference KiCad transform."""
    if not isinstance(transform, dict) or set(transform) != {'rotation_deg', 'offset_mm'}:
        raise ValueError('Invalid alignment_transform keys')
    offset = transform['offset_mm']
    if not isinstance(offset, list) or len(offset) != 2:
        raise ValueError('alignment_transform.offset_mm must contain two numbers')
    values = [transform['rotation_deg'], *offset]
    if any(type(value) not in (int, float) for value in values):
        raise ValueError('alignment_transform requires finite numbers, not booleans or strings')
    try:
        angle, dx, dy = map(float, values)
    except (ValueError, OverflowError) as exc:
        raise ValueError('Invalid alignment_transform numbers') from exc
    if not all(map(math.isfinite, (angle, dx, dy))):
        raise ValueError('alignment_transform requires finite numbers')
    if abs(angle) > 3600 or max(abs(dx), abs(dy)) > 1000:
        raise ValueError('alignment_transform exceeds 3600 degrees or 1000 mm')
    return {'rotation_deg': angle, 'offset_mm': [dx, dy]}


def manual_alignment(converted_source, reference_source, transform):
    """Evaluate an explicit rigid transform without relaxing pad correspondence."""
    transform = validate_alignment_transform(transform)
    result = estimate_alignment(converted_source, reference_source, method='center')
    if not result['ok']:
        return result
    converted = _pad_positions(_parse_module(converted_source))
    reference = _pad_positions(_parse_module(reference_source))
    angle = math.radians(transform['rotation_deg'])
    c, s = math.cos(angle), math.sin(angle)
    dx, dy = transform['offset_mm']
    error = max(math.hypot(c*x+s*y+dx-reference[number][0],
                           -s*x+c*y+dy-reference[number][1])
                for number, (x, y) in converted.items())
    return {**result, **transform, 'method': 'manual', 'max_error_mm': error,
            'requires_review': error > _TOLERANCE + 1e-12,
            'reason': 'Explicit rigid transform; inspect numbered-pad residuals and geometry'}


def _validate_supported(source, *, allow_models=False, render_only=False):
    root = _parse_module(source)
    for node in root.children[2:]:
        # Rendering delegates format support to KiCad; the transform whitelist
        # must not hide native objects that we cannot safely rewrite ourselves.
        if not render_only and node.head not in _SUPPORTED and not (allow_models and node.head == 'model'):
            raise ValueError(f'Unsupported footprint object: {node.head or node.value}')
    at = root.child('at')
    if at and any(abs(_number(at.arg(i) or '0')) > 1e-9 for i in (1,2,3)):
        raise ValueError('Unsupported board-instance origin/orientation; expected a library footprint')
    return root


def _find_kicad_python():
    candidates = []
    cli = shutil.which('kicad-cli')
    if cli:
        candidates.append(Path(cli).resolve().with_name('python.exe' if os.name == 'nt' else 'python3'))
    program_files = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'KiCad'
    if program_files.is_dir():
        def version(path):
            return tuple(int(p) if p.isdigit() else 0 for p in path.parent.parent.name.split('.'))
        candidates.extend(sorted(program_files.glob('*/bin/python.exe'), key=version, reverse=True))
    return next((path for path in candidates if path.is_file()), None)


def _run_worker(payload):
    python = _find_kicad_python()
    if python is None:
        raise RuntimeError('KiCad bundled Python with pcbnew is unavailable')
    environment = os.environ.copy()
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    completed = subprocess.run(
        [str(python), '-B', str(Path(__file__).with_name('footprint_geometry_worker.py'))],
        input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8',
        timeout=60, env=environment,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    if completed.returncode:
        raise RuntimeError(f'KiCad geometry worker exited {completed.returncode}: '
                           + (completed.stderr or completed.stdout)[-2000:])
    try:
        result = json.loads(completed.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError('KiCad geometry worker returned invalid JSON') from exc
    if not isinstance(result, dict) or not result.get('ok'):
        raise RuntimeError(result.get('reason', 'KiCad geometry worker failed') if isinstance(result, dict)
                           else 'KiCad geometry worker returned an invalid result')
    return result


def _outside_project(path):
    resolved = path.resolve()
    if resolved == _ROOT or _ROOT in resolved.parents:
        raise ValueError('Geometry work/output must be outside the source repository')
    for parent in (resolved, *resolved.parents):
        if parent.is_dir() and next(parent.glob('*.kicad_pro'), None) is not None:
            raise ValueError('Geometry work/output must be outside a KiCad project')


def align_module(converted: Path, reference: Path, output: Path, *, method='rigid', transform=None, mapping=None) -> dict:
    """Write a staging library module using pcbnew, or copy identity bytes.

    Inputs are never overwritten. Nonidentity inputs are conservatively
    checked for unsupported objects before invoking the native worker.
    An explicit transform replaces the estimated pose, never composes with it;
    numbered-pad eligibility remains strict, with residuals flagged for review.
    """
    result = _failure('Alignment has not run')
    try:
        converted, reference, output = map(Path, (converted,reference,output))
        if output.resolve() in (converted.resolve(), reference.resolve()):
            return _failure('Alignment output must not overwrite either input footprint')
        _outside_project(output)
        raw = converted.read_bytes()
        source = raw.decode('utf-8-sig')
        original_source = source
        target = reference.read_text(encoding='utf-8-sig')
        if mapping is not None:
            mapping = validate_mapping(mapping, list(_pad_positions(_parse_module(target))),
                                       list(_pad_positions(_parse_module(source))))
            source = adapt_numbers(source, mapping)
            raw = source.encode('utf-8')
        _validate_supported(source, allow_models=True)
        _validate_supported(target, allow_models=True)
        if mapping is not None:
            result = estimate_mapped_alignment(original_source, target, mapping, method=method)
            if transform is not None:
                transform = validate_alignment_transform(transform)
                angle = math.radians(transform['rotation_deg'])
                c, s = math.cos(angle), math.sin(angle)
                errors = []
                converted_positions = _pad_positions(_parse_module(original_source))
                reference_positions = _pad_positions(_parse_module(target))
                for source_number, target_number in mapping['alignment_pairs']:
                    x, y = converted_positions[target_number]
                    u, v = reference_positions[source_number]
                    errors.append(math.hypot(c*x+s*y+transform['offset_mm'][0]-u,
                                             -s*x+c*y+transform['offset_mm'][1]-v))
                result = {**result, **transform, 'method': 'manual',
                          'max_error_mm': max(errors), 'requires_review': max(errors) > _TOLERANCE}
        else:
            result = (estimate_alignment(source, target, method=method) if transform is None
                      else manual_alignment(source, target, transform))
        if not result['ok']:
            return result
        identity = abs(result['rotation_deg']) < 1e-9 and max(map(abs,result['offset_mm'])) < 1e-9
        if identity:
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_bytes(raw)
        else:
            _validate_supported(source)
            mapped = output.parent / ('mapped-' + uuid4().hex + '.kicad_mod')
            mapped.write_bytes(raw)
            _run_worker({'operation': 'align', 'converted': str(mapped.resolve()),
                         'output': str(output.resolve()), 'rotation_deg': result['rotation_deg'],
                         'offset_mm': result['offset_mm']})
        return result
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return {**result, 'ok': False, 'reason': str(exc)}


def board_references(board: Path, refs, work: Path) -> list:
    """Read saved PCB instances into isolated front-side library coordinates."""
    _outside_project(Path(work))
    return _run_worker({'operation':'board_references','board':str(Path(board).resolve()),
                        'refs':list(refs),'work':str(Path(work).resolve())})['records']


def render_comparison(reference: Path, converted_aligned: Path, work: Path) -> dict:
    """Return native per-layer SVG strings in one transparent millimeter frame.

    Reference/value are forced visible only in disposable preview copies and
    isolated from physical layers. Their default_visible flags are False.
    SVG strings are image resources, not markup to insert into a live DOM.
    """
    try:
        reference, converted_aligned, work = map(Path,(reference,converted_aligned,work))
        _outside_project(work)
        for source in (reference,converted_aligned):
            _validate_supported(source.read_text(encoding='utf-8-sig'), allow_models=True, render_only=True)
        return _run_worker({'operation': 'render', 'reference': str(reference.resolve()),
                            'converted': str(converted_aligned.resolve()), 'work': str(work.resolve())})
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return {'ok': False, 'reason': str(exc)}

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

from kicad_sexpr import parse_sexpr


_TOLERANCE = 0.025
_ROOT = Path(__file__).resolve().parents[1]
_SUPPORTED = {
    'version', 'generator', 'generator_version', 'layer', 'descr', 'tags', 'attr',
    'autoplace_cost90', 'autoplace_cost180', 'solder_mask_margin', 'solder_paste_margin',
    'solder_paste_ratio', 'clearance', 'zone_connect', 'thermal_width', 'thermal_gap',
    'path', 'uuid', 'tedit', 'tstamp', 'private_layers', 'net_tie_pad_groups',
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


def _validate_supported(source, *, allow_models=False):
    root = _parse_module(source)
    for node in root.children[2:]:
        if node.head not in _SUPPORTED and not (allow_models and node.head == 'model'):
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


def align_module(converted: Path, reference: Path, output: Path, *, method='rigid') -> dict:
    """Write a staging library module using pcbnew, or copy identity bytes.

    Inputs are never overwritten. Nonidentity inputs are conservatively
    checked for unsupported objects before invoking the native worker.
    """
    result = _failure('Alignment has not run')
    try:
        converted, reference, output = map(Path, (converted,reference,output))
        if output.resolve() in (converted.resolve(), reference.resolve()):
            return _failure('Alignment output must not overwrite either input footprint')
        _outside_project(output)
        raw = converted.read_bytes()
        source = raw.decode('utf-8-sig')
        target = reference.read_text(encoding='utf-8-sig')
        _validate_supported(source, allow_models=True)
        _validate_supported(target, allow_models=True)
        result = estimate_alignment(source,target,method=method)
        if not result['ok']:
            return result
        identity = abs(result['rotation_deg']) < 1e-9 and max(map(abs,result['offset_mm'])) < 1e-9
        if identity:
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_bytes(raw)
        else:
            _validate_supported(source)
            _run_worker({'operation': 'align', 'converted': str(converted.resolve()),
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
            _validate_supported(source.read_text(encoding='utf-8-sig'), allow_models=True)
        return _run_worker({'operation': 'render', 'reference': str(reference.resolve()),
                            'converted': str(converted_aligned.resolve()), 'work': str(work.resolve())})
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return {'ok': False, 'reason': str(exc)}

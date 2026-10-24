"""Alignment and native KiCad geometry/plotting, using disposable fixtures only."""

import importlib
import importlib.util
import json
import math
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from _scratch import temporary_project

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
MODULE = 'part_selection_footprint_geometry'
from part_selection_footprint_geometry import _find_kicad_python

KICAD = _find_kicad_python()
POINTS = [('1', -2, -1), ('2', 2, -1), ('3', 1, 2)]


def footprint(points=POINTS, extra='', name='Synthetic'):
    return f'''(footprint "{name}" (version 20241229) (generator "pcbnew")
      (layer "F.Cu")
      (property "Reference" "REF**" (at -1 -4 0) (layer "F.SilkS")
        (effects (font (size 1 1) (thickness 0.15))))
      (property "Value" "Fixture" (at 0 4 0) (layer "F.Fab")
        (effects (font (size 1 1) (thickness 0.15))))
      {''.join(f'(pad "{n}" smd rect (at {x} {y}) (size 0.8 0.5) (layers "F.Cu" "F.Paste" "F.Mask"))' for n, x, y in points)}
      {extra})'''


@pytest.fixture
def geometry():
    return importlib.import_module(MODULE)


@pytest.fixture
def workspace():
    with temporary_project(prefix='geometry-') as directory:
        yield Path(directory)


def test_module_exists_before_implementation():
    assert importlib.util.find_spec(MODULE) is not None, 'geometry module is not implemented yet'


def test_identity_estimate(geometry):
    result = geometry.estimate_alignment(footprint(), footprint())
    assert result['ok'] and result['reason']
    assert result['rotation_deg'] == pytest.approx(0)
    assert result['offset_mm'] == pytest.approx([0, 0])
    assert result['max_error_mm'] == pytest.approx(0)


@pytest.mark.parametrize('transform', [
    None, [], {}, {'rotation_deg': 0}, {'rotation_deg': 0, 'offset_mm': [0, 0], 'scale': 1},
    {'rotation_deg': True, 'offset_mm': [0, 0]},
    {'rotation_deg': '90', 'offset_mm': [0, 0]},
    {'rotation_deg': 0, 'offset_mm': [False, 0]},
    {'rotation_deg': 0, 'offset_mm': '00'},
    {'rotation_deg': 0, 'offset_mm': [0]},
    {'rotation_deg': 0, 'offset_mm': [0, 0, 0]},
    {'rotation_deg': 0, 'offset_mm': (0, 0)},
    {'rotation_deg': float('nan'), 'offset_mm': [0, 0]},
    {'rotation_deg': 0, 'offset_mm': [0, float('inf')]},
    {'rotation_deg': 3601, 'offset_mm': [0, 0]},
    {'rotation_deg': 0, 'offset_mm': [-1000.001, 0]},
    {'rotation_deg': 10**400, 'offset_mm': [0, 0]},
])
def test_manual_transform_rejects_invalid_or_unbounded_values(geometry, transform):
    with pytest.raises(ValueError, match='alignment_transform'):
        geometry.validate_alignment_transform(transform)


def test_manual_transform_is_absolute_kicad_rotation_not_an_auto_delta(geometry):
    angle = math.radians(37.5)
    c, s = math.cos(angle), math.sin(angle)
    targets = [(n, c*x+s*y+4, -s*x+c*y-3) for n, x, y in POINTS]
    transform = {'rotation_deg': 37.5, 'offset_mm': [4, -3]}
    result = geometry.manual_alignment(footprint(), footprint(targets), transform)
    assert result['ok'] and result['method'] == 'manual' and not result['requires_review']
    assert result['rotation_deg'] == 37.5 and result['offset_mm'] == [4, -3]
    assert result['max_error_mm'] < 1e-12
    result['offset_mm'][0] = 9
    assert transform['offset_mm'] == [4, -3], 'Never retain the caller-owned offset list'


def test_manual_residual_is_evaluated_without_refitting_or_rejecting_review(geometry):
    result = geometry.manual_alignment(footprint(), footprint(),
        {'rotation_deg': 0, 'offset_mm': [.1, -.2]})
    assert result['ok'] and result['requires_review']
    assert result['max_error_mm'] == pytest.approx(math.hypot(.1, .2))
    assert result['offset_mm'] == [.1, -.2]
    assert geometry.estimate_alignment(footprint(), footprint())['method'] == 'rigid'


@pytest.mark.parametrize('points', [
    [('1', 0, 0)], [('1', 0, 0), ('1', 1, 0)],
    [('1', 0, 0), ('2', 0, 0), ('3', 0, 0)],
    [('A', -2, -1), ('B', 2, -1), ('C', 1, 2)],
])
def test_manual_transform_cannot_override_ambiguous_or_missing_numbered_pads(geometry, points):
    result = geometry.manual_alignment(footprint(points), footprint(),
        {'rotation_deg': 90, 'offset_mm': [4, -3]})
    assert not result['ok']


@pytest.mark.parametrize('transform', [lambda x,y: (x+1,y), lambda x,y: (-y,x), lambda x,y: (-x,y)])
def test_shared_reference_does_not_fit_away_local_transform(geometry, transform):
    other = [(n,*transform(x,y)) for n,x,y in POINTS]
    assert '局部中心不同' in geometry.reference_layout_difference(footprint(other), footprint())


def test_shared_reference_ignores_text_shape_and_size_not_numbering(geometry):
    changed = footprint(extra='(fp_text user "+" (at 5 8 90) (layer "F.SilkS"))')
    changed = changed.replace('smd rect', 'smd circle').replace('0.8 0.5', '1 1')
    assert geometry.reference_layout_difference(changed, footprint()) == ''
    assert '编号不同' in geometry.reference_layout_difference(footprint([('9',1,2)]), footprint())


@pytest.mark.parametrize('offset', [
    (0, .000002), (.000001, .000001), (.000003, .000003),
    (0, .00001), (.00001, .00001), (.000012, .000016),
])
def test_shared_reference_accepts_native_nanometer_rounding(geometry, offset):
    dx, dy = offset
    points = [(n, x+dx, y+dy) for n,x,y in POINTS]
    assert geometry.reference_layout_difference(footprint(points), footprint()) == ''


def test_shared_reference_accepts_single_pad_historical_decimal_difference(geometry):
    baseline = [('1', 0, 0), ('2', 0, -.65001), ('3', 0, -1.3)]
    other = [('1', 0, 0), ('2', 0, -.65), ('3', 0, -1.3)]
    assert geometry.reference_layout_difference(footprint(other), footprint(baseline)) == ''
    assert geometry.reference_layout_difference(footprint(baseline), footprint(other)) == ''


@pytest.mark.parametrize('offset', [.000021, .001, .05])
def test_shared_reference_still_rejects_real_pad_displacements(geometry, offset):
    points = POINTS[:-1] + [('3', 1+offset, 2)]
    message = geometry.reference_layout_difference(footprint(points), footprint())
    assert '局部中心不同' in message
    assert '偏差' in message and '0.000020000 mm' in message and '不比较丝印' in message


def test_shared_reference_uses_absolute_euclidean_tolerance(geometry):
    points = [('1', 1000, 1000), ('2', 1002, 1000)]
    moved = [('1', 1000.000015, 1000.000015), points[1]]
    assert '局部中心不同' in geometry.reference_layout_difference(footprint(moved), footprint(points))


@pytest.mark.parametrize('points', [
    [('1', 0, 0)], [('1', 0, 0), ('1', 1, 0)],
    [('1', float('nan'), 0), ('2', 1, 0)],
])
def test_shared_reference_keeps_ambiguous_geometry_protection(geometry, points):
    assert geometry.reference_layout_difference(footprint(points), footprint(points))


@pytest.mark.parametrize('angle', [90, -90, 180, 37.5])
def test_numbered_points_determine_kicad_rotation_and_translation(geometry, angle):
    radians = math.radians(angle)
    c, s = math.cos(radians), math.sin(radians)
    reference = [(n, c*x+s*y+4, -s*x+c*y-3) for n, x, y in POINTS]
    result = geometry.estimate_alignment(footprint(), footprint(list(reversed(reference))))
    assert result['ok']
    assert math.remainder(result['rotation_deg'] - angle, 360) == pytest.approx(0, abs=1e-7)
    assert result['offset_mm'] == pytest.approx([4, -3])
    assert result['max_error_mm'] < 1e-7


def test_two_distinct_numbered_pads_are_sufficient(geometry):
    result = geometry.estimate_alignment(footprint(POINTS[:2]), footprint([('1', 7, 2), ('2', 7, -2)]))
    assert result['ok'] and result['rotation_deg'] == pytest.approx(90)


@pytest.mark.parametrize('points,reason', [
    ([('1', 0, 0)], 'point'),
    ([('1', 0, 0), ('2', 0, 0)], 'coincident'),
    ([('1', 0, 0), ('1', 1, 0)], 'duplicate'),
])
def test_ambiguous_pad_correspondences_are_refused(geometry, points, reason):
    result = geometry.estimate_alignment(footprint(points), footprint(points))
    assert not result['ok'] and reason in result['reason'].lower()


def test_number_mismatch_is_not_resolved_by_outline(geometry):
    result = geometry.estimate_alignment(footprint(), footprint([('A', x, y) for _, x, y in POINTS]))
    assert not result['ok'] and 'number' in result['reason'].lower()


@pytest.mark.parametrize('transform', [lambda x,y: (-x,y), lambda x,y: (x*1.2,y*1.2), lambda x,y: (x+0.2*y,y)])
def test_mirror_scale_and_different_pitch_are_refused(geometry, transform):
    result = geometry.estimate_alignment(footprint(), footprint([(n,*transform(x,y)) for n,x,y in POINTS]))
    assert not result['ok'] and result['max_error_mm'] > 0.025


def test_small_rounding_residual_is_allowed(geometry):
    points = POINTS[:-1] + [('3', 1.01, 2.01)]
    assert geometry.estimate_alignment(footprint(), footprint(points))['ok']


def test_center_alignment_accepts_pitch_and_shape_differences(geometry):
    original = [('1', 0, 0), ('2', .635, 0), ('3', .635, -5.334), ('4', 0, -5.334)]
    converted = [('1', -1.99, 2.69), ('2', -1.35, 2.69),
                 ('3', -1.35, -2.75), ('4', -1.99, -2.75)]
    source = footprint(converted).replace('smd rect', 'smd circle').replace('0.8 0.5', '0.8 0.8')
    result = geometry.estimate_alignment(source, footprint(original), method='center')
    assert result['ok'] and result['method'] == 'center'
    assert result['requires_review'] and result['max_error_mm'] > .025
    assert result['rotation_deg'] == pytest.approx(0)
    assert result['offset_mm'] == pytest.approx([1.9875, -2.637])
    assert result['reference_center_mm'] == pytest.approx([.3175, -2.667])


def test_center_uses_envelope_not_pad_count_weighted_centroid(geometry):
    source = footprint([('1', 0, 0), ('2', 1, 0), ('3', 10, 4)])
    target = footprint([('1', 5, -2), ('2', 7, -2), ('3', 15, 2)])
    result = geometry.estimate_alignment(source, target, method='center')
    assert result['ok']
    angle = math.radians(result['rotation_deg'])
    rotated = [(math.cos(angle)*x+math.sin(angle)*y,
                -math.sin(angle)*x+math.cos(angle)*y) for _, x, y in [('1', 0, 0), ('2', 1, 0), ('3', 10, 4)]]
    center = [(min(p[i] for p in rotated)+max(p[i] for p in rotated))/2 for i in (0, 1)]
    assert [center[i]+result['offset_mm'][i] for i in (0, 1)] == pytest.approx([10, 0])


def test_center_alignment_still_refuses_ambiguous_numbering(geometry):
    assert not geometry.estimate_alignment(footprint(), footprint([('A', 0, 0)]), method='center')['ok']


@pytest.mark.parametrize('bad', ['not a module', '(footprint "x"', footprint([('1', 'nan', 0), ('2', 1, 0)])])
def test_invalid_or_nonfinite_source_is_refused(geometry, bad):
    result = geometry.estimate_alignment(bad, footprint())
    assert not result['ok'] and result['reason']


def test_unnumbered_mechanical_pads_do_not_establish_direction(geometry):
    pads = POINTS + [('', 12, 12), ('', 13, 12)]
    assert geometry.estimate_alignment(footprint(pads), footprint(pads))['ok']


def test_identity_copies_original_bytes_without_worker(geometry, workspace, monkeypatch):
    converted, reference, output = [workspace / n for n in ['in.kicad_mod','ref.kicad_mod','out.kicad_mod']]
    raw = ('\ufeff' + footprint() + '\r\n').encode('utf-8')
    converted.write_bytes(raw)
    reference.write_text(footprint(), encoding='utf-8')
    monkeypatch.setattr(geometry, '_run_worker', lambda *a, **kw: pytest.fail('identity must not invoke KiCad'))
    result = geometry.align_module(converted, reference, output)
    assert result['ok'] and output.read_bytes() == raw


def test_failed_alignment_does_not_create_output(geometry, workspace):
    converted, reference, output = [workspace / n for n in ['in.kicad_mod','ref.kicad_mod','out.kicad_mod']]
    converted.write_text(footprint(), encoding='utf-8')
    reference.write_text(footprint([('1',0,0)]), encoding='utf-8')
    assert not geometry.align_module(converted, reference, output)['ok']
    assert not output.exists()


def test_center_alignment_writes_reviewable_geometry(geometry, workspace, monkeypatch):
    converted, reference, output = [workspace / n for n in ['in.kicad_mod', 'ref.kicad_mod', 'preview.kicad_mod']]
    converted.write_text(footprint(), encoding='utf-8')
    reference.write_text(footprint([('1', 0, 0), ('2', 2.06, 0), ('3', 1, 2)]), encoding='utf-8')

    def worker(payload):
        Path(payload['output']).write_bytes(converted.read_bytes())

    monkeypatch.setattr(geometry, '_run_worker', worker)
    result = geometry.align_module(converted, reference, output, method='center')
    assert result['ok'] and result['requires_review'] and output.exists()


def test_alignment_never_overwrites_input(geometry, workspace):
    path = workspace / 'in.kicad_mod'
    path.write_text(footprint(), encoding='utf-8')
    before = path.read_bytes()
    assert not geometry.align_module(path, path, path)['ok']
    assert path.read_bytes() == before


@pytest.mark.parametrize('invalid_input', ['converted', 'reference'])
def test_even_identity_alignment_rejects_board_instance_origin(geometry, workspace, invalid_input):
    paths = {key: workspace / (key + '.kicad_mod') for key in ('converted', 'reference', 'output')}
    for key in ('converted', 'reference'):
        source = footprint(extra='(at 4 -3 90)' if key == invalid_input else '')
        paths[key].write_text(source, encoding='utf-8')
    result = geometry.align_module(paths['converted'], paths['reference'], paths['output'])
    assert not result['ok'] and 'origin/orientation' in result['reason']
    assert not paths['output'].exists()


def test_native_unavailable_is_an_explicit_failure(geometry, workspace, monkeypatch):
    converted, reference, output = [workspace / n for n in ['in.kicad_mod','ref.kicad_mod','out.kicad_mod']]
    converted.write_text(footprint(), encoding='utf-8')
    reference.write_text(footprint([(n,x+2,y) for n,x,y in POINTS]), encoding='utf-8')
    monkeypatch.setattr(geometry, '_find_kicad_python', lambda: None)
    result = geometry.align_module(converted, reference, output)
    assert not result['ok'] and 'KiCad' in result['reason'] and not output.exists()


def complex_footprint():
    return footprint(extra='''
      (pad "4" smd custom (at 0 -2 23) (size 0.5 0.5) (layers "F.Cu" "F.Mask")
        (options (clearance outline) (anchor rect))
        (primitives (gr_poly (pts (xy -0.4 -0.2) (xy 0.6 -0.2) (xy 0.3 0.7)) (width 0) (fill yes))))
      (fp_line (start -3 -2) (end 3 -2) (stroke (width 0.18) (type default)) (layer "F.SilkS"))
      (fp_line (start -3 2) (end 3 2) (stroke (width 0.18) (type default)) (layer "B.SilkS"))
      (fp_arc (start 3 0) (mid 2 1) (end 1 0) (stroke (width 0.12) (type default)) (layer "F.Fab"))
      (fp_circle (center -2 1) (end -1.5 1) (stroke (width 0.12) (type default)) (fill none) (layer "F.Fab"))
      (fp_rect (start -4 -3) (end 4 3) (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd"))
      (fp_poly (pts (xy 0 0) (xy 1 0) (xy 0.5 0.7)) (stroke (width 0.1) (type default)) (fill solid) (layer "F.SilkS"))
      (fp_text user "User" (at 1 -3 15) (layer "F.Fab") (effects (font (size 0.6 0.6) (thickness 0.1))))
      (zone (net 0) (net_name "") (layer "F.Cu") (hatch edge 0.5)
        (connect_pads (clearance 0.2)) (min_thickness 0.2)
        (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
        (polygon (pts (xy 5 0) (xy 6 0) (xy 6 1) (xy 5 1))))
    ''')


def native(code, *arguments):
    result = subprocess.run([str(KICAD), '-B', '-c', code, *map(str, arguments)],
                            capture_output=True, text=True, encoding='utf-8', timeout=45,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(KICAD is None, reason='Real bundled KiCad Python is unavailable')
def test_native_center_alignment_keeps_placement_and_center_with_different_pad_shapes(geometry, workspace):
    converted, reference, output = [workspace / n for n in ['new.kicad_mod','old.kicad_mod','aligned.kicad_mod']]
    old = [('1', 0, 0), ('2', .635, 0), ('3', .635, -5.334), ('4', 0, -5.334)]
    new = [('1', -1.99, 2.69), ('2', -1.35, 2.69), ('3', -1.35, -2.75), ('4', -1.99, -2.75)]
    reference.write_text(footprint(old), encoding='utf-8')
    converted.write_text(footprint(new).replace('smd rect', 'smd circle')
                         .replace('0.8 0.5', '0.8 0.8'), encoding='utf-8')
    before = reference.read_bytes(), converted.read_bytes()
    result = geometry.align_module(converted, reference, output, method='center')
    assert result['ok'] and result['requires_review'], result
    check = native('''import pcbnew,json,sys
def load(path):
    return pcbnew.PCB_IO_KICAD_SEXPR().Parse(open(path,encoding='utf-8').read()).Cast()
def xy(v): return [pcbnew.ToMM(v.x),pcbnew.ToMM(v.y)]
checks=[]
for angle in [0,90,37,-90]:
    board=pcbnew.BOARD()
    a,b=load(sys.argv[1]),load(sys.argv[2])
    for f in (a,b):
        board.Add(f)
        f.SetOrientationDegrees(angle)
        f.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(90.5),pcbnew.FromMM(96.735)))
    def center(f):
        points=[xy(p.GetPosition()) for p in f.Pads()]
        return [(min(p[i] for p in points)+max(p[i] for p in points))/2 for i in (0,1)]
    checks.append({'positions':[xy(f.GetPosition()) for f in (a,b)],
        'angles':[f.GetOrientationDegrees() for f in (a,b)],
        'centers':[center(f) for f in (a,b)],
        'shapes':[int(next(iter(f.Pads())).GetShape()) for f in (a,b)]})
print(json.dumps(checks))''', reference, output)
    for row in check:
        assert row['positions'][0] == row['positions'][1] == pytest.approx([90.5,96.735])
        assert row['angles'][0] == row['angles'][1]
        assert row['centers'][0] == pytest.approx(row['centers'][1], abs=2e-6)
        assert row['shapes'][0] != row['shapes'][1]
    assert (reference.read_bytes(), converted.read_bytes()) == before


@pytest.mark.skipif(KICAD is None, reason='Real bundled KiCad Python is unavailable')
@pytest.mark.parametrize('transform', [None, {'rotation_deg': 90, 'offset_mm': [7, -5]}])
def test_real_native_transform_covers_complex_geometry_and_keeps_library_origin(geometry, workspace, transform):
    converted, reference, output = [workspace / n for n in ['complex.kicad_mod','ref.kicad_mod','out.kicad_mod']]
    converted.write_text(complex_footprint(), encoding='utf-8')
    points = POINTS + [('4',0,-2)]
    reference.write_text(footprint([(n,y+7,-x-5) for n,x,y in points]), encoding='utf-8')
    result = geometry.align_module(converted, reference, output, transform=transform)
    assert result['ok'], result
    assert result['rotation_deg'] == pytest.approx(90)
    assert result['offset_mm'] == pytest.approx([7,-5])
    assert geometry.estimate_alignment(output.read_text(encoding='utf-8'), reference.read_text(encoding='utf-8'))['max_error_mm'] < 1e-6
    check = native('''import pcbnew,json,sys
f=pcbnew.FootprintLoad(str(__import__('pathlib').Path(sys.argv[1]).parent),__import__('pathlib').Path(sys.argv[1]).stem)
if f is None: f=pcbnew.PCB_IO_KICAD_SEXPR().Parse(open(sys.argv[1],encoding='utf-8').read()).Cast()
def xy(v): return [pcbnew.ToMM(v.x),pcbnew.ToMM(v.y)]
shapes=[s for s in f.GraphicalItems() if s.GetClass()=='PCB_SHAPE']
line=next(s for s in shapes if s.GetShape()==pcbnew.SHAPE_T_SEGMENT and s.GetLayer()==pcbnew.F_SilkS)
pad=next(p for p in f.Pads() if p.GetNumber()=='4')
zone=next(iter(f.Zones()))
outline=zone.Outline().COutline(0)
print(json.dumps({'origin':xy(f.GetPosition()),'orientation':f.GetOrientationDegrees(),
'line_start':xy(line.GetStart()),'line_end':xy(line.GetEnd()),'pad_angle':pad.GetOrientationDegrees(),
'reference':xy(f.Reference().GetPosition()),
'value':xy(f.Value().GetPosition()),'zone_first':xy(outline.CPoint(0)),
'shapes':len(shapes),'graphics':len(list(f.GraphicalItems()))}))''', output)
    assert check['origin'] == pytest.approx([0,0]) and check['orientation'] == pytest.approx(0)
    assert check['line_start'] == pytest.approx([5,-2])
    assert check['line_end'] == pytest.approx([5,-8])
    assert check['reference'] == pytest.approx([3,-4])
    assert check['value'] == pytest.approx([11,-5])
    assert check['zone_first'] == pytest.approx([7,-10])
    assert check['pad_angle'] == pytest.approx(113)
    assert check['shapes'] == 6 and check['graphics'] == 7
    from kicad_sexpr import parse_sexpr
    saved = parse_sexpr(output.read_text(encoding='utf-8'), 'footprint')
    custom = next(p for p in saved.children_named('pad') if p.arg(1) == '4')
    primitive = custom.child('primitives').children_named('gr_poly')
    assert len(primitive) == 1
    assert [(float(p.arg(1)), float(p.arg(2))) for p in primitive[0].child('pts').children_named('xy')] == [
        (-0.4,-0.2),(0.6,-0.2),(0.3,0.7)]


@pytest.mark.parametrize('extra', ['(model "unknown.step" (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))',
                                  '(future_geometry (at 1 2))'])
@pytest.mark.parametrize('transform', [None, {'rotation_deg': 0, 'offset_mm': [2, 0]}])
def test_nonidentity_unsupported_objects_are_rejected_not_dropped(geometry, workspace, extra, transform):
    converted, reference, output = [workspace / n for n in ['in.kicad_mod','ref.kicad_mod','out.kicad_mod']]
    converted.write_text(footprint(extra=extra), encoding='utf-8')
    reference.write_text(footprint([(n,x+2,y) for n,x,y in POINTS]), encoding='utf-8')
    result = geometry.align_module(converted, reference, output, transform=transform)
    assert not result['ok'] and 'unsupported' in result['reason'].lower() and not output.exists()


@pytest.mark.skipif(KICAD is None, reason='Real bundled KiCad Python is unavailable')
def test_real_native_svg_layers_share_mm_frame_and_isolate_labels(geometry, workspace):
    reference, converted = workspace/'existing.kicad_mod', workspace/'converted.kicad_mod'
    reference.write_text(complex_footprint(), encoding='utf-8')
    converted.write_text(complex_footprint(), encoding='utf-8')
    result = geometry.render_comparison(reference, converted, workspace/'plots')
    assert result.get('ok', True), result
    assert result['origin_mm'] == [0,0]
    assert result['existing_pad_one'] == result['converted_pad_one'] == [-2,-1]
    expected_pads = [{'number': n, 'position_mm': [x, y]} for n, x, y in POINTS + [('4', 0, -2)]]
    assert result['existing_pads'] == result['converted_pads'] == expected_pads
    assert result['existing_center_mm'] == result['converted_center_mm'] == [0, 0]
    assert len(result['view_box']) == 4 and min(result['view_box'][2:]) > 0
    ids = {layer['id'] for layer in result['layers']}
    assert {'copper','F.SilkS','B.SilkS','reference','value','F.Fab','F.CrtYd'} <= ids
    assert all(isinstance(layer['default_visible'], bool) and layer['label'] for layer in result['layers'])
    for side, color in [('existing_layers','#16806a'), ('converted_layers','#c34078')]:
        assert set(result[side]) == ids
        for layer_id, svg in result[side].items():
            root = ET.fromstring(svg)
            assert list(map(float,root.attrib['viewBox'].split())) == pytest.approx(result['view_box'])
            assert root.attrib['width'].endswith('mm') and root.attrib['height'].endswith('mm')
            assert color in svg and 'PCBNEW' in svg
            assert '<script' not in svg and '<foreignObject' not in svg
            assert any(e.tag.rsplit('}',1)[-1] in ('path','circle','rect','polygon','polyline') for e in root.iter()), layer_id
        # KiCad stroke-text output retains text in comments; no reference/value geometry
        # may also be emitted in the physical layer files.
        assert 'REF**' not in result[side]['F.SilkS']
        assert 'Fixture' not in result[side]['F.Fab']
        assert 'REF**' in result[side]['reference'] and 'Fixture' not in result[side]['reference']
        assert 'Fixture' in result[side]['value'] and 'REF**' not in result[side]['value']


def test_render_rejects_work_inside_repository(geometry, workspace):
    reference = workspace/'in.kicad_mod'
    reference.write_text(footprint(), encoding='utf-8')
    result = geometry.render_comparison(reference, reference, ROOT/'forbidden-geometry-work')
    assert result['ok'] is False and not (ROOT/'forbidden-geometry-work').exists()


@pytest.mark.skipif(KICAD is None, reason='Real bundled KiCad Python is unavailable')
def test_native_render_reports_all_duplicate_numbered_pads_and_empty_center(geometry, workspace):
    reference, converted = workspace/'ref.kicad_mod', workspace/'new.kicad_mod'
    reference.write_text(footprint([('1', 0, 0), ('1', 4, 2), ('2', 1, 0), ('', 40, 40)]), encoding='utf-8')
    converted.write_text(footprint([('', 5, 5)]), encoding='utf-8')
    result = geometry.render_comparison(reference, converted, workspace/'plots')
    assert result['ok'], result
    assert result['existing_pads'] == [
        {'number': '1', 'position_mm': [0, 0]}, {'number': '1', 'position_mm': [4, 2]},
        {'number': '2', 'position_mm': [1, 0]}]
    assert result['existing_center_mm'] == [2, 1]
    assert result['existing_pad_one'] is None
    assert result['converted_pads'] == [] and result['converted_center_mm'] is None


@pytest.mark.skipif(KICAD is None, reason='Real bundled KiCad Python is unavailable')
def test_native_preview_accepts_group_without_authorizing_transform(geometry, workspace):
    source = workspace/'grouped.kicad_mod'
    source.write_text(footprint(extra='(group "preview" (uuid "7503d31f-9ef4-4b3b-b39a-2712e953f945") (members))'), encoding='utf-8')
    before = source.read_bytes()
    result = geometry.render_comparison(source,source,workspace/'plots')
    assert result.get('ok'), result
    assert result['existing_layers']['copper']
    assert source.read_bytes() == before
    with pytest.raises(ValueError,match='Unsupported footprint object: group'):
        geometry._validate_supported(source.read_text(encoding='utf-8'))


@pytest.mark.skipif(KICAD is None, reason='Real bundled KiCad Python is unavailable')
def test_preview_allows_reference_model_without_loading_or_modifying_it(geometry, workspace):
    reference, converted = workspace/'existing.kicad_mod', workspace/'converted.kicad_mod'
    reference.write_text(footprint(extra='''(model "${KIPRJMOD}/missing.step"
        (offset (xyz 1 2 3)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 90)))'''), encoding='utf-8')
    converted.write_text(footprint(), encoding='utf-8')
    before = reference.read_bytes()
    result = geometry.render_comparison(reference, converted, workspace/'plots')
    assert result.get('ok'), result
    assert result['existing_pad_one'] == [-2, -1]
    assert reference.read_bytes() == before

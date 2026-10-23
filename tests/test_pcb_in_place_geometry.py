import math
from copy import deepcopy

import pytest

from part_selection_pcb_geometry import solve_placement


def shape(back=False):
    return {'position_mm': [0, 0], 'rotation_deg': 0, 'back': back,
            'pads': [{'number': '1', 'position_mm': [-1, 0], 'size_mm': [.8, .8]},
                     {'number': '2', 'position_mm': [1, 0], 'size_mm': [.8, .8]}]}


@pytest.mark.parametrize('angle', [0, 37, 90, 180, -90])
@pytest.mark.parametrize('back', [False, True])
def test_world_pad_centers_determine_placement(angle, back):
    target = shape(back)
    source = shape(back)
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    for pad in source['pads']:
        x, y = pad['position_mm']
        pad['position_mm'] = [c*x+s*y+80, -s*x+c*y+90]
    result = solve_placement(source, target)
    assert result['ok']
    assert result['position_mm'] == pytest.approx([80, 90])
    assert result['rotation_deg'] == pytest.approx(angle)
    assert result['back'] == back
    assert result['max_error_mm'] <= .000005


def test_pad_shape_and_size_difference_is_reported_not_rejected():
    source, target = shape(), shape()
    target['pads'][0]['size_mm'] = [1, 1]
    target['pads'][0]['shape'] = 'circle'
    result = solve_placement(source, target)
    assert result['ok']
    assert result['differences'][0]['number'] == '1'


@pytest.mark.parametrize('problem', ['duplicate', 'missing', 'pitch', 'coincident', 'side', 'nonfinite'])
def test_unsafe_mapping_is_rejected(problem):
    source, target = shape(), shape()
    if problem == 'duplicate':
        target['pads'].append(target['pads'][0])
    elif problem == 'missing':
        target['pads'].pop()
    elif problem == 'pitch':
        target['pads'][0]['position_mm'][0] -= .01
    elif problem == 'coincident':
        for pad in target['pads']:
            pad['position_mm'] = [0, 0]
    elif problem == 'side':
        target['back'] = True
    else:
        source['pads'][0]['position_mm'][0] = float('nan')
    result = solve_placement(source, target)
    assert not result['ok']
    assert result['reason']


@pytest.mark.parametrize('back', [False, True])
def test_center_alignment_keeps_standard_pitch_and_reports_pad_movement(back):
    source, target = shape(back), shape(back)
    source['pads'][0]['position_mm'] = [89.0524, 88.607645]
    source['pads'][1]['position_mm'] = [92.6592, 88.607645]
    target['pads'][0]['position_mm'] = [0, -1.524]
    target['pads'][1]['position_mm'] = [0, 1.524]
    before = deepcopy((source, target))
    assert not solve_placement(source, target)['ok']
    result = solve_placement(source, target, alignment='center')
    assert result['ok']
    assert result['rotation_deg'] == pytest.approx(90)
    assert result['position_mm'] == pytest.approx([90.8558, 88.607645])
    assert result['center_error_mm'] <= 1e-9
    assert result['max_error_mm'] == pytest.approx(.2794)
    assert [pad['error_mm'] for pad in result['pad_errors']] == pytest.approx([.2794, .2794])
    assert (source, target) == before


def test_center_alignment_does_not_allow_unknown_pin_mapping():
    source, target = shape(), shape()
    target['pads'][1]['number'] = '3'
    assert not solve_placement(source, target, alignment='center')['ok']
    assert not solve_placement(source, target, alignment='unknown')['ok']

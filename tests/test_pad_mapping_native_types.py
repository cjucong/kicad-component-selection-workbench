"""Native protobuf construction with synthetic pads; no editor or CAD writes."""
import subprocess

import pytest

from test_pcb_native_types import runtime


SCRIPT = r'''
from uuid import uuid4
from kipy.board_types import FootprintInstance, Pad
from kipy.geometry import Vector2
from part_selection_pcb_session import describe, replacement, persisted_footprint_matches, encode, decode
from part_selection_pad_mapping import validate_mapping

def fixture(numbers, name):
    f=FootprintInstance();f.proto.id.value=str(uuid4());f.definition.id.library='Local';f.definition.id.name=name
    f.reference_field.text.value='U1'
    for n in numbers:
        p=Pad();p.number=n;p.proto.id.value=str(uuid4());p.proto.net.name='net_'+n
        p.position=Vector2.from_xy(int(n)*1000000,0);f.definition.add_item(p)
    return f

old=fixture([str(n) for n in range(1,COUNT+1)],'Old')
target=fixture([str(n) for n in range(1,TARGET+1)],'New')
plan=validate_mapping({'schema':1,'pairs':[{'source':str(n),'target':str(n),'align':True} for n in range(1,min(COUNT,TARGET)+1)],
                      'dropped':[str(n) for n in range(TARGET+1,COUNT+1)]},
                     [p.number for p in old.definition.pads],[p.number for p in target.definition.pads])
updated=replacement(describe(old),describe(target),{'rotation_deg':37,'position_mm':[80,90],'mapping':plan})
assert updated.id==old.id
assert {p.number for p in updated.definition.pads}=={str(n) for n in range(1,TARGET+1)}
before={p.number:p for p in old.definition.pads}
for pad in updated.definition.pads:
    if pad.number in before:
        assert pad.id==before[pad.number].id
        assert pad.proto.net==before[pad.number].proto.net
    else:
        assert not pad.proto.net.name
        assert pad.id.value not in {p.id.value for p in old.definition.pads}|{p.id.value for p in target.definition.pads}
assert updated.position.x==80000000 and updated.position.y==90000000
assert abs(updated.orientation.degrees-37)<1e-9
assert persisted_footprint_matches(updated,decode(encode(updated)))
if TARGET>COUNT:
    from part_selection_pcb_geometry import adapted_placement
    for pad in updated.definition.pads:
        if pad.number not in before:
            pad.proto.net.name='later_user_connection_'+pad.number
    evidence={'before':'Local:Old','after':'Local:New',
        'source':{'placement':{'position_mm':[80,90],'rotation_deg':37,'back':False}},
        'adaptation':{'schema':1,'pose':'reference-local','mapping':plan,'raw_target':{'id':'Local:New'}}}
    placement=adapted_placement(describe(updated),describe(target),evidence)
    repeated=replacement(describe(updated),describe(target),placement)
    wired={p.number:p for p in updated.definition.pads}
    for pad in repeated.definition.pads:
        assert pad.id==wired[pad.number].id and pad.proto.net==wired[pad.number].proto.net
print('verified')
'''


@pytest.mark.parametrize('count,target', [(8,10),(10,8)])
def test_native_added_and_dropped_pads_preserve_nets_identity_and_pose(count, target):
    python=runtime()
    if python is None:
        pytest.skip('KiCad desktop kipy runtime required')
    result=subprocess.run([str(python),'-B','-c',f'COUNT={count}\nTARGET={target}\n'+SCRIPT],
                          capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode==0,result.stdout+result.stderr


@pytest.mark.parametrize('before,angle,changed', [(0,90,True),(0,360,False),(180,-180,False),(-180,540,False)])
def test_native_mapped_geometry_reports_pad_orientation_without_center_movement(before,angle,changed):
    python=runtime()
    if python is None:
        pytest.skip('KiCad desktop kipy runtime required')
    script = r'''
from copy import deepcopy
from kipy.board_types import FootprintInstance, Pad
from part_selection_pcb_session import describe
from part_selection_pcb_geometry import mapped_pad_changes
old=FootprintInstance()
pad=Pad();pad.number='1'
pad.proto.pad_stack.angle.value_degrees=BEFORE
layer=pad.proto.pad_stack.copper_layers[0]
layer.size.x_nm=1000000;layer.size.y_nm=2000000
old.definition.add_item(pad)
target=FootprintInstance(deepcopy(old.proto))
target.definition.pads[0].proto.pad_stack.angle.value_degrees=ANGLE
report=mapped_pad_changes(describe(old),describe(target),{'pairs':[{'source':'1','target':'1','align':True}]})
assert report['max_error_mm']==report['center_error_mm']==0
if CHANGED:
    assert report['differences']==[{'number':'1','fields':['rotation_deg'],
        'before':{'rotation_deg':0.0},'after':{'rotation_deg':90.0}}],report
else:
    assert report['differences']==[],report
'''
    result=subprocess.run([str(python),'-B','-c',f'BEFORE={before}\nANGLE={angle}\nCHANGED={changed}\n'+script],
                          capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode==0,result.stdout+result.stderr

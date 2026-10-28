"""Native protobuf regressions; no application or CAD files are opened."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


def runtime():
    if importlib.util.find_spec('kipy'):
        return Path(sys.executable)
    base = Path(os.environ.get('LOCALAPPDATA', '')) / 'KiCadPartSelection'
    return next(base.glob('*/desktop-runtime/Scripts/python.exe'), None)


SCRIPT = r'''
from uuid import uuid4
from copy import deepcopy
from kipy.board_types import FootprintInstance, Pad, BoardText, BoardTextBox, BoardShape, Field
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.geometry import Vector2
from part_selection_pcb_session import replacement, describe
def fp(ref):
 f=FootprintInstance();f.proto.id.value=str(uuid4());f.layer=BoardLayer.BL_F_Cu
 f.definition.id.library='Local';f.definition.id.name='Standard'
 for field in ('reference_field','value_field','description_field','datasheet_field'):
  getattr(f,field).proto.text.id.value=str(uuid4())
 f.reference_field.text.value=ref
 for number,x in [('1',-1000000),('2',1000000)]:
  p=Pad();p.proto.id.value=str(uuid4());p.number=number;p.position=Vector2.from_xy(x,0);f.definition.add_item(p)
 return f
old1,old2,target=fp('R1'),fp('R2'),fp('Target')
text=BoardText();text.proto.id.value=str(uuid4());text.value='graphic';target.definition.add_item(text)
placement={'position_mm':[0,0],'rotation_deg':0}
if MODE=='ids':
 a=replacement(describe(old1),describe(target),placement);b=replacement(describe(old2),describe(target),placement)
 aid={item.id.value for item in a.definition.items};bid={item.id.value for item in b.definition.items}
 assert not aid & bid, 'Cached target child UUIDs reused across instances'
 assert {p.id.value for p in a.definition.pads}=={p.id.value for p in old1.definition.pads}
elif MODE=='unsupported':
 box=BoardTextBox();old1.definition.add_item(box)
 try:replacement(describe(old1),describe(target),placement)
 except ValueError:pass
 else:raise AssertionError('Unsupported source text box was silently discarded')
elif MODE.startswith('persisted_'):
 from part_selection_pcb_session import persisted_footprint_matches, encode, decode, footprint_bytes
 expected=fp('R1');expected.position=Vector2.from_xy(1000000,2000000)
 expected.proto.orientation.value_degrees=126.99999928704327
 field=Field();field.name='Comment';field.proto.text.id.value=str(uuid4())
 field.proto.text.text.position.x_nm=12345;expected.definition.add_item(field)
 shape=BoardShape();shape.proto.id.value=str(uuid4())
 shape.proto.shape.polygon.polygons.add().outline.nodes.add().point.x_nm=12345
 expected.definition.add_item(shape)
 actual=decode(encode(expected))
 mode=MODE.removeprefix('persisted_')
 if mode=='rounding':
  actual.proto.position.y_nm+=1
  actual.proto.orientation.value_degrees=126.9999993
 elif mode=='eight_decimal_rounding':
  expected.proto.orientation.value_degrees=54.00002872281633
  actual.proto.orientation.value_degrees=54.00002872
  actual.definition.pads[0].proto.position.x_nm+=1
 elif mode=='limit':actual.proto.position.x_nm-=1
 elif mode=='pad_rounding':actual.definition.pads[0].proto.position.x_nm+=1
 elif mode=='pad_position':actual.definition.pads[0].proto.position.x_nm+=2
 elif mode=='pad_angle':actual.definition.pads[0].proto.pad_stack.angle.value_degrees+=.000001
 elif mode in ('field_rounding','field_position'):
  next(x for x in actual.definition.items if isinstance(x,Field)).proto.text.text.position.x_nm+=1 if mode=='field_rounding' else 2
 elif mode in ('polygon_rounding','polygon_position'):
  next(x for x in actual.definition.items if isinstance(x,BoardShape)).proto.shape.polygon.polygons[0].outline.nodes[0].point.x_nm+=1 if mode=='polygon_rounding' else 2
 elif mode=='position':actual.proto.position.x_nm+=2
 elif mode=='noncanonical_angle':actual.proto.orientation.value_degrees+=1e-8
 elif mode=='angle':actual.proto.orientation.value_degrees+=.000001
 elif mode=='field':actual.value_field.text.value='changed'
 elif mode=='net':actual.definition.pads[0].proto.net.name='changed'
 elif mode=='uuid':actual.definition.pads[0].proto.id.value=str(uuid4())
 elif mode=='size':actual.definition.pads[0].proto.pad_stack.drill.diameter.x_nm+=1
 elif mode=='count':actual.definition.add_item(deepcopy(actual.definition.items[0]))
 elif mode=='metadata':actual.proto.symbol_sheet_name='changed'
 elif mode=='presence':actual.proto.orientation.ClearField('value_degrees')
 elif mode=='unknown':actual.proto.MergeFromString(b'\xd8\x07\x01')
 elif mode=='pad_unknown':actual.definition.pads[0].proto.MergeFromString(b'\xd8\x07\x01')
 elif mode=='order':actual.definition.items=list(reversed(actual.definition.items))
 assert persisted_footprint_matches(expected,actual)==(mode in ('rounding','eight_decimal_rounding','limit','order','pad_rounding','field_rounding','polygon_rounding')), mode
 if mode in ('rounding','limit'):assert footprint_bytes(expected)!=footprint_bytes(actual)
else:
 from part_selection_pcb_session import footprint_bytes, encode, PcbSession
 field=Field();field.name='Comment';field.text.value='fixture comment';field.proto.text.id.value=str(uuid4())
 old1.definition.add_item(field)
 expected=replacement(describe(old1),describe(target),placement)
 actual=FootprintInstance(deepcopy(expected.proto))
 actual.definition.items=list(reversed(actual.definition.items))
 assert encode(expected)!=encode(actual), 'Fixture did not reorder objects'
 assert footprint_bytes(expected)==footprint_bytes(actual), 'Unordered object contents rejected'
 if MODE=='order':
  text='(kicad_pcb (footprint "Local:Standard" (uuid "'+old1.id.value+'")))'
  before={'session_id':'fixture','text':text,'parts':{'R1':describe(old1)}}
  after={'session_id':'fixture','text':text,'parts':{'R1':describe(actual)}}
  assert PcbSession.verify(before,after,[{'ref':'R1','proto':encode(expected)}])['ok']
 else:
  if MODE=='field_change':
   next(item for item in actual.definition.items if isinstance(item,Field)).text.value='changed'
  elif MODE=='geometry_change':actual.definition.pads[0].proto.pad_stack.angle.value_degrees=31
  elif MODE=='net_change':actual.definition.pads[0].proto.net.name='changed'
  elif MODE=='id_change':actual.definition.pads[0].proto.id.value=str(uuid4())
  elif MODE=='count_change':actual.definition.add_item(deepcopy(actual.definition.items[0]))
  elif MODE=='position_change':actual.position=Vector2.from_xy(10,20)
  elif MODE=='metadata_change':actual.proto.symbol_sheet_name='changed'
  assert footprint_bytes(expected)!=footprint_bytes(actual), 'Real content change was ignored'
print('verified')
'''


@pytest.mark.parametrize('mode', ['ids', 'unsupported', 'order', 'field_change', 'geometry_change',
                                'net_change', 'id_change', 'count_change', 'position_change', 'metadata_change',
                                *('persisted_'+mode for mode in ('rounding','eight_decimal_rounding','limit','position','angle',
                                    'pad_rounding','pad_position','pad_angle','field','net','uuid',
                                    'size','count','metadata','presence','order','unknown','pad_unknown',
                                    'noncanonical_angle','field_rounding','field_position',
                                    'polygon_rounding','polygon_position'))])
def test_replacement_native_proto_boundaries(mode):
    python = runtime()
    if python is None:
        pytest.skip('KiCad desktop kipy runtime required for native type verification')
    result = subprocess.run([str(python), '-B', '-c', 'MODE=' + repr(mode) + '\n' + SCRIPT],
        capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('mode', ['rounding_order','multiplicity','real_move','shape','ambiguous_matching',
                                'reverse_segment','reverse_move','reverse_width','reverse_layer'])
def test_geometry_completion_matches_unordered_items_without_losing_content(mode):
    python=runtime()
    if python is None:
        pytest.skip('KiCad desktop kipy runtime required')
    script = r'''
from kipy.board_types import FootprintInstance, BoardShape
from part_selection_pcb_session import encode, decode, geometry_matches, describe
expected=FootprintInstance()
for x,y in [(100000000,100),(100000000,200)]:
    shape=BoardShape()
    shape.proto.shape.segment.start.x_nm=x
    shape.proto.shape.segment.start.y_nm=y
    shape.proto.shape.segment.end.x_nm=x+50
    shape.proto.shape.segment.end.y_nm=0
    expected.definition.add_item(shape)
actual=decode(encode(expected))
if MODE=='rounding_order':
    actual.definition.items[0].proto.shape.segment.start.x_nm+=1
    actual.definition.items[1].proto.shape.segment.start.x_nm-=1
elif MODE=='real_move':actual.definition.items[0].proto.shape.segment.start.x_nm+=6
elif MODE=='shape':actual.definition.items[0].proto.shape.segment.end.y_nm+=100
elif MODE=='multiplicity':actual.definition.items=[actual.definition.items[0],actual.definition.items[0]]
elif MODE=='ambiguous_matching':
    for item,x in zip(expected.definition.items,[0,5]):
        item.proto.shape.segment.start.x_nm=x
        item.proto.shape.segment.start.y_nm=0
        item.proto.shape.segment.end.x_nm=50
        item.proto.shape.segment.end.y_nm=0
    actual=decode(encode(expected))
    for item,x in zip(actual.definition.items,[4,-5]):item.proto.shape.segment.start.x_nm=x
elif MODE.startswith('reverse_'):
    segment=actual.definition.items[0].proto.shape.segment
    start=type(segment.start)();start.CopyFrom(segment.start)
    segment.start.CopyFrom(segment.end);segment.end.CopyFrom(start)
    if MODE=='reverse_move':segment.start.x_nm+=6
    if MODE=='reverse_width':actual.definition.items[0].proto.shape.attributes.stroke.width.value_nm+=1
    if MODE=='reverse_layer':actual.definition.items[0].proto.layer+=1
assert geometry_matches(describe(expected),describe(actual)) == (MODE in ('rounding_order','ambiguous_matching','reverse_segment')),MODE
print('verified',MODE)
'''
    result=subprocess.run([str(python),'-B','-c','MODE='+repr(mode)+'\n'+script],
                          capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode==0,result.stdout+result.stderr


@pytest.mark.parametrize('mode', ['duplicate_closure','two_closures','open','moved_vertex',
    'missing_vertex','reversed','node_unknown','line_unknown','pad_change','extra_shape',
    'hole','hole_move','custom_pad','custom_pad_move','arc_end'])
def test_closed_polygon_native_roundtrip_preserves_exact_geometry(mode):
    python=runtime()
    if python is None:
        pytest.skip('KiCad desktop kipy runtime required')
    script=r'''
from copy import deepcopy
from kipy.board_types import FootprintInstance, BoardShape, Pad
from part_selection_pcb_session import encode, decode, footprint_bytes, geometry_matches, PcbSession, describe
original=FootprintInstance()
original.proto.id.value='instance'
original.reference_field.text.value='U49'
original.definition.id.library='Local';original.definition.id.name='Fixture'
shape=BoardShape();shape.proto.id.value='drawing'
outline=shape.proto.shape.polygon.polygons.add().outline
outline.closed=MODE!='open'
for x,y in [(100,100),(200,100),(200,200),(100,200),(100,100)]:
    point=outline.nodes.add().point;point.x_nm=x;point.y_nm=y
if MODE=='two_closures':outline.nodes.add().CopyFrom(outline.nodes[0])
if MODE=='node_unknown':
    outline.nodes[0].MergeFromString(b'\xd8\x07\x01')
    outline.nodes[-1].CopyFrom(outline.nodes[0])
if MODE=='line_unknown':outline.MergeFromString(b'\xd8\x07\x01')
if MODE=='arc_end':
    outline.nodes[0].ClearField('point')
    outline.nodes[0].arc.start.x_nm=100
    outline.nodes[-1].CopyFrom(outline.nodes[0])
if MODE in ('hole','hole_move'):
    hole=shape.proto.shape.polygon.polygons[0].holes.add()
    hole.CopyFrom(outline)
original.definition.add_item(shape)
pad=Pad();pad.number='1';pad.proto.id.value='pad'
if MODE in ('custom_pad','custom_pad_move'):
    native=deepcopy(pad.proto)
    custom=native.pad_stack.copper_layers[0].custom_shapes.add().shape.polygon.polygons.add().outline
    custom.CopyFrom(outline)
    pad=Pad(native)
original.definition.add_item(pad)
actual=decode(encode(original))
live=next(x for x in actual.definition.items if isinstance(x,BoardShape)).proto.shape.polygon.polygons[0].outline
del live.nodes[-1]
if MODE=='two_closures':del live.nodes[-1]
if MODE=='moved_vertex':live.nodes[1].point.x_nm+=1
if MODE=='missing_vertex':del live.nodes[1]
if MODE=='reversed':
    nodes=list(live.nodes);del live.nodes[:];live.nodes.extend(reversed(nodes))
if MODE=='pad_change':actual.definition.pads[0].proto.position.x_nm+=1
if MODE=='extra_shape':actual.definition.add_item(deepcopy(shape))
if MODE in ('hole','hole_move'):
    hole=next(x for x in actual.definition.items if isinstance(x,BoardShape)).proto.shape.polygon.polygons[0].holes[0]
    del hole.nodes[-1]
    if MODE=='hole_move':hole.nodes[1].point.x_nm+=1
if MODE in ('custom_pad','custom_pad_move'):
    native=deepcopy(actual.definition.pads[0].proto)
    custom=native.pad_stack.copper_layers[0].custom_shapes[0].shape.polygon.polygons[0].outline
    del custom.nodes[-1]
    if MODE=='custom_pad_move':custom.nodes[1].point.x_nm+=1
    actual.definition.items=[Pad(native) if isinstance(item,Pad) else item for item in actual.definition.items]
accepted=MODE in ('duplicate_closure','two_closures','hole','custom_pad')
assert (footprint_bytes(original)==footprint_bytes(actual)) is accepted, MODE
if accepted:
    assert geometry_matches(describe(original),describe(actual))
    text='(kicad_pcb (footprint "Local:Fixture" (uuid "instance")))'
    before={'session_id':'fixture','text':text,'parts':{'U49':describe(original)}}
    after={'session_id':'fixture','text':text,'parts':{'U49':describe(actual)}}
    assert PcbSession.verify(before,after,[{'ref':'U49','proto':encode(original),'fields':{},'kind':'fields'}])['ok']
print('verified')
'''
    result=subprocess.run([str(python),'-B','-c','MODE='+repr(mode)+'\n'+script],
        capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode==0,result.stderr

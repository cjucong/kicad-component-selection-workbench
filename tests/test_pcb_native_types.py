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
from kipy.board_types import FootprintInstance, Pad, BoardText, BoardTextBox, Field
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
                                'net_change', 'id_change', 'count_change', 'position_change', 'metadata_change'])
def test_replacement_native_proto_boundaries(mode):
    python = runtime()
    if python is None:
        pytest.skip('KiCad desktop kipy runtime required for native type verification')
    result = subprocess.run([str(python), '-B', '-c', 'MODE=' + repr(mode) + '\n' + SCRIPT],
        capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr

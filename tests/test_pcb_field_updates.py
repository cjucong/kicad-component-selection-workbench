"""Native protobuf checks: metadata edits never rebuild footprint geometry."""
import subprocess

import pytest

from test_pcb_native_types import runtime


SCRIPT = r'''
from copy import deepcopy
from uuid import uuid4
from kipy.board_types import FootprintInstance, Field, Pad
from kipy.geometry import Vector2
from part_selection_pcb_session import describe, decode, encode, metadata_replacement, PcbSession
fp=FootprintInstance();fp.proto.id.value=str(uuid4())
fp.reference_field.text.value='R1';fp.value_field.text.value='100k'
fp.definition.id.library='Local';fp.definition.id.name='Old'
fp.position=Vector2.from_xy(12000000,34000000)
for name in ('reference_field','value_field','description_field','datasheet_field'):
 getattr(fp,name).proto.text.id.value=str(uuid4())
pad=Pad();pad.number='1';pad.proto.id.value=str(uuid4());fp.definition.add_item(pad)
field=Field();field.name='Comment';field.text.value='resistor';field.visible=True
field.proto.text.id.value=str(uuid4());field.proto.text.text.position.x_nm=1234567
fp.definition.add_item(field)
before=deepcopy(fp.proto)
if MODE=='rename':
 result=decode(metadata_replacement(describe(fp),footprint='Other:Renamed'))
 expected=deepcopy(before);expected.definition.id.library_nickname='Other';expected.definition.id.entry_name='Renamed'
 assert result.proto==expected, 'Rename changed more than the complete library ID'
elif MODE=='fields':
 result=decode(metadata_replacement(describe(fp),fields={'Value':'22k','Comment':'resistor low drift','Brand':'Yageo'}))
 original_field=next(f for f in fp.definition.items if isinstance(f,Field))
 updated=next(f for f in result.definition.items if isinstance(f,Field) and f.name=='Comment')
 check=deepcopy(updated.proto);check.text.text.text=original_field.text.value
 assert check==original_field.proto, 'Field pose, styling or visibility changed'
 added=next(f for f in result.definition.items if isinstance(f,Field) and f.name=='Brand')
 assert added.visible is False and added.text.value=='Yageo'
 assert not added.proto.text.HasField('parent'), 'Embedded footprint fields must match native IPC serialization'
 assert added.proto.text.id.value not in [original_field.proto.text.id.value,fp.value_field.proto.text.id.value]
 assert result.proto.position==before.position and result.proto.orientation==before.orientation
 assert result.definition.pads[0].proto==pad.proto
 text='(kicad_pcb (footprint "Local:Old" (uuid "'+fp.id.value+'")))'
 a={'session_id':'fixture','text':text,'parts':{'R1':describe(fp)}}
 b={'session_id':'fixture','text':text,'parts':{'R1':describe(result)}}
 assert PcbSession.verify(a,b,[{'ref':'R1','proto':encode(result),'fields':{'Value':'22k','Comment':'resistor low drift','Brand':'Yageo'}}])['ok']
elif MODE=='empty':
 result=decode(metadata_replacement(describe(fp),fields={'Comment':''}))
 assert result.value_field.text.value=='100k'
 assert next(f for f in result.definition.items if isinstance(f,Field)).text.value==''
else:
 if MODE=='duplicates':
  other=Field();other.name='comment';fp.definition.add_item(other)
  fields={'Comment':'new'}
 elif MODE=='conflicting_targets':fields={'Value':'22k','value':'33k'}
 else:fields={'Reference':'R2'}
 try:metadata_replacement(describe(fp),fields=fields)
 except ValueError:pass
 else:raise AssertionError('Ambiguous or protected metadata write accepted')
assert fp.proto==before or MODE=='duplicates', 'Input protobuf mutated'
print('verified')
'''


@pytest.mark.parametrize('mode', ['rename','fields','empty','duplicates','conflicting_targets','protected'])
def test_metadata_replacement_native_proto(mode):
    python = runtime()
    if python is None:
        pytest.skip('KiCad desktop kipy runtime required for native type verification')
    result = subprocess.run([str(python),'-B','-c','MODE='+repr(mode)+'\n'+SCRIPT],
                            capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode == 0, result.stderr


def test_completed_geometry_ignores_identity_and_fields_but_not_shapes_or_pads():
    script = r'''
from part_selection_pcb_session import geometry_signature, geometry_matches
a=describe(fp)
b=decode(a['proto']);b.value_field.text.value='changed'
b.proto.id.value=str(uuid4());b.definition.pads[0].proto.id.value=str(uuid4())
assert geometry_signature(a)==geometry_signature(describe(b))
b.locked=True
from kipy.proto.common.types.base_types_pb2 import LS_LOCKED
b.definition.pads[0].proto.locked=LS_LOCKED
assert geometry_signature(a)==geometry_signature(describe(b)), 'Lock metadata is not footprint geometry'
assert geometry_matches(a,describe(b))
b.definition.pads[0].position=Vector2.from_xy(2,0)
assert geometry_matches(a,describe(b))
b.definition.pads[0].position=Vector2.from_xy(10,0)
assert not geometry_matches(a,describe(b))
b.definition.pads[0].position=Vector2.from_xy(1000000,0)
assert geometry_signature(a)!=geometry_signature(describe(b))
'''
    python=runtime()
    if python is None: pytest.skip('Native kipy required')
    setup=SCRIPT.split("if MODE=='rename':")[0]
    result=subprocess.run([str(python),'-B','-c',setup+script],capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode==0, result.stderr


@pytest.mark.parametrize('change', ['metadata','new_field','noop','replace','position','rotation','pad','net','unlock','field_position'])
def test_locked_native_update_allows_only_explicit_metadata(change):
    script = r'''
from part_selection_pcb_session import validate_locked_update
fp.locked=True
original=describe(fp)
fields={'Value':'22k'} if CHANGE=='metadata' else {'Brand':'Yageo'} if CHANGE=='new_field' else {}
proposed=decode(metadata_replacement(original,footprint='Other:Renamed',fields=fields))
if CHANGE=='position':proposed.proto.position.x_nm+=1
elif CHANGE=='rotation':proposed.proto.orientation.value_degrees+=90
elif CHANGE=='pad':proposed.definition.pads[0].proto.position.x_nm+=1
elif CHANGE=='net':proposed.definition.pads[0].proto.net.name='wrong'
elif CHANGE=='unlock':proposed.locked=False
elif CHANGE=='field_position':next(x for x in proposed.definition.items if isinstance(x,Field)).proto.text.text.position.x_nm+=1
item={'ref':'R1','proto':encode(proposed),'fields':fields,'kind':'replace' if CHANGE=='replace' else 'rename'}
if CHANGE in ('metadata','new_field','noop'):
 validate_locked_update(original,item)
else:
 try:validate_locked_update(original,item)
 except ValueError as exc:assert 'R1' in str(exc)
 else:raise AssertionError('Locked instance geometry, lock or connectivity was modified')
assert fp.locked is True
'''
    python=runtime()
    if python is None:pytest.skip('Native kipy required')
    setup=SCRIPT.split("if MODE=='rename':")[0]
    result=subprocess.run([str(python),'-B','-c','CHANGE='+repr(change)+'\n'+setup+script],
        capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode==0, result.stderr


@pytest.mark.parametrize('operation', ['rename','fields','noop','unknown'])
@pytest.mark.parametrize('locked', [False, True])
def test_metadata_preflight_skips_library_reads_and_blocks_unknown_intent(scratch_project, operation, locked):
    script = r'''
import json, threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from part_selection_pcb import PcbUpdates
from part_selection_footprint_changes import board_copper_hashes
root=Path(ROOT)
fp_text='(kicad_pcb (footprint "Local:Old" (property "Reference" "R1") (layer "F.Cu") (at 12 34) (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))))'
fp.proto.symbol_path.path.add().value='symbol'
fp.locked=LOCKED
source=describe(fp)
session=SimpleNamespace(snapshot=Mock(return_value={'session_id':'one','text':fp_text,'parts':{'R1':source}}),
 read_library=Mock(side_effect=AssertionError('metadata launched library reader')),apply=Mock(return_value={'ok':True}))
app=SimpleNamespace(project_root=root,config={'schematic_dir':root},overrides_path=root/'records.json',selection_save_lock=threading.RLock())
(root/'Test.kicad_pro').write_text('{}')
(root/'Test.kicad_sch').write_text('(kicad_sch (uuid "root") (symbol (uuid "symbol") (property "Reference" "R1") (property "Value" "22k") (property "Footprint" "Local:Renamed") (instances (project "Test" (path "/root" (reference "R1"))))))')
app.overrides_path.write_text(json.dumps({'R1':{'field_overrides':{'footprint':'Local:Renamed'},'footprint_change':{
 'schema':1,'operation':'rename','before':'Local:Old','after':'Local:Renamed',
 'source':{'instance_geometry_hash':board_copper_hashes(fp_text,['R1'])['R1']}}}}))
if OPERATION!='rename':
 app.overrides_path.write_text('{}')
 if OPERATION!='unknown':
  path=root/'Test.kicad_sch';path.write_text(path.read_text().replace('Local:Renamed','Local:Old'))
 if OPERATION=='noop':
  path=root/'Test.kicad_sch';path.write_text(path.read_text().replace('22k','100k'))
service=PcbUpdates(app,session=session)
preview=service.preview(['R1'],'__schematic__')
item=preview['items'][0]
assert item['kind']=={'noop':'none'}.get(OPERATION,OPERATION) and item['needs_ack'] is False
if OPERATION=='unknown':
 assert preview['blocked'] is True
 try:service.apply(preview['preview_id'],True)
 except ValueError:pass
 else:raise AssertionError('Unknown footprint operation was written')
 session.apply.assert_not_called()
else:
 assert item['field_changes']==([] if OPERATION=='noop' else [{'name':'Value','before':'100k','after':'22k'}])
 assert service.apply(preview['preview_id'],True)['ok']
 if OPERATION=='noop':session.apply.assert_not_called()
 else:session.apply.assert_called_once()
session.read_library.assert_not_called()
'''
    python = runtime()
    if python is None:
        pytest.skip('KiCad desktop kipy runtime required')
    # Use the same real native fixture, without the mode-specific assertions.
    setup = SCRIPT.split("if MODE=='rename':")[0]
    result = subprocess.run([str(python),'-B','-c','ROOT='+repr(str(scratch_project))+'\nOPERATION='+repr(operation)+'\nLOCKED='+repr(locked)+'\n'+setup+script],
        capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode == 0, result.stderr

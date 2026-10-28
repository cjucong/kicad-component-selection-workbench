"""Pad completion must preserve board annotations, never hide copper changes."""
import subprocess
import pytest
from test_pcb_native_types import runtime

@pytest.mark.parametrize('mode', ['silk','fab','model','pad_move','pad_size','drill','number','layer','pose','rotation','copper','mask'])
def test_native_pad_completion(mode):
    python = runtime()
    if python is None:
        pytest.skip('Native kipy runtime required')
    script = r'''
from kipy.board_types import FootprintInstance, Pad, BoardShape, Footprint3DModel
from kipy.proto.board.board_types_pb2 import BoardLayer as L
from part_selection_pcb_session import describe, decode, pad_geometry_matches
fp=FootprintInstance(); fp.layer=L.BL_F_Cu
p=Pad();p.number='1';fp.definition.add_item(p)
old=describe(fp);new=decode(old['proto'])
if MODE in ('silk','fab','copper','mask'):
 s=BoardShape();s.layer={'silk':L.BL_F_SilkS,'fab':L.BL_F_Fab,'copper':L.BL_F_Cu,'mask':L.BL_F_Mask}[MODE]
 s.proto.shape.segment.end.x_nm=1000000;new.definition.add_item(s)
elif MODE=='model':new.definition.add_item(Footprint3DModel())
elif MODE=='pad_move':new.definition.pads[0].proto.position.x_nm+=10000
elif MODE=='pad_size':new.definition.pads[0].proto.pad_stack.copper_layers.add().size.x_nm=10000
elif MODE=='drill':new.definition.pads[0].proto.pad_stack.drill.diameter.x_nm=10000
elif MODE=='number':new.definition.pads[0].number='2'
elif MODE=='layer':new.layer=L.BL_B_Cu
elif MODE=='pose':new.proto.position.x_nm+=10000
elif MODE=='rotation':new.proto.orientation.value_degrees=90
assert pad_geometry_matches(old,describe(new)) == (MODE in ('silk','fab','model')),MODE
'''
    result=subprocess.run([str(python),'-B','-c','MODE='+repr(mode)+'\n'+script],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr

def test_table_annotation_edits_do_not_invalidate_verified_pads(status_app):
    from test_design_status import current_board,replacement_record,observation,status
    app,board=status_app
    replacement_record(app);current_board(board)
    text=board.read_text().replace('(path ', '(fp_line (start 0 0) (end 1 0) (layer "F.SilkS") (uuid "silk")) (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")) (path ')
    board.write_text(text)
    assert app.design_status.observe(observation(app,board))
    board.write_text(text.replace('(end 1 0)','(end 2 0)'))
    assert not status(app)['pcb_unverified_refs']
    board.write_text(board.read_text().replace('(size 1 1)','(size 2 1)'))
    assert status(app)['pcb_unverified_refs']=='U1'

from test_design_status import status_app


def test_copper_text_invalidates_pad_evidence(status_app):
    from test_design_status import current_board,replacement_record,observation,status
    app,board=status_app
    replacement_record(app);current_board(board)
    text=board.read_text().replace('(path ', '(fp_text user "copper" (at 0 0) (layer "F.Cu")) (path ')
    board.write_text(text)
    assert app.design_status.observe(observation(app,board))
    board.write_text(text.replace('(at 0 0)','(at 4 0)'))
    assert status(app)['pcb_unverified_refs']=='U1'


def test_native_annotation_textbox_completion_never_replaces():
    python=runtime()
    if python is None:pytest.skip('Native kipy required')
    script=r'''
from kipy.board_types import FootprintInstance, Pad, BoardTextBox
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer as L
from part_selection_pcb_session import describe,decode,encode,annotation_completion_matches
fp=FootprintInstance();fp.layer=L.BL_F_Cu
p=Pad();p.number='1';p.position=Vector2.from_xy(0,0);p.proto.pad_stack.angle.value_degrees=0;fp.definition.add_item(p)
old=describe(fp);box=BoardTextBox();box.layer=L.BL_F_SilkS;fp.definition.add_item(box)
before=encode(fp)
assert annotation_completion_matches(describe(fp),old,{'position_mm':[0,0],'rotation_deg':0})
assert encode(fp)==before
target=decode(old['proto']);target.definition.pads[0].number='2'
try:
 result=annotation_completion_matches(describe(fp),describe(target),{'position_mm':[0,0],'rotation_deg':0})
except ValueError:pass
else:assert not result
'''
    result=subprocess.run([str(python),'-B','-c',script],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr

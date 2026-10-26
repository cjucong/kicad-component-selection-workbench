"""Native saved-PCB copper equivalence probe, without GUI or production writes."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))


def native(root, phase):
    import pcbnew as p
    from part_selection_pcb_geometry import solve_placement
    root=Path(root)
    assert root.name=='project' and root.parent.name.startswith('unify-native-')
    pretty=root/'Local.pretty'
    def pads(fp):
        return {pad.GetNumber():pad for pad in fp.Pads()}
    def snapshot(fp):
        return {'position_mm':[p.ToMM(fp.GetPosition().x),p.ToMM(fp.GetPosition().y)],
                'rotation_deg':fp.GetOrientationDegrees(),'back':fp.IsFlipped(),
                'pads':[{'number':pad.GetNumber(),'position_mm':[p.ToMM(pad.GetPosition().x),p.ToMM(pad.GetPosition().y)]}
                        for pad in fp.Pads()]}
    if phase=='build':
        pretty.mkdir()
        raw='(footprint "Standard" (layer "F.Cu") '+''.join(
            '(pad "%s" smd rect (at %s 0) (size .8 1) (layers "F.Cu" "F.Paste" "F.Mask"))' % (n,x)
            for n,x in [('1',-1),('2',1)])+')'
        standard=p.PCB_IO_KICAD_SEXPR().Parse(raw).Cast()
        standard.SetFPID(p.LIB_ID('Local','Standard'))
        p.PCB_IO_KICAD_SEXPR().FootprintSave(str(pretty),standard)
        board=p.BOARD()
        for index,ref in enumerate(('R1','U1')):
            fp=p.FootprintLoad(str(pretty),'Standard')
            for pad in fp.Pads():
                pad.Rotate(p.VECTOR2I(0,0),p.EDA_ANGLE(90,p.DEGREES_T))
                pad.Move(p.VECTOR2I(p.FromMM(4),p.FromMM(-3)))
            fp.SetFPID(p.LIB_ID('Local','Legacy_'+ref))
            p.PCB_IO_KICAD_SEXPR().FootprintSave(str(pretty),fp)
            board.Add(fp);fp.SetReference(ref)
            if index:fp.Flip(p.VECTOR2I(0,0),False)
            fp.SetOrientationDegrees(37 if not index else -23)
            fp.SetPosition(p.VECTOR2I(p.FromMM(100+index*20),p.FromMM(80)))
            for number,pad in pads(fp).items():
                net=p.NETINFO_ITEM(board,ref+'_'+number);board.Add(net);pad.SetNet(net)
                track=p.PCB_TRACK(board);track.SetStart(pad.GetPosition());track.SetEnd(pad.GetPosition()+p.VECTOR2I(1000000,0))
                track.SetLayer(p.B_Cu if index else p.F_Cu);track.SetWidth(150000);track.SetNet(net);board.Add(track)
        p.SaveBoard(str(root/'project.kicad_pcb'),board)
        return {'built':True}
    board=p.LoadBoard(str(root/'project.kicad_pcb'))
    original={fp.GetReference():fp for fp in board.GetFootprints()}
    tracks=[(t.GetStart().x,t.GetStart().y,t.GetEnd().x,t.GetEnd().y,t.GetNetname()) for t in board.GetTracks()]
    expected={ref:{n:(pad.GetPosition().x,pad.GetPosition().y,pad.GetNetname()) for n,pad in pads(fp).items()}
              for ref,fp in original.items()}
    for ref,old in original.items():
        target=p.FootprintLoad(str(pretty),'Standard')
        target_board=p.BOARD();target_board.Add(target)
        if old.IsFlipped():target.Flip(p.VECTOR2I(0,0),False)
        placement=solve_placement(snapshot(old),snapshot(target))
        assert placement['ok'] and placement['max_error_mm']<=.000005,placement
        target.SetOrientationDegrees(placement['rotation_deg'])
        target.SetPosition(p.VECTOR2I(*(p.FromMM(v) for v in placement['position_mm'])))
        # This fixture changes only the origin/pose of identical two-pad copper.
        # Keep native item/net ownership intact while adopting standard local coordinates.
        old.SetOrientationDegrees(placement['rotation_deg'])
        old.SetPosition(target.GetPosition())
        for n,pad in pads(old).items():
            pad.SetPosition(pads(target)[n].GetPosition())
            pad.SetOrientation(pads(target)[n].GetOrientation())
        old.SetFPID(p.LIB_ID('Local','Standard'))
    p.SaveBoard(str(root/'project.kicad_pcb'),board)
    reopened=p.LoadBoard(str(root/'project.kicad_pcb'))
    max_error=0
    for fp in reopened.GetFootprints():
        assert fp.GetFPID().GetLibItemName().c_str()=='Standard'
        for n,pad in pads(fp).items():
            x,y,net=expected[fp.GetReference()][n]
            error=max(abs(x-pad.GetPosition().x),abs(y-pad.GetPosition().y))
            assert error<=5
            max_error=max(max_error,error)
            assert pad.GetNetname()==net
    assert tracks==[(t.GetStart().x,t.GetStart().y,t.GetEnd().x,t.GetEnd().y,t.GetNetname()) for t in reopened.GetTracks()]
    return {'native_saved_reloaded':True,'front_and_back':True,'pad_error_nm_max':max_error,'nets_tracks_preserved':True,
            'scope':'native pcbnew roundtrip; not IPC transaction or GUI reopening'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child',type=Path);parser.add_argument('--phase')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if args.child:
        print(json.dumps(native(args.child,args.phase)));return
    from _scratch import temporary_project
    from part_selection_footprint_geometry import _find_kicad_python
    from part_selection_server import PartSelectionApp
    from part_selection_footprint_unify import FootprintUnifier
    from part_selection_overrides import save_overrides,load_overrides
    python=_find_kicad_python()
    assert python and args.report and args.report.is_absolute() and not args.report.is_relative_to(ROOT)
    with temporary_project(prefix='unify-native-') as root:
        root=Path(root)
        def run(phase):
            done=subprocess.run([str(python),'-B',str(Path(__file__).resolve()),'--child',str(root),'--phase',phase],
                capture_output=True,text=True,encoding='utf-8',timeout=45,creationflags=subprocess.CREATE_NO_WINDOW)
            assert done.returncode==0,(done.returncode,done.stdout,done.stderr)
            return json.loads(done.stdout)
        run('build')
        (root/'fp-lib-table').write_text('(fp_lib_table (lib (name "Local") (type "KiCad") (uri "${KIPRJMOD}/Local.pretty")))')
        (root/'project.kicad_pro').write_text('{}')
        (root/'project.kicad_sch').write_text('(kicad_sch (uuid "root") '+''.join(
            '(symbol (uuid "%s") (property "Reference" "%s") (property "Value" "TEST") (property "Comment" "TEST") '
            '(property "Footprint" "Local:Legacy_%s") (property "LCSC" "C100") '
            '(instances (project "project" (path "/root" (reference "%s")))))' % (ref,ref,ref,ref) for ref in ('R1','U1'))+')')
        app=PartSelectionApp(root/'settings.json')
        try:
            save_overrides(app.overrides_path,{ref:{'review_status':'Confirmed'} for ref in ('R1','U1')})
            before=(root/'project.kicad_pcb').read_bytes()
            library=(root/'Local.pretty/Standard.kicad_mod').read_bytes()
            report=FootprintUnifier(app).run(apply=True,targets={'TEST':'Local:Standard'})
            assert len(report['changes'])==2 and not report['skipped'],report
            assert before==(root/'project.kicad_pcb').read_bytes()
            assert all(r['review_status']=='Confirmed' and r['workbench_undo'] for r in load_overrides(app.overrides_path).values())
            report.update(run('replace'))
            assert library==(root/'Local.pretty/Standard.kicad_mod').read_bytes()
            report.update(standard_library_unchanged=True,production_cad_written=False)
        finally:
            app.search_jobs.close();app.selection_jobs.close();app.design_sync.close()
    report['fixture_cleaned']=not root.exists()
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    main()

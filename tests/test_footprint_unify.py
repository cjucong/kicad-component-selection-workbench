"""Standard-library reuse and atomic cross-row unification in disposable projects."""
from pathlib import Path

import pytest

from part_selection_overrides import load_overrides, save_overrides
from test_row_footprint import footprint_app


def module(name, shift=0, pitch=2, size=.8, silk=0):
    return ('(footprint "%s" (layer "F.Cu") '
            '(pad "1" smd rect (at %s 0) (size %s 1) (layers "F.Cu" "F.Paste" "F.Mask")) '
            '(pad "2" smd rect (at %s 0) (size %s 1) (layers "F.Cu" "F.Paste" "F.Mask")) '
            '(fp_line (start 0 %s) (end 1 %s) (stroke (width .1) (type solid)) (layer "F.SilkS")))'
            % (name, shift, size, shift+pitch, size, silk, silk))


@pytest.mark.parametrize('pitch,size,accepted', [(2,.8,True),(2.0508,.8,False),(2,.9,False)])
def test_copper_equivalence_ignores_origin_and_silk_not_pitch_or_size(scratch_project, pitch, size, accepted):
    from part_selection_footprints import copper_equivalence
    reference, target = scratch_project/'ref.kicad_mod', scratch_project/'target.kicad_mod'
    reference.write_text(module('Old', shift=4, silk=2), encoding='utf-8')
    target.write_text(module('Standard', pitch=pitch, size=size, silk=9), encoding='utf-8')
    before = target.read_bytes()
    result = copper_equivalence(target, reference, scratch_project/'align')
    assert result['ok'] is accepted, result
    assert target.read_bytes() == before
    if accepted:
        assert result['max_error_mm'] <= .000005
        assert result['offset_mm'] == [4, 0]


def unification_fixture(app):
    root = app.project_root
    (root/'Local.pretty/Original.kicad_mod').write_text(module('Original', shift=3), encoding='utf-8')
    (root/'Local.pretty/Standard.kicad_mod').write_text(module('Standard', silk=4), encoding='utf-8')
    (root/'Local.pretty/BadPitch.kicad_mod').write_text(module('BadPitch', pitch=2.0508), encoding='utf-8')
    path=root/'test.kicad_sch'
    path.write_text('(kicad_sch '+''.join(
        '(symbol (property "Reference" "%s") (property "Value" "OPA_TEST") '
        '(property "Comment" "OPA_TEST") (property "Footprint" "Local:%s") (property "LCSC" "C100"))'
        % pair for pair in [('R1','Original'),('R2','Standard'),('R3','BadPitch')])+')', encoding='utf-8')
    records=load_overrides(app.overrides_path)
    for ref in ('R2','R3'):
        records[ref]={'stock_c_code':'C100','decision':'AcceptManual','review_status':'Confirmed','note':ref}
    save_overrides(app.overrides_path,records)
    return records


def test_batch_dry_run_and_atomic_apply_keep_material_confirmation_and_cad(footprint_app):
    from part_selection_footprint_unify import FootprintUnifier
    app=footprint_app
    before=unification_fixture(app)
    cad={p:p.read_bytes() for p in app.project_root.rglob('*') if p.is_file() and p!=app.overrides_path}
    service=FootprintUnifier(app)
    report=service.run(targets={'OPA_TEST':'Local:Standard'})
    assert load_overrides(app.overrides_path)==before
    assert [e['ref'] for e in report['changes']]==['R1'], report
    assert [e['ref'] for e in report['skipped']]==['R3']
    result=service.run(apply=True,targets={'OPA_TEST':'Local:Standard'})
    after=load_overrides(app.overrides_path)
    assert after['R1']['field_overrides']['footprint']=='Local:Standard'
    assert after['R1']['workbench_undo']['record']==before['R1']
    for ref in before:
        assert after[ref]['review_status']==before[ref]['review_status']
        for key in ('note','decision','stock_c_code','manual_c_code','accepted_c_code'):
            assert after[ref].get(key)==before[ref].get(key)
    assert after['R3']==before['R3']
    assert Path(result['backup']).is_file()
    assert all(p.read_bytes()==data for p,data in cad.items())
    assert not list(app.project_root.rglob('*_mapped*'))


def test_batch_rejects_stale_source_without_writing_any_records(footprint_app, monkeypatch):
    from part_selection_footprint_unify import FootprintUnifier
    from part_selection_design_source import DesignSource
    before=unification_fixture(footprint_app)
    monkeypatch.setattr(DesignSource,'validate',lambda *a:False)
    with pytest.raises(ValueError,match='变化|改变'):
        FootprintUnifier(footprint_app).run(apply=True,targets={'OPA_TEST':'Local:Standard'})
    assert load_overrides(footprint_app.overrides_path)==before


def test_batch_conflicting_codes_do_not_merge_or_clear_selection(footprint_app):
    from part_selection_footprint_unify import FootprintUnifier
    records=unification_fixture(footprint_app)
    records['R2']['stock_c_code']='C200'
    save_overrides(footprint_app.overrides_path,records)
    result=FootprintUnifier(footprint_app).run(apply=True,targets={'OPA_TEST':'Local:Standard'})
    assert not result['changes']
    assert load_overrides(footprint_app.overrides_path)==records


def test_reusing_equivalent_library_keeps_confirmation_without_a_copy(footprint_app):
    from test_row_footprint import prepare, choice
    app=footprint_app
    unification_fixture(app)
    preview=app.row_footprint({'refs':'R1','mode':'library'})['preview']
    app.footprint_choices.library_detail(preview['preview_id'],'Local:Standard')
    result=app.row_footprint({'footprint_choice':choice(preview,existing_id='Local:Standard')})
    assert result['library']['footprint']=='Local:Standard'
    assert result['library']['reused']
    assert load_overrides(app.overrides_path)['R1']['review_status']=='Confirmed'
    assert not list(app.project_root.rglob('*_mapped*'))


@pytest.mark.parametrize('edit', ['mask', 'mechanical', 'duplicate', 'renumber', 'drill', 'unknown'])
def test_equivalence_rejects_other_copper_differences(scratch_project, edit):
    from part_selection_footprints import copper_equivalence
    a,b=scratch_project/'a.kicad_mod',scratch_project/'b.kicad_mod'
    original=module('A')
    changed=original
    if edit=='mask':
        changed=changed[:-1]+' (solder_mask_margin .05))'
    elif edit=='mechanical':
        extra=' (pad "" np_thru_hole circle (at %s 0) (size .8 .8) (drill .8) (layers "*.Cu" "*.Mask")))'
        original=original[:-1]+extra % '3'
        changed=changed[:-1]+extra % '4'
    elif edit=='duplicate':
        changed=changed.replace('pad "2"','pad "1"')
    elif edit=='renumber':
        changed=changed.replace('pad "2"','pad "3"')
    elif edit=='unknown':
        changed=changed[:-1]+' (unknown_geometry (size 1 2)))'
    else:
        changed=changed.replace('(size 0.8 1)', '(size 0.8 1) (drill .3)')
    a.write_text(original,encoding='utf-8');b.write_text(changed,encoding='utf-8')
    assert not copper_equivalence(b,a,scratch_project/'out')['ok']


def test_equivalence_supports_numbered_rotation_not_mirroring(scratch_project):
    from part_selection_footprints import copper_equivalence
    a,b=scratch_project/'a.kicad_mod',scratch_project/'b.kicad_mod'
    a.write_text(module('A'),encoding='utf-8')
    b.write_text(module('B').replace('(at 0 0)','(at 5 4 90)').replace('(at 2 0)','(at 5 2 90)'),encoding='utf-8')
    assert copper_equivalence(b,a,scratch_project/'out')['ok']


def test_batch_uses_saved_instance_identity_even_when_bound_library_is_missing(footprint_app, monkeypatch):
    from part_selection_footprint_unify import FootprintUnifier
    app=footprint_app
    unification_fixture(app)
    (app.project_root/'project.kicad_pcb').write_text('(kicad_pcb)')
    snapshot=app.project_root.parent/'saved.kicad_mod'
    snapshot.write_text(module('Saved',shift=3),encoding='utf-8')
    (app.project_root/'Local.pretty/Original.kicad_mod').unlink()
    monkeypatch.setattr('part_selection_footprint_geometry.board_references',lambda *args:
        [{'ref':'R1','path':str(snapshot),'footprint':'Local:PCB_Original'}])
    result=FootprintUnifier(app).run(apply=True,targets={'OPA_TEST':'Local:Standard'})
    assert [r['ref'] for r in result['changes']]==['R1']
    assert load_overrides(app.overrides_path)['R1']['footprint_change']['before']=='Local:PCB_Original'


@pytest.mark.parametrize('conflict', ['code','review'])
def test_batch_final_group_includes_planned_arrivals_at_unused_target(footprint_app, conflict):
    from part_selection_footprint_unify import FootprintUnifier
    app=footprint_app
    records=unification_fixture(app)
    (app.project_root/'Local.pretty/Third.kicad_mod').write_text(module('Third'),encoding='utf-8')
    if conflict=='code':
        records['R2'].update(stock_c_code='C200',manual_c_code='C200',accepted_c_code='C200')
    else:
        records['R2']['review_status']='Pending'
    save_overrides(app.overrides_path,records)
    result=FootprintUnifier(app).run(apply=True,targets={'OPA_TEST':'Local:Third'})
    assert [r['ref'] for r in result['changes']]==['R1'],result
    app.rows(selected_bom='__schematic__',include_stock=False)
    after=load_overrides(app.overrides_path)
    assert after['R1']['review_status']=='Confirmed'
    assert after['R1']['stock_c_code']=='C100'
    assert after['R2']['review_status']==records['R2']['review_status']
    assert after['R2']['stock_c_code']==records['R2']['stock_c_code']
    assert after['R2'].get('decision')!='ClearSelection'

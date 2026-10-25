import pytest
import hashlib
from pathlib import Path

from part_selection_pad_mapping import validate_mapping, adapt_numbers
from part_selection_footprint_geometry import estimate_mapped_alignment
from part_selection_footprints import FootprintChoices
from part_selection_pad_mapping import symbol_terminals, verify_extra_terminals, dropped_terminal_report
from kicad_sexpr import parse_sexpr


def test_explicit_drop_is_distinct_from_excluding_an_alignment_point():
    plan = {'schema': 1, 'pairs': [
        {'source': '1', 'target': 'A', 'align': True},
        {'source': '2', 'target': 'B', 'align': True},
        {'source': '3', 'target': 'C', 'align': False}], 'dropped': ['4']}
    result = validate_mapping(plan, ['1', '2', '3', '4'], ['A', 'B', 'C', '9'])
    assert result['dropped'] == ['4']
    assert result['extras'] == ['9']
    assert result['number_map'] == {'A': '1', 'B': '2', 'C': '3'}
    assert result['alignment_pairs'] == [('1', 'A'), ('2', 'B')]
    text = '(footprint "example" (pad "A" smd rect (at 0 0)) (pad "9" smd rect (at 5 0)))'
    assert '(pad "1"' in adapt_numbers(text, result)
    assert '(pad "9"' in adapt_numbers(text, result)
    with pytest.raises(ValueError, match='unaccounted'):
        validate_mapping({**plan, 'dropped': []}, ['1', '2', '3', '4'], ['A', 'B', 'C', '9'])


def test_target_extra_is_preserved_and_dropped_source_is_explicit():
    plan = validate_mapping(
        {'schema': 1, 'pairs': [
            {'source': '1', 'target': 'A', 'align': True},
            {'source': '2', 'target': 'B', 'align': True}], 'dropped': ['3']},
        ['1', '2', '3'], ['A', 'B', '9'])
    assert plan['extras'] == ['9']
    assert '9' not in plan['number_map']


def test_mapped_alignment_ignores_target_extra_and_dropped_source():
    plan = validate_mapping(
        {'schema': 1, 'pairs': [
            {'source': '1', 'target': 'A', 'align': True},
            {'source': '2', 'target': 'B', 'align': True}], 'dropped': ['3']},
        ['1', '2', '3'], ['A', 'B', '9'])
    original = '(footprint "old" (pad "1" smd rect (at 0 0)) (pad "2" smd rect (at 2 0)) (pad "3" smd rect (at 4 0)))'
    target = '(footprint "new" (pad "A" smd rect (at 10 5)) (pad "B" smd rect (at 12 5)) (pad "9" smd rect (at 14 5)))'
    result = estimate_mapped_alignment(target, original, plan)
    assert result['ok']
    assert result['max_error_mm'] == pytest.approx(0)


def fp(name, numbers):
    return '(footprint "'+name+'" (version 20241229) (layer "F.Cu") '+''.join(
        f'(pad "{n}" smd rect (at {i} 0) (size .5 .5) (layers "F.Cu" "F.Paste" "F.Mask"))'
        for i,n in enumerate(numbers))+')'


def test_extra_pad_cannot_reconnect_a_schematic_terminal_on_native_update():
    page = parse_sexpr('(kicad_sch (lib_symbols (symbol "Local:IC" (symbol "IC_1_1" '
        '(pin passive line (number "1")) (pin passive line (number "9")))) ) '
        '(symbol (lib_id "Local:IC") (unit 1)))', 'kicad_sch')
    terminals = symbol_terminals(page.child('symbol'), page)
    assert terminals == ['1', '9']
    verify_extra_terminals(['10'], terminals)
    with pytest.raises(ValueError, match='已有端子同号'):
        verify_extra_terminals(['9'], terminals)
    with pytest.raises(ValueError, match='无法核验'):
        verify_extra_terminals(['9'], None)
    verify_extra_terminals([], None)


@pytest.mark.parametrize('net', ['(net 3 "signal")', '(net "signal")'])
def test_dropped_terminal_report_does_not_confuse_membership_with_physical_contact(net):
    code = '3' if '3 ' in net else '"signal"'
    text = '(kicad_pcb (footprint "Local:Old" (property "Reference" "U1") (pad "9" smd rect '+net+')) '
    text += '(segment (net '+code+')) (via (net '+code+')) (segment (net 0)))'
    report = dropped_terminal_report(text, 'U1', ['9'])[0]
    assert report['net_name'] == 'signal' and report['net_assigned']
    assert report['same_net_objects'] == {'segment':1,'arc':0,'via':1,'zone':0}
    assert '未核验物理接触' in report['physical_connection']


@pytest.mark.parametrize('net,name,assigned', [('', '', False), ('(net "")', '', False),
    ('(net 0 "")', '', False), ('(net "0")', '0', True), ('(net "123")', '123', True)])
def test_terminal_net_report_distinguishes_empty_and_numeric_native_names(net, name, assigned):
    text = '(kicad_pcb (footprint "Local:Old" (property "Reference" "U1") (pad "9" smd rect '+net+')))'
    report = dropped_terminal_report(text, 'U1', ['9'])[0]
    assert report['net_name'] == name and report['net_assigned'] is assigned


@pytest.mark.parametrize('source_count,target_count', [(8,10),(10,8)])
@pytest.mark.parametrize('mode', ['existing','new'])
def test_library_mapping_publishes_copy_with_evidence_and_explicit_drop_consent(scratch_project, source_count, target_count, mode):
    root = scratch_project
    pretty = root/'Local.pretty'; pretty.mkdir()
    original=pretty/'Old.kicad_mod'; target=pretty/'New.kicad_mod'
    original.write_text(fp('Old',[str(n) for n in range(1,source_count+1)]),encoding='utf-8')
    target.write_text(fp('New',[str(n) for n in range(1,target_count+1)]),encoding='utf-8')
    (root/'fp-lib-table').write_text('(fp_lib_table (lib (name "Local") (type "KiCad") (uri "${KIPRJMOD}/Local.pretty")))',encoding='utf-8')
    before={p:p.read_bytes() for p in (original,target)}
    before_table=(root/'fp-lib-table').read_bytes()
    service=FootprintChoices(root)
    preview=service.prepare_library(['Local:Old'])
    service.library_detail(preview['preview_id'],'Local:New')
    mapping={'schema':1,'pairs':[{'source':str(n),'target':str(n),'align':True} for n in range(1,min(source_count,target_count)+1)],
             'dropped':[str(n) for n in range(target_count+1,source_count+1)]}
    library='Local' if mode=='existing' else 'AdaptedLib'
    published=pretty/'Adapted.kicad_mod' if mode=='existing' else root/'Libraries/DSMU/AdaptedLib.pretty/Adapted.kicad_mod'
    choice={'preview_id':preview['preview_id'],'source':'existing','existing_id':'Local:New','reference_id':'Local:Old',
            'target_mode':mode,'target_library':library,'name':'Adapted','mapping':mapping,
            'alignment_transform':{'rotation_deg':0,'offset_mm':[0,0]},'acknowledged':True}
    if mapping['dropped']:
        with pytest.raises(ValueError,match='明确确认放弃'):
            service.commit(choice)
    choice['dropped_acknowledged']=True
    result=service.commit(choice)
    assert result['footprint']==library+':Adapted'
    assert set(result['adaptation']['mapping']['dropped'])==set(mapping['dropped'])
    assert len(result['adaptation']['mapping']['extras'])==max(0,target_count-source_count)
    assert all(p.read_bytes()==data for p,data in before.items())
    evidence=service.change_evidence(choice,['U1'],{'U1':'Local:Old'},result)['U1']
    assert evidence['adaptation']['mapping']['pairs']==mapping['pairs']
    assert evidence['target']['hash']==hashlib.sha256(published.read_bytes()).hexdigest()
    assert service.commit(choice)==result
    service.rollback(preview['preview_id'])
    assert not published.exists()
    assert (root/'fp-lib-table').read_bytes()==before_table
    assert all(p.read_bytes()==data for p,data in before.items())


@pytest.mark.parametrize('problem',['duplicate_target','unaccounted','collision','empty_align','bad_bool','dropped_number_reused'])
def test_invalid_mapping_never_becomes_an_adapted_footprint(problem):
    plan={'schema':1,'pairs':[{'source':'1','target':'A','align':True},{'source':'2','target':'B','align':True}],'dropped':[]}
    source,target=['1','2'],['A','B','9']
    if problem=='duplicate_target':plan['pairs'][1]['target']='A'
    elif problem=='unaccounted':source.append('3')
    elif problem=='collision':target.append('1')
    elif problem=='empty_align':
        for pair in plan['pairs']:pair['align']=False
    elif problem=='bad_bool':plan['pairs'][0]['align']='false'
    else:source.append('9');plan['dropped']=['9']
    with pytest.raises(ValueError):validate_mapping(plan,source,target)


@pytest.mark.parametrize('head', ['net_tie_pad_groups', 'jumper_pad_groups'])
def test_renumbering_rejects_electrical_groups_without_changing_the_source(head):
    source = '(footprint "Grouped" ('+head+' "A,B") (pad "A" smd rect) (pad "B" smd rect))'
    with pytest.raises(ValueError, match='分组'):
        adapt_numbers(source, {'number_map':{'A':'1','B':'2'}})
    assert adapt_numbers(source, {'number_map':{'A':'A','B':'B'}}) == source


def test_mapped_library_protects_pcb_baseline_id_and_alias_path(scratch_project):
    pretty = scratch_project/'Local.pretty';pretty.mkdir()
    for name in ('Old', 'Intermediate', 'Target'):
        (pretty/(name+'.kicad_mod')).write_text(fp(name,['1','2']),encoding='utf-8')
    (scratch_project/'fp-lib-table').write_text('(fp_lib_table '+''.join(
        f'(lib (name "{name}") (type "KiCad") (uri "${{KIPRJMOD}}/Local.pretty"))'
        for name in ('Local','Alias'))+')',encoding='utf-8')
    service = FootprintChoices(scratch_project)
    preview = service.prepare_library(['Local:Intermediate'])
    service.library_detail(preview['preview_id'],'Local:Target')
    state=service.previews[preview['preview_id']]
    baseline=next(e for e in state['existing'] if e['id']=='Local:Intermediate')
    baseline['placement']={'footprint':'Local:Old'}
    mapping={'schema':1,'pairs':[{'source':n,'target':n,'align':True} for n in ('1','2')],'dropped':[]}
    original=(pretty/'Old.kicad_mod').read_bytes()
    for library in ('Local','Alias'):
        with pytest.raises(ValueError, match='原'):
            service.commit({'preview_id':preview['preview_id'],'source':'existing','existing_id':'Local:Target',
                'reference_id':'Local:Intermediate','mapping':mapping,'acknowledged':True,
                'target_mode':'existing','target_library':library,'name':'Old',
                'alignment_transform':{'rotation_deg':0,'offset_mm':[0,0]}})
    assert (pretty/'Old.kicad_mod').read_bytes()==original


def test_auto_generated_manual_mapping_cannot_use_a_recovery_reference(scratch_project):
    pretty=scratch_project/'Local.pretty';pretty.mkdir()
    for name in ('Old','Recovery','Target'):
        (pretty/(name+'.kicad_mod')).write_text(fp(name,['1','2']),encoding='utf-8')
    (scratch_project/'fp-lib-table').write_text('(fp_lib_table (lib (name "Local") (type "KiCad") (uri "${KIPRJMOD}/Local.pretty")))')
    service=FootprintChoices(scratch_project)
    preview=service.prepare_library(['Local:Old'])
    for name in ('Recovery','Target'):
        service.library_detail(preview['preview_id'],'Local:'+name)
    state=service.previews[preview['preview_id']]
    state['alignment_ids']=['Local:Old','Local:Recovery']
    target=next(e for e in state['existing'] if e['id']=='Local:Target')
    with pytest.raises(ValueError,match='原实例或原绑定库'):
        service._alignment(state,'Local:Recovery',{'rotation_deg':0,'offset_mm':[0,0]},target=target)


def test_manual_movement_and_geometry_report_excludes_unnumbered_mechanical_pads():
    from part_selection_pcb_geometry import mapped_pad_changes
    source={'pads':[{'number':'1','position_mm':[0,0],'geometry':{'shape':'circle'}},
        {'number':'','position_mm':[3,4]},{'number':'','position_mm':[6,8]}]}
    target={'pads':[{'number':'1','position_mm':[0,0],'geometry':{'shape':'rect'}},
        {'number':'','position_mm':[30,40]}]}
    mapping={'pairs':[{'source':'1','target':'1','align':True}]}
    report=mapped_pad_changes(source,target,mapping)
    assert report['max_error_mm']==report['center_error_mm']==0
    assert len(report['pad_errors'])==1
    assert report['differences']==[{'number':'1','fields':['geometry'],
        'before':{'geometry':{'shape':'circle'}},'after':{'geometry':{'shape':'rect'}}}]

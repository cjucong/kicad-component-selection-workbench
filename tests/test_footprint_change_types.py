"""Explicit footprint intent survives save/undo; names never establish intent."""
from copy import deepcopy

from part_selection_overrides import load_overrides, save_overrides
from test_row_footprint import footprint_app, prepare, choice


def test_explicit_rename_has_bound_evidence(footprint_app):
    app = footprint_app
    preview = prepare(app, 'rename')
    app.footprint_choices.library_detail(preview['preview_id'], 'Local:Original')
    app.row_footprint({'footprint_choice': choice(preview, 'rename')})
    record = load_overrides(app.overrides_path)['R1']
    evidence = record.get('footprint_change')
    assert evidence is not None, 'Rename intent must be retained, not inferred from changed names'
    assert evidence['schema'] == 1
    assert evidence['operation'] == 'rename'
    assert evidence['before'] == 'Local:Original'
    assert evidence['after'] == 'Local:Renamed'
    assert evidence['source']['geometry_hash']
    assert evidence['target']['hash']
    assert record['review_status'] == 'Confirmed'


def test_explicit_library_replace_retains_intent(footprint_app):
    app = footprint_app
    preview = prepare(app)
    app.footprint_choices.library_detail(preview['preview_id'], 'Local:Other')
    app.row_footprint({'footprint_choice': choice(preview)})
    evidence = load_overrides(app.overrides_path)['R1'].get('footprint_change')
    assert evidence is not None, 'Replacement needs intent even when target names match'
    assert evidence['operation'] == 'replace'
    assert evidence['after'] == 'Local:Other'


def test_manual_footprint_edit_cannot_reuse_old_rename_intent(footprint_app):
    app = footprint_app
    records = load_overrides(app.overrides_path)
    records['R1']['footprint_change'] = {'schema':1, 'operation':'rename',
        'before':'Local:Before', 'after':'Local:Original', 'source':{}, 'target':{}}
    save_overrides(app.overrides_path, records)
    app.save_field_override({'refs':'R1','bom':'__schematic__',
        'field_overrides':{'footprint':'Local:Other'}})
    record = load_overrides(app.overrides_path)['R1']
    assert not record.get('footprint_change'), 'Manual footprint edit must invalidate old rename evidence'
    assert record['workbench_undo']['record']['footprint_change']['operation'] == 'rename'


def test_unrelated_field_edit_keeps_explicit_intent(footprint_app):
    app = footprint_app
    records = load_overrides(app.overrides_path)
    evidence = {'schema':1, 'operation':'rename', 'before':'Local:Before',
        'after':'Local:Original', 'source':{'geometry_hash':'fixture'}, 'target':{}}
    records['R1']['footprint_change'] = deepcopy(evidence)
    save_overrides(app.overrides_path, records)
    app.save_field_override({'refs':'R1','bom':'__schematic__','field_overrides':{'value':'220k'}})
    assert load_overrides(app.overrides_path)['R1'].get('footprint_change') == evidence


def test_old_name_difference_is_unknown():
    from part_selection_footprint_changes import classify_footprint_change
    record = {'field_overrides':{'footprint':'Local:Renamed'}}
    assert classify_footprint_change(record, {'footprint':'Local:Original'})['kind'] == 'unknown'


def test_same_name_replace_is_not_rename():
    from part_selection_footprint_changes import classify_footprint_change
    record = {'field_overrides':{'footprint':'Local:Original'}, 'footprint_change':{
        'schema':1,'operation':'replace','before':'Local:Original','after':'Local:Original',
        'source':{'geometry_hash':'old'},'target':{'hash':'new'}}}
    assert classify_footprint_change(record, {'footprint':'Local:Original','geometry_hash':'old'})['kind'] == 'replace'


def test_stale_geometry_rejects_rename():
    from part_selection_footprint_changes import classify_footprint_change
    record = {'field_overrides':{'footprint':'Local:Renamed'}, 'footprint_change':{
        'schema':1,'operation':'rename','before':'Local:Original','after':'Local:Renamed',
        'source':{'geometry_hash':'old'},'target':{'hash':'new'}}}
    assert classify_footprint_change(record, {'footprint':'Local:Original','geometry_hash':'edited'})['kind'] == 'unknown'
    assert classify_footprint_change(record, {'footprint':'Local:Original','geometry_hash':'old'})['kind'] == 'rename'
    assert classify_footprint_change(record, {'footprint':'Local:Renamed','geometry_hash':'old'})['kind'] == 'none'


def test_same_library_reselection_still_records_replace_operation(footprint_app):
    app = footprint_app
    preview = prepare(app)
    app.footprint_choices.library_detail(preview['preview_id'], 'Local:Original')
    app.row_footprint({'footprint_choice':choice(preview, existing_id='Local:Original')})
    assert load_overrides(app.overrides_path)['R1']['footprint_change']['operation'] == 'replace'


def test_live_board_copper_evidence_ignores_labels_but_not_pad_changes():
    from part_selection_footprint_changes import board_copper_hashes
    text = '''(kicad_pcb (footprint "Local:Old" (layer "F.Cu") (at 10 20 90)
      (property "Reference" "R1") (property "Comment" "resistor")
      (fp_text user "label" (at 1 2))
      (pad "1" smd rect (at -1 0 90) (size 1 2) (layers "F.Cu") (uuid "one") (net 1 "N1"))))'''
    original = board_copper_hashes(text, ['R1'])['R1']
    labels = text.replace('Local:Old', 'Other:Renamed').replace('resistor','new').replace('(at 1 2)', '(at 5 6)')
    labels = labels.replace('(size 1 2)', '(size 1.000 2.0)').replace('uuid "one"', 'uuid "changed"')
    assert board_copper_hashes(labels, ['R1'])['R1'] == original
    assert board_copper_hashes(text.replace('(size 1 2)', '(size 1 3)'), ['R1'])['R1'] != original
    record = {'field_overrides':{'footprint':'Other:Renamed'}, 'footprint_change':{
        'schema':1,'operation':'rename','before':'Local:Old','after':'Other:Renamed',
        'source':{'instance_geometry_hash':original}}}
    from part_selection_footprint_changes import classify_footprint_change
    assert classify_footprint_change(record, {'footprint':'Local:Old','instance_geometry_hash':original})['kind'] == 'rename'
    assert classify_footprint_change(record, {'footprint':'Local:Old','instance_geometry_hash':'changed'})['kind'] == 'unknown'

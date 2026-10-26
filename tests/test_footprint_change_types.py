"""Explicit footprint intent survives save/undo; names never establish intent."""
from copy import deepcopy
import pytest

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
    row = app.rows(selected_bom='__schematic__', include_stock=False)[0]
    assert row['footprint_intent_by_ref']['R1'] == 'rename'


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


def test_completed_rename_does_not_reapply_historical_geometry():
    from part_selection_footprint_changes import classify_footprint_change
    record = {'field_overrides':{'footprint':'Local:New'}, 'footprint_change':{
        'schema':1,'operation':'rename','before':'Local:Old','after':'Local:New',
        'source':{'board':{'hash':'old'},'instance_source_hash':'historical'}}}
    assert classify_footprint_change(record, {'footprint':'Local:New',
        'instance_source_hash':'later-edit'})['kind'] == 'none'
    assert classify_footprint_change(record, {'footprint':'Local:Old',
        'instance_source_hash':'later-edit'})['kind'] == 'unknown'


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


def test_rename_source_binds_instance_uuid_and_graphics_not_editable_labels():
    from part_selection_footprint_changes import board_copper_hashes, classify_footprint_change
    text = '''(kicad_pcb (footprint "Local:Old" (uuid "instance") (layer "F.Cu") (at 10 20)
      (property "Reference" "R1") (fp_text user "label" (at 1 2))
      (fp_line (start 0 0) (end 1 1) (stroke (width 0.1) (type default)) (layer "F.SilkS"))))'''
    original = board_copper_hashes(text, ['R1'], include_instance=True)['R1']
    for changed in (text.replace('"instance"','"other"'), text.replace('(end 1 1)','(end 2 2)')):
        assert board_copper_hashes(changed, ['R1'], include_instance=True)['R1'] != original
    assert board_copper_hashes(text.replace('(at 1 2)','(at 3 4)'), ['R1'], include_instance=True)['R1'] == original
    record = {'field_overrides':{'footprint':'Local:New'}, 'footprint_change':{
        'schema':1,'operation':'rename','before':'Local:Old','after':'Local:New',
        'source':{'instance_source_hash':original}}}
    assert classify_footprint_change(record, {'footprint':'Local:Old','instance_source_hash':original})['kind'] == 'rename'
    assert classify_footprint_change(record, {'footprint':'Local:Old','instance_source_hash':'different'})['kind'] == 'unknown'


@pytest.mark.parametrize('current', ['Local:Old', 'Local:New'])
@pytest.mark.parametrize('old_style,new_style', [('default','solid'), ('solid','default')])
def test_rename_stroke_aliases_preserve_legacy_identity(current, old_style, new_style):
    from part_selection_footprint_changes import board_copper_hashes, board_source_hash_variants, classify_footprint_change
    text = '''(kicad_pcb (footprint "Local:Old" (uuid "instance") (layer "B.Cu") (at 10 20 90)
      (property "Reference" "R1") (path "/sheet/symbol")
      (fp_line (start 0 0) (end 1 1) (stroke (width 0.1) (type STYLE)) (layer "B.SilkS"))
      (pad "1" smd rect (at 1 0) (size 1 2) (layers "B.Cu"))))'''
    before = text.replace('STYLE', old_style)
    after = text.replace('STYLE', new_style).replace('Local:Old', current)
    source_hash = board_copper_hashes(before, ['R1'], include_instance=True)['R1']
    variants = board_source_hash_variants(after, ['R1'])['R1']
    record = {'field_overrides':{'footprint':'Local:New'}, 'footprint_change':{
        'schema':1, 'operation':'rename', 'before':'Local:Old', 'after':'Local:New',
        'source':{'instance_source_hash':source_hash}}}
    live = {'footprint':current, 'instance_source_hash':variants[0], 'instance_source_hash_variants':variants}
    assert classify_footprint_change(record, live)['kind'] == ('rename' if current == 'Local:Old' else 'none')
    for old, new in [('"instance"','"other"'), ('/sheet/symbol','/sheet/other'),
                     ('(at 10 20 90)', '(at 10 21 90)'), ('(layer "B.Cu")', '(layer "F.Cu")'),
                     ('(size 1 2)', '(size 1 3)'), ('(end 1 1)', '(end 2 2)'),
                     ('(width 0.1)', '(width 0.2)'), ('(type '+new_style+')', '(type dash)'),
                     ('(pad "1"', '(pad "2"')]:
        changed = board_source_hash_variants(after.replace(old, new), ['R1'])['R1']
        assert classify_footprint_change(record, {**live, 'instance_source_hash':changed[0],
            'instance_source_hash_variants':changed})['kind'] == ('unknown' if current == 'Local:Old' else 'none'), (old, new)


def test_new_rename_evidence_canonicalizes_mixed_stroke_aliases_only():
    from part_selection_footprint_changes import board_copper_hashes, board_source_hash_variants
    text = '''(kicad_pcb (footprint "Local:Old" (uuid "instance") (layer "F.Cu")
      (property "Reference" "R1")
      (fp_rect (start 0 0) (end 1 1) (stroke (width 0.1) (type default)) (layer "F.SilkS"))
      (fp_line (start 2 2) (end 3 3) (stroke (width 0.2) (type solid)) (layer "F.Fab"))
      (pad "1" smd rect (at 0 0) (size 1 2) (layers "F.Cu") (custom (type default)))))'''
    canonical = board_copper_hashes(text, ['R1'], include_instance=True, stroke_style='solid')['R1']
    saved = text.replace('(type default)) (layer', '(type solid)) (layer')
    assert canonical in board_source_hash_variants(saved, ['R1'])['R1']
    assert canonical not in board_source_hash_variants(saved.replace('(custom (type default))',
        '(custom (type solid))'), ['R1'])['R1'], 'Only a stroke/type alias may be normalized'


@pytest.mark.parametrize('problem', ['', 'pad', 'uuid', 'width', 'board_hash', 'instance_hash', 'missing'])
def test_mixed_legacy_source_requires_verified_original_board(scratch_project, problem):
    import hashlib
    from part_selection_footprint_changes import board_copper_hashes, board_source_hash_variants, recover_legacy_source_hashes
    old = '''(kicad_pcb (footprint "Local:Old" (uuid "instance") (layer "F.Cu") (at 1 2)
      (property "Reference" "R1")
      (fp_line (start 0 0) (end 1 1) (stroke (width 0.1) (type default)) (layer "F.SilkS"))
      (fp_line (start 2 0) (end 3 1) (stroke (width 0.1) (type solid)) (layer "F.Fab"))
      (pad "1" smd rect (at 0 0) (size 1 2) (layers "F.Cu"))))'''
    saved = old.replace('(type default)', '(type solid)')
    source_hash = board_copper_hashes(old, ['R1'], include_instance=True)['R1']
    original = scratch_project/'original.kicad_pcb'
    if problem != 'missing':
        original.write_bytes(old.encode())
    source = {'instance_source_hash':source_hash, 'board':{'hash':hashlib.sha256(old.encode()).hexdigest()}}
    if problem == 'board_hash':source['board']['hash'] = 'other-board'
    elif problem == 'instance_hash':source['instance_source_hash'] = 'other-instance'
    elif problem == 'pad':saved = saved.replace('(size 1 2)', '(size 1 3)')
    elif problem == 'uuid':saved = saved.replace('"instance"', '"other"')
    elif problem == 'width':saved = saved.replace('(width 0.1)', '(width 0.2)')
    live = board_source_hash_variants(saved, ['R1'])
    assert source_hash not in live['R1'], 'Fixture must exercise mixed legacy spelling recovery'
    cache = {}
    recover_legacy_source_hashes({'R1':source}, live, [original], cache)
    assert (source_hash in live['R1']) is (not problem)
    # A cached source proof must compare the next current state again.
    changed = board_source_hash_variants(saved.replace('(at 1 2)', '(at 3 4)'), ['R1'])
    recover_legacy_source_hashes({'R1':source}, changed, [], cache)
    assert source_hash not in changed['R1']


def test_source_alias_variants_parse_the_board_once(monkeypatch):
    import kicad_sexpr
    from part_selection_footprint_changes import board_source_hash_variants
    calls = []
    parse = kicad_sexpr.parse_sexpr
    def tracked(*args):
        calls.append(True)
        return parse(*args)
    monkeypatch.setattr(kicad_sexpr, 'parse_sexpr', tracked)
    board_source_hash_variants('(kicad_pcb (footprint "Local:Old" (property "Reference" "R1")))', ['R1'])
    assert len(calls) == 1


@pytest.mark.parametrize('problem', ['', 'larger', 'pad', 'pose', 'uuid', 'copper', 'unknown',
                                    'width', 'board_hash', 'instance_hash', 'missing'])
def test_native_reload_silkscreen_rounding_requires_exact_source(scratch_project, problem):
    import hashlib
    from part_selection_footprint_changes import (
        board_copper_hashes, board_source_hash_variants, recover_legacy_source_hashes)
    old = '''(kicad_pcb (footprint "Local:Old" (uuid "instance") (layer "F.Cu") (at 1 2)
      (property "Reference" "R1") (path "/sheet/symbol")
      (fp_poly (pts (xy 107.036042 -21.345639) (xy 111 -18) (xy 107 -18))
        (stroke (width 0.15) (type solid)) (fill no) (layer "F.SilkS")
        (future_metadata (xy 0 0)))
      (pad "1" smd rect (at 0 0) (size 1 2) (layers "F.Cu"))))'''
    saved = old.replace('-21.345639', '-21.345638')
    if problem == 'copper':
        old = old.replace('F.SilkS', 'F.Cu')
        saved = saved.replace('F.SilkS', 'F.Cu')
    source_hash = board_copper_hashes(old, ['R1'], include_instance=True)['R1']
    original = scratch_project/'original.kicad_pcb'
    if problem != 'missing':
        original.write_bytes(old.encode())
    source = {'instance_source_hash':source_hash,
              'board':{'hash':hashlib.sha256(old.encode()).hexdigest()}}
    if problem == 'larger':saved = saved.replace('-21.345638', '-21.345637')
    elif problem == 'pad':saved = saved.replace('(at 0 0)', '(at 0.000001 0)')
    elif problem == 'pose':saved = saved.replace('(at 1 2)', '(at 1.000001 2)')
    elif problem == 'uuid':saved = saved.replace('"instance"', '"other"')
    elif problem == 'width':saved = saved.replace('(width 0.15)', '(width 0.150001)')
    elif problem == 'unknown':saved = saved.replace('(future_metadata (xy 0 0))',
                                                 '(future_metadata (xy 0.000001 0))')
    elif problem == 'board_hash':source['board']['hash'] = 'wrong-board'
    elif problem == 'instance_hash':source['instance_source_hash'] = 'wrong-instance'
    live = board_source_hash_variants(saved, ['R1'])
    cache = {}
    # A rejected prior preview must not prevent recovery after undoing its edit.
    rejected = saved.replace('(size 1 2)', '(size 1 3)')
    rejected_hashes = board_source_hash_variants(rejected, ['R1'])
    recover_legacy_source_hashes({'R1':source}, rejected_hashes, [original], cache,
                                 live_text=rejected)
    assert source_hash not in rejected_hashes['R1']
    recover_legacy_source_hashes({'R1':source}, live, [original], cache, live_text=saved)
    assert (source_hash in live['R1']) is (not problem)
    changed = saved.replace('(size 1 2)', '(size 1 2.000001)')
    changed_hashes = board_source_hash_variants(changed, ['R1'])
    recover_legacy_source_hashes({'R1':source}, changed_hashes, [], cache, live_text=changed)
    assert source_hash not in changed_hashes['R1'], 'Cached proof must bind the exact live state'


@pytest.mark.parametrize('problem', ['', 'board_hash', 'instance_hash', 'missing',
                                    'pad', 'uuid', 'width', 'larger', 'unknown'])
def test_reload_recovers_canonical_evidence_from_verified_mixed_strokes(scratch_project, problem):
    import hashlib
    from part_selection_footprint_changes import (
        board_copper_hashes, board_source_hash_variants, recover_legacy_source_hashes)

    old = '''(kicad_pcb (footprint "Local:Old" (uuid "instance") (layer "F.Cu") (at 1 2)
      (property "Reference" "R1") (path "/sheet/symbol")
      (fp_line (start 0 0) (end 5 0) (stroke (width 0.15) (type default))
        (layer "F.SilkS") (future_metadata (xy 0 0)))
      (fp_line (start 2 0) (end 1 0) (stroke (width 0.15) (type solid)) (layer "F.SilkS"))
      (pad "1" smd rect (at 0 0) (size 1 2) (layers "F.Cu"))))'''
    saved = old.replace('(start 0 0)', '(start 0.000001 0)')
    source_hash = board_copper_hashes(old, ['R1'], include_instance=True, stroke_style='solid')['R1']
    assert source_hash != board_copper_hashes(old, ['R1'], include_instance=True)['R1'], (
        'Exercise new canonical evidence, not a legacy raw-source hash')
    original = scratch_project / 'canonical-original.kicad_pcb'
    if problem != 'missing':
        original.write_bytes(old.encode())
    source = {'instance_source_hash': source_hash,
              'board': {'hash': hashlib.sha256(old.encode()).hexdigest()}}
    if problem == 'board_hash':
        source['board']['hash'] = 'wrong-board'
    elif problem == 'instance_hash':
        source['instance_source_hash'] = 'wrong-instance'
    elif problem == 'pad':
        saved = saved.replace('(at 0 0)', '(at 0.000001 0)')
    elif problem == 'uuid':
        saved = saved.replace('"instance"', '"other"')
    elif problem == 'width':
        saved = saved.replace('(width 0.15)', '(width 0.150001)', 1)
    elif problem == 'larger':
        saved = saved.replace('(start 0.000001 0)', '(start 0.000002 0)')
    elif problem == 'unknown':
        saved = saved.replace('(future_metadata (xy 0 0))',
                              '(future_metadata (xy 0.000001 0))')
    live = board_source_hash_variants(saved, ['R1'])
    assert source_hash not in live['R1'], 'The verified-source recovery path must be exercised'
    recover_legacy_source_hashes({'R1': source}, live, [original], {}, live_text=saved)
    assert (source['instance_source_hash'] in live['R1']) is (not problem)


@pytest.mark.parametrize('problem', ['', 'larger', 'pad', 'uuid', 'layer', 'width',
                                    'missing_duplicate', 'reused_duplicate'])
def test_reload_graphics_pair_as_multiset_preserving_duplicate_counts(scratch_project, problem):
    import hashlib
    from part_selection_footprint_changes import (
        board_copper_hashes, board_source_hash_variants, recover_legacy_source_hashes)

    first = ('(fp_line (start 0 0) (end 5 0) '
             '(stroke (width 0.15) (type solid)) (layer "F.SilkS"))')
    second = first.replace('(start 0 0)', '(start 0.000001 0)').replace('(end 5 0)', '(end 1 0)')
    moved = first.replace('(start 0 0)', '(start 0.000001 0)')
    prefix = ('(kicad_pcb (footprint "Local:Old" (uuid "instance") (layer "F.Cu") (at 1 2) '
              '(property "Reference" "R1") (path "/sheet/symbol") ')
    suffix = '(pad "1" smd rect (at 0 0) (size 1 2) (layers "F.Cu"))))'
    old = prefix + first + first + second + suffix
    saved = prefix + moved + moved + second + suffix
    # Before drift, start sorts the long lines first; after drift, their
    # starts tie and end sorts the short line first. Positional zip is wrong.
    if problem == 'larger':
        saved = saved.replace(moved, moved.replace('0.000001', '0.000002'), 1)
    elif problem == 'pad':
        saved = saved.replace('(at 0 0)', '(at 0.000001 0)')
    elif problem == 'uuid':
        saved = saved.replace('"instance"', '"other"')
    elif problem == 'layer':
        saved = saved.replace('(layer "F.SilkS")', '(layer "F.Fab")', 1)
    elif problem == 'width':
        saved = saved.replace('(width 0.15)', '(width 0.150001)', 1)
    elif problem == 'missing_duplicate':
        saved = saved.replace(moved, '', 1)
    elif problem == 'reused_duplicate':
        saved = saved.replace(moved, second, 1)
    original = scratch_project / 'multiset-original.kicad_pcb'
    original.write_bytes(old.encode())
    source_hash = board_copper_hashes(old, ['R1'], include_instance=True)['R1']
    source = {'instance_source_hash': source_hash,
              'board': {'hash': hashlib.sha256(old.encode()).hexdigest()}}
    live = board_source_hash_variants(saved, ['R1'])
    assert source_hash not in live['R1'], 'The verified-source recovery path must be exercised'
    recover_legacy_source_hashes({'R1': source}, live, [original], {}, live_text=saved)
    assert (source_hash in live['R1']) is (not problem)

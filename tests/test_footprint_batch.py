"""Project-wide footprint references; all CAD fixtures are disposable."""
import pytest

from part_selection_overrides import load_overrides, save_overrides
from test_row_footprint import footprint_app, prepare, choice


def add_references(app):
    path = app.project_root / 'test.kicad_sch'
    text = path.read_text(encoding='utf-8')
    text = text[:-1] + ''.join(
        '(symbol (property "Reference" "%s") (property "Value" "%s") '
        '(property "Comment" "resistor") (property "Footprint" "%s"))' % (ref, value, footprint)
        for ref, value, footprint in [('R2', '220k', 'Local:Original'),
                                      ('R3', '10k', 'Other:Original')]) + ')'
    path.write_text(text, encoding='utf-8')
    records = load_overrides(app.overrides_path)
    records['R2'] = {'stock_c_code':'C200', 'decision':'AcceptManual', 'review_status':'Confirmed'}
    save_overrides(app.overrides_path, records)


def test_batch_uses_full_id_and_keeps_anchor_part_and_other_rows(footprint_app):
    app = footprint_app
    add_references(app)
    preview = prepare(app)
    assert preview['scope']['refs'] == ['R1', 'R2']
    assert preview['scope']['footprint'] == 'Local:Original'
    assert preview['c_code'] == 'C100'
    app.footprint_choices.library_detail(preview['preview_id'], 'Local:Other')
    with pytest.raises(ValueError, match='全部.*封装|影响范围'):
        app.row_footprint({'footprint_choice':choice(preview)})
    result = app.row_footprint({'footprint_choice':choice(preview, scope_acknowledged=True)})
    assert result['refs'] == ['R1', 'R2']
    records = load_overrides(app.overrides_path)
    assert records['R1']['stock_c_code'] == 'C100'
    assert records['R2']['stock_c_code'] == 'C200'
    assert records['R2']['field_overrides']['footprint'] == 'Local:Other'
    assert not records.get('R3')


@pytest.mark.parametrize('change', ['join', 'leave', 'new_symbol'])
def test_batch_membership_is_revalidated_before_publication(footprint_app, change):
    app = footprint_app
    add_references(app)
    preview = prepare(app, 'rename')
    records = load_overrides(app.overrides_path)
    if change == 'join':
        records['R3'] = {'field_overrides':{'footprint':'Local:Original'}}
    elif change == 'leave':
        records['R2']['field_overrides'] = {'footprint':'Local:Other'}
    else:
        path = app.project_root/'test.kicad_sch'
        path.write_text(path.read_text(encoding='utf-8')[:-1] +
            '(symbol (property "Reference" "R4") (property "Footprint" "Local:Original")))', encoding='utf-8')
    save_overrides(app.overrides_path, records)
    with pytest.raises(ValueError, match='变化|修改|影响范围'):
        app.row_footprint({'footprint_choice':choice(preview, 'rename', scope_acknowledged=True)})
    assert not (app.project_root/'Local.pretty/Renamed.kicad_mod').exists()


def test_batch_rename_captures_undo_for_every_ref_without_writing_cad(footprint_app):
    app = footprint_app
    add_references(app)
    path = app.project_root/'test.kicad_sch'
    before = path.read_bytes()
    preview = prepare(app, 'rename')
    app.footprint_choices.library_detail(preview['preview_id'], 'Local:Original')
    app.row_footprint({'footprint_choice':choice(preview, 'rename', scope_acknowledged=True)})
    records = load_overrides(app.overrides_path)
    for ref in ('R1', 'R2'):
        assert records[ref]['review_status'] == 'Confirmed'
        assert records[ref]['field_overrides']['footprint'] == 'Local:Renamed'
        assert records[ref]['workbench_undo']
        assert records[ref]['footprint_change']['operation'] == 'rename'
    assert path.read_bytes() == before


def test_footprint_batch_merging_does_not_inherit_target_material(footprint_app):
    app = footprint_app
    add_references(app)
    path = app.project_root/'test.kicad_sch'
    path.write_text(path.read_text(encoding='utf-8').replace(
        '"R3") (property "Value" "10k"', '"R3") (property "Value" "100k"').replace(
        '"Other:Original"', '"Local:Other"'), encoding='utf-8')
    records = load_overrides(app.overrides_path)
    records['R3'] = {'stock_c_code':'C300','decision':'AcceptManual','review_status':'Confirmed'}
    save_overrides(app.overrides_path, records)
    preview = prepare(app)
    app.footprint_choices.library_detail(preview['preview_id'], 'Local:Other')
    app.row_footprint({'footprint_choice':choice(preview, scope_acknowledged=True)})
    after = load_overrides(app.overrides_path)
    assert after['R1']['stock_c_code'] == 'C100'
    assert after['R2']['stock_c_code'] == 'C200'
    assert after['R3'] == records['R3']
    assert after['R1']['review_status'] == after['R2']['review_status'] == 'Pending'


def hierarchical_batch(app):
    root = app.project_root
    pages = root / 'DOC'
    nested = pages / 'sub'
    nested.mkdir(parents=True)
    (root / 'test.kicad_pro').write_text('{}', encoding='utf-8')
    (root / 'test.kicad_sch').write_text('''(kicad_sch (uuid "root")
      (symbol (uuid "r1") (property "Reference" "R1")
        (property "Value" "100k") (property "Footprint" "Local:Original")
        (instances (project "test" (path "/root" (reference "R1")))))
      (sheet (uuid "a") (property "Sheetfile" "DOC/Top.kicad_sch"))
      (sheet (uuid "b") (property "Sheetfile" "DOC/Top.kicad_sch")))''', encoding='utf-8')
    (pages / 'Top.kicad_sch').write_text('''(kicad_sch (uuid "top")
      (sheet (uuid "nested") (property "Sheetfile" "sub/Parts.kicad_sch")))''', encoding='utf-8')
    child = nested / 'Parts.kicad_sch'
    child.write_text('''(kicad_sch (uuid "parts")
      (symbol (uuid "shared") (property "Reference" "R2")
        (property "Value" "220k") (property "Comment" "resistor")
        (property "Footprint" "Local:Original")
        (instances (project "test"
          (path "/root/a/nested" (reference "R2"))
          (path "/root/b/nested" (reference "R3")))))
      (symbol (uuid "other") (property "Reference" "R4")
        (property "Footprint" "Other:Original")
        (instances (project "test" (path "/root/a/nested" (reference "R4"))))))''', encoding='utf-8')
    (pages / 'Orphan.kicad_sch').write_text('''(kicad_sch (uuid "orphan")
      (symbol (uuid "orphan-symbol") (property "Reference" "R99")
        (property "Footprint" "Local:Original")
        (instances (project "test" (path "/root" (reference "R99"))))))''', encoding='utf-8')
    app.config['schematic_dir'] = pages
    return child


def test_batch_covers_root_nested_and_repeated_instances_not_orphans(footprint_app):
    app = footprint_app
    hierarchical_batch(app)
    records = load_overrides(app.overrides_path)
    records['R2'] = {'stock_c_code':'C200', 'field_overrides':{'value':'manual value'}}
    records['R3'] = {'stock_c_code':'C300', 'field_overrides':{'comment':'manual comment'}}
    save_overrides(app.overrides_path, records)
    preview = prepare(app, 'rename')
    assert preview['scope']['refs'] == ['R1', 'R2', 'R3']
    app.footprint_choices.library_detail(preview['preview_id'], 'Local:Original')
    with pytest.raises(ValueError, match='影响范围'):
        app.row_footprint({'footprint_choice':choice(preview, 'rename')})
    app.row_footprint({'footprint_choice':choice(preview, 'rename', scope_acknowledged=True)})
    after = load_overrides(app.overrides_path)
    for ref in ('R1', 'R2', 'R3'):
        assert after[ref]['stock_c_code'] == records[ref]['stock_c_code']
        assert after[ref]['field_overrides'] == {
            **records[ref].get('field_overrides', {}), 'footprint':'Local:Renamed'}
    assert 'R4' not in after and 'R99' not in after


@pytest.mark.parametrize('change', ['uuid', 'pins', 'formatting', 'instance'])
def test_batch_rejects_changed_hierarchy_bytes_with_unchanged_fields(footprint_app, change):
    app = footprint_app
    child = hierarchical_batch(app)
    preview = prepare(app, 'rename')
    app.footprint_choices.library_detail(preview['preview_id'], 'Local:Original')
    before = load_overrides(app.overrides_path)
    text = child.read_text(encoding='utf-8')
    if change == 'uuid':
        text = text.replace('(uuid "shared")', '(uuid "replacement")')
    elif change == 'pins':
        text = text.replace('(uuid "shared")', '(uuid "shared") (pin "1" (uuid "changed-pin"))')
    elif change == 'instance':
        text = text.replace('/root/b/nested', '/root/wrong/nested')
    else:
        text += '\n'
    child.write_text(text, encoding='utf-8')
    with pytest.raises(ValueError, match='变化|改变|修改'):
        app.row_footprint({'footprint_choice':choice(preview, 'rename', scope_acknowledged=True)})
    assert load_overrides(app.overrides_path) == before
    assert not (app.project_root/'Local.pretty/Renamed.kicad_mod').exists()


def test_legacy_batch_rejects_content_change_without_field_change(footprint_app):
    app = footprint_app
    preview = prepare(app, 'rename')
    app.footprint_choices.library_detail(preview['preview_id'], 'Local:Original')
    path = app.project_root / 'test.kicad_sch'
    path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='变化|改变|修改'):
        app.row_footprint({'footprint_choice':choice(preview, 'rename')})
    assert not (app.project_root/'Local.pretty/Renamed.kicad_mod').exists()


def test_hierarchy_sync_rejects_missing_writer_targets_before_closing_or_writing(footprint_app, monkeypatch):
    from unittest.mock import Mock
    app = footprint_app
    hierarchical_batch(app)
    before = {path:path.read_bytes() for path in app.project_root.rglob('*.kicad_sch')}
    close = Mock(side_effect=AssertionError('Must not close project'))
    monkeypatch.setattr(app, 'prepare_kicad_for_external_schematic_write', close)
    with pytest.raises(ValueError, match='R1.*R2.*R3'):
        app.bulk_sync_bom_state_to_schematic('__schematic__', selected_refs=['R1','R2','R3'],
            prepared_fields={ref:{'footprint':'Local:Renamed'} for ref in ('R1','R2','R3')},
            validate_source=lambda:True)
    close.assert_not_called()
    assert all(path.read_bytes() == content for path, content in before.items())

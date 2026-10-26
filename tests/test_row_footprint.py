"""Independent footprint editing: disposable project, never production CAD."""
import json
import threading
from copy import deepcopy
from unittest.mock import patch

import pytest

from part_selection_overrides import load_overrides, save_overrides
from part_selection_server import PartSelectionApp
from part_stock_lookup import load_stock_cache, save_stock_cache
from test_footprint_choices import module


@pytest.fixture
def footprint_app(scratch_project):
    root = scratch_project
    pretty = root / 'Local.pretty'
    pretty.mkdir()
    for name, width in [('Original', .8), ('Other', .9)]:
        (pretty / (name + '.kicad_mod')).write_text(module(name, width), encoding='utf-8')
    (root / 'fp-lib-table').write_text('(fp_lib_table (version 7) (lib (name "Local") '
        '(type "KiCad") (uri "${KIPRJMOD}/Local.pretty")))', encoding='utf-8')
    (root / 'test.kicad_sch').write_text('(kicad_sch (symbol (property "Reference" "R1") '
        '(property "Comment" "resistor") (property "Value" "100k") '
        '(property "Footprint" "Local:Original") (property "LCSC" "C100")))', encoding='utf-8')
    config = root / 'settings.json'
    config.write_text(json.dumps({'source_bom':'__schematic__', 'schematic_dir':'.', 'bom_dir':'data'}), encoding='utf-8')
    app = PartSelectionApp(config)
    save_overrides(app.overrides_path, {'R1': {'review_status':'Confirmed', 'note':'keep note',
        'review_confirmed_at':'2026-10-01T12:00:00', 'field_overrides':{'description':'keep material'},
        'accepted_c_code':'C100', 'manual_c_code':'C100', 'stock_c_code':'C100', 'decision':'AcceptManual'}})
    save_stock_cache(app.stock_cache_path, {'C100': {'ok':True,'c_code':'C100','package':'0603','model':'Resistor'}})
    yield app
    app.search_jobs.close()
    app.selection_jobs.close()


def prepare(app, mode='library', **extra):
    return app.row_footprint({'refs':'R1','bom':'__schematic__','mode':mode, **extra})['preview']


def choice(preview, source='existing', **extra):
    return {'preview_id':preview['preview_id'], 'source':source, 'existing_id':
        'Local:Original' if source == 'rename' else 'Local:Other', 'acknowledged':True,
        'target_mode':'existing', 'target_library':'Local', 'name':'Renamed', **extra}


def project_files(root):
    return {str(path.relative_to(root)):path.read_bytes() for path in root.rglob('*') if path.is_file()}


def test_preview_and_cancel_do_not_modify_records_libraries_or_cache(footprint_app):
    app = footprint_app
    before = project_files(app.project_root)
    preview = prepare(app)
    assert preview['c_code'] == 'C100'
    assert preview['default_id'] == 'Local:Original'
    assert preview['existing'][0]['numbers'] == ['1','2']
    assert project_files(app.project_root) == before


def test_library_edit_only_changes_footprint_and_confirmation(footprint_app):
    app = footprint_app
    before = deepcopy(load_overrides(app.overrides_path)['R1'])
    cache = load_stock_cache(app.stock_cache_path)
    schematic = (app.project_root/'test.kicad_sch').read_bytes()
    preview = prepare(app)
    app.footprint_choices.library_detail(preview['preview_id'], 'Local:Other')
    result = app.row_footprint({'footprint_choice':choice(preview)})
    after = load_overrides(app.overrides_path)['R1']
    assert result['ok']
    assert after['field_overrides'] == {**before['field_overrides'],'footprint':'Local:Other'}
    assert after['review_status'] == 'Pending'
    for key in ('accepted_c_code','manual_c_code','stock_c_code','decision','note','review_confirmed_at'):
        assert after[key] == before[key]
    assert after.get('workbench_undo')
    assert load_stock_cache(app.stock_cache_path) == cache
    assert (app.project_root/'test.kicad_sch').read_bytes() == schematic


def test_rename_without_code_keeps_confirmation_and_geometry(footprint_app):
    app = footprint_app
    records = load_overrides(app.overrides_path)
    records['R1'].update(decision='ClearSelection',accepted_c_code='',manual_c_code='',stock_c_code='')
    save_overrides(app.overrides_path, records)
    initial = prepare(app)
    assert initial['c_code'] == ''
    preview = prepare(app, 'rename', parent_preview=initial['preview_id'])
    app.footprint_choices.library_detail(preview['preview_id'], 'Local:Original')
    result = app.row_footprint({'footprint_choice':choice(preview,'rename')})
    after = load_overrides(app.overrides_path)['R1']
    assert after['review_status'] == 'Confirmed'
    assert after['field_overrides']['footprint'] == 'Local:Renamed'
    assert after['stock_c_code'] == ''
    assert result['library']['renamed']
    with pytest.raises(ValueError, match='唯一有效'):
        prepare(app, 'convert')


def test_reselecting_same_library_geometry_keeps_confirmation(footprint_app):
    app = footprint_app
    preview = prepare(app)
    app.footprint_choices.library_detail(preview['preview_id'], 'Local:Original')
    app.row_footprint({'footprint_choice':choice(preview,existing_id='Local:Original')})
    assert load_overrides(app.overrides_path)['R1']['review_status'] == 'Confirmed'


def test_post_sync_rename_undo_preserves_selection_and_does_not_write_cad(footprint_app):
    app = footprint_app
    records = load_overrides(app.overrides_path)
    records['R1']['schematic_synced_at'] = '2026-10-23T16:00:00'
    records['R1']['workbench_undo'] = {'record': {
        **deepcopy(records['R1']), 'schematic_synced_at': '2026-10-22T12:00:00'},
        'created_at': '2026-10-22T13:00:00'}
    save_overrides(app.overrides_path, records)
    expected = deepcopy(records['R1'])
    expected.pop('workbench_undo')
    schematic = (app.project_root/'test.kicad_sch').read_bytes()
    preview = prepare(app, 'rename')
    app.footprint_choices.library_detail(preview['preview_id'], 'Local:Original')
    app.row_footprint({'footprint_choice': choice(preview, 'rename')})
    undo = app.undo_workbench_changes({'refs': 'R1'})
    assert undo['mode'] == 'snapshot'
    assert [item['field'] for item in undo['changes']] == ['footprint']
    app.undo_workbench_changes({'refs': 'R1', 'confirm': True, 'revision': undo['revision']})
    assert load_overrides(app.overrides_path)['R1'] == expected
    assert (app.project_root/'test.kicad_sch').read_bytes() == schematic
    assert (app.project_root/'Local.pretty/Renamed.kicad_mod').exists()


@pytest.mark.parametrize('change', ['record','schematic'])
def test_stale_preview_cannot_publish(footprint_app, change):
    app = footprint_app
    preview = prepare(app,'rename')
    app.footprint_choices.library_detail(preview['preview_id'],'Local:Original')
    if change == 'record':
        records = load_overrides(app.overrides_path)
        records['R1']['note'] = 'newer edit'
        save_overrides(app.overrides_path, records)
    else:
        path = app.project_root/'test.kicad_sch'
        path.write_text(path.read_text(encoding='utf-8').replace('100k','220k'),encoding='utf-8')
    with pytest.raises(ValueError,match='变化|修改'):
        app.row_footprint({'footprint_choice':choice(preview,'rename')})
    assert not (app.project_root/'Local.pretty/Renamed.kicad_mod').exists()


def test_row_preview_cannot_be_used_to_select_a_candidate(footprint_app):
    preview = prepare(footprint_app)
    with pytest.raises(ValueError, match='关联选料'):
        footprint_app.select_candidate({'footprint_choice':choice(preview)})


def test_record_write_failure_rolls_back_library_publication(footprint_app):
    app = footprint_app
    before = project_files(app.project_root)
    preview = prepare(app,'rename')
    app.footprint_choices.library_detail(preview['preview_id'],'Local:Original')
    with patch.object(app,'save_field_override',side_effect=OSError('disk unavailable')):
        with pytest.raises(OSError,match='disk unavailable'):
            app.row_footprint({'footprint_choice':choice(preview,'rename')})
    assert project_files(app.project_root) == before


def stage_conversion(service, code, progress=None):
    folder = service.staging_root/'Libraries/DSMU/LCSC'/code
    pretty = folder/('LCSC_'+code+'.pretty')
    pretty.mkdir(parents=True)
    (pretty/'R0603.kicad_mod').write_text(module(),encoding='utf-8')
    (folder/('LCSC_'+code+'.kicad_sym')).write_text('(kicad_symbol_lib (symbol "Part" '
        f'(property "Footprint" "LCSC_{code}:R0603")))',encoding='utf-8')
    return {'footprint':f'LCSC_{code}:R0603','symbol':f'LCSC_{code}:Part'}


@pytest.mark.parametrize('operation', ['row', 'candidate', 'rollback'])
def test_converted_footprint_does_not_publish_unused_symbols(footprint_app, operation):
    app = footprint_app
    before = project_files(app.project_root)
    with patch('part_selection_footprints.LibraryImporter.convert',
               side_effect=lambda code, progress=None: stage_conversion(app.footprint_choices, code, progress)):
        if operation == 'candidate':
            preview = app.select_candidate({'refs':'R1', 'footprint_mode':'convert', 'phase':'preview',
                'candidate':{'c_code':'C100', 'model':'Resistor', 'package':'0603'}})['preview']
            apply = app.select_candidate
        else:
            preview = prepare(app, 'convert')
            apply = app.row_footprint
        payload = {'footprint_choice':choice(preview, 'converted', existing_id='Local:Original', name='Imported')}
        if operation == 'rollback':
            with patch.object(app, 'save_field_override', side_effect=OSError('disk unavailable')):
                with pytest.raises(OSError, match='disk unavailable'):
                    apply(payload)
            assert project_files(app.project_root) == before
        else:
            result = apply(payload)
            assert result['ok']
            assert (app.project_root/'Local.pretty/Imported.kicad_mod').is_file()
            assert 'symbol' not in result['library']
            assert load_overrides(app.overrides_path)['R1']['field_overrides']['footprint'] == 'Local:Imported'
        assert not list(app.project_root.rglob('*.kicad_sym'))
        assert not (app.project_root/'sym-lib-table').exists()
        assert not (app.project_root/'Libraries').exists()
        assert (app.project_root/'test.kicad_sch').read_bytes() == before['test.kicad_sch']


def test_network_conversion_does_not_block_local_preview_and_reuses_cache(footprint_app):
    app = footprint_app
    initial = prepare(app)
    started, release = threading.Event(), threading.Event()
    results = []
    def convert(code, progress=None):
        started.set()
        assert release.wait(5)
        return stage_conversion(app.footprint_choices,code,progress)
    def run():
        try:
            results.append(prepare(app,'convert',parent_preview=initial['preview_id']))
        except Exception as error:
            results.append(error)
    with patch('part_selection_footprints.LibraryImporter.convert', side_effect=convert) as mock:
        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert started.wait(3)
            # This call must finish before releasing the blocked network converter.
            assert prepare(app)['kind'] == 'library'
            assert prepare(app,'rename')['kind'] == 'rename'
        finally:
            release.set()
            thread.join(10)
        assert not thread.is_alive()
        assert not isinstance(results[0],Exception), results
        prepare(app,'convert')
        assert mock.call_count == 1


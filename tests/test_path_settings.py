"""Safe path configuration and migration against disposable engineering data."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from part_selection_server import PartSelectionApp, make_handler
from part_selection_settings import save_settings, settings_snapshot, SettingsGate, apply_runtime_settings


def test_snapshot_reports_converter_and_detected_schematic_without_rewriting_legacy_settings(app):
    config = app.config['config_path']
    original = config.read_bytes()
    with patch('part_selection_settings.find_converter', return_value=Path('C:/converter/easykiconverter.exe')):
        view = settings_snapshot(app.config)
    assert view['converter_path'].endswith('easykiconverter.exe')
    assert view['schematic_default'] == '.'
    assert config.read_bytes() == original


def test_snapshot_handles_missing_optional_converter(app):
    with patch('part_selection_settings.find_converter', side_effect=FileNotFoundError()):
        assert settings_snapshot(app.config)['converter_path'] == ''


def test_settings_directory_opener_only_opens_registered_directories(app):
    from part_selection_settings import open_settings_directory
    with patch('part_selection_settings.os.startfile', create=True) as open_path:
        assert open_settings_directory(app.config, {'target':'project'})['ok']
        open_path.assert_called_once_with(app.project_root)
        with pytest.raises(ValueError):
            open_settings_directory(app.config, {'target':'C:/Windows'})
    missing = app.project_root / 'not-created'
    with pytest.raises(ValueError, match='不存在'):
        open_settings_directory({**app.config,'bom_dir':missing}, {'target':'data'})
    assert not missing.exists()


def test_configured_external_bom_is_available_for_explicit_import(app):
    source = app.project_root.parent / 'external.csv'
    source.write_text('Designator,Value\nR1,100R\n')
    app.config['source_bom'] = source
    assert any(Path(item['path']) == source for item in app.available_boms())
    assert app.resolve_bom_path(str(source)) == source
    assert settings_snapshot(app.config)['import_source'] == str(source)
    with pytest.raises(ValueError):
        app.resolve_bom_path(str(source.parent / 'unconfigured.csv'))


def test_snapshot_normalizes_browsed_project_bom_for_one_shot_selection(app):
    source = app.project_root / 'inside.csv'
    source.write_text('Designator,Value\nR1,100R\n')
    app.save_path_settings(payload(app, source_bom=str(source)))
    assert settings_snapshot(app.config)['import_source'] == 'inside.csv'


def test_startup_ignores_remembered_bom_but_consumes_explicit_import_once(app):
    from part_selection_server import render_index
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required')
    function = re.search(r'async function loadBomOptions\(.*?(?=const rowEditQueue)', render_index(app), re.S).group()
    script = r"""
const vm=require('vm'),assert=require('assert');
const store=new Map(), selected=[];
const data={project_root:'project',default_bom:'__schematic__',boms:[
 {path:'__schematic__',name:'schematic'},{path:'old.csv',name:'csv'}]};
const c={fetch:async()=>({json:async()=>data}),esc:x=>x,
 loadUiSettings:()=>({selectedBom:'old.csv'}),updateUiSettings:x=>selected.push(x.selectedBom),
 bomSelect:{innerHTML:''},sessionStorage:{getItem:k=>store.get(k)||null,removeItem:k=>store.delete(k)}};
vm.runInNewContext(process.argv[1],c);
(async()=>{
 await c.loadBomOptions(); assert(c.bomSelect.innerHTML.includes('value="__schematic__" selected'));
 store.set('partSelectionSourceOnce',JSON.stringify({project:'project',source:'old.csv'}));
 await c.loadBomOptions(); assert(c.bomSelect.innerHTML.includes('value="old.csv" selected'));
 await c.loadBomOptions(); assert(c.bomSelect.innerHTML.includes('value="__schematic__" selected'));
 store.set('partSelectionSourceOnce',JSON.stringify({project:'other',source:'old.csv'}));
 await c.loadBomOptions(); assert(c.bomSelect.innerHTML.includes('value="__schematic__" selected'));
 store.set('partSelectionSourceOnce','invalid json');
 await c.loadBomOptions(); assert(c.bomSelect.innerHTML.includes('value="__schematic__" selected'));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run([node,'-e',script,function],capture_output=True,text=True,encoding='utf-8',timeout=15)
    assert result.returncode==0, result.stderr


@pytest.fixture
def app(scratch_project):
    root = Path(scratch_project)
    (root / 'example.kicad_pro').write_text('{}')
    (root / 'example.kicad_sch').write_text('(kicad_sch)')
    config = root / 'BOM/part_selection_config.json'
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({'project_root': '..', 'schematic_dir': '.', 'custom_setting': {'keep': True}}))
    result = PartSelectionApp(config)
    yield result
    result.search_jobs.close()
    result.selection_jobs.close()


def payload(app, **values):
    return {'revision': settings_snapshot(app.config)['revision'], 'values': values, 'copy_existing': True}


def record(app):
    data = {'R1': {'note': 'retain', 'alternate_parts': [{'c_code': 'C123', 'model': 'sample'}]}}
    app.overrides_path.write_text(json.dumps(data))
    return data


def test_defaults_and_project_identity(app):
    view = settings_snapshot(app.config)
    assert view['values']['output_overrides'] == 'BOM/part_selection_overrides.json'
    assert Path(view['resolved']['bom_dir']) == app.project_root / 'BOM'
    assert Path(view['config_path']).is_file()


def test_migration_preserves_originals_unknown_config_and_structured_records(app):
    original = record(app)
    app.stock_cache_path.write_text('{"C123":{"stock":10}}')
    (app.bom_dir / 'part_selection_pump_table_sample.csv').write_text('sample export')
    (app.bom_dir / 'part_selection_schematic_sync_log.csv').write_text('sync log')
    old = app.overrides_path
    result = app.save_path_settings(payload(app, bom_dir='parts', output_overrides='parts/selection.json', stock_cache='parts/stock.json'))
    assert json.loads(app.overrides_path.read_text()) == original
    assert json.loads(old.read_text()) == original
    assert (app.bom_dir / 'part_selection_pump_table_sample.csv').read_text() == 'sample export'
    assert result['copied_files'] == 4
    assert (Path(result['backup_path']) / 'config.json').is_file()
    assert json.loads(app.config['config_path'].read_text())['custom_setting'] == {'keep': True}
    assert app.config['project_root'] == app.project_root


def test_conflicting_destination_leaves_records_config_and_active_paths_unchanged(app):
    record(app)
    target = app.project_root / 'new.json'
    target.write_text('{"other":true}')
    before = app.config['config_path'].read_bytes()
    with pytest.raises(ValueError, match='未覆盖'):
        app.save_path_settings(payload(app, output_overrides='new.json'))
    assert target.read_text() == '{"other":true}'
    assert app.config['config_path'].read_bytes() == before
    assert app.overrides_path == app.project_root / 'BOM/part_selection_overrides.json'


def test_existing_identical_destination_is_reused(app):
    record(app)
    (app.project_root / 'same.json').write_bytes(app.overrides_path.read_bytes())
    assert app.save_path_settings(payload(app, output_overrides='same.json'))['ok']


def test_requires_copy_instead_of_silently_losing_existing_selection(app):
    record(app)
    change = payload(app, output_overrides='new.json')
    change['copy_existing'] = False
    with pytest.raises(ValueError, match='确认复制'):
        app.save_path_settings(change)
    assert not (app.project_root / 'new.json').exists()


def test_atomic_config_failure_rolls_back_copied_files(app):
    record(app)
    before = app.config['config_path'].read_bytes()
    with patch('part_selection_settings._write_atomic', side_effect=OSError('disk failed')):
        with pytest.raises(OSError, match='disk failed'):
            app.save_path_settings(payload(app, output_overrides='new.json'))
    assert not (app.project_root / 'new.json').exists()
    assert app.config['config_path'].read_bytes() == before
    assert app.overrides_path.is_file()


def test_destination_created_during_backup_is_not_silently_used(app):
    import shutil
    record(app)
    before = app.config['config_path'].read_bytes()
    target = app.project_root / 'new.json'
    original_copy = shutil.copy2

    def copy_and_create(*args, **kwargs):
        result = original_copy(*args, **kwargs)
        target.write_text('{"concurrent":true}')
        return result

    with patch('part_selection_settings.shutil.copy2', side_effect=copy_and_create):
        with pytest.raises(ValueError, match='未覆盖'):
            app.save_path_settings(payload(app, output_overrides='new.json'))
    assert target.read_text() == '{"concurrent":true}'
    assert app.config['config_path'].read_bytes() == before


def test_partial_copy_failure_removes_only_new_file(app):
    from part_selection_settings import _copy_exclusive
    target = app.project_root / 'partial.json'
    with patch('part_selection_settings.os.fsync', side_effect=OSError('disk failed')):
        with pytest.raises(OSError, match='disk failed'):
            _copy_exclusive(target, b'example')
    assert not target.exists()


@pytest.mark.parametrize('fields', [
    {'output_overrides': 'file.csv'}, {'output_overrides': 'BOM/lcsc_stock_cache.json'},
    {'output_overrides': 'BOM/part_selection_config.json'}, {'source_bom': 'missing.csv'},
    {'schematic_dir': 'missing-folder'}, {'project_root': 'somewhere'},
    {'runtime_cache': 'relative'}, {'desktop_home': 'relative'},
    {'runtime_cache': '.'}, {'converter_executable': 'missing.exe'},
    {'lcsc_bom_pattern': '../outside/*.csv'},
])
def test_invalid_settings_are_rejected_without_writing(app, fields):
    before = app.config['config_path'].read_bytes()
    with pytest.raises(ValueError):
        app.save_path_settings(payload(app, **fields))
    assert app.config['config_path'].read_bytes() == before


def test_stale_configuration_is_not_overwritten(app):
    change = payload(app, project_name='new name')
    app.config['config_path'].write_text('{"project_root":"..","schematic_dir":"."}')
    with pytest.raises(ValueError, match='其他窗口'):
        app.save_path_settings(change)


def test_external_output_paths_are_supported(app):
    target = app.project_root.parent / 'custom-output'
    record(app)
    app.save_path_settings(payload(app, bom_dir=str(target), output_overrides=str(target / 'selection.json')))
    assert app.overrides_path == target / 'selection.json'
    assert app.overrides_path.is_file()


def test_runtime_changes_are_persisted_for_next_launch(app):
    cache, home = app.project_root.parent / 'cache', app.project_root.parent / 'home'
    result = app.save_path_settings(payload(app, runtime_cache=str(cache), desktop_home=str(home)))
    assert result['restart_required']
    assert result['settings']['values']['runtime_cache'] == str(cache)
    assert not app.config.get('runtime_cache')
    with patch.dict(os.environ):
        from part_selection_overrides import load_config
        apply_runtime_settings(load_config(app.config['config_path']))
        assert os.environ['KICAD_PART_SELECTION_CACHE'] == str(cache)
        assert os.environ['KICAD_PART_SELECTION_HOME'] == str(home)


def test_active_background_selection_rejects_configuration_change(app):
    with patch.object(app.selection_jobs, 'busy', return_value=True):
        with pytest.raises(ValueError, match='尚未完成'):
            app.save_path_settings(payload(app, project_name='new'))


def test_gate_rejects_other_active_requests_but_allows_current_request():
    gate = SettingsGate()
    gate.active = 2
    with pytest.raises(ValueError):
        with gate.editing():
            pass
    gate.active = 1
    with gate.editing():
        assert gate.active == 1


def test_http_settings_persist_and_apply_without_touching_cad(app):
    before = (app.project_root / 'example.kicad_sch').read_bytes()
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base + '/api/path_settings') as response:
            settings = json.load(response)
        body = {'revision': settings['revision'], 'values': {'bom_dir': 'exports'}, 'copy_existing': True}
        request = Request(base + '/api/path_settings', json.dumps(body).encode(), {'Content-Type': 'application/json'})
        with urlopen(request) as response:
            assert json.load(response)['ok']
        assert app.bom_dir == app.project_root / 'exports'
        with pytest.raises(HTTPError):
            urlopen(request)
        assert (app.project_root / 'example.kicad_sch').read_bytes() == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

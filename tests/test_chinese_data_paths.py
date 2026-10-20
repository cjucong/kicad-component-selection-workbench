"""Chinese filenames and non-destructive upgrade, exclusively in scratch projects."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from part_selection_overrides import DATA_DIRECTORY, CONFIG_NAME, DATA_FILE_NAMES, load_config, load_overrides
from part_selection_paths import resolve_config
from part_selection_runtime import runtime_directory
from part_selection_server import PartSelectionApp


@pytest.fixture
def project(scratch_project):
    root = Path(scratch_project) / '中文工程 空格'
    root.mkdir()
    (root / '测试.kicad_pro').write_text('{}', encoding='utf-8')
    (root / '测试.kicad_sch').write_text(
        '(kicad_sch (symbol (property "Reference" "C1") (property "Comment" "capacitor") '
        '(property "Value" "100nF") (property "Footprint" "器件库:0603")))', encoding='utf-8')
    return root


def legacy_files(root, **updates):
    bom = root / 'BOM'
    bom.mkdir()
    config = bom / 'part_selection_config.json'
    config.write_text(json.dumps({'project_root': '..', 'schematic_dir': '.', 'custom_option': '保留', **updates},
                                 ensure_ascii=False), encoding='utf-8')
    (bom / 'part_selection_overrides.json').write_text(json.dumps({
        'C1': {'accepted_c_code': 'C123', 'review_status': 'Confirmed', 'note': '中文备注',
               'field_overrides': {'value': '100nF, 50V'}, 'alternate_parts': [{'c_code': 'C456'}],
               'procurement_choice': {'order_url': 'https://example.com/order?part=C123'}}},
        ensure_ascii=False), encoding='utf-8')
    (bom / 'lcsc_stock_cache.json').write_text('{"C123":{"stock":10}}', encoding='utf-8')
    (bom / 'part_selection_input.txt').write_text('C1\n', encoding='utf-8')
    (bom / 'part_selection_schematic_sync_log.csv').write_text('ref,value\nC1,100nF\n', encoding='utf-8-sig')
    (bom / 'part_selection_schematic_sync_runtime.jsonl').write_text('{"event":"old"}\n', encoding='utf-8')
    (bom / 'part_selection_pump_table_20260101.csv').write_text('采购表', encoding='utf-8-sig')
    (bom / 'part_selection_change_log_20260101.csv').write_text('变更表', encoding='utf-8-sig')
    return config


def close(app):
    app.search_jobs.close()
    app.selection_jobs.close()


def test_new_project_chinese_defaults_and_round_trip(project):
    config = resolve_config(project)
    assert config == project / DATA_DIRECTORY / CONFIG_NAME
    loaded = load_config(config)
    assert loaded['project_root'] == project
    for key, name in DATA_FILE_NAMES.items():
        assert loaded[key] == config.parent / name
    app = PartSelectionApp(config)
    before = (project / '测试.kicad_sch').read_bytes()
    try:
        app.save_field_override({'refs': 'C1', 'field_overrides': {'value': '100nF, 50V'}, 'note': '可读写中文'})
        assert load_overrides(app.overrides_path)['C1']['note'] == '可读写中文'
        rows = app.rows(include_stock=False)
        assert rows[0]['value'] == '100nF, 50V'
        assert app.export_pump_table({})['ok']
        assert list(config.parent.glob('采购BOM表_*.csv'))
        assert resolve_config(project) == config
        assert (project / '测试.kicad_sch').read_bytes() == before
    finally:
        close(app)


@pytest.mark.parametrize('explicit', [False, True])
def test_migration_preserves_selections_logs_exports_custom_options_and_originals(project, explicit):
    old = legacy_files(project)
    originals = {p: p.read_bytes() for p in old.parent.iterdir()}
    with patch('part_selection_paths._check_legacy_service'):
        new = resolve_config(project, old if explicit else None)
    config = load_config(new)
    assert config['project_root'] == project
    assert config['custom_option'] == '保留'
    assert load_overrides(config['output_overrides']) == load_overrides(old.parent / 'part_selection_overrides.json')
    assert config['stock_cache'].read_bytes() == originals[old.parent / 'lcsc_stock_cache.json']
    assert (new.parent / '原理图同步记录.csv').read_bytes() == originals[old.parent / 'part_selection_schematic_sync_log.csv']
    assert (runtime_directory(project) / '原理图同步运行日志.jsonl').is_file()
    assert not (new.parent / '原理图同步运行日志.jsonl').exists()
    assert (new.parent / '采购BOM表_20260101.csv').read_text(encoding='utf-8-sig') == '采购表'
    assert (new.parent / '选料变更记录_20260101.csv').is_file()
    assert all(p.read_bytes() == data for p, data in originals.items())
    assert Path(config['data_migration_backup']).is_dir()
    assert not Path(config['data_migration_backup']).is_relative_to(project)
    assert resolve_config(project, old) == new
    assert resolve_config(project) == new


def test_migration_keeps_explicit_custom_record_locations(project):
    custom = project / '自定义数据'
    custom.mkdir()
    record = custom / '人工记录.json'
    record.write_text('{"C1":{"note":"custom"}}', encoding='utf-8')
    legacy_files(project, output_overrides='自定义数据/人工记录.json')
    with patch('part_selection_paths._check_legacy_service'):
        new = resolve_config(project)
    assert load_config(new)['output_overrides'] == record


def test_custom_config_path_is_not_moved(project):
    custom = project / '自定义配置.json'
    custom.write_text('{"schematic_dir":"."}', encoding='utf-8')
    before = custom.read_bytes()
    assert resolve_config(project, custom) == custom
    assert custom.read_bytes() == before


def test_conflicting_chinese_file_is_not_overwritten(project):
    old = legacy_files(project)
    target = project / DATA_DIRECTORY / DATA_FILE_NAMES['output_overrides']
    target.parent.mkdir()
    target.write_bytes(b'{"different":true}')
    before = old.read_bytes()
    with patch('part_selection_paths._check_legacy_service'), pytest.raises(ValueError, match='未覆盖'):
        resolve_config(project)
    assert target.read_bytes() == b'{"different":true}'
    assert old.read_bytes() == before
    assert not (target.parent / CONFIG_NAME).exists()


def test_failed_migration_rolls_back_created_files_and_keeps_originals(project):
    from part_selection_settings import _copy_exclusive
    old = legacy_files(project)
    before = old.read_bytes()

    def fail_config(path, data):
        if path.name == CONFIG_NAME:
            raise OSError('disk failed')
        return _copy_exclusive(path, data)

    with patch('part_selection_paths._check_legacy_service'), patch('part_selection_settings._copy_exclusive', side_effect=fail_config):
        with pytest.raises(OSError, match='disk failed'):
            resolve_config(project)
    assert old.read_bytes() == before
    assert not list((project / DATA_DIRECTORY).glob('*'))
    with patch('part_selection_paths._check_legacy_service'):
        assert resolve_config(project).is_file()


def test_old_record_changes_after_migration_are_reported_instead_of_ignored(project):
    old = legacy_files(project)
    with patch('part_selection_paths._check_legacy_service'):
        resolve_config(project)
    (old.parent / 'part_selection_overrides.json').write_text('{"C1":{"note":"changed"}}', encoding='utf-8')
    with pytest.raises(ValueError, match='迁移后又被修改'):
        resolve_config(project, old)


def test_live_legacy_service_blocks_migration(project):
    legacy_files(project)
    with patch('part_selection_paths._check_legacy_service', side_effect=ValueError('旧工作台后台仍在运行')):
        with pytest.raises(ValueError, match='后台仍在运行'):
            resolve_config(project)
    assert not (project / DATA_DIRECTORY / CONFIG_NAME).exists()


def test_migrated_config_cannot_redirect_to_another_project(project):
    legacy_files(project)
    with patch('part_selection_paths._check_legacy_service'):
        new = resolve_config(project)
    raw = json.loads(new.read_text(encoding='utf-8'))
    raw['project_root'] = str(project.parent)
    new.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(ValueError, match='another project'):
        resolve_config(project)


def test_legacy_service_guard_checks_project_without_using_system_proxy(project):
    from contextlib import contextmanager
    from io import StringIO
    from part_selection_paths import _check_legacy_service

    @contextmanager
    def response(*args, **kwargs):
        yield StringIO(json.dumps({'project_root': str(project)}))

    with patch('part_selection_paths.build_opener') as build:
        build.return_value.open.side_effect = response
        with pytest.raises(ValueError, match='旧工作台后台仍在运行'):
            _check_legacy_service(project)
        assert build.return_value.open.call_count == 1


def test_schematic_sync_chinese_records_and_external_runtime_log(project):
    app = PartSelectionApp(resolve_config(project))
    try:
        with patch.object(app, 'prepare_kicad_for_external_schematic_write', return_value={'ok': True}), \
             patch.object(app, 'notify_kicad_schematic_changed', return_value={'ok': True}):
            result = app.sync_schematic({'refs': 'C1', 'field_overrides': {'value': '100nF, 50V'}})
        assert result['ok']
        assert (app.bom_dir / '原理图同步记录.csv').is_file()
        assert Path(result['runtime_log']) == runtime_directory(project) / '原理图同步运行日志.jsonl'
        assert '100nF, 50V' in (project / '测试.kicad_sch').read_text(encoding='utf-8')
        assert not list(project.rglob('*.jsonl'))
    finally:
        close(app)

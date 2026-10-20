"""An installed tool must never infer its project from its source directory."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from _scratch import temporary_project

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tools/kicad_plugin'))


class StandalonePathsTests(unittest.TestCase):
    def setUp(self):
        self.scratch = temporary_project()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        (self.root / 'test.kicad_pro').write_text('{}')
        (self.root / 'test.kicad_sch').write_text('(kicad_sch)')

    def test_new_config_defaults_to_schematic_instead_of_old_project_bom(self):
        from part_selection_overrides import load_config
        config = self.root / 'BOM/config.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'project_root': '..'}))
        loaded = load_config(config)
        self.assertEqual(loaded['source_bom'].name, '__schematic__')
        self.assertEqual(loaded['bom_dir'], self.root / 'BOM')

    def test_default_rows_read_schematic_without_explicit_bom(self):
        from part_selection_server import PartSelectionApp
        (self.root / 'test.kicad_sch').write_text(
            '(kicad_sch (symbol (property "Reference" "R1") '
            '(property "Comment" "resistor") (property "Value" "10k") '
            '(property "Footprint" "MyLibrary:0603")))')
        config = self.root / 'settings.json'
        config.write_text(json.dumps({'schematic_dir': '.'}))
        app = PartSelectionApp(config)
        self.addCleanup(app.search_jobs.close)
        self.addCleanup(app.selection_jobs.close)
        self.assertEqual(app.refs(), ['R1'])
        self.assertEqual(app.rows(include_stock=False)[0]['refs'], 'R1')

    def test_paths_expand_environment_and_resolve_relative_to_project(self):
        from part_selection_overrides import load_config
        config = self.root / 'paths.json'
        config.write_text(json.dumps({'project_root': '.', 'bom_dir': 'parts',
            'converter_executable': 'bin/converter.exe', 'stock_cache': '${PARTS_PATH}/stock.json'}))
        with patch.dict(os.environ, {'PARTS_PATH': str(self.root / 'external')}):
            loaded = load_config(config)
        self.assertEqual(loaded['bom_dir'], self.root / 'parts')
        self.assertEqual(loaded['converter_executable'], self.root / 'bin/converter.exe')
        self.assertEqual(loaded['stock_cache'], self.root / 'external/stock.json')

    def test_explicit_config_is_preserved_and_wrong_project_rejected(self):
        import desktop
        config = self.root / 'custom/settings.json'
        config.parent.mkdir()
        data = json.dumps({'project_root': '..', 'output_overrides': 'parts/selected.json'})
        config.write_text(data)
        self.assertEqual(desktop.ensure_config(self.root, config), config)
        self.assertEqual(config.read_text(), data)
        config.write_text(json.dumps({'project_root': str(ROOT)}))
        with self.assertRaisesRegex(ValueError, 'another project'):
            desktop.ensure_config(self.root, config)

    def test_custom_cache_stays_project_specific_and_external(self):
        from part_selection_runtime import runtime_directory
        base = self.root.parent / 'custom-cache'
        with patch.dict(os.environ, {'KICAD_PART_SELECTION_CACHE': str(base)}):
            cache = runtime_directory(self.root)
        self.assertEqual(cache.parent, base)
        self.assertNotEqual(cache.name, self.root.name)

    def test_custom_cache_rejects_project_internal_or_relative_path(self):
        from part_selection_runtime import runtime_directory
        for base in (str(self.root / 'bad'), 'relative-cache'):
            with self.subTest(base=base), patch.dict(os.environ, {'KICAD_PART_SELECTION_CACHE': base}):
                with self.assertRaises(ValueError):
                    runtime_directory(self.root)

    def test_desktop_home_can_be_configured(self):
        import desktop
        home = self.root.parent / 'desktop-home'
        with patch.dict(os.environ, {'KICAD_PART_SELECTION_HOME': str(home)}):
            self.assertEqual(desktop.cache_directory(), home)

    def test_server_requires_explicit_project_or_config(self):
        import part_selection_server as server
        with patch.object(sys, 'argv', ['server']), self.assertRaises(SystemExit):
            server.parse_args()
        with patch.object(sys, 'argv', ['server', '--project', str(self.root)]):
            args = server.parse_args()
        self.assertEqual(args.config, self.root / 'BOM/part_selection_config.json')

    def test_package_is_self_contained_without_fpga_tool(self):
        import package_plugin
        files = package_plugin.package_files()
        self.assertIn('plugins/app/kicad_sexpr.py', files)
        self.assertNotIn('plugins/app/fpga_pin_assignment.py', files)

    def test_lightweight_upgrade_preserves_bundled_converter(self):
        import package_plugin
        plugins = self.root / 'plugins'
        target = plugins / 'com_smu_part-selection'
        converter = target / 'third_party/EasyKiConverter/runtime/easykiconverter.exe'
        converter.parent.mkdir(parents=True)
        converter.write_bytes(b'existing converter')
        package_plugin.install_plugin(plugins, self.root.parent / 'backups')
        self.assertEqual(converter.read_bytes(), b'existing converter')

    def test_hidden_launch_bridge_forwards_explicit_config_and_project(self):
        import launch_part_selection
        import desktop
        arguments = ['--hidden', '--desktop', '--project', str(self.root),
                     '--config', str(self.root / 'settings with spaces.json')]
        with patch.object(desktop, 'main', return_value=0) as launch:
            self.assertEqual(launch_part_selection.main(arguments), 0)
        self.assertEqual(launch.call_args.args[0], arguments[2:])

    def test_external_bom_directory_lists_and_accepts_only_configured_files(self):
        from part_selection_server import PartSelectionApp
        external = self.root.parent / 'external-parts'
        external.mkdir()
        bom = external / 'parts.csv'
        bom.write_text('Designator,Value,Footprint\nR1,10k,0603\n')
        config = self.root / 'settings.json'
        config.write_text(json.dumps({'bom_dir': str(external), 'schematic_dir': '.'}))
        app = PartSelectionApp(config)
        self.addCleanup(app.search_jobs.close)
        self.addCleanup(app.selection_jobs.close)
        self.assertIn(str(bom), [item['path'] for item in app.available_boms()])
        self.assertEqual(app.resolve_bom_path(str(bom)), bom)
        unrelated = self.root.parent / 'unrelated.csv'
        unrelated.write_text('Designator\nR9\n')
        with self.assertRaises(ValueError):
            app.resolve_bom_path(str(unrelated))

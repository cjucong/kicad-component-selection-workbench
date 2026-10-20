import json
import shutil
import subprocess
import sys
from _scratch import temporary_project
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from part_selection_library import LibraryImporter, SelectionJobs
from part_selection_runtime import runtime_directory
import threading
import time


class LibraryImporterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_project()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.importer = LibraryImporter(self.root)
        validator = patch('part_selection_library.validate_with_kicad')
        validator.start()
        self.addCleanup(validator.stop)

    def cli(self, args, **kwargs):
        self.assertEqual(args[1:3], ['convert', 'component'])
        self.assertIn('--symbol', args)
        self.assertIn('--footprint', args)
        self.assertNotIn('--debug', args)
        self.assertLessEqual(kwargs['timeout'], 180)
        output = Path(args[args.index('--output') + 1])
        name = args[args.index('--lib-name') + 1]
        output.mkdir(parents=True, exist_ok=True)
        (output / (name + '.pretty')).mkdir()
        (output / (name + '.kicad_sym')).write_text(
            f'(kicad_symbol_lib (version 20241209) (generator test) (symbol "Part" (property "Footprint" "{name}:Part")))', encoding='utf-8')
        (output / (name + '.pretty') / 'Part.kicad_mod').write_text(
            '(footprint "Part" (version 20241229) (generator test) (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))', encoding='utf-8')
        return subprocess.CompletedProcess(args, 0, b'converted', b'')

    def convert(self):
        with patch('part_selection_library.find_converter', return_value=Path('converter.exe')), patch('part_selection_library.subprocess.run', side_effect=self.cli):
            return self.importer.convert('C123')

    def test_import_register_and_reuse_without_running_again(self):
        result = self.convert()
        self.assertEqual(result['footprint'], 'LCSC_C123:Part')
        self.assertEqual(result['symbol'], 'LCSC_C123:Part')
        for table in ('sym-lib-table', 'fp-lib-table'):
            content = (self.root / table).read_text(encoding='utf-8')
            self.assertIn('${KIPRJMOD}/Libraries/DSMU/LCSC/C123/', content)
        with patch('part_selection_library.subprocess.run', side_effect=AssertionError('must use verified cache')):
            self.assertTrue(self.importer.convert('C123')['cached'])

    def test_failure_leaves_tables_and_destination_untouched(self):
        original = '(fp_lib_table (version 7))'
        (self.root / 'fp-lib-table').write_text(original)
        with patch('part_selection_library.find_converter', return_value=Path('converter.exe')), patch('part_selection_library.subprocess.run', return_value=subprocess.CompletedProcess([], 1, b'', b'failed')):
            with self.assertRaises(RuntimeError):
                self.importer.convert('C123')
        self.assertEqual((self.root / 'fp-lib-table').read_text(), original)
        self.assertFalse((self.root / 'Libraries/DSMU/LCSC/C123').exists())

    def test_timeout_preserves_output_and_converter_log(self):
        def timeout(args, **kwargs):
            Path(args[args.index('--log-file') + 1]).write_text('Proxy connection timed out')
            raise subprocess.TimeoutExpired(args, 180, output=b'partial download', stderr=b'network failure')
        with patch('part_selection_library.find_converter', return_value=Path('converter.exe')), patch('part_selection_library.subprocess.run', side_effect=timeout):
            with self.assertRaisesRegex(RuntimeError, 'network/conversion.*log'):
                self.importer.convert('C123')
        logs = list((runtime_directory(self.root) / 'part_selection_converter_logs').glob('*.log'))
        self.assertEqual(len(logs), 1)
        content = logs[0].read_text()
        self.assertIn('partial download', content)
        self.assertIn('Proxy connection timed out', content)
        self.assertIn('system_proxy_enabled', content)
        self.assertFalse((self.root / 'Libraries').exists())

    def test_collision_does_not_replace_existing_registration(self):
        original = '(fp_lib_table (lib (name "LCSC_C123") (type "KiCad") (uri "elsewhere")))'
        (self.root / 'fp-lib-table').write_text(original)
        with self.assertRaisesRegex(ValueError, 'collision'):
            self.convert()
        self.assertEqual((self.root / 'fp-lib-table').read_text(), original)
        self.assertFalse((self.root / 'sym-lib-table').exists())

    def test_modified_library_is_never_overwritten(self):
        self.convert()
        footprint = self.root / 'Libraries/DSMU/LCSC/C123/LCSC_C123.pretty/Part.kicad_mod'
        footprint.write_text('user edit')
        with self.assertRaisesRegex(ValueError, 'modified'):
            self.importer.convert('C123')
        self.assertEqual(footprint.read_text(), 'user edit')

    def test_unsafe_codes_are_rejected(self):
        for code in ('../C123', 'C123 --debug', '', 'C123/other'):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.importer.convert(code)

    def test_missing_footprint_and_malformed_symbols_fail(self):
        def bad(args, **kwargs):
            result = self.cli(args, **kwargs)
            output = Path(args[args.index('--output') + 1])
            next(output.glob('*.kicad_sym')).write_text('(kicad_symbol_lib')
            return result
        with patch('part_selection_library.find_converter', return_value=Path('converter.exe')), patch('part_selection_library.subprocess.run', side_effect=bad):
            with self.assertRaises(ValueError):
                self.importer.convert('C123')
        self.assertFalse((self.root / 'sym-lib-table').exists())

    def test_registration_failure_rolls_back_tables_and_publication(self):
        original = '(sym_lib_table (version 7))'
        (self.root / 'sym-lib-table').write_text(original)
        import os
        replace = os.replace
        def fail_second(source, destination):
            if Path(destination).name == 'fp-lib-table':
                raise OSError('table locked')
            return replace(source, destination)
        with patch('part_selection_library.os.replace', side_effect=fail_second):
            with self.assertRaisesRegex(OSError, 'table locked'):
                self.convert()
        self.assertEqual((self.root / 'sym-lib-table').read_text(), original)
        self.assertFalse((self.root / 'Libraries/DSMU/LCSC/C123').exists())
        self.assertFalse((self.root / 'fp-lib-table').exists())

    def test_rollback_preserves_a_concurrent_table_edit(self):
        import os
        replace = os.replace
        def mutate_then_fail(source, destination):
            if Path(destination).name == 'fp-lib-table':
                (self.root / 'sym-lib-table').write_text('(sym_lib_table (version 7) (lib (name "UserEdit")))')
                raise OSError('table locked')
            return replace(source, destination)
        with patch('part_selection_library.os.replace', side_effect=mutate_then_fail):
            with self.assertRaisesRegex(RuntimeError, 'Concurrent library edits preserved'):
                self.convert()
        self.assertIn('UserEdit', (self.root / 'sym-lib-table').read_text())
        self.assertTrue((self.root / 'Libraries/DSMU/LCSC/C123').is_dir())

    def test_kicad_parse_failure_blocks_publication(self):
        with patch('part_selection_library.validate_with_kicad', side_effect=ValueError('invalid pad size')):
            with self.assertRaisesRegex(ValueError, 'invalid pad size'):
                self.convert()
        self.assertFalse((self.root / 'sym-lib-table').exists())
        self.assertFalse((self.root / 'Libraries/DSMU/LCSC/C123').exists())

    def test_legacy_converter_names_and_dangling_models_are_normalized(self):
        def legacy(args, **kwargs):
            result = self.cli(args, **kwargs)
            path = next(Path(args[args.index('--output') + 1]).rglob('*.kicad_mod'))
            path.write_text('(footprint easykiconverter:Part (model "../missing.wrl"))')
            return result
        with patch('part_selection_library.find_converter', return_value=Path('converter.exe')), patch('part_selection_library.subprocess.run', side_effect=legacy):
            self.importer.convert('C123')
        content = next((self.root / 'Libraries').rglob('*.kicad_mod')).read_text()
        self.assertIn('(footprint "Part"', content)
        self.assertNotIn('(model', content)

    def test_progress_job_returns_verified_result_and_reports_failures(self):
        def select(payload, progress):
            progress('validating', 'checking')
            if payload.get('fail'):
                raise RuntimeError('bad component')
            return {'ok': True, 'fields': {'footprint': 'LCSC_C123:Part'}}
        jobs = SelectionJobs(select)
        self.addCleanup(jobs.close)
        for fail in (False, True):
            state = jobs.start({'fail': fail})
            deadline = time.monotonic() + 3
            while not state['done'] and time.monotonic() < deadline:
                time.sleep(.01)
                state = jobs.status(state['job_id'])
            self.assertTrue(state['done'])
            self.assertEqual(state['ok'], not fail)
            if fail:
                self.assertIn('bad component', state['error'])
            else:
                self.assertEqual(state['result']['fields']['footprint'], 'LCSC_C123:Part')


if __name__ == '__main__':
    unittest.main()

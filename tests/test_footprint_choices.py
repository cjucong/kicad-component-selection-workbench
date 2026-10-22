"""Footprint choices operate on scratch projects, never the active board."""
import json
import sys
from _scratch import temporary_project
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from part_selection_footprints import FootprintChoices


def module(name='R0603', width=0.8, pin='2'):
    return (f'(footprint "{name}" (version 20241229) (generator test) (layer "F.Cu") '
            f'(pad "1" smd rect (at -0.75 0) (size {width} 0.86) (layers "F.Cu" "F.Paste" "F.Mask")) '
            f'(pad "{pin}" smd rect (at 0.75 0) (size {width} 0.86) (layers "F.Cu" "F.Paste" "F.Mask")))')


class FootprintChoicesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_project()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.pretty = self.root / 'Libraries/DSMU/DSMU.pretty'
        self.pretty.mkdir(parents=True)
        self.old = self.pretty / '0603.kicad_mod'
        self.old.write_text(module('0603', .7112), encoding='utf-8')
        (self.root / 'fp-lib-table').write_text('(fp_lib_table (version 7) '
            '(lib (name "DSMU") (type "KiCad") (uri "${KIPRJMOD}/Libraries/DSMU/DSMU.pretty")))')
        self.service = FootprintChoices(self.root)
        self.pin = '2'
        self.addCleanup(patch.stopall)
        patch('part_selection_footprints.render_previews', return_value={
            'existing_svg': '<svg xmlns="http://www.w3.org/2000/svg"/>',
            'converted_svg': '<svg xmlns="http://www.w3.org/2000/svg"/>'}).start()
        patch('part_selection_footprints.LibraryImporter.convert', side_effect=self.convert).start()

    def convert(self, code, progress=None, importer=None):
        # Staged importer is isolated outside the project.
        root = self.service.staging_root
        dest = root / 'Libraries/DSMU/LCSC' / code
        pretty = dest / ('LCSC_' + code + '.pretty')
        pretty.mkdir(parents=True)
        (pretty / 'R0603.kicad_mod').write_text(module(pin=self.pin))
        (dest / ('LCSC_' + code + '.kicad_sym')).write_text(
            f'(kicad_symbol_lib (version 20241209) (generator test) (symbol "Part" '
            f'(property "Footprint" "LCSC_{code}:R0603")))')
        return {'footprint': f'LCSC_{code}:R0603', 'symbol': f'LCSC_{code}:Part', 'c_code': code}

    def prepare(self):
        return self.service.prepare('C123', ['DSMU:0603'], package='0603')

    def choice(self, preview, **extra):
        return {'preview_id': preview['preview_id'], 'source': 'converted', 'existing_id': 'DSMU:0603',
                'target_mode': 'existing', 'target_library': 'DSMU', 'name': '0603', 'acknowledged': True, **extra}

    def test_prepare_leaves_project_library_and_tables_untouched(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        preview = self.prepare()
        self.assertFalse(self.service.staging_root.is_relative_to(self.root))
        self.assertFalse((self.root / 'Temp').exists())
        self.assertEqual(preview['default_library'], 'DSMU')
        self.assertEqual(preview['suggested_name'], '0603')
        self.assertEqual(preview['existing'][0]['id'], 'DSMU:0603')
        self.assertTrue(preview['compatible'])
        self.assertIn('单焊盘尺寸', str(preview['metrics']))
        self.assertTrue(all(p.read_bytes() == data for p, data in before.items()))
        self.assertFalse((self.root / 'sym-lib-table').exists())

    def test_merge_save_as_and_symbol_reference_and_idempotent_reuse(self):
        preview = self.prepare()
        before = self.old.read_bytes()
        self.assertIn('conflict', self.service.commit(self.choice(preview)))
        selected = self.choice(preview, name='Chosen0603')
        result = self.service.commit(selected)
        self.assertTrue(Path(result['backup']).is_absolute())
        self.assertFalse(Path(result['backup']).is_relative_to(self.root))
        self.assertTrue(Path(result['backup']).is_dir())
        self.assertEqual(result['footprint'], 'DSMU:Chosen0603')
        self.assertEqual(self.old.read_bytes(), before)
        imported = self.pretty / 'Chosen0603.kicad_mod'
        self.assertTrue(imported.exists())
        self.assertIn('(footprint "Chosen0603"', imported.read_text())
        sym_lib, _ = result['symbol'].split(':', 1)
        symbols = list(self.root.glob('Libraries/DSMU/LCSC/symbols/*.kicad_sym'))
        self.assertEqual(len(symbols), 1)
        self.assertIn('DSMU:Chosen0603', symbols[0].read_text())
        self.assertIn(sym_lib, (self.root / 'sym-lib-table').read_text())
        self.assertEqual(self.service.commit(selected)['footprint'], result['footprint'])

    def test_new_library_and_independent_library(self):
        for mode in ('new', 'independent'):
            with self.subTest(mode=mode):
                preview = self.prepare()
                result = self.service.commit(self.choice(preview, target_mode=mode,
                    target_library='MyParts', target_folder='Libraries/DSMU'))
                self.assertTrue((self.root / result['target_path']).is_dir())
                self.assertIn(result['footprint'].split(':')[0], (self.root / 'fp-lib-table').read_text())

    def test_invalid_paths_names_and_overwrite_are_rejected(self):
        preview = self.prepare()
        for extra in ({'name': '../bad'}, {'name': 'CON'}, {'target_library': 'missing'},
                      {'target_mode': 'new', 'target_library': 'DSMU'},
                      {'target_mode': 'new', 'target_library': 'New', 'target_folder': '../outside'},
                      {'target_mode': 'new', 'target_library': 'New', 'target_folder': 'C:/outside'},
                      {'collision': 'overwrite'}, {'acknowledged': False}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.service.commit(self.choice(preview, **extra))
        self.assertFalse((self.pretty / '0603_C123.kicad_mod').exists())

    def test_independent_library_can_be_reused_in_a_later_preview(self):
        first = self.service.commit(self.choice(self.prepare(), target_mode='independent'))
        second = self.service.commit(self.choice(self.prepare(), target_mode='independent'))
        self.assertEqual(second['footprint'], first['footprint'])
        self.assertTrue(second['reused'])

    def test_changed_existing_file_and_changed_staging_are_not_accepted(self):
        preview = self.prepare()
        self.old.write_text(module('0603', .6))
        with self.assertRaisesRegex(ValueError, 'changed|变化'):
            self.service.commit(self.choice(preview))

    def test_number_mismatch_blocks_conversion_but_existing_can_be_reused(self):
        self.pin = 'EP'
        preview = self.prepare()
        self.assertFalse(preview['compatible'])
        with self.assertRaises(ValueError):
            self.service.commit(self.choice(preview))
        result = self.service.commit(self.choice(preview, source='existing', acknowledged=False))
        self.assertEqual(result['footprint'], 'DSMU:0603')
        self.assertFalse((self.root / 'sym-lib-table').exists())

    def test_external_staged_files_are_still_checked_for_changes(self):
        preview = self.prepare()
        state = self.service.previews[preview['preview_id']]
        state['module'].write_text(module(width=0.6))
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.service.commit(self.choice(preview))
        self.assertFalse((self.pretty / '0603_C123.kicad_mod').exists())

    def test_staged_paths_cannot_escape_the_preview_directory(self):
        from part_selection_library import digest
        preview = self.prepare()
        state = self.service.previews[preview['preview_id']]
        outside = Path(self.tmp.name) / 'outside.kicad_mod'
        outside.write_text(module())
        state['module'] = outside
        state['module_hash'] = digest(outside)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            self.service.commit(self.choice(preview))
        self.assertFalse((self.pretty / '0603_C123.kicad_mod').exists())

    def test_same_geometry_reuses_same_name(self):
        self.old.write_text(module('0603'))
        preview = self.prepare()
        result = self.service.commit(self.choice(preview))
        self.assertEqual(result['footprint'], 'DSMU:0603')

    def test_unknown_preview_is_not_trusted(self):
        with self.assertRaises(ValueError):
            self.service.commit({'preview_id': '../../fake'})

    def test_library_selection_is_lazy_and_never_converts_or_writes_libraries(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('part_selection_footprints.LibraryImporter.convert') as convert, \
             patch('part_selection_footprints.render_previews') as render:
            preview = self.service.prepare_library(['DSMU:0603'], package='0603')
            self.assertEqual(preview['kind'], 'library')
            self.assertIn('DSMU:0603', [e['id'] for e in preview['items']])
            detail = self.service.library_detail(preview['preview_id'], 'DSMU:0603')
            self.assertTrue(detail['compatible'])
            self.assertNotIn('svg', detail)
            result = self.service.commit({'preview_id': preview['preview_id'], 'source':'existing',
                'existing_id':'DSMU:0603', 'acknowledged':True})
            self.assertEqual(result['footprint'], 'DSMU:0603')
            convert.assert_not_called()
            render.assert_not_called()
        self.assertEqual(before, {p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_library_selection_checks_pin_numbers_ack_and_loaded_selection(self):
        (self.pretty / 'Other.kicad_mod').write_text(module('Other', pin='3'))
        preview = self.service.prepare_library(['DSMU:0603'])
        choice = {'preview_id':preview['preview_id'], 'source':'existing', 'existing_id':'DSMU:0603', 'acknowledged':True}
        with self.assertRaises(ValueError):
            self.service.commit(choice)
        self.service.library_detail(preview['preview_id'], 'DSMU:0603')
        with self.assertRaises(ValueError):
            self.service.commit({**choice, 'acknowledged':False})
        detail = self.service.library_detail(preview['preview_id'], 'DSMU:Other')
        self.assertFalse(detail['compatible'])
        with self.assertRaises(ValueError):
            self.service.commit({**choice, 'existing_id':'DSMU:Other'})

    def test_library_detail_preview_is_opt_in_and_selected_file_changes_rejected(self):
        preview = self.service.prepare_library(['DSMU:0603'])
        with patch('part_selection_footprints.render_previews', return_value={'converted_svg':'<svg/>'}) as render:
            detail = self.service.library_detail(preview['preview_id'], 'DSMU:0603', render=True)
            self.assertEqual(detail['svg'], '<svg/>')
            render.assert_called_once()
        self.old.write_text(module('0603', .5))
        with self.assertRaisesRegex(ValueError, '变化|changed'):
            self.service.commit({'preview_id':preview['preview_id'], 'source':'existing',
                'existing_id':'DSMU:0603', 'acknowledged':True})

    def test_library_selection_rejects_unknown_paths_and_unreadable_originals(self):
        preview = self.service.prepare_library(['Missing:0603'])
        detail = self.service.library_detail(preview['preview_id'], 'DSMU:0603')
        self.assertFalse(detail['compatible'])
        with self.assertRaises(ValueError):
            self.service.library_detail(preview['preview_id'], '../outside')
        with self.assertRaises(ValueError):
            self.service.commit({'preview_id':preview['preview_id'], 'source':'existing',
                'existing_id':'DSMU:0603', 'acknowledged':True})

    def test_staged_file_change_is_rejected(self):
        preview = self.prepare()
        staged = next(self.service.staging_root.rglob('R0603.kicad_mod'))
        staged.write_text(module(width=.4))
        with self.assertRaisesRegex(ValueError, 'changed|变化'):
            self.service.commit(self.choice(preview))

    def test_registration_failure_rolls_back_new_files(self):
        preview = self.prepare()
        table = self.root / 'fp-lib-table'
        before = table.read_bytes()
        original_open = Path.open
        def fail_table(path, *args, **kwargs):
            if path == self.root / 'sym-lib-table' and args and args[0] == 'xb':
                raise OSError('locked table')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', fail_table):
            with self.assertRaises(OSError):
                self.service.commit(self.choice(preview, name='Chosen0603'))
        self.assertEqual(table.read_bytes(), before)
        self.assertFalse((self.pretty / 'Chosen0603.kicad_mod').exists())
        self.assertEqual(list(self.root.glob('Libraries/DSMU/LCSC/symbols/*.kicad_sym')), [])

    def test_rename_preview_is_local_only_and_restricted_to_original_geometry(self):
        (self.pretty / 'Other.kicad_mod').write_text(module('Other'))
        with patch('part_selection_footprints.LibraryImporter.convert') as convert, \
             patch('part_selection_footprints.render_previews') as render, \
             patch.object(Path, 'glob', side_effect=AssertionError('rename must not scan every footprint')):
            preview = self.service.prepare_rename(['DSMU:0603'], package='R0603', code='C123')
        self.assertEqual(preview['kind'], 'rename')
        self.assertEqual(preview['suggested_name'], 'R0603')
        self.assertEqual([item['id'] for item in preview['items']], ['DSMU:0603'])
        convert.assert_not_called()
        render.assert_not_called()

    def test_rename_copies_original_without_geometric_or_library_side_effects(self):
        from part_selection_footprints import geometry
        original = module('0603', .7112)[:-1] + ('\n  ; keep original annotations\n'
            '(property "Reference" "REF**" (at 1 2 90) (layer "F.SilkS")) '
            '(fp_text user "Keep me" (at -1 3) (layer "B.SilkS")) '
            '(model "${KIPRJMOD}/3D/model.step" (offset (xyz 1 2 3)) (rotate (xyz 0 0 90))))')
        self.old.write_text(original, encoding='utf-8')
        before, table = self.old.read_bytes(), (self.root / 'fp-lib-table').read_bytes()
        preview = self.service.prepare_rename(['DSMU:0603'], 'R0603', code='C123')
        self.service.library_detail(preview['preview_id'], 'DSMU:0603')
        result = self.service.commit(self.choice(preview, source='rename', name='R0603'))
        self.assertEqual(result['footprint'], 'DSMU:R0603')
        self.assertEqual(self.old.read_bytes(), before)
        self.assertEqual((self.root / 'fp-lib-table').read_bytes(), table)
        self.assertEqual(geometry((self.pretty / 'R0603.kicad_mod').read_text()), geometry(before.decode()))
        self.assertEqual((self.pretty / 'R0603.kicad_mod').read_text(), original.replace('(footprint "0603"', '(footprint "R0603"', 1))
        self.assertFalse((self.root / 'sym-lib-table').exists())
        self.assertNotIn('symbol', result)
        self.assertEqual(self.service.commit(self.choice(preview, source='rename', name='R0603')), result)

    def test_rename_conflict_requires_choice_without_writing_or_suffixing(self):
        target = self.pretty / 'R0603.kicad_mod'
        target.write_text(module('R0603', width=1.2))
        (self.pretty / 'Other.kicad_mod').write_text(module('Other', width=1.4))
        before = target.read_bytes()
        preview = self.service.prepare_rename(['DSMU:0603'], 'R0603', code='C123')
        self.service.library_detail(preview['preview_id'], 'DSMU:0603')
        files = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.service.commit(self.choice(preview, source='rename', name='R0603'))
        self.assertEqual(result['conflict']['footprint'], 'DSMU:R0603')
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse((self.pretty / 'R0603_C123.kicad_mod').exists())
        self.assertNotIn('result', self.service.previews[preview['preview_id']])
        self.assertEqual(files, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_rename_overwrite_requires_bound_token_and_backs_up_original(self):
        from part_selection_footprints import rename_content
        target = self.pretty / 'R0603.kicad_mod'
        target.write_text(module('R0603', width=1.2))
        (self.pretty / 'Other.kicad_mod').write_text(module('Other', width=1.4))
        before = target.read_bytes()
        preview = self.service.prepare_rename(['DSMU:0603'], 'R0603', code='C123')
        self.service.library_detail(preview['preview_id'], 'DSMU:0603')
        choice = self.choice(preview, source='rename', name='R0603')
        offer = self.service.commit(choice)['conflict']
        for extra in ({'collision_token':'forged'}, {'name':'Other'}, {'source':'converted'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.service.commit({**choice, 'collision':'overwrite', 'collision_token':offer['token'], **extra})
        result = self.service.commit({**choice, 'collision':'overwrite', 'collision_token':offer['token']})
        self.assertEqual(result['footprint'], 'DSMU:R0603')
        self.assertTrue(result['overwritten'])
        self.assertEqual(rename_content(target.read_text()), rename_content(self.old.read_text()))
        backup = Path(result['backup'])
        transaction = json.loads((backup / 'transaction.json').read_text())
        index = [Path(p) for p in transaction['files']].index(target.relative_to(self.root))
        self.assertEqual((backup / str(index)).read_bytes(), before)
        self.assertFalse((self.pretty / 'R0603_C123.kicad_mod').exists())

    def test_changed_destination_requires_new_overwrite_approval(self):
        target = self.pretty / 'R0603.kicad_mod'
        preview = self.prepare()
        target.write_text(module('R0603', width=1.2))
        choice = self.choice(preview, name='R0603')
        offer = self.service.commit(choice)['conflict']
        target.write_text(module('R0603', width=1.4))
        changed = target.read_bytes()
        retry = self.service.commit({**choice, 'collision':'overwrite', 'collision_token':offer['token']})['conflict']
        self.assertTrue(retry['changed'])
        self.assertNotEqual(retry['token'], offer['token'])
        self.assertEqual(target.read_bytes(), changed)
        result = self.service.commit({**choice, 'collision':'overwrite', 'collision_token':retry['token']})
        self.assertEqual(result['footprint'], 'DSMU:R0603')
        self.assertTrue(result['overwritten'])

    def test_overwrite_rolls_back_destination_if_symbol_registration_fails(self):
        preview = self.prepare()
        choice = self.choice(preview)
        token = self.service.commit(choice)['conflict']['token']
        files = {p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        original_open = Path.open
        def fail_table(path, *args, **kwargs):
            if path == self.root / 'sym-lib-table' and args and args[0] == 'xb':
                raise OSError('locked table')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', fail_table), self.assertRaisesRegex(OSError, 'locked'):
            self.service.commit({**choice, 'collision':'overwrite', 'collision_token':token})
        self.assertEqual(files, {p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertNotIn('result', self.service.previews[preview['preview_id']])

    def test_rename_requires_loaded_original_and_acknowledgment(self):
        (self.pretty / 'Other.kicad_mod').write_text(module('Other'))
        preview = self.service.prepare_rename(['DSMU:0603'], 'R0603', code='C123')
        with self.assertRaises(ValueError):
            self.service.commit(self.choice(preview, source='rename', name='R0603'))
        with self.assertRaises(ValueError):
            self.service.library_detail(preview['preview_id'], 'DSMU:Other')
        self.service.library_detail(preview['preview_id'], 'DSMU:0603')
        with self.assertRaises(ValueError):
            self.service.commit(self.choice(preview, source='rename', name='R0603', acknowledged=False))

    def test_conversion_dialog_can_rename_original_even_if_converted_pins_differ(self):
        from part_selection_footprints import geometry
        self.pin = 'EP'
        preview = self.prepare()
        result = self.service.commit(self.choice(preview, source='rename', name='R0603'))
        self.assertEqual(result['footprint'], 'DSMU:R0603')
        self.assertEqual(geometry((self.pretty / 'R0603.kicad_mod').read_text()), geometry(self.old.read_text()))
        self.assertFalse((self.root / 'sym-lib-table').exists())

    def test_comparison_is_lazy_cached_and_import_uses_aligned_geometry(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        preview = self.prepare()
        alignment = {'ok': True, 'rotation_deg': 90, 'offset_mm': [1, 2], 'max_error_mm': 0}
        def align(converted, reference, output, *, method):
            self.assertEqual(method, 'center')
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(module('Aligned', width=.9))
            return alignment
        native = SimpleNamespace(align_module=Mock(side_effect=align), render_comparison=Mock(return_value={
            'view_box': [-2, -2, 4, 4], 'layers': [], 'existing_layers': {}, 'converted_layers': {}}))
        with patch.dict(sys.modules, {'part_selection_footprint_geometry': native}):
            result = self.service.comparison(preview['preview_id'], 'DSMU:0603')
            self.assertEqual(result['alignment'], alignment)
            self.assertEqual(result, self.service.comparison(preview['preview_id'], 'DSMU:0603'))
            self.assertFalse((self.pretty / '0603_C123.kicad_mod').exists())
            selected = self.service.commit(self.choice(preview, name='Aligned0603'))
        self.assertIn('(size 0.9 0.86)', (self.pretty / selected['footprint'].split(':')[1]).with_suffix('.kicad_mod').read_text())
        native.align_module.assert_called_once()
        native.render_comparison.assert_called_once()

    def test_failed_alignment_blocks_conversion_not_rename(self):
        from types import SimpleNamespace
        preview = self.prepare()
        native = SimpleNamespace(align_module=lambda *args, **kwargs: {'ok': False, 'reason': 'ambiguous pad positions'})
        with patch.dict(sys.modules, {'part_selection_footprint_geometry': native}):
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                self.service.commit(self.choice(preview))
            result = self.service.commit(self.choice(preview, source='rename', name='LocalCopy'))
        self.assertEqual(result['footprint'], 'DSMU:LocalCopy')

    def test_aligned_staging_tamper_is_rejected_before_publishing(self):
        from types import SimpleNamespace
        preview = self.prepare()
        def align(converted, reference, output, *, method):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(module('Aligned'))
            return {'ok': True, 'rotation_deg': 0, 'offset_mm': [0, 0], 'max_error_mm': 0}
        native = SimpleNamespace(align_module=align, render_comparison=lambda *args: {})
        with patch.dict(sys.modules, {'part_selection_footprint_geometry': native}):
            self.service.comparison(preview['preview_id'], 'DSMU:0603')
        staged = self.service.previews[preview['preview_id']]['aligned']['DSMU:0603']['path']
        staged.write_text(module(width=5))
        with self.assertRaisesRegex(ValueError, 'changed|变化'):
            self.service.commit(self.choice(preview))

    def test_rename_cannot_merge_originals_with_different_field_placements(self):
        self.old.write_text(module('0603')[:-1] + '(property "Value" "Local" (at 0 2) (layer "F.Fab")))')
        (self.pretty / 'Other.kicad_mod').write_text(module('Other')[:-1] + '(property "Value" "Other" (at 0 -2 90) (layer "F.Fab")))')
        preview = self.service.prepare_rename(['DSMU:0603', 'DSMU:Other'], 'Renamed', code='C123')
        self.service.library_detail(preview['preview_id'], 'DSMU:0603')
        with self.assertRaisesRegex(ValueError, '不一致|拆行'):
            self.service.commit(self.choice(preview, source='rename', name='Renamed'))
        self.assertFalse((self.pretty / 'Renamed.kicad_mod').exists())


if __name__ == '__main__':
    unittest.main()

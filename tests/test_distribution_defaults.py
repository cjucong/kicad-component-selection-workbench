"""Keep the original public release's project-independent defaults covered."""
import sys
import unittest
from pathlib import Path

from _scratch import temporary_project

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))


class DistributionDefaultsTest(unittest.TestCase):
    def test_bulk_sync_does_not_remap_another_projects_designators(self):
        from part_selection_server import DEFAULT_BULK_SYNC_REF_MAP
        self.assertEqual(DEFAULT_BULK_SYNC_REF_MAP, {})

    def test_config_uses_generic_project_sources(self):
        from part_selection_overrides import DEFAULT_CONFIG
        self.assertEqual(DEFAULT_CONFIG['source_bom'], '__schematic__')
        self.assertEqual(DEFAULT_CONFIG['lcsc_bom_pattern'], 'BOM/*LCSC*.csv')

    def test_lcsc_detection_has_no_original_project_prefix(self):
        from part_selection_helper import find_latest_lcsc_csv
        with temporary_project() as directory:
            bom = Path(directory) / 'Example_LCSC_BOM.csv'
            bom.write_text('Designator,Value\nR1,10k\n')
            self.assertEqual(find_latest_lcsc_csv(Path(directory)), bom)

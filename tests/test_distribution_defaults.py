import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


class DistributionDefaultsTest(unittest.TestCase):
    def test_bulk_sync_never_remaps_another_projects_designators(self):
        from part_selection_server import DEFAULT_BULK_SYNC_REF_MAP

        self.assertEqual(DEFAULT_BULK_SYNC_REF_MAP, {})

    def test_default_config_uses_generic_project_sources(self):
        from part_selection_overrides import DEFAULT_CONFIG

        self.assertEqual(DEFAULT_CONFIG["source_bom"], "__schematic__")
        self.assertEqual(DEFAULT_CONFIG["lcsc_bom_pattern"], "BOM/*LCSC*.csv")

    def test_lcsc_csv_detection_is_not_tied_to_the_original_project(self):
        from part_selection_helper import find_latest_lcsc_csv

        (ROOT / "Temp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "Temp") as directory:
            bom = Path(directory) / "Example_LCSC_BOM.csv"
            bom.write_text("Designator,Value\nR1,10k\n", encoding="utf-8")
            self.assertEqual(find_latest_lcsc_csv(Path(directory)), bom)


if __name__ == "__main__":
    unittest.main()

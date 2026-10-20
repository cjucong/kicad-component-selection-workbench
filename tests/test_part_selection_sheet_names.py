import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from part_selection_helper import load_schematic_parts
from part_selection_server import group_parts_for_review


class SchematicSheetNameTest(unittest.TestCase):
    def test_renamed_sheet_is_used_for_display_and_jump_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = root / "DOC"
            doc.mkdir()
            (doc / "LDO.kicad_sch").write_text(
                '(kicad_sch (symbol (property "Reference" "U27") '
                '(property "Value" "TPS82130SILR")))', encoding="utf-8"
            )
            hierarchy = root / "main.kicad_sch"
            hierarchy.write_text(
                '(kicad_sch (sheet (property "Sheetname" "POWER") '
                '(property "Sheetfile" "DOC/LDO.kicad_sch")))', encoding="utf-8"
            )

            part = load_schematic_parts(doc)["U27"]
            self.assertEqual(part.page, "POWER")
            row = group_parts_for_review([part], {})[0]
            self.assertEqual(row["page_refs"], "POWER: U27")
            self.assertEqual(row["ref_links"][0]["url"], "kicad-sch-search://U27?page=POWER")

            hierarchy.write_text(hierarchy.read_text(encoding="utf-8").replace("POWER", "power"), encoding="utf-8")
            self.assertEqual(load_schematic_parts(doc)["U27"].page, "power")

    def test_sibling_sheet_file_is_resolved_relative_to_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "DOC"
            doc.mkdir()
            (doc / "Top.kicad_sch").write_text(
                '(kicad_sch (sheet (property "Sheetname" "Power \\"A\\"") '
                '(property "Sheetfile" "LDO.kicad_sch")))', encoding="utf-8"
            )
            (doc / "LDO.kicad_sch").write_text(
                '(kicad_sch (symbol (property "Reference" "U23")))', encoding="utf-8"
            )
            self.assertEqual(load_schematic_parts(doc)["U23"].page, 'Power "A"')

    def test_unreferenced_sheet_keeps_filename_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp)
            (doc / "LDO.kicad_sch").write_text(
                '(kicad_sch (symbol (property "Reference" "U44")))', encoding="utf-8"
            )
            self.assertEqual(load_schematic_parts(doc)["U44"].page, "LDO")

    def test_distinct_footprint_ids_are_not_silently_merged(self):
        from part_selection_helper import PartInfo

        rows = group_parts_for_review([
            PartInfo(ref=ref, page="POWER", comment="TPS82130SILR", value="TPS82130SILR", footprint=fp)
            for ref, fp in [("U23", "DSMU:USIP__PCB_U23"), ("U44", "DSMU:USIP__PCB_U23"), ("U27", "DSMU:USIP__PCB_U27")]
        ], {})
        self.assertEqual([row["refs"] for row in rows], ["U23,U44", "U27"])


if __name__ == "__main__":
    unittest.main()

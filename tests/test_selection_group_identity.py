import copy
import sys
from _scratch import temporary_project
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from part_selection_helper import PartInfo
from part_selection_server import (PartSelectionApp, group_parts_for_review,
                                   row_schematic_sync_fields, saved_parameter_signature,
                                   enrich_rows_with_schematic_sync_state)
from part_selection_overrides import load_overrides, save_overrides
from part_stock_lookup import enrich_rows_with_cached_stock


class SelectionGroupIdentityTest(unittest.TestCase):
    def parts(self, first_code="C5596752", second_code="C5596752"):
        return [PartInfo(ref=ref, comment="Capacitor", value="47uF",
                         footprint="DSMU:0805", accepted_c_code=code)
                for ref, code in (("C116", first_code), ("C1669", second_code))]

    def test_same_code_description_formats_merge_without_losing_notes_or_sources(self):
        descriptions = [
            "型号: CL21A476MPYNNWE\nCAP CER 47uF 10V X5R 0805\n"
            "容值: 47uF; 温度系数: X5R; 精度: ±20%; 额定电压: 10V\n"
            "Tape & Reel (TR)\nMultilayer Ceramic Capacitors MLCC - SMD/SMT",
            "型号: CL21A476MPYNNWE\n"
            "容值: 47uF; 精度: ±20%; 额定电压: 10V; 温度系数: X5R\n圆盘\n贴片电容(MLCC)",
        ]
        overrides = {
            ref: {"field_overrides": {"description": description,
                                      "description_c_code": "C5596752"}}
            for ref, description in zip(("C116", "C1669"), descriptions)
        }
        overrides["C1669"].update(review_status="Pending", note="trial: verify DC bias")
        before = copy.deepcopy(overrides)
        rows = group_parts_for_review(self.parts(), overrides)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["refs"], "C116,C1669")
        self.assertEqual(row["description_by_ref"], dict(zip(("C116", "C1669"), descriptions)))
        self.assertEqual(row["notes_by_ref"], {"C1669": "trial: verify DC bias"})
        self.assertIn("C1669", row["note"])
        self.assertIn("verify DC bias", row["note"])
        self.assertIn(row_schematic_sync_fields(row)["description"], descriptions)
        self.assertEqual(overrides, before)

    def test_only_comment_value_and_footprint_control_group_identity(self):
        descriptions = ("47uF, 10V, X5R", "47uF, 25V, X7R")
        overrides = {
            ref: {"review_status": status,
                  "alternate_parts": [{"c_code": alt}],
                  "procurement_choice": {"supplier": supplier},
                  "field_overrides": {"description": description, "description_c_code": "C5596752"}}
            for ref, status, alt, supplier, description in (
                ("C116", "Pending", "C101", "A", descriptions[0]),
                ("C1669", "Confirmed", "C202", "B", descriptions[1]),
            )
        }
        rows = group_parts_for_review(self.parts(), overrides)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["refs"], "C116,C1669")
        self.assertEqual(rows[0]["per_ref_selection"]["C116"]["review_status"], "Pending")
        self.assertEqual(rows[0]["per_ref_selection"]["C1669"]["review_status"], "Pending")
        self.assertNotEqual(rows[0]["per_ref_selection"]["C116"]["alternate_parts"],
                            rows[0]["per_ref_selection"]["C1669"]["alternate_parts"])

    def test_saving_unchanged_aggregate_preserves_individual_notes(self):
        with temporary_project() as directory:
            app = PartSelectionApp.__new__(PartSelectionApp)
            app.overrides_path = Path(directory) / "overrides.json"
            original = {"C116": {"note": "original note"}, "C1669": {"note": "trial note"}}
            note = group_parts_for_review(self.parts(), original)[0]["note"]
            for field_save in (False, True):
                with self.subTest(field_save=field_save):
                    save_overrides(app.overrides_path, original)
                    payload = {"refs": "C116,C1669", "note": note}
                    if field_save:
                        app.save_field_override({**payload, "field_overrides": {"value": "47uF"}})
                    else:
                        app.save_override({**payload, "review_status": "Confirmed"})
                    actual = load_overrides(app.overrides_path)
                    self.assertEqual({ref: item["note"] for ref, item in actual.items()},
                                     {ref: item["note"] for ref, item in original.items()})
            app.save_override({"refs": "C116,C1669", "note": "new shared note"})
            self.assertTrue(all(record["note"] == "new shared note"
                                for record in load_overrides(app.overrides_path).values()))

    def test_different_codes_clear_entire_merged_row(self):
        rows = group_parts_for_review(self.parts("C15850", "C907991"), {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["effective_c_code"], "")
        self.assertEqual(rows[0]["per_ref_selection"]["C116"]["effective_c_code"], "")
        self.assertEqual(rows[0]["per_ref_selection"]["C1669"]["effective_c_code"], "")
        self.assertEqual(rows[0]["review_status"], "Pending")
        self.assertFalse(rows[0]["selection_codes_mixed"])

    def test_common_effective_code_is_not_rebuilt_from_historical_codes(self):
        overrides = {"C116": {"field_overrides": {"description": "model: PART; rating: 10V",
                                                  "description_c_code": "C123"}},
                     "C1669": {"decision": "AcceptManual", "manual_c_code": "C123",
                                "field_overrides": {"description": "rating: 10V; model: PART",
                                                    "description_c_code": "C123"}}}
        rows = group_parts_for_review(self.parts("C123", "C456"), overrides)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["effective_c_code"], "C123")
        fields = row_schematic_sync_fields(rows[0])
        self.assertEqual(fields["lcsc_c_code"], "C123")
        self.assertEqual(fields["description"], "model: PART; rating: 10V")

    def test_list_limits_remain_attached_to_parameter_names(self):
        first = "working temperature: -40C、85C; storage temperature: -55C、125C"
        second = "working temperature: -40C、125C; storage temperature: -55C、85C"
        self.assertNotEqual(saved_parameter_signature(first, "DSMU:0805"),
                            saved_parameter_signature(second, "DSMU:0805"))

    def test_merged_equivalent_description_does_not_create_pending_sync(self):
        first = "model: PART; voltage: 10V"
        second = "voltage: 10V; model: PART"
        rows = group_parts_for_review(self.parts(), {
            ref: {"field_overrides": {"description": description, "description_c_code": "C5596752"}}
            for ref, description in (("C116", first), ("C1669", second))})
        fields = row_schematic_sync_fields(rows[0])
        actual = {"C116": {**fields, "description": first},
                  "C1669": {**fields, "description": second}}
        enrich_rows_with_schematic_sync_state(rows, Path("unused"), actual_by_ref=actual)
        self.assertFalse(rows[0]["field_changed_refs"])

    def test_conflicting_manual_selection_requires_reselection(self):
        rows = group_parts_for_review(self.parts(), {
            "C1669": {"decision": "AcceptManual", "manual_c_code": "C907991"}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["per_ref_selection"]["C116"]["effective_c_code"], "")
        self.assertEqual(rows[0]["per_ref_selection"]["C1669"]["effective_c_code"], "")

    def test_pending_and_confirmed_share_one_pending_state(self):
        rows = group_parts_for_review(self.parts(), {"C116": {"review_status": "Confirmed"}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["per_ref_selection"]["C116"]["review_status"], "Pending")
        self.assertEqual(rows[0]["per_ref_selection"]["C1669"]["review_status"], "Pending")
        self.assertEqual(rows[0]["review_status"], "Pending")
        self.assertFalse(rows[0]["review_status_mixed"])

    def test_code_present_on_only_some_references_marks_aggregate_unconfirmed(self):
        rows = group_parts_for_review(self.parts("C5596752", ""), {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["review_status"], "Pending")
        self.assertFalse(rows[0]["selection_codes_mixed"])
        self.assertEqual(rows[0]["per_ref_selection"]["C116"]["effective_c_code"], "C5596752")
        self.assertEqual(rows[0]["per_ref_selection"]["C1669"]["effective_c_code"], "C5596752")

    def test_all_references_without_codes_do_not_count_as_code_disagreement(self):
        rows = group_parts_for_review(self.parts("", ""), {
            "C116": {"review_status": "Confirmed"},
            "C1669": {"review_status": "Confirmed"},
        })
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["review_status"], "Confirmed")
        self.assertFalse(rows[0]["selection_codes_mixed"])

    def test_status_and_code_disagreement_are_reported_as_unconfirmed_by_export_logic(self):
        from part_selection_server import row_filter_keys, row_is_pump_exportable, row_review_status
        confirmed_vs_pending = group_parts_for_review(
            self.parts(), {"C116": {"review_status": "Confirmed"}}
        )[0]
        self.assertEqual(row_review_status(confirmed_vs_pending), "Pending")
        self.assertIn("pending_review", row_filter_keys(confirmed_vs_pending))
        mixed_codes = group_parts_for_review(self.parts("C123", "C456"), {})[0]
        self.assertFalse(row_is_pump_exportable(mixed_codes))
        self.assertIn("pending_review", row_filter_keys(mixed_codes))

    def test_rejected_and_unselected_share_row_without_losing_disposition(self):
        rows = group_parts_for_review(self.parts("", ""), {"C1669": {"decision": "Reject"}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["per_ref_selection"]["C1669"]["decision"], "Reject")
        self.assertEqual(rows[0]["per_ref_selection"]["C116"]["decision"], "")

    def test_explicit_value_constraints_and_alternates_stay_separate(self):
        parts = self.parts()
        parts[1].value = "47uF, 25V, X7R"
        self.assertEqual(len(group_parts_for_review(parts, {})), 2)
        parts[1].value = "47uF"
        overrides = {"C1669": {"alternate_parts": [{"c_code": "C123"}]}}
        rows = group_parts_for_review(parts, overrides)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["refs"], "C116,C1669")
        self.assertEqual(rows[0]["per_ref_selection"]["C1669"]["alternate_parts"],
                         [{"c_code": "C123", "model": "", "package": "", "param": ""}])

    def test_sync_fields_remain_per_reference_inside_visual_group(self):
        overrides = {
            "C116": {"accepted_c_code": "C123", "field_overrides": {
                "description": "model: A", "description_c_code": "C123"}},
            "C1669": {"accepted_c_code": "C456", "field_overrides": {
                "description": "model: B", "description_c_code": "C456"}},
        }
        row = group_parts_for_review(self.parts("C123", "C456"), overrides)[0]
        for fields in row["per_ref_sync_fields"].values():
            self.assertNotIn("lcsc_c_code", fields)
            self.assertNotIn("description", fields)
        self.assertEqual(row["override"]["field_overrides"]["description"], "model: A")

    def test_cleared_conflicting_codes_do_not_load_old_stock(self):
        with temporary_project() as directory:
            cache_path = Path(directory) / "stock.json"
            cache_path.write_text('{"C15850":{"ok":true,"c_code":"C15850","stock":"12"},'
                                  '"C907991":{"ok":true,"c_code":"C907991","stock":"345"}}',
                                  encoding="utf-8")
            rows = group_parts_for_review(self.parts("C15850", "C907991"), {})
            from part_selection_server import enrich_rows_with_selection_stock
            enrich_rows_with_selection_stock(rows, cache_path, fetch=False)
            self.assertEqual(rows[0]["stock"]["status"], "NeedCCode")
            self.assertEqual(rows[0]["stock"]["c_code"], "")
            self.assertNotIn("entries", rows[0]["stock"])

    def test_schematic_diff_uses_each_reference_description_and_code(self):
        overrides = {
            "C116": {"accepted_c_code": "C123", "field_overrides": {
                "description": "model: A", "description_c_code": "C123"}},
            "C1669": {"accepted_c_code": "C456", "field_overrides": {
                "description": "model: B", "description_c_code": "C456"}},
        }
        rows = group_parts_for_review(self.parts("C123", "C456"), overrides)
        actual = {ref: dict(fields) for ref, fields in rows[0]["per_ref_sync_fields"].items()}
        actual["C1669"]["value"] = "different"
        enrich_rows_with_schematic_sync_state(rows, Path("unused"), actual_by_ref=actual)
        self.assertEqual(rows[0]["field_changed_refs"], "C1669")
        self.assertEqual(rows[0]["field_diffs"]["value"]["refs"], "C1669")

    def test_cached_description_provenance_is_not_a_schematic_field(self):
        from part_selection_server import enrich_rows_with_selection_stock, SCHEMATIC_SYNC_FIELDS
        import json

        with temporary_project() as directory:
            cache_path = Path(directory) / "stock.json"
            cache_path.write_text(json.dumps({"C5596752": {
                "ok": True, "c_code": "C5596752", "model": "TEST47UF", "stock": "12",
                "param": "47uF 10V X5R 0805"}}), encoding="utf-8")
            for real_change in (False, True):
                with self.subTest(real_change=real_change):
                    rows = group_parts_for_review(self.parts(), {})
                    enrich_rows_with_selection_stock(rows, cache_path, fetch=False)
                    per_ref = rows[0]["per_ref_sync_fields"]
                    self.assertEqual(per_ref["C116"]["description_c_code"], "C5596752")
                    actual = {ref: {key: value for key, value in fields.items()
                                    if key in SCHEMATIC_SYNC_FIELDS}
                              for ref, fields in per_ref.items()}
                    if real_change:
                        actual["C1669"]["value"] = "different"
                    enrich_rows_with_schematic_sync_state(rows, Path("unused"), actual_by_ref=actual)
                    self.assertEqual(rows[0].get("field_changed_refs", ""),
                                     "C1669" if real_change else "")
                    self.assertNotIn("description_c_code", rows[0].get("field_diffs", {}))
                    if real_change:
                        self.assertEqual(set(rows[0]["field_diffs_by_ref"]["C1669"]), {"value"})


if __name__ == "__main__":
    unittest.main()

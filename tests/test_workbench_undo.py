import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from _scratch import temporary_project

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from part_selection_overrides import load_overrides, save_override, save_overrides
from part_selection_server import PartSelectionApp, render_index
from part_selection_helper import PartInfo


class WorkbenchUndoTest(unittest.TestCase):
    def setUp(self):
        directory = temporary_project(prefix="undo_")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        config = self.root / "config.json"
        config.write_text(json.dumps({"source_bom": "__schematic__", "schematic_dir": "DOC"}))
        self.app = PartSelectionApp(config)
        self.actual = {"R1": {"comment": "resistor", "value": "10k", "footprint": "Lib:0603",
                              "lcsc_c_code": "C101", "description": "Original specifications"}}
        self.reader = mock.patch.object(self.app, "read_schematic_fields", side_effect=lambda *args: {
            ref: dict(self.actual[ref]) for ref in args[1] if ref in self.actual})
        self.reader.start()
        self.addCleanup(self.reader.stop)
        self.initial = {"R1": {"accepted_c_code": "C101", "manual_c_code": "C101",
                               "stock_c_code": "C101", "decision": "AcceptManual",
                               "review_status": "Confirmed", "review_confirmed_at": "2020-01-01T00:00:00",
                               "schematic_synced_at": "2020-01-01T00:00:00", "note": "keep note",
                               "field_overrides": {"value": "10k", "description": "Original specifications",
                                                   "description_c_code": "C101"}},
                        "U2": {"note": "unrelated", "review_status": "Pending"}}
        save_overrides(self.app.overrides_path, self.initial)

    def edit(self, **fields):
        return self.app.save_field_override({"refs": "R1", "field_overrides": fields})

    def preview(self, **extra):
        return self.app.undo_workbench_changes({"refs": "R1", **extra})

    def commit(self, preview):
        return self.app.undo_workbench_changes({"refs": "R1", "confirm": True, "revision": preview["revision"]})

    def test_edit_chain_restores_first_snapshot_after_reload_without_touching_cad(self):
        self.edit(value="22k", footprint="Lib:0805")
        self.app.save_override({"refs": "R1", "accepted_c_code": "C202", "manual_c_code": "C202",
                                "stock_c_code": "C202", "review_status": "Pending", "note": "changed"})
        self.edit(value="33k")
        loaded = load_overrides(self.app.overrides_path)
        self.assertEqual(loaded["R1"]["workbench_undo"]["record"], self.initial["R1"])
        before_preview = self.app.overrides_path.read_bytes()
        preview = self.preview()
        self.assertEqual(self.app.overrides_path.read_bytes(), before_preview)
        self.assertEqual(preview["mode"], "snapshot")
        self.assertTrue(preview["changes"])
        with mock.patch("part_selection_server.update_schematic_symbol_field_map") as cad_write:
            self.commit(preview)
            cad_write.assert_not_called()
        restored = load_overrides(self.app.overrides_path)
        self.assertEqual(restored, self.initial)

    def test_new_record_can_be_removed_on_undo(self):
        save_overrides(self.app.overrides_path, {"U2": self.initial["U2"]})
        self.edit(value="22k")
        self.commit(self.preview())
        self.assertEqual(load_overrides(self.app.overrides_path), {"U2": self.initial["U2"]})

    def test_stale_snapshot_offers_saved_schematic_preview_not_synced_history(self):
        records = json.loads(json.dumps(self.initial))
        records["R1"]["workbench_undo"] = {"record": {
            **self.initial["R1"], "schematic_synced_at": "2019-01-01",
            "field_overrides": {"value": "obsolete"}}, "created_at": "2019-01-02"}
        records["R1"]["field_overrides"]["footprint"] = "Lib:Renamed"
        save_overrides(self.app.overrides_path, records)
        before = self.app.overrides_path.read_bytes()
        preview = self.preview()
        self.assertEqual(preview["mode"], "schematic")
        self.assertTrue(preview["available"])
        self.assertEqual(self.app.overrides_path.read_bytes(), before)
        self.assertFalse(any(item["new"] == "obsolete" for item in preview["changes"]))
        self.commit(preview)
        restored = load_overrides(self.app.overrides_path)["R1"]
        self.assertEqual(restored["field_overrides"]["footprint"], "Lib:0603")
        self.assertEqual(restored["schematic_synced_at"], self.initial["R1"]["schematic_synced_at"])
        self.assertNotIn("workbench_undo", restored)

    def test_fresh_edit_replaces_snapshot_from_previous_sync_epoch(self):
        records = json.loads(json.dumps(self.initial))
        records["R1"]["workbench_undo"] = {"record": {
            **self.initial["R1"], "schematic_synced_at": "2019-01-01"},
            "created_at": "2019-01-02"}
        save_overrides(self.app.overrides_path, records)
        self.edit(footprint="Lib:Renamed")
        preview = self.preview()
        self.assertEqual(preview["mode"], "snapshot")
        self.commit(preview)
        self.assertEqual(load_overrides(self.app.overrides_path), self.initial)

    def test_stale_preview_is_rejected_without_writing(self):
        self.edit(value="22k")
        preview = self.preview()
        self.edit(value="33k")
        before = self.app.overrides_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "变化|修改"):
            self.commit(preview)
        self.assertEqual(self.app.overrides_path.read_bytes(), before)

    def test_sync_clears_snapshot_and_prevents_reversing_synced_edits(self):
        self.edit(value="22k")
        preview = self.preview()
        self.app.record_schematic_sync_overrides({}, {"R1": {"comment": "resistor", "value": "22k",
                                                              "footprint": "Lib:0603"}})
        self.actual["R1"]["value"] = "22k"
        self.assertNotIn("workbench_undo", load_overrides(self.app.overrides_path)["R1"])
        with self.assertRaises(ValueError):
            self.commit(preview)
        self.assertFalse(self.preview()["available"])

    def test_schematic_changed_after_preview_rejects_commit(self):
        self.edit(value="22k")
        preview = self.preview()
        self.actual["R1"]["value"] = "47k"
        before = self.app.overrides_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "变化|修改"):
            self.commit(preview)
        self.assertEqual(self.app.overrides_path.read_bytes(), before)

    def test_legacy_pending_edits_restore_saved_schematic_without_guessing_history(self):
        save_override(self.app.overrides_path, "R1", {"accepted_c_code": "C202", "manual_c_code": "C202",
            "stock_c_code": "C202", "field_overrides": {"value": "22k", "footprint": "Lib:0805",
                                                        "description": "New specifications", "description_c_code": "C202"}})
        preview = self.preview()
        self.assertEqual(preview["mode"], "schematic")
        self.commit(preview)
        restored = load_overrides(self.app.overrides_path)
        self.assertEqual(restored["R1"]["accepted_c_code"], "C101")
        self.assertEqual(restored["R1"]["field_overrides"]["value"], "10k")
        self.assertEqual(restored["R1"]["field_overrides"]["description"], "Original specifications")
        self.assertEqual(restored["R1"]["review_confirmed_at"], "2020-01-01T00:00:00")
        self.assertEqual(restored["U2"], self.initial["U2"])

    def test_missing_schematic_ref_aborts_whole_row(self):
        self.edit(value="22k")
        self.actual.clear()
        before = self.app.overrides_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "缺失|不存在"):
            self.preview()
        self.assertEqual(self.app.overrides_path.read_bytes(), before)

    def test_note_only_edit_restores_confirmation_time_and_does_not_require_cad_change(self):
        self.app.save_override({"refs": "R1", "note": "new note", "review_status": "Pending"})
        self.commit(self.preview())
        self.assertEqual(load_overrides(self.app.overrides_path), self.initial)

    def test_failed_restore_keeps_latest_record_and_snapshot_for_retry(self):
        self.edit(value="22k")
        preview = self.preview()
        before = self.app.overrides_path.read_bytes()
        with mock.patch("part_selection_overrides.os.replace", side_effect=OSError("file locked")):
            with self.assertRaises(OSError):
                self.commit(preview)
        self.assertEqual(self.app.overrides_path.read_bytes(), before)
        self.commit(self.preview())
        self.assertEqual(load_overrides(self.app.overrides_path), self.initial)

    def test_partial_sync_only_clears_successful_refs(self):
        self.app.save_override({"refs": "R1,U2", "note": "changed"})
        self.app.record_schematic_sync_overrides({}, {"R1": self.actual["R1"]})
        records = load_overrides(self.app.overrides_path)
        self.assertNotIn("workbench_undo", records["R1"])
        self.assertIn("workbench_undo", records["U2"])

    def test_undo_cannot_clear_an_unpreviewed_sibling_selection_after_regroup(self):
        self.edit(value="22k")
        save_override(self.app.overrides_path, "R2", {"accepted_c_code": "C202", "stock_c_code": "C202",
            "review_status": "Confirmed", "field_overrides": {"value": "10k"}})
        parts = {ref: PartInfo(ref, comment="resistor", value="10k", footprint="Lib:0603")
                 for ref in ("R1", "R2")}
        before = self.app.overrides_path.read_bytes()
        with mock.patch.object(self.app, "schematic_parts", return_value=parts):
            with self.assertRaisesRegex(ValueError, "合并|冲突"):
                self.preview()
        self.assertEqual(self.app.overrides_path.read_bytes(), before)

    def test_adopting_one_cad_field_preserves_undo_for_other_workbench_edits(self):
        self.app.save_override({"refs": "R1", "note": "new note"})
        self.edit(value="22k")
        self.actual["R1"]["value"] = "47k"
        self.app.adopt_schematic_fields({"refs": "R1", "fields": ["value"]})
        self.assertTrue(self.preview()["available"])
        self.commit(self.preview())
        restored = load_overrides(self.app.overrides_path)["R1"]
        self.assertEqual(restored["note"], "keep note")
        self.assertEqual(restored["field_overrides"]["value"], "47k")

    def test_already_synced_single_preflight_commits_snapshot_without_touching_cad(self):
        self.edit(value="22k")
        self.actual["R1"]["value"] = "22k"
        with mock.patch.object(self.app, "verify_schematic_field_values", return_value={"ok": True}), \
             mock.patch.object(self.app, "write_schematic_fields") as write:
            result = self.app.sync_schematic({"refs": "R1", "field_overrides": {"value": "22k"}})
            write.assert_not_called()
        self.assertTrue(result["already_synced"])
        self.assertNotIn("workbench_undo", load_overrides(self.app.overrides_path)["R1"])

    def test_code_only_verified_sync_also_retires_prior_undo(self):
        self.app.save_override({"refs": "R1", "accepted_c_code": "C202", "manual_c_code": "C202",
                                "stock_c_code": "C202"})
        self.actual["R1"]["lcsc_c_code"] = "C202"
        with mock.patch.object(self.app, "verify_schematic_field_values", return_value={"ok": True}):
            result = self.app.sync_schematic({"refs": "R1", "field_overrides": {"lcsc_c_code": "C202"}})
        self.assertTrue(result["already_synced"])
        self.assertNotIn("workbench_undo", load_overrides(self.app.overrides_path)["R1"])
        self.assertFalse(self.preview()["available"])

    def test_already_synced_bulk_preflight_commits_snapshot_without_touching_cad(self):
        self.edit(value="22k")
        self.actual["R1"]["value"] = "22k"
        part = PartInfo("R1", comment="resistor", value="22k", footprint="Lib:0603")
        row = {"refs_list": ["R1"], "comment": "resistor", "value": "22k", "footprint": "Lib:0603"}
        with mock.patch.object(self.app, "rows", return_value=[row]), \
             mock.patch.object(self.app, "schematic_parts", return_value={"R1": part}), \
             mock.patch.object(self.app, "verify_schematic_field_values", return_value={"ok": True}), \
             mock.patch.object(self.app, "write_schematic_fields") as write:
            result = self.app.bulk_sync_bom_state_to_schematic("__schematic__")
            write.assert_not_called()
        self.assertTrue(result["already_synced"])
        self.assertNotIn("workbench_undo", load_overrides(self.app.overrides_path)["R1"])

    def test_ui_exposes_preview_and_confirm_with_edit_queue_flush(self):
        source = render_index(self.app)
        self.assertIn('data-action="undo-workbench"', source)
        self.assertIn('"/api/undo_workbench"', source)
        self.assertIn("async function undoWorkbenchChanges", source)
        block = source.split("async function undoWorkbenchChanges", 1)[1].split("async function", 1)[0]
        self.assertIn("await flushRowEdits()", block)
        self.assertIn("window.confirm", block)


if __name__ == "__main__":
    unittest.main()

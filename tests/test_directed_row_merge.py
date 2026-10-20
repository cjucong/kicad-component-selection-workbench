"""An explicit field edit inherits the existing destination's selection, not a conflict clear."""
import copy
import json
import pytest

from part_selection_overrides import load_overrides, save_overrides
from part_selection_runtime import runtime_directory
from part_selection_server import PartSelectionApp


def make_app(root):
    schematic = root / "test.kicad_sch"
    schematic.write_text('(kicad_sch ' + ''.join(
        '(symbol (property "Reference" "' + ref + '") (property "Comment" "capacitor") '
        '(property "Value" "' + value + '") (property "Footprint" "Lib:0603") '
        '(property "LCSC" "' + code + '") (property "Description" "' + model + '"))'
        for ref, value, code, model in (("C1", "4.7nF", "C100", "SOURCE"),
                                       ("C2", "4.7nF", "C100", "SOURCE"),
                                       ("C9", "4.7nF 50V", "C200", "TARGET"))
    ) + ')')
    config = root / "config.json"
    config.write_text(json.dumps({"source_bom": "__schematic__", "schematic_dir": "."}))
    app = PartSelectionApp(config)
    save_overrides(app.overrides_path, {
        "C1": {"note": "source note", "schematic_synced_at": "2026-01-01T00:00:00",
               "field_overrides": {"description": "OLD", "description_c_code": "C100"},
               "alternate_parts": [{"c_code": "C101"}]},
        "C9": {"note": "target note", "review_status": "Confirmed", "review_confirmed_at": "2026-01-02T00:00:00",
               "field_overrides": {"description": "TARGET 50V", "description_c_code": "C200"},
               "alternate_parts": [{"c_code": "C201", "model": "ALT-TARGET"}]}})
    return app, schematic


def test_value_edit_inherits_destination_and_survives_reload(scratch_project):
    app, schematic = make_app(scratch_project)
    try:
        before = load_overrides(app.overrides_path)
        cad = schematic.read_bytes()
        result = app.save_field_override({"refs": "C1,C2", "field_overrides": {"value": "4.7nF 50V"}})
        row = app.rows(include_stock=False)[0]
        assert row["refs"] == "C1,C2,C9"
        assert row["effective_c_code"] == "C200"
        assert row["review_status"] == "Confirmed"
        saved = load_overrides(app.overrides_path)
        assert saved["C9"] == before["C9"], "The existing destination must remain untouched"
        for ref in ("C1", "C2"):
            assert saved[ref]["manual_c_code"] == "C200"
            assert saved[ref]["field_overrides"]["description"] == "TARGET 50V"
            assert saved[ref]["alternate_parts"] == before["C9"]["alternate_parts"]
            assert saved[ref]["review_confirmed_at"] == before["C9"]["review_confirmed_at"]
            assert row["per_ref_sync_fields"][ref]["lcsc_c_code"] == "C200"
        assert saved["C1"]["note"] == "source note"
        assert saved["C1"]["schematic_synced_at"] == before["C1"]["schematic_synced_at"]
        assert row["field_diffs_by_ref"]["C1"]["value"]["old"] == "4.7nF"
        assert schematic.read_bytes() == cad
        assert result["merged_into"][0]["target_refs"] == ["C9"]
        backup = list(runtime_directory(scratch_project).glob("row_merge_*/overrides_before.json"))
        assert len(backup) == 1 and load_overrides(backup[0]) == before
        app.save_field_override({"refs": "C1,C2,C9", "field_overrides": {"value": "4.7nF 50V"}})
        assert app.rows(include_stock=False)[0]["effective_c_code"] == "C200"
        assert len(list(runtime_directory(scratch_project).glob("row_merge_*/overrides_before.json"))) == 1
    finally:
        app.search_jobs.close()
        app.selection_jobs.close()


def test_no_destination_does_not_replace_selection(scratch_project):
    app, _ = make_app(scratch_project)
    try:
        result = app.save_field_override({"refs": "C1,C2", "field_overrides": {"value": "4.7nF 250V"}})
        rows = app.rows(include_stock=False)
        assert len(rows) == 2
        assert {r["effective_c_code"] for r in rows} == {"C100", "C200"}
        assert not result.get("merged_into")
        assert not list(runtime_directory(scratch_project).glob("row_merge_*"))
    finally:
        app.search_jobs.close()
        app.selection_jobs.close()


@pytest.mark.parametrize("field,old_value,new_value", [
    ("value", "4.7nF", "4.7nF 50V"),
    ("comment", "Capacitor", "capacitor"),
    ("footprint", "Other:0603", "Lib:0603"),
])
def test_all_group_fields_have_direction_not_reference_order(scratch_project, field, old_value, new_value):
    from part_selection_helper import load_schematic_parts
    app, schematic = make_app(scratch_project)
    try:
        records = load_overrides(app.overrides_path)
        # C9 moves into the lower-numbered C1/C2 group; do not retain C9's old selection.
        records["C2"] = {"alternate_parts": copy.deepcopy(records["C1"]["alternate_parts"])}
        records["C9"]["field_overrides"].update({"value": "4.7nF", field: old_value})
        for ref in ("C1", "C2"):
            records.setdefault(ref, {}).setdefault("field_overrides", {})[field] = new_value
        for ref in ("C1", "C2", "C9"):
            records[ref]["schematic_field_snapshot"] = {
                "comment": "capacitor", "footprint": "Lib:0603",
                "value": "4.7nF 50V" if ref == "C9" else "4.7nF"}
        save_overrides(app.overrides_path, records)
        app.save_field_override({"refs": "C9", "field_overrides": {field: new_value}})
        rows = app.rows(include_stock=False)
        assert len(rows) == 1
        assert rows[0]["effective_c_code"] == "C100"
        assert rows[0]["review_status"] == "Pending"
        assert len(load_schematic_parts(schematic.parent)) == 3
    finally:
        app.search_jobs.close()
        app.selection_jobs.close()


def test_order_only_destination_transfers_url_without_fake_c_code(scratch_project):
    app, _ = make_app(scratch_project)
    try:
        records = load_overrides(app.overrides_path)
        records["C9"].update(decision="ClearSelection", procurement_choice={
            "order_url": "https://so.szlcsc.com/foreign/detail/test-order.html",
            "supplier": "Digikey", "manufacturer_part_number": "TARGET-ORDER"})
        records["C1"]["procurement_choice"] = {
            "order_url": "https://so.szlcsc.com/foreign/detail/old.html", "supplier": "Old"}
        save_overrides(app.overrides_path, records)
        target = copy.deepcopy(load_overrides(app.overrides_path)["C9"])
        app.save_field_override({"refs": "C1,C2", "field_overrides": {"value": "4.7nF 50V"}})
        row = app.rows(include_stock=False)[0]
        assert row["effective_c_code"] == ""
        assert row["procurement_choice"] == target["procurement_choice"]
        for fields in row["per_ref_sync_fields"].values():
            assert fields["procurement_url"] == target["procurement_choice"]["order_url"]
        assert load_overrides(app.overrides_path)["C9"] == target
    finally:
        app.search_jobs.close()
        app.selection_jobs.close()


def test_existing_conflict_without_key_change_is_not_resolved_arbitrarily(scratch_project):
    app, _ = make_app(scratch_project)
    try:
        records = load_overrides(app.overrides_path)
        for ref in ("C1", "C2"):
            records.setdefault(ref, {}).setdefault("field_overrides", {})["value"] = "4.7nF 50V"
        save_overrides(app.overrides_path, records)
        result = app.save_field_override({"refs": "C1,C2", "field_overrides": {"value": "4.7nF 50V"}})
        assert not result["merged_into"]
        assert app.rows(include_stock=False)[0]["effective_c_code"] == ""
    finally:
        app.search_jobs.close()
        app.selection_jobs.close()


def test_ambiguous_destination_is_not_arbitrarily_inherited(scratch_project):
    app, schematic = make_app(scratch_project)
    try:
        text = schematic.read_text()
        schematic.write_text(text[:-1] + '(symbol (property "Reference" "C10") '
            '(property "Comment" "capacitor") (property "Value" "4.7nF 50V") '
            '(property "Footprint" "Lib:0603") (property "LCSC" "C300")))')
        result = app.save_field_override({"refs": "C1,C2", "field_overrides": {"value": "4.7nF 50V"}})
        assert not result["merged_into"]
        assert app.rows(include_stock=False)[0]["effective_c_code"] == ""
    finally:
        app.search_jobs.close()
        app.selection_jobs.close()


def test_target_with_per_ref_notes_or_alternates_still_preserves_its_main_code(scratch_project):
    app, schematic = make_app(scratch_project)
    try:
        schematic.write_text(schematic.read_text()[:-1] + '(symbol (property "Reference" "C10") '
            '(property "Comment" "capacitor") (property "Value" "4.7nF 50V") '
            '(property "Footprint" "Lib:0603") (property "LCSC" "C200")))')
        app.save_field_override({"refs": "C1,C2", "field_overrides": {"value": "4.7nF 50V"}})
        assert app.rows(include_stock=False)[0]["effective_c_code"] == "C200"
        assert load_overrides(app.overrides_path)["C1"]["note"] == "source note"
    finally:
        app.search_jobs.close()
        app.selection_jobs.close()


def test_target_cached_parameters_replace_source_parameters(scratch_project):
    from part_stock_lookup import save_stock_cache
    app, _ = make_app(scratch_project)
    try:
        records = load_overrides(app.overrides_path)
        records["C9"].pop("field_overrides")
        save_overrides(app.overrides_path, records)
        save_stock_cache(app.stock_cache_path, {"C200": {"c_code": "C200", "ok": True,
            "param": "TARGET-CACHED 4.7nF 50V", "param_detail": "Voltage: 50V", "brand": "TARGET BRAND"}})
        app.save_field_override({"refs": "C1,C2", "field_overrides": {"value": "4.7nF 50V"}})
        row = app.rows(include_stock=False)[0]
        assert row["effective_c_code"] == "C200"
        assert "TARGET-CACHED" in row["per_ref_sync_fields"]["C1"]["description"]
        assert "OLD" not in row["per_ref_sync_fields"]["C1"]["description"]
    finally:
        app.search_jobs.close()
        app.selection_jobs.close()

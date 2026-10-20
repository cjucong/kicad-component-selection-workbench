import copy
import sys
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from part_selection_helper import PartInfo
from part_selection_runtime import runtime_directory
from part_selection_overrides import choose_effective_c_code, load_overrides, save_overrides
from part_selection_server import (PartSelectionApp, group_parts_for_review,
                                   row_effective_c_code, row_schematic_sync_fields,
                                   row_code_status)


def parts(codes=("C123", "C456")):
    return [PartInfo(ref=ref, comment="Capacitor", value="10pF, C0G",
                     footprint="DSMU:0402", accepted_c_code=code)
            for ref, code in zip(("C173", "C1010"), codes)]


def test_distinct_codes_clear_entire_row_without_historical_fallback():
    row = group_parts_for_review(parts(), {})[0]
    assert row["effective_c_code"] == ""
    assert row_effective_c_code(row) == ""
    assert row["review_status"] == "Pending"
    assert not row["selection_codes_mixed"]
    assert all(not item["effective_c_code"] for item in row["per_ref_selection"].values())
    assert "lcsc_c_code" not in row_schematic_sync_fields(row)
    assert all("lcsc_c_code" not in fields for fields in row["per_ref_sync_fields"].values())


def test_unique_nonempty_code_fills_blank_references_and_remains_pending():
    row = group_parts_for_review(parts(("C123", "")), {})[0]
    assert row["effective_c_code"] == "C123"
    assert not row["selection_codes_mixed"]
    assert row["review_status"] == "Pending"
    assert {item["effective_c_code"] for item in row["per_ref_selection"].values()} == {"C123"}
    assert {fields["lcsc_c_code"] for fields in row["per_ref_sync_fields"].values()} == {"C123"}


def test_same_code_metadata_does_not_create_second_selection_mode():
    overrides = {"C173": {"review_status": "Confirmed"}, "C1010": {
        "decision": "AcceptManual", "manual_c_code": "C123", "review_status": "Confirmed",
        "field_overrides": {"description": "model: TEST", "description_c_code": "C123"},
        "note": "keep me"}}
    original = copy.deepcopy(overrides)
    row = group_parts_for_review(parts(("C123", "C123")), overrides)[0]
    assert not row["selection_mixed"]
    assert row["review_status"] == "Confirmed"
    assert row_code_status(row) != "Mixed"
    assert row["notes_by_ref"]["C1010"] == "keep me"
    assert overrides == original


def test_mixed_review_status_becomes_pending_for_every_reference():
    row = group_parts_for_review(parts(("C123", "C123")), {
        "C173": {"review_status": "Confirmed"}})[0]
    assert row["review_status"] == "Pending"
    assert {item["review_status"] for item in row["per_ref_selection"].values()} == {"Pending"}
    assert not row["review_status_mixed"]


def test_clear_selection_is_an_explicit_tombstone():
    assert choose_effective_c_code({"accepted_c_code": "C123", "lcsc_match_c_code": "C456"},
                                   {"decision": "ClearSelection"}) == ""
    assert row_effective_c_code({"effective_c_code": "", "accepted_c_code": "C123"}) == ""


def test_persisted_policy_survives_reload_and_keeps_notes_and_alternates(scratch_project):
    app = PartSelectionApp.__new__(PartSelectionApp)
    app.project_root = scratch_project
    app.bom_dir = scratch_project / "BOM"
    app.overrides_path = app.bom_dir / "overrides.json"
    save_overrides(app.overrides_path, {"C173": {
        "note": "audit", "alternate_parts": [{"c_code": "C789"}],
        "review_status": "Confirmed"}})
    app.normalize_row_selections(parts())
    saved = load_overrides(app.overrides_path)
    assert saved["C173"]["note"] == "audit"
    assert saved["C173"]["alternate_parts"][0]["c_code"] == "C789"
    assert all(saved[ref]["decision"] == "ClearSelection" for ref in ("C173", "C1010"))
    assert all(saved[ref]["review_status"] == "Pending" for ref in ("C173", "C1010"))
    assert group_parts_for_review(parts(), saved)[0]["effective_c_code"] == ""
    before = app.overrides_path.read_bytes()
    app.normalize_row_selections(parts())
    assert app.overrides_path.read_bytes() == before
    assert len(list(runtime_directory(scratch_project).glob("row_selection_*/*.json"))) == 1


def test_explicit_constraints_still_split_groups():
    items = parts()
    items[1].value += ", 250V"
    rows = group_parts_for_review(items, {})
    assert len(rows) == 2
    assert {row["effective_c_code"] for row in rows} == {"C123", "C456"}


def test_ref_search_normalizes_full_group_before_filtering_and_allows_reselection(scratch_project):
    app = PartSelectionApp.__new__(PartSelectionApp)
    app.project_root = scratch_project
    app.bom_dir = scratch_project / "BOM"
    app.overrides_path = app.bom_dir / "overrides.json"
    app.stock_cache_path = app.bom_dir / "stock.json"
    app.config = {"schematic_dir": scratch_project / "DOC"}
    source = {part.ref: part for part in parts()}
    with patch("part_selection_server.load_schematic_parts", return_value=source), \
         patch.object(app, "reconcile_parts_with_schematic", return_value=({}, {})), \
         patch("part_selection_server.enrich_rows_with_schematic_sync_state"):
        row = app.rows("C173", "__schematic__", include_stock=False)[0]
        assert row["refs_list"] == ["C173"]
        assert row["effective_c_code"] == ""
        assert load_overrides(app.overrides_path)["C1010"]["decision"] == "ClearSelection"
        with patch("part_selection_server.read_schematic_symbol_field_values", return_value={}):
            app.save_override({"refs": "C173,C1010", "decision": "AcceptManual",
                               "accepted_c_code": "C789", "manual_c_code": "C789",
                               "stock_c_code": "C789", "review_status": "Confirmed"})
        rows = app.rows("", "__schematic__", include_stock=False)
        assert len(rows) == 1
        assert rows[0]["effective_c_code"] == "C789"
        assert rows[0]["review_status"] == "Confirmed"
        assert not rows[0]["selection_codes_mixed"]
        assert len(list(runtime_directory(scratch_project).glob("row_selection_*/*.json"))) == 1


def test_workbench_has_one_main_code_and_one_review_control_template(scratch_project):
    from part_selection_server import render_index
    app = PartSelectionApp(scratch_project / "BOM" / "config.json")
    page = render_index(app)
    assert "各位号选料独立" not in page
    assert "全部待确认" not in page
    assert "工程: <span" not in page
    assert "立创: <span" not in page
    assert 'Object.hasOwn(row, "effective_c_code")' in page
    assert "await reloadRows(" not in page

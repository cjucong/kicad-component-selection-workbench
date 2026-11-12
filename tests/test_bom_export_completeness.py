"""BOM membership follows design scope, independently of procurement decisions."""
import json

import pytest

from test_bom_export_template import export_rows, make_row
from test_selected_bom_export import read_export
from part_selection_overrides import save_override
from part_selection_server import PartSelectionApp


@pytest.mark.parametrize("scope", ["all", "all_non_nc", "current_filter"])
def test_design_parts_without_selection_records_are_not_silently_omitted(scratch_project, scope):
    _, _, records = export_rows(scratch_project, [
        make_row("R1,R2", value="0Ω", effective_c_code="C2001", per_ref_selection={}, stock={}),
        make_row("R3,R4,R5", value="10kΩ", effective_c_code="C2002", per_ref_selection={}, stock={}),
        make_row("J1,J2", value="Connector", effective_c_code="", per_ref_selection={}, stock={}),
        make_row("TP1", value="Test point", effective_c_code="", per_ref_selection={}, stock={}),
    ], export_filter=scope)
    assert {record["位号"]: record["单套用量"] for record in records} == {
        "R1,R2": "2", "R3,R4,R5": "3", "J1,J2": "2", "TP1": "1",
    }


@pytest.fixture
def unselected_app(scratch_project):
    symbols = []
    parts = [(f"R{n}", "resistor", "0", "0603", "C2001") for n in range(1, 29)]
    parts += [(f"R{n}", "resistor", "10k", "0402", "C2002") for n in range(101, 149)]
    parts += [("J1", "connector", "Connector", "SMA", ""),
              ("J2", "connector", "Connector", "SMA", ""),
              ("TP1", "testpoint", "Test point", "TP_SMD", ""),
              ("C1", "capacitor", "NC/100nF", "0402", "")]
    for ref, comment, value, footprint, code in parts:
        symbols.append(f'(symbol (property "Reference" "{ref}") '
                       f'(property "Comment" "{comment}") (property "Value" "{value}") '
                       f'(property "Footprint" "Library:{footprint}") (property "LCSC" "{code}"))')
    (scratch_project / "example.kicad_sch").write_text(
        "(kicad_sch " + " ".join(symbols) + ")", encoding="utf-8")
    config = scratch_project / "settings.json"
    config.write_text(json.dumps({"source_bom": "__schematic__", "schematic_dir": ".",
                                 "bom_dir": "data"}), encoding="utf-8")
    app = PartSelectionApp(config)
    for ref, *_ in parts:
        if ref.startswith("R"):
            save_override(app.overrides_path, ref, {"decision": "ClearSelection",
                "accepted_c_code": "", "manual_c_code": "", "stock_c_code": "",
                "review_status": "Confirmed"})
    yield app
    app.search_jobs.close()
    app.selection_jobs.close()


@pytest.mark.parametrize("scope", ["all", "all_non_nc", "current_filter", "selected"])
def test_cleared_selection_and_no_code_parts_reach_csv_without_reselecting(unselected_app, scope):
    app = unselected_app
    expected = {f"R{n}" for n in range(1, 29)} | {f"R{n}" for n in range(101, 149)} | {"J1", "J2", "TP1"}
    source = app.project_root / "example.kicad_sch"
    before = (source.read_bytes(), app.overrides_path.read_bytes())
    payload = {"export_filter": scope}
    if scope == "selected":
        payload["selected_refs"] = sorted(expected)
    result = app.export_pump_table(payload)
    records = read_export(result)
    refs = [ref for record in records for ref in record["位号"].split(",")]
    nc_refs = {"C1"} if scope in {"all", "current_filter"} else set()
    assert set(refs) == expected | nc_refs
    assert len(refs) == len(set(refs)) == 79 + len(nc_refs)
    fitted = [record for record in records if record["装配状态"] == "装配"]
    assert sorted(int(record["单套用量"]) for record in fitted) == [1, 2, 28, 48]
    assert sum(int(record["设计数量"]) for record in records) == len(refs)
    assert all(record["商品编号"] == record["LCSC_URL"] == "" for record in records)
    assert all("未选择 C 编码" in record["Note"] for record in fitted)
    assert result["reference_count"] == len(refs)
    assert result["unselected_reference_count"] == 79
    assert (source.read_bytes(), app.overrides_path.read_bytes()) == before


@pytest.mark.parametrize("decision,review", [("Reject", "Rejected"), ("ClearSelection", "Pending"),
                                            ("NoChange", "Confirmed"), ("DNP", "Pending")])
def test_selection_decision_does_not_remove_design_parts_or_mark_them_nc(scratch_project, decision, review):
    row = make_row("R1", value="0", effective_c_code="", stock={}, review_status=review,
                   per_ref_selection={"R1": {"decision": decision, "review_status": review,
                                             "effective_c_code": ""}})
    _, _, records = export_rows(scratch_project, [row], export_filter="all_non_nc")
    assert records[0]["位号"] == "R1"
    assert records[0]["单套用量"] == "1" and records[0]["装配状态"] == "装配"
    assert records[0]["商品编号"] == ""


@pytest.mark.parametrize("scope", ["stock_issue", "stock_error", "low_stock"])
def test_stock_scopes_keep_selection_requirements(scratch_project, scope):
    _, _, records = export_rows(scratch_project, [
        make_row("J1", effective_c_code="", per_ref_selection={}, stock={}),
        make_row("R1", per_ref_selection={"R1": {"decision": "Reject", "review_status": "Rejected"}}),
    ], export_filter=scope)
    assert records == []

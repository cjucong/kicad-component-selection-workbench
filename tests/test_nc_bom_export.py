"""NC design entries remain visible in exported BOMs without becoming demand."""
import pytest
from unittest.mock import Mock

from test_bom_export_template import export_rows, make_row, TEMPLATE_COLUMNS
from test_selected_bom_export import selected_app, read_export
from part_selection_inventory import save_inventory
from part_selection_server import PartSelectionApp


@pytest.mark.parametrize("export_filter", ["current_filter", "all"])
@pytest.mark.parametrize("marker", ["NC", "10k, NC", "DNP", "DNF", "NO LOAD"])
def test_nc_without_product_code_is_preserved_with_zero_assembly_quantity(
        scratch_project, export_filter, marker):
    nc = make_row("C9,C10", value=marker, effective_c_code="", per_ref_selection={}, stock={})
    result, columns, rows = export_rows(scratch_project, [make_row(), nc],
                                        export_filter=export_filter)
    assert result["count"] == 2
    assert columns[:10] == TEMPLATE_COLUMNS
    record = next(row for row in rows if row["位号"] == "C9,C10")
    assert record["单套用量"] == "0"
    assert record["设计数量"] == "2"
    assert record["装配状态"] == "NC / 不装"
    assert record["Value"] == marker
    assert record["商品编号"] == record["LCSC_URL"] == ""
    assert "NC / 不装" in record["Note"]


def test_selected_nc_row_reaches_real_csv_export(selected_app):
    result = selected_app.export_pump_table({"export_filter": "selected",
        "selected_refs": ["C2"], "bom": "BOM/source.csv"})
    rows = read_export(result)
    assert result["count"] == 1
    assert rows[0]["位号"] == "C2"
    assert rows[0]["装配状态"] == "NC / 不装"


def test_explicit_nc_filter_exports_nc_and_keeps_original_note(scratch_project):
    _, _, rows = export_rows(scratch_project, [make_row(), make_row("R9", value="NC")],
                            export_filter="current_filter", active_filters=["nc_device"])
    assert [row["位号"] for row in rows] == ["R9"]
    assert '中文备注, "保留"\n第二行' in rows[0]["Note"]


@pytest.mark.parametrize("scope", ["all_non_nc", "stock_issue", "low_stock", "stock_error"])
def test_procurement_scopes_still_exclude_nc(scratch_project, scope):
    _, _, rows = export_rows(scratch_project, [make_row("R9", value="NC")],
                            export_filter=scope)
    assert rows == []


def test_part_number_starting_nc_remains_fitted(scratch_project):
    _, _, rows = export_rows(scratch_project,
        [make_row("U1", comment="NC7SZ logic gate", value="NC7SZ125")], export_filter="all")
    assert rows[0]["单套用量"] == rows[0]["设计数量"] == "1"
    assert rows[0]["装配状态"] == "装配"


@pytest.mark.parametrize("record,expected", [
    ({"quantity": 42, "status": "ready"}, "42"),
    ({"quantity": 0, "status": "ready"}, "0"),
    (None, "0"),
    ({"quantity": None, "status": "unresolved"}, "待核实"),
])
def test_nc_inventory_exports_actual_balance_and_distinguishes_unknown(scratch_project, record, expected):
    app = object.__new__(PartSelectionApp)
    app.local_inventory_path = scratch_project / "inventory.json"
    save_inventory(app.local_inventory_path, {"schema_version": 1,
        "items": {"C1001": record} if record is not None else {}})
    before = app.local_inventory_path.read_bytes()
    source = [make_row("R9", value="NC")]
    app.enrich_local_inventory(source)
    _, _, rows = export_rows(scratch_project, source, export_filter="all")
    assert rows[0]["本地库存"] == expected
    assert rows[0]["单套用量"] == "0"
    assert app.local_inventory_path.read_bytes() == before


def test_nc_http_export_includes_status_and_opens_output_folder(selected_app):
    from test_export_folder import post_export
    selected_app._open_directory = Mock(return_value={"ok": True})
    status, result = post_export(selected_app, "/api/export_pump_table", {
        "export_filter": "selected", "selected_refs": ["C2"], "bom": "BOM/source.csv"})
    assert status == 200 and result["count"] == 1
    assert read_export(result)[0]["装配状态"] == "NC / 不装"
    selected_app._open_directory.assert_called_once_with(selected_app.bom_dir)

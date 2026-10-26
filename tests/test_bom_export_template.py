"""LCSC template ordering, using synthetic rows and disposable export directories."""
import copy
import pytest
import csv
from pathlib import Path
from unittest.mock import Mock

from part_selection_server import PartSelectionApp
from part_selection_inventory import save_inventory


TEMPLATE_COLUMNS = [
    "序号", "型号", "封装", "参数/规格", "单套用量",
    "品牌", "分类", "商品编号", "物料编码", "位号",
]


def make_row(refs="C1,C2", **updates):
    row = {
        "refs": refs, "quantity": len(refs.split(",")),
        "comment": "Capacitor", "value": "100nF, 50V, X7R",
        "footprint": "测试库:C_0603", "effective_c_code": "C1001",
        "review_status": "Confirmed",
        "per_ref_selection": {
            ref: {"effective_c_code": "C1001", "review_status": "Confirmed"}
            for ref in refs.split(",")
        },
        "stock": {
            "ok": True, "c_code": "C1001", "model": "TEST-104",
            "package": "0603", "brand": "测试品牌", "category": "贴片电容",
            "param_detail": "容值: 100nF; 额定电压: 50V", "stock": "12345",
            "delivery": "1-3个工作日", "moq": "10", "mpq": "10", "price": "0.02",
        },
        "note": '中文备注, "保留"\n第二行', "field_changed_refs": ["C1"],
    }
    row.update(updates)
    return row


def export_rows(scratch_project, rows, **payload):
    app = object.__new__(PartSelectionApp)
    app.bom_dir = scratch_project / "BOM" / "选料工作台"
    app.rows = Mock(return_value=rows)
    before = copy.deepcopy(rows)
    result = app.export_pump_table({"export_filter": "all_non_nc", **payload})
    app.rows.assert_called_once_with("", "", include_stock=False)
    assert rows == before
    path = Path(result["path"])
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        records = list(reader)
        columns = reader.fieldnames
    return result, columns, records


def test_template_columns_come_first_and_preserve_engineering_details(scratch_project):
    result, columns, rows = export_rows(scratch_project, [make_row()])
    assert result["ok"] and result["count"] == 1
    assert columns[:10] == TEMPLATE_COLUMNS
    assert len(columns) == len(set(columns))
    assert columns[10:] == [
        "Comment", "Value", "Footprint", "LCSC_URL", "Stock", "Delivery", "MOQ",
        "MPQ", "Price", "Param", "ReviewStatus", "PerReferenceSelection",
        "SchematicSyncStatus", "Note", "本地库存", "本地库存说明",
    ]
    row = rows[0]
    assert [row[key] for key in TEMPLATE_COLUMNS] == [
        "1", "TEST-104", "0603", "100nF, 50V, X7R", "2",
        "测试品牌", "贴片电容", "C1001", "", "C1,C2",
    ]
    assert row["Comment"] == "Capacitor"
    assert row["Value"] == "100nF, 50V, X7R"
    assert row["Footprint"] == "测试库:C_0603"
    assert row["Stock"] == "12345"
    assert row["Delivery"] == "1-3个工作日"
    assert row["MOQ"] == "10" and row["MPQ"] == "10"
    assert row["Price"] == "0.02"
    assert "容值: 100nF" in row["Param"]
    assert row["ReviewStatus"] == "Confirmed"
    assert row["SchematicSyncStatus"] == "NeedsSchematicUpdate"
    assert row["Note"] == '中文备注, "保留"\n第二行'


def test_missing_metadata_uses_design_value_and_footprint_not_fake_product(scratch_project):
    _, _, rows = export_rows(scratch_project, [make_row(stock={})])
    row = rows[0]
    assert row["型号"] == row["品牌"] == row["分类"] == ""
    assert row["封装"] == "C_0603"
    assert row["参数/规格"] == "100nF, 50V, X7R"
    assert row["商品编号"] == "C1001"
    assert row["物料编码"] == ""


def test_stale_product_metadata_is_not_exported_for_new_code(scratch_project):
    row = make_row()
    row["stock"]["c_code"] = "C9999"
    _, _, rows = export_rows(scratch_project, [row])
    assert rows[0]["商品编号"] == "C1001"
    for key in ("型号", "品牌", "分类", "Stock", "Param", "Delivery", "MOQ", "MPQ", "Price"):
        assert rows[0][key] == ""
    assert rows[0]["封装"] == "C_0603"


def test_numbering_is_contiguous_after_filtering_and_does_not_multiply_quantity(scratch_project):
    result, _, rows = export_rows(scratch_project, [
        make_row("C9", value="NC"), make_row("C1,C2"),
        make_row("R1", review_status="Pending", value="10k"),
        make_row("C3,C4,C5"),
    ], export_filter="current_filter", active_filters=["review_confirmed"])
    assert result["count"] == 2
    assert [row["序号"] for row in rows] == ["1", "2"]
    assert [row["位号"] for row in rows] == ["C1,C2", "C3,C4,C5"]
    assert [row["单套用量"] for row in rows] == ["2", "3"]


def test_empty_export_retains_template_header(scratch_project):
    result, columns, rows = export_rows(scratch_project, [])
    assert result["count"] == 0 and rows == []
    assert columns[:10] == TEMPLATE_COLUMNS


def test_missing_design_value_falls_back_to_matching_product_specification(scratch_project):
    row = make_row(value="", footprint="0603")
    row["stock"].pop("package")
    _, _, rows = export_rows(scratch_project, [row])
    assert rows[0]["封装"] == "0603"
    assert rows[0]["参数/规格"] == "容值: 100nF; 额定电压: 50V"
    assert rows[0]["Value"] == ""


def test_unidentified_cached_product_cannot_supply_template_metadata(scratch_project):
    row = make_row()
    row["stock"].pop("c_code")
    _, _, rows = export_rows(scratch_project, [row])
    assert rows[0]["型号"] == rows[0]["品牌"] == rows[0]["Param"] == ""
    assert rows[0]["商品编号"] == "C1001"


def test_export_includes_local_balance_without_allocating_or_subtracting(scratch_project):
    inventory = {"c_code": "C1001", "inventory": 100, "status": "sufficient"}
    _, columns, rows = export_rows(scratch_project, [make_row(local_inventory=inventory),
        make_row("C3", value="different group", local_inventory=inventory)])
    assert columns[:10] == TEMPLATE_COLUMNS
    assert [row["本地库存"] for row in rows] == ["100", "100"]
    assert all("共享" in row["本地库存说明"] for row in rows)


@pytest.mark.parametrize("local, code, expected", [
    ({"c_code": "C1001", "inventory": 0, "status": "insufficient"}, "C1001", "0"),
    ({"c_code": "C1001", "inventory": None, "status": "unmatched"}, "C1001", "0"),
    ({"c_code": "C1001", "inventory": 5, "status": "unresolved"}, "C1001", "待核实"),
    ({"c_code": "C1001", "inventory": True, "status": "sufficient"}, "C1001", "待核实"),
    ({"c_code": "C999", "inventory": 100, "status": "sufficient"}, "C1001", "待核实"),
    ({}, "C1001", "待核实"),
    ({}, "", "—"),
])
def test_export_inventory_unknown_zero_and_stale_are_distinct(scratch_project, local, code, expected):
    _, _, rows = export_rows(scratch_project, [make_row(local_inventory=local, effective_c_code=code)])
    assert rows[0]["本地库存"] == expected


def test_export_uses_inventory_enrichment_from_persisted_local_balances(scratch_project):
    app = object.__new__(PartSelectionApp)
    app.local_inventory_path = scratch_project / "本地库存.json"
    save_inventory(app.local_inventory_path, {"schema_version": 1, "items": {
        "C1001": {"quantity": 120, "status": "ready"}}})
    before = app.local_inventory_path.read_bytes()
    source_rows = [make_row()]
    app.enrich_local_inventory(source_rows)
    _, _, rows = export_rows(scratch_project, source_rows)
    assert rows[0]["本地库存"] == "120"
    assert app.local_inventory_path.read_bytes() == before

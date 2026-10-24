"""Stock quantity is not evidence of dispatch readiness; synthetic fixtures only."""
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from part_selection_server import cache_candidate_stock, render_index, row_filter_keys, row_stock_is_low
from part_stock_lookup import (
    build_candidate_item,
    candidate_stock_result,
    load_stock_cache,
    parse_lcsc_product,
    parse_szlcsc_next_data,
    save_stock_cache,
)


@pytest.mark.parametrize("extra, expected", [
    ({"stockSz": 500}, "发货时间待核实"),
    ({"domesticStockVO": {"shipImmediately": 0}}, "发货时间待核实"),
    ({"domesticStockVO": {"shipImmediately": 0, "ship3Days": 500}}, "预计3天发货 500"),
    ({"domesticStockVO": {"shipImmediately": 20}}, "可立即发货 20"),
    ({"domesticStockVO": {"shipImmediately": 20, "ship3Days": 480}},
     "可立即发货 20；预计3天发货 480"),
    ({"deliveryTimeWayDays": "1-3个工作日", "stockSz": 500,
      "domesticStockVO": {"shipImmediately": 0}}, "1-3个工作日"),
    ({"deliveryTimeWayDays": "1-3个工作日",
      "domesticStockVO": {"shipImmediately": 20, "ship3Days": 480}},
     "1-3个工作日；可立即发货 20；预计3天发货 480"),
    ({"hasStockNow": "no", "domesticStockVO": {"shipImmediately": 500}}, "发货时间待核实"),
])
def test_positive_stock_does_not_imply_immediate_dispatch(extra, expected):
    result = parse_lcsc_product("C123", {"productCode": "C123", "stockNumber": 500, **extra})
    assert result["stock"] == "500"
    assert result["delivery"] == expected
    assert result["channel"] == "商城库存"
    assert "4小时" not in result["delivery"]


def test_domestic_search_preserves_explicit_dispatch_evidence_without_inventing_it():
    records = [
        {"totalStockNumber": 500, "productVO": {"productCode": "C123", "stockNumber": 500,
            "hasStockNow": "no", "deliveryTimeWayDays": "1-3个工作日"}},
        {"totalStockNumber": 100, "productVO": {"productCode": "C124", "stockNumber": 100}},
        {"totalStockNumber": 30, "domesticStockVO": {"shipImmediately": 10, "ship3Days": 20},
            "productVO": {"productCode": "C125"}},
    ]
    payload = {"props": {"pageProps": {"soData": {"searchResult": {"productRecordList": records}}}}}
    page = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + '</script>'
    products = parse_szlcsc_next_data(page)
    assert products[0].get("hasStockNow") == "no"
    assert not products[1].get("domesticStockVO", {}).get("shipImmediately")
    results = [parse_lcsc_product(p["productCode"], p) for p in products]
    assert [p["delivery"] for p in results] == [
        "1-3个工作日", "发货时间待核实", "可立即发货 10；预计3天发货 20"]
    assert results[0]["immediate_stock"] == "0"
    assert results[1]["immediate_stock"] == ""
    assert results[2]["immediate_stock"] == "10"


def test_candidate_cache_retains_verified_dispatch_quantities():
    candidate = build_candidate_item("C123", {"productCode": "C123", "stockNumber": 500,
        "domesticStockVO": {"shipImmediately": 0, "ship3Days": 500}})
    saved = candidate_stock_result(candidate)
    assert saved["immediate_stock"] == "0"
    assert saved["three_day_stock"] == "500"
    assert saved["delivery"] == "预计3天发货 500"
    assert saved["delivery_schema"] == 1


def test_selected_candidate_cache_preserves_dispatch_evidence(scratch_project):
    path = Path(scratch_project) / "stock.json"
    candidate = build_candidate_item("C123", {"productCode": "C123", "stockNumber": 500,
        "domesticStockVO": {"shipImmediately": 20, "ship3Days": 480}})
    cache_candidate_stock(path, candidate)
    result = load_stock_cache(path)["C123"]
    assert result["immediate_stock"] == "20"
    assert result["three_day_stock"] == "480"
    assert result["delivery_schema"] == 1


def test_stock_only_filter_is_not_a_promise_of_immediate_dispatch():
    html = render_index(SimpleNamespace(project_root=Path(__file__).resolve().parents[1], config={}))
    label = re.search(r'<label class="candidate-stock-filter"[^>]*>.*?</label>', html).group()
    assert "有库存" in label
    assert "不代表立即发货" in label


@pytest.mark.parametrize("quantity, normal", [("", False), ("待核实", False),
    ("NaN", False), ("Infinity", False), ("0", False), ("99", False), ("100", True)])
def test_stock_quantity_filter_does_not_count_unknown_as_normal(quantity, normal):
    keys = row_filter_keys({"value": "10uF", "stock": {"ok": True, "stock": quantity}})
    assert ("stock_normal" in keys) is normal


@pytest.mark.parametrize("quantity, low", [("", False), (None, False), ("NaN", False),
    ("Infinity", False), ("0", True), ("99", True), ("100", False)])
def test_mixed_code_rows_do_not_export_unknown_stock_as_zero(quantity, low):
    row = {"stock": {"entries": [{"stock": {"ok": True, "stock": quantity}}]}}
    assert row_stock_is_low(row) is low


def test_old_cache_removes_fabricated_fast_dispatch_without_writing_records(scratch_project):
    path = Path(scratch_project) / "stock.json"
    old = {"C123": {"ok": True, "c_code": "C123", "stock": "500", "channel": "现货",
        "delivery": "现货最快4小时发货", "model": "MODEL", "fetched_at": "2026-10-20T12:00:00"},
        "C124": {"ok": True, "c_code": "C124", "stock": "500", "channel": "合作库存/订货",
        "delivery": "1-3个工作日"}}
    save_stock_cache(path, old)
    before = path.read_bytes()
    loaded = load_stock_cache(path)
    assert loaded["C123"]["delivery"] == "发货时间待核实"
    assert loaded["C123"]["channel"] == "商城库存"
    assert loaded["C123"]["model"] == "MODEL"
    assert loaded["C123"]["fetched_at"] == "2026-10-20T12:00:00"
    assert loaded["C124"]["delivery"] == "1-3个工作日"
    assert path.read_bytes() == before


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js required")
def test_main_supply_surfaces_dispatch_time_and_never_labels_total_stock_ready():
    root = Path(__file__).resolve().parents[1]
    html = render_index(SimpleNamespace(project_root=root, config={}))
    names = "renderStock|renderStockCell|stockDeliveryText"
    helpers = re.findall(r'function (?:' + names + r')\([^\n]*\) \{.*?\n\}', html, re.S)
    script = "const assert=require('node:assert/strict');\n" + "\n".join(helpers) + r'''
const esc = value => String(value ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const renderSupplyOrder = () => '';
const deferred = {ok:true,c_code:'C123',stock:'650',channel:'合作库存/订货',delivery:'1-3个工作日'};
const content = renderStockCell(deferred);
assert.match(content,/商城库存 <strong>650<\/strong>/);
assert.match(content.split('<details')[0],/发货[：:]\s*1-3个工作日/);
assert.doesNotMatch(content,/现货|立即发货/);
assert.match(renderStockCell({...deferred,stock:'0'}),/商城库存 <strong>0<\/strong>/);
assert.match(renderStockCell({...deferred,stock:''}),/商城库存 <strong>待核实<\/strong>/);
assert.match(renderStockCell({...deferred,delivery:'现货最快4小时发货'}),/发货时间待核实/);
assert.doesNotMatch(renderStockCell({...deferred,delivery:'现货最快4小时发货'}),/4小时/);
assert.match(renderStockCell({...deferred,delivery:''}),/发货时间待核实/);
assert.match(renderStockCell({...deferred,delivery:'<img src=x onerror=alert(1)>'}),/&lt;img/);
assert.doesNotMatch(renderStockCell({...deferred,delivery:'<img src=x>'}),/<img/);
assert.doesNotMatch(renderStockCell({...deferred,c_code:''}),/发货：1-3/);
'''
    result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True,
                            text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js required")
def test_unknown_stock_does_not_get_a_ready_or_sufficient_badge():
    html = render_index(SimpleNamespace(project_root=Path(__file__).resolve().parents[1], config={}))
    names = "stockNumber|stockIsLow|stockIsIssue|stockAlreadyQueried|stockVisual"
    helpers = re.findall(r'function (?:' + names + r')\([^\n]*\) \{.*?\n\}', html, re.S)
    script = "const assert=require('node:assert/strict');\n" + "\n".join(helpers) + r'''
for(const quantity of ['',null,'待核实','NaN','Infinity']) {
 const item={ok:true,stock:quantity};
 assert(Number.isNaN(stockNumber(item)));
 assert.equal(stockIsLow(item),false);
 assert.equal(stockIsIssue(item),true);
 assert.equal(stockVisual(item).text,'库存待核实');
}
assert.equal(stockNumber({stock:0}),0);
assert.equal(stockVisual({ok:true,stock:'100'}).text,'有库存');
assert.equal(stockVisual({ok:true,stock:'0'}).text,'低库存');
'''
    result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True,
                            text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr

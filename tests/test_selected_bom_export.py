"""Checked BOM scopes using disposable projects and real frontend functions."""
import csv
import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from part_selection_server import PartSelectionApp, render_index
from part_selection_overrides import save_override
from test_bom_export_template import make_row, TEMPLATE_COLUMNS
from test_export_folder import post_export


@pytest.fixture
def selected_app(scratch_project):
    (scratch_project / "BOM").mkdir()
    (scratch_project / "DOC").mkdir()
    (scratch_project / "DOC/test.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    (scratch_project / "BOM/source.csv").write_text(
        'Comment,Description,Designator,Footprint,Value\n'
        'Resistor,test,"R1,R2",0603,10k\n'
        'Capacitor,test,C1,0402,100nF\n'
        'Capacitor,test,C2,0402,NC\n', encoding="utf-8")
    config = scratch_project / "BOM/config.json"
    config.write_text(json.dumps({
        "source_bom": "BOM/source.csv", "schematic_dir": "DOC",
        "output_overrides": "BOM/overrides.json", "bom_dir": "BOM/exports",
    }), encoding="utf-8")
    app = PartSelectionApp(config)
    for ref in ("R1", "R2", "C1", "C2"):
        save_override(app.overrides_path, ref, {
            "decision": "AcceptManual", "accepted_c_code": "C1001",
            "review_status": "Pending",
        })
    yield app
    app.search_jobs.close()
    app.selection_jobs.close()


def read_export(result):
    path = Path(result["path"])
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        assert reader.fieldnames[:10] == TEMPLATE_COLUMNS
    return rows


@pytest.mark.parametrize("refs,expected", [
    (["R1", "R2"], {"R1,R2": "2"}),
    (["R2"], {"R2": "1"}),
    ([" r2 ", "R2", "c1"], {"R2": "1", "C1": "1"}),
    (["C2", "C1"], {"C1": "1"}),
    (["C2"], {}),
])
def test_selected_refs_are_scoped_before_grouping_and_nc_filtering(selected_app, refs, expected):
    source_path = selected_app.project_root / "BOM/source.csv"
    source = source_path.read_bytes()
    overrides = selected_app.overrides_path.read_bytes()
    result = selected_app.export_pump_table({
        "export_filter": "selected", "selected_refs": refs,
        "bom": "BOM/source.csv",
        "refs": "R999", "active_filters": ["review_confirmed"],
    })
    rows = read_export(result)
    assert {row["位号"]: row["单套用量"] for row in rows} == expected
    assert result["count"] == len(expected)
    assert [row["序号"] for row in rows] == [str(n + 1) for n in range(len(rows))]
    assert source_path.read_bytes() == source
    assert selected_app.overrides_path.read_bytes() == overrides


@pytest.mark.parametrize("refs", [None, [], "R1", {}, [None], [1], [""], [" "], ["R1,R2"], ["R1 R2"]])
def test_invalid_selection_never_reads_rows_or_creates_output(selected_app, refs):
    with patch.object(selected_app, "rows") as rows:
        with pytest.raises(ValueError, match="请先勾选"):
            selected_app.export_pump_table({"export_filter": "selected", "selected_refs": refs,
                                            "bom": "BOM/source.csv"})
    rows.assert_not_called()
    assert not selected_app.bom_dir.exists()


@pytest.mark.parametrize("refs", [["R999"], ["R1", "R999"]])
def test_stale_selection_is_rejected_not_silently_exported(selected_app, refs):
    with pytest.raises(ValueError, match="位号已变化"):
        selected_app.export_pump_table({"export_filter": "selected", "selected_refs": refs,
                                        "bom": "BOM/source.csv"})
    assert not selected_app.bom_dir.exists()


def test_unexpected_group_members_reject_instead_of_leaking_unselected_refs(selected_app):
    with patch.object(selected_app, "rows", return_value=[make_row("R1,R2")]):
        with pytest.raises(ValueError, match="位号已变化"):
            selected_app.export_pump_table({"export_filter": "selected", "selected_refs": ["R1"],
                                            "bom": "BOM/source.csv"})
    assert not selected_app.bom_dir.exists()


def test_grouped_export_preserves_saved_product_and_selection_fields(selected_app):
    row = make_row("C1,C2")
    with patch.object(selected_app, "rows", return_value=[row]) as rows, \
            patch.object(selected_app, "refs", return_value=["C1", "C2"]):
        result = selected_app.export_pump_table({
            "export_filter": "selected", "selected_refs": ["c2", "C1"],
        })
    rows.assert_called_once_with("C1,C2", "", include_stock=False)
    record, = read_export(result)
    assert record["单套用量"] == "2" and record["位号"] == "C1,C2"
    assert record["商品编号"] == "C1001" and record["型号"] == "TEST-104"
    assert record["ReviewStatus"] == "Confirmed"
    assert "C1: C=C1001" in record["PerReferenceSelection"]
    assert "C2: C=C1001" in record["PerReferenceSelection"]
    assert row["review_status"] == "Confirmed"


@pytest.mark.parametrize("refs,success", [(["R2"], True), ([], False), (["R999"], False)])
def test_http_exports_only_selected_and_opens_folder_only_on_success(selected_app, refs, success):
    with patch.object(selected_app, "_open_directory", return_value={"ok": True}) as opened:
        status, result = post_export(selected_app, "/api/export_pump_table", {
            "export_filter": "selected", "selected_refs": refs,
            "bom": "BOM/source.csv",
        })
    assert result["ok"] is success
    if success:
        assert status == 200
        assert [row["位号"] for row in read_export(result)] == ["R2"]
        opened.assert_called_once_with(selected_app.bom_dir)
    else:
        assert status >= 400
        opened.assert_not_called()
        assert not selected_app.bom_dir.exists()


def test_frontend_uses_visible_checked_refs_and_blocks_empty_or_stale_selection(selected_app):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required")
    html = render_index(selected_app)
    assert '<option value="selected">勾选行（当前可见）</option>' in html
    selection = re.search(r"function visibleSyncRows\(\).*?(?=function updateSyncSelection)", html, re.S).group()
    exports = re.search(r"function showExportResult\(.*?(?=const pendingStockRequests)", html, re.S).group()
    listener = re.search(r'document.getElementById\("exportPumpTable"\).addEventListener\("click", async \(\) => \{.*?\n\}\);', html, re.S).group()
    script = r"""
const assert = require('node:assert/strict');
const refsInput={value:'old filter'}, bomSelect={value:''}, activeFilters=new Map();
let rowsLoading=false, loadedRowsBom='', mode='selected';
const selectedRowRefs=new Set(['C1','C2','R1']);
const rowsEl={querySelectorAll:()=>[
  {dataset:{refs:'C1,C2'},classList:{contains:()=>false}},
  {dataset:{refs:'R1'},classList:{contains:()=>true}},
  {dataset:{refs:'U1'},classList:{contains:()=>false}}
]};
const requests=[];
global.fetch=async (url, options)=>{
  requests.push({url,payload:JSON.parse(options.body)});
  return {json:async()=>({ok:true,count:1,path:'BOM/test.csv',folder_opened:true})};
};
let click;
const document={getElementById:()=>({get value(){return mode}, addEventListener:(_,handler)=>click=handler})};
const states=[];
const setState=(text,style)=>states.push({text,style});
const closeToolbarMenus=()=>{};
let finishSave, failSave=false;
const flushRowEdits=()=>failSave ? Promise.reject(new Error('save failed')) : new Promise(resolve=>finishSave=resolve);
""" + selection + exports + listener + r"""
(async()=>{
  const saving=click();
  assert.equal(requests.length,0);
  finishSave(); await saving;
  assert.deepEqual(requests[0].payload.selected_refs,['C1','C2']);
  assert.equal(requests[0].payload.export_filter,'selected');
  let count=requests.length;
  failSave=true; await click(); failSave=false;
  assert.equal(requests.length,count);
  assert.match(states.at(-1).text,/save failed/);
  selectedRowRefs.clear();
  await assert.rejects(exportPumpTable(),/请先勾选/);
  selectedRowRefs.add('C1');
  for(const [loading,bom] of [[true,''],[false,null],[false,'other.csv']]){
    rowsLoading=loading; loadedRowsBom=bom;
    await assert.rejects(exportPumpTable(),/数据源加载完成/);
  }
  assert.equal(requests.length,count);
  rowsLoading=false; loadedRowsBom=''; mode='all_non_nc';
  await exportPumpTable();
  assert.equal(requests.at(-1).payload.export_filter,'all_non_nc');
  assert.ok(!('selected_refs' in requests.at(-1).payload));
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr

"""Real Chromium checked-row BOM export against a disposable schematic."""
import csv
import json
import os
from pathlib import Path
import subprocess
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

from _scratch import temporary_project
from part_selection_server import PartSelectionApp, make_handler
from part_selection_overrides import save_override


BROWSER = r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1600,height:1000}}), errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.setDefaultTimeout(15000);
  await page.goto(process.env.SELECTED_BOM_TEST_URL);
  const caps=page.locator('#rows tr[data-refs="C1,C2"]');
  const resistor=page.locator('#rows tr[data-refs="R1"]');
  await caps.waitFor();
  let requests=0;
  page.on('request',request=>{if(request.url().endsWith('/api/export_pump_table')) requests++;});
  const exportBom=async()=>{
    await page.locator('#exportPumpMenu').click();
    await page.locator('#pumpExportMode').selectOption('selected');
    await page.locator('#exportPumpTable').click();
  };
  await caps.locator('[data-select-row]').check();
  let response=page.waitForResponse('**/api/export_pump_table');
  await exportBom();
  assert.equal((await (await response).json()).count,1);
  await resistor.locator('[data-select-row]').check();
  await page.locator('#bomSearch').fill('100nF');
  await page.waitForFunction(()=>document.querySelector('#rows tr[data-refs="R1"]').classList.contains('hidden-row'));
  response=page.waitForResponse('**/api/export_pump_table');
  await exportBom();
  assert.equal((await (await response).json()).count,1);
  await caps.locator('[data-select-row]').uncheck();
  await exportBom();
  await page.waitForFunction(()=>document.querySelector('#state').textContent.includes('请先勾选'));
  assert.equal(requests,2);
  await page.locator('#bomSearch').fill('');
  await resistor.waitFor({state:'visible'});
  response=page.waitForResponse('**/api/export_pump_table');
  await exportBom();
  assert.equal((await (await response).json()).count,1);
  await resistor.locator('[data-select-row]').uncheck();
  await page.locator('#rows tr[data-refs="R9"] [data-select-row]').check();
  response=page.waitForResponse('**/api/export_pump_table');
  await exportBom();
  assert.equal((await (await response).json()).count,1);
  await page.locator('#exportPumpMenu').click();
  await page.locator('#pumpExportMode').selectOption('all');
  if(process.env.NC_BOM_SCREENSHOT) await page.screenshot({path:process.env.NC_BOM_SCREENSHOT});
  response=page.waitForResponse('**/api/export_pump_table');
  await page.locator('#exportPumpTable').click();
  let exported=await (await response).json();
  assert.equal(exported.count,7);
  assert.equal(exported.reference_count,83);
  assert.equal(exported.unselected_reference_count,79);
  await page.waitForFunction(()=>document.querySelector('#state').textContent.includes('83 个位号'));
  assert.match(await page.locator('#state').textContent(),/79 个位号未选择 C 编码/);
  await page.locator('#rows tr[data-refs="R9"] [data-select-row]').uncheck();
  await page.locator('#rows tr[data-refs="TP1"] [data-select-row]').check();
  response=page.waitForResponse('**/api/export_pump_table');
  await exportBom();
  exported=await (await response).json();
  assert.equal(exported.count,1);
  assert.equal(exported.reference_count,1);
  for(const scope of ['all_non_nc','current_filter']) {
    await page.locator('#exportPumpMenu').click();
    await page.locator('#pumpExportMode').selectOption(scope);
    response=page.waitForResponse('**/api/export_pump_table');
    await page.locator('#exportPumpTable').click();
    exported=await (await response).json();
    assert.equal(exported.reference_count,scope==='all_non_nc'?82:83);
    assert.equal(exported.unselected_reference_count,79);
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checked_group:true,hidden_checked_excluded:true,empty_selection_blocked:true,
    restored_visible_selection:true,nc_without_code:true,all_includes_nc:true,
    cleared_selection_76_refs:true,unselected_connectors_and_testpoint:true,page_errors:errors}));
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1});
'''


def main():
    with temporary_project(prefix="selected-bom-browser-") as directory:
        root = Path(directory)
        symbols = []
        parts = [
            ("C1", "Capacitor", "100nF", "C1001", "0603"),
            ("C2", "Capacitor", "100nF", "C1001", "0603"),
            ("R1", "Resistor", "10k", "C2001", "0603"),
            ("R9", "Resistor", "NC", "", "0603"),
        ]
        cleared = [(f"R{n}", "Resistor", "0", "C2002", "0603") for n in range(101, 129)]
        cleared += [(f"R{n}", "Resistor", "10k", "C2003", "0402") for n in range(201, 249)]
        parts += cleared + [
            ("J1", "Connector", "Connector", "", "SMA"),
            ("J2", "Connector", "Connector", "", "SMA"),
            ("TP1", "Testpoint", "Test point", "", "TP_SMD"),
        ]
        for ref, comment, value, code, footprint in parts:
            symbols.append(f'(symbol (property "Reference" "{ref}") '
                           f'(property "Comment" "{comment}") (property "Value" "{value}") '
                           f'(property "Footprint" "Library:{footprint}") (property "LCSC" "{code}"))')
        schematic = "(kicad_sch " + " ".join(symbols) + ")"
        source = root / "example.kicad_sch"
        source.write_text(schematic, encoding="utf-8")
        (root / "example.kicad_pro").write_text("{}", encoding="utf-8")
        config = root / "settings.json"
        config.write_text(json.dumps({"source_bom": "__schematic__", "schematic_dir": ".",
                                     "bom_dir": "data"}), encoding="utf-8")
        app = PartSelectionApp(config)
        for ref, *_ in cleared:
            save_override(app.overrides_path, ref, {"decision": "ClearSelection",
                "accepted_c_code": "", "manual_c_code": "", "stock_c_code": ""})
        original_overrides = app.overrides_path.read_bytes()
        app._open_directory = Mock(return_value={"ok": True})
        exports = []
        original_export = app.export_pump_table

        def capture_export(payload):
            result = original_export(payload)
            with Path(result["path"]).open(encoding="utf-8-sig", newline="") as file:
                exports.append(list(csv.DictReader(file)))
            return result

        app.export_pump_table = capture_export
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(["node", "-e", BROWSER], capture_output=True, text=True,
                encoding="utf-8", timeout=75, env={**os.environ,
                    "SELECTED_BOM_TEST_URL": f"http://127.0.0.1:{server.server_port}"},
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            print(result.stdout)
            assert result.returncode == 0, result.stderr
            assert [[(row["位号"], row["单套用量"]) for row in rows] for rows in exports[:4]] == [
                [("C1,C2", "2")], [("C1,C2", "2")], [("R1", "1")],
                [("R9", "0")],
            ]
            expected = {ref for ref, *_ in parts}
            missing_before_fix = {ref for ref, *_ in cleared} | {"J1", "J2", "TP1"}
            for index, refs in [(4, expected), (6, expected - {"R9"}), (7, expected)]:
                actual = [ref for row in exports[index] for ref in row["位号"].split(",")]
                assert set(actual) == refs and len(actual) == len(refs)
                assert sum(int(row["单套用量"]) for row in exports[index]) == 82
                for row in exports[index]:
                    if set(row["位号"].split(",")) & missing_before_fix:
                        assert row["商品编号"] == "" and "未选择 C 编码" in row["Note"]
            assert [(row["位号"], row["单套用量"]) for row in exports[5]] == [("TP1", "1")]
            assert next(row for row in exports[4] if row["位号"] == "R9")["装配状态"] == "NC / 不装"
            assert source.read_text(encoding="utf-8") == schematic
            assert app.overrides_path.read_bytes() == original_overrides
            assert app._open_directory.call_count == 8
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            app.search_jobs.close()
            app.selection_jobs.close()


if __name__ == "__main__":
    main()

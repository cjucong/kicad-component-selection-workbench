"""Exercise pending-edit undo in Chromium using a disposable, offline project."""
import json
import os
from pathlib import Path
import subprocess
import threading
from http.server import ThreadingHTTPServer

from _scratch import temporary_project
from part_selection_overrides import load_overrides, save_overrides
from part_selection_server import PartSelectionApp, make_handler
from part_selection_runtime import runtime_directory
from part_stock_lookup import save_stock_cache


BROWSER = r'''
const {chromium}=require('playwright'), assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:900}}), errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.setDefaultTimeout(12000);
  await page.goto(process.env.UNDO_TEST_URL);
  const row=page.locator('#rows tr[data-refs="R1"]');
  await row.waitFor();
  assert.equal(await page.locator('[data-action="undo-workbench"]').count(),2);
  await row.locator('[data-field="value"]').fill('22k');
  let acceptUndo=false;
  page.on('dialog',async dialog=>{
   assert.match(dialog.message(),/10k/);
   assert.match(dialog.message(),/不会修改原理图/);
   if(acceptUndo) await dialog.accept(); else await dialog.dismiss();
  });
  await row.locator('[data-action="undo-workbench"]').click();
  await page.waitForFunction(()=>!document.querySelector('#rows tr[data-refs="R1"] [data-action="undo-workbench"]').disabled);
  assert.equal(await row.locator('[data-field="value"]').inputValue(),'22k');
  await row.getByRole('textbox',{name:'主料 C 编码'}).fill('C202');
  await page.waitForFunction(()=>currentRows.find(r=>r.refs==='R1').effective_c_code==='C202');
  await page.reload();
  await row.waitFor();
  acceptUndo=true;
  await row.locator('[data-action="undo-workbench"]').click();
  await page.waitForFunction(()=>document.querySelector('#rows tr[data-refs="R1"] [data-field="value"]').value==='10k');
  assert.equal(await row.getByRole('textbox',{name:'主料 C 编码'}).inputValue(),'C101');
  assert.equal(await row.locator('[data-field="review_status"]').inputValue(),'Confirmed');
  assert.equal(await page.locator('#rows tr[data-refs="C1"] [data-field="value"]').inputValue(),'100nF');
  await page.reload();
  await row.waitFor();
  assert.equal(await row.locator('[data-field="value"]').inputValue(),'10k');
  for(const width of [1440,768,390]) {
   await page.setViewportSize({width,height:900});
   const control=row.locator('[data-action="undo-workbench"]');
   await control.scrollIntoViewIfNeeded();
   const cell=await row.locator('[data-col="review"]').boundingBox(),box=await control.boundingBox();
   assert(box.x>=cell.x-1&&box.x+box.width<=cell.x+cell.width+1);
  }
  await page.setViewportSize({width:1440,height:900});
  await row.locator('[data-col="review"]').scrollIntoViewIfNeeded();
  await page.screenshot({path:process.env.UNDO_SCREENSHOT});
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({cancel:true,immediate_autosave_flush:true,reload_snapshot:true,
    restore_fields_and_selection:true,confirmed_preserved:true,viewports:[1440,768,390],page_errors:errors}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def main():
    with temporary_project(prefix="browser-workbench-undo-") as directory:
        root = Path(directory)
        schematic = ('(kicad_sch (symbol (property "Reference" "R1") '
                     '(property "Comment" "resistor") (property "Value" "10k") '
                     '(property "Footprint" "Lib:0603") (property "LCSC" "C101")) '
                     '(symbol (property "Reference" "C1") (property "Comment" "capacitor") '
                     '(property "Value" "100nF") (property "Footprint" "Lib:0402")))')
        path = root / "test.kicad_sch"
        path.write_text(schematic, encoding="utf-8")
        config = root / "settings.json"
        config.write_text(json.dumps({"schematic_dir": ".", "source_bom": "__schematic__"}))
        app = PartSelectionApp(config)
        initial = {"R1": {"accepted_c_code": "C101", "manual_c_code": "C101", "stock_c_code": "C101",
                           "decision": "AcceptManual", "review_status": "Confirmed",
                           "review_confirmed_at": "2020-01-01T00:00:00"}}
        save_overrides(app.overrides_path, initial)
        stock = {"ok": True, "c_code": "C101", "stock": "100", "package": "0603", "param_detail": "阻值: 10kΩ"}
        save_stock_cache(app.stock_cache_path, {"C101": stock})
        app.stock = lambda code: {**stock, "c_code": code}
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            screenshot = runtime_directory(Path(__file__).resolve().parents[1]) / "workbench-undo-smoke.png"
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            result = subprocess.run(["node", "-e", BROWSER], capture_output=True, text=True, encoding="utf-8",
                env={**os.environ, "UNDO_TEST_URL": f"http://127.0.0.1:{server.server_port}",
                     "UNDO_SCREENSHOT": str(screenshot)}, timeout=90, **options)
            print(result.stdout)
            if result.returncode:
                raise AssertionError(result.stderr)
            assert path.read_text(encoding="utf-8") == schematic
            restored = load_overrides(app.overrides_path)
            assert restored["R1"]["accepted_c_code"] == "C101"
            assert "workbench_undo" not in restored["R1"]
            print(f"Screenshot: {screenshot}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            app.search_jobs.close()
            app.selection_jobs.close()


if __name__ == "__main__":
    main()

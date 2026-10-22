"""Exercise selective Value copying in real Chromium and a disposable project."""
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
  const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.setDefaultTimeout(12000);
  await page.goto(process.env.VALUE_TEST_URL);
  const row=page.locator('#rows tr[data-refs="R1262"]');
  const dialog=page.locator('#valueParametersDialog');
  const open=async()=>{
   await row.locator('[data-action="value-parameters"]').click();
   await dialog.waitFor({state:'visible'});
  };
  await row.waitFor(); await open();
  assert.equal(await dialog.locator('[data-value-key="nominal"]').isChecked(),true);
  assert.equal(await dialog.locator('[data-value-key="tcr"]').isChecked(),true);
  assert.equal(await dialog.locator('[data-value-key="voltage"]').isChecked(),false);
  assert.equal(await dialog.locator('[data-value-preview]').textContent(),'5.1kΩ, ±5ppm/℃');
  await dialog.locator('[data-value-key="tolerance"]').check();
  assert.equal(await dialog.locator('[data-value-preview]').textContent(),'5.1kΩ, ±5ppm/℃, ±0.1%');
  await dialog.locator('[data-value-cancel]').click();
  assert.equal(await row.locator('[data-field="value"]').inputValue(),'5.1k, 5ppm');
  await open();
  await dialog.locator('[data-value-key="tolerance"]').check();
  for(const width of [1440,768,390]) {
   await page.setViewportSize({width,height:900});
   const box=await dialog.boundingBox();
   assert(box.x>=0&&box.x+box.width<=width+1);
   assert(box.y>=0&&box.y+box.height<=901);
   for(const selector of ['[data-value-options]','[data-value-preview]','[data-value-apply]']) {
    const control=await dialog.locator(selector).boundingBox();
    assert(control.x>=box.x&&control.x+control.width<=box.x+box.width+1);
   }
   await page.screenshot({path:process.env.VALUE_SCREENSHOT.replace('.png',`-${width}.png`)});
  }
  await dialog.locator('[data-value-apply]').click();
  await dialog.waitFor({state:'hidden'});
  const expected='5.1kΩ, ±5ppm/℃, ±0.1%';
  assert.equal(await row.locator('[data-field="value"]').inputValue(),expected);
  assert.equal(await row.locator('[data-field="review_status"]').inputValue(),'Confirmed');
  assert.equal(await row.getByRole('textbox',{name:'主料 C 编码'}).inputValue(),'C101');
  assert.match(await row.locator('[data-col="value"] .field-diff').textContent(),/5.1k/);
  await page.reload(); await row.waitFor();
  assert.equal(await row.locator('[data-field="value"]').inputValue(),expected);
  await open();
  // A changed C-code/Value/source cannot receive properties from an obsolete dialog.
  await page.evaluate(()=>{currentRows.find(r=>r.refs==='R1262').effective_c_code='C999';});
  await dialog.locator('[data-value-key="voltage"]').check();
  await dialog.locator('[data-value-apply]').click();
  await dialog.locator('[data-value-error]').waitFor({state:'visible'});
  assert.match(await dialog.locator('[data-value-error]').textContent(),/已变化/);
  await dialog.locator('[data-value-cancel]').click();
  await page.reload(); await row.waitFor();
  // Unsaved text is flushed before opening; its special note survives composition.
  await row.locator('[data-field="value"]').fill(expected+', 特殊低噪声');
  await open();
  await dialog.locator('[data-value-key="voltage"]').check();
  await dialog.locator('[data-value-apply]').click();
  await dialog.waitFor({state:'hidden'});
  assert.equal(await row.locator('[data-field="value"]').inputValue(),expected+', 特殊低噪声, 100V');
  await page.setViewportSize({width:1440,height:900});
  await row.locator('[data-action="value-parameters"]').scrollIntoViewIfNeeded();
  const cell=await row.locator('[data-col="value"]').boundingBox(),
   input=await row.locator('[data-field="value"]').boundingBox(),
   button=await row.locator('[data-action="value-parameters"]').boundingBox();
  assert(input.x+input.width<=button.x+1);
  assert(button.x+button.width<=cell.x+cell.width+1);
  // Capacitor preselection and explicitly unavailable missing voltage.
  await page.locator('#rows tr[data-refs="C1"] [data-action="value-parameters"]').click();
  assert.equal(await dialog.locator('[data-value-key="voltage"]').isDisabled(),true);
  assert.match(await dialog.locator('[data-value-options]').textContent(),/未提供/);
  await dialog.press('Escape'); await dialog.waitFor({state:'hidden'});
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({cancel:true,selective_apply:true,confirmed_preserved:true,
    code_preserved:true,reload:true,stale_refused:true,special_notes:true,missing_disabled:true,
    viewports:[1440,768,390],page_errors:errors}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def main():
    with temporary_project(prefix='browser-value-parameters-') as directory:
        root = Path(directory)
        schematic = ('(kicad_sch (symbol (property "Reference" "R1262") '
            '(property "Comment" "PTFR0603B5K10V9") (property "Value" "5.1k, 5ppm") '
            '(property "Footprint" "Lib:0603") (property "LCSC" "C101")) '
            '(symbol (property "Reference" "C1") (property "Comment" "capacitor") '
            '(property "Value" "100nF, 50V, X7R") (property "Footprint" "Lib:0402") '
            '(property "LCSC" "C102")))')
        path = root / 'test.kicad_sch'
        path.write_text(schematic, encoding='utf-8')
        config = root / 'settings.json'
        config.write_text(json.dumps({'schematic_dir': '.', 'source_bom': '__schematic__'}))
        app = PartSelectionApp(config)
        save_overrides(app.overrides_path, {'R1262': {
            'accepted_c_code': 'C101', 'manual_c_code': 'C101', 'stock_c_code': 'C101',
            'decision': 'AcceptManual', 'review_status': 'Confirmed',
            'review_confirmed_at': '2020-01-01T00:00:00'}})
        stock = {'ok': True, 'c_code': 'C101', 'stock': '1000', 'package': '0603',
            'param_detail': '阻值: 5.1kΩ; 温度系数: ±5ppm/℃; 精度: ±0.1%; 最大工作电压: 100V; 功率: 100mW'}
        save_stock_cache(app.stock_cache_path, {'C101': stock, 'C102': {
            'ok': True, 'c_code': 'C102', 'stock': '1000', 'package': '0402', 'param_detail': '容值: 100nF; 温度系数: X7R'}})
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            screenshot = runtime_directory(Path(__file__).resolve().parents[1]) / 'value-parameters-smoke.png'
            options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            result = subprocess.run(['node', '-e', BROWSER], capture_output=True, text=True, encoding='utf-8',
                env={**os.environ, 'VALUE_TEST_URL': f'http://127.0.0.1:{server.server_port}',
                     'VALUE_SCREENSHOT': str(screenshot)}, timeout=120, **options)
            print(result.stdout)
            if result.returncode:
                raise AssertionError(result.stderr)
            assert path.read_text(encoding='utf-8') == schematic
            saved = load_overrides(app.overrides_path)['R1262']
            assert saved['accepted_c_code'] == 'C101'
            assert saved['review_status'] == 'Confirmed'
            assert saved['review_confirmed_at'] == '2020-01-01T00:00:00'
            assert saved['field_overrides']['value'] == '5.1kΩ, ±5ppm/℃, ±0.1%, 特殊低噪声, 100V'
            print(f'Screenshots: {screenshot.parent}/value-parameters-smoke-*.png')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            app.search_jobs.close()
            app.selection_jobs.close()


if __name__ == '__main__':
    main()

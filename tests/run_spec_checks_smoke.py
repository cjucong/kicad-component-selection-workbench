"""Chromium warning/inventory messages; only disposable schematic/cache data."""
import json
import os
from pathlib import Path
import subprocess
import threading
from http.server import ThreadingHTTPServer

from _scratch import temporary_project
from part_selection_server import PartSelectionApp, make_handler
from part_stock_lookup import save_stock_cache


BROWSER = r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:900}}), errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.setDefaultTimeout(12000);
  await page.goto(process.env.SPEC_TEST_URL);
  const row=ref=>page.locator(`#rows tr[data-refs="${ref}"]`);
  await page.waitForSelector('#rows tr[data-refs="C1"]');
  assert.equal(await row('C1').getByRole('button',{name:'查看选料规格警告'}).count(),1);
  assert.equal(await row('R1').locator('.spec-warning-icon').count(),0);
  await row('C1').getByRole('button',{name:'查看选料规格警告'}).click();
  assert.match(await page.locator('#specWarningDialog').innerText(),/原理图 100nF.*商品 10nF/);
  assert.match(await page.locator('#specWarningDialog').innerText(),/原理图 DSMU:0603.*商品 0402/);
  await page.getByRole('button',{name:'关闭参数核对'}).click();
  await page.waitForFunction(()=>document.querySelector('#rows tr[data-refs="C1"] [data-local-inventory]').textContent.includes('现货充足'));
  assert.match(await row('C1').locator('[data-local-inventory]').innerText(),/本地库存为零.*本行需求 3/);
  assert.doesNotMatch(await row('C1').locator('[data-local-inventory]').innerText(),/未匹配/);
  assert.match(await row('R1').locator('[data-local-inventory]').innerText(),/现货不足/);
  // This endpoint serves a new correct result, updates the disposable cache, and performs no external lookup.
  await page.evaluate(async()=>{
   const tr=document.querySelector('#rows tr[data-refs="C1"]');
   const result=await (await fetch('/api/stock?code=C100')).json();
   setStockCells(tr,result.stock);
  });
  await page.waitForFunction(()=>!document.querySelector('#rows tr[data-refs="C1"] .spec-warning-icon'));
  // Re-rendering a fresh baseline invalidates an older in-flight check, even for the same C code.
  let releaseCheck, requestedResolve;
  const requested=new Promise(resolve=>requestedResolve=resolve);
  const release=new Promise(resolve=>releaseCheck=resolve);
  await page.route('**/api/spec_check',async route=>{
   requestedResolve();
   await release;
   await route.fulfill({contentType:'application/json',body:JSON.stringify({ok:true,checks:[{
    refs:'C1',c_code:'C100',check:{c_code:'C100',status:'mismatch',issues:[{field:'value',refs:['C1'],expected:'100nF',actual:'10nF'}]}
   }]})});
  });
  await page.evaluate(()=>refreshSpecificationWarnings(true));
  await requested;
  await page.evaluate(()=>renderRows(currentRows));
  releaseCheck();
  await page.waitForTimeout(350);
  assert.equal(await row('C1').locator('.spec-warning-icon').count(),0);
  await page.unroute('**/api/spec_check');
  // An unsynchronized Value edit must not change the saved-schematic comparison.
  await row('C1').locator('[data-field="value"]').fill('1uF');
  await page.waitForTimeout(1000);
  assert.equal(await row('C1').locator('.spec-warning-icon').count(),0);
  await page.reload();
  await page.waitForSelector('#rows tr[data-refs="C1"]');
  assert.equal(await row('C1').locator('.spec-warning-icon').count(),0);
  await page.setViewportSize({width:390,height:844});
  await row('R1').locator('[data-field="replacement_c_code"]').fill('C999');
  await page.waitForFunction(()=>document.querySelector('#rows tr[data-refs="R1"] .spec-unknown'));
  await row('R1').getByRole('button',{name:'查看待核对规格'}).click();
  const bounds=await page.locator('#specWarningDialog').boundingBox();
  assert(bounds.x>=0 && bounds.x+bounds.width<=391);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({warnings:true,equivalent_values:true,live_refresh:true,saved_baseline:true,
    inventory_messages:true,unknown_code:true,mobile_bounds:true,page_errors:errors}));
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def main():
    with temporary_project(prefix='browser-spec-checks-') as directory:
        root = Path(directory)
        schematic = '(kicad_sch ' + ' '.join(
            f'(symbol (property "Reference" "{ref}") (property "Comment" "{comment}") '
            f'(property "Value" "{value}") (property "Footprint" "DSMU:0603") (property "LCSC" "{code}"))'
            for ref, comment, value, code in [('C1', 'capacitor', '100nF', 'C100'), ('R1', 'resistor', '100K', 'C200')]) + ')'
        (root / 'test.kicad_sch').write_text(schematic, encoding='utf-8')
        config = root / 'settings.json'
        config.write_text(json.dumps({'schematic_dir': '.', 'source_bom': '__schematic__', 'bom_dir': 'data'}), encoding='utf-8')
        app = PartSelectionApp(config)
        cache = {'C100': {'ok': True, 'c_code': 'C100', 'stock': '200', 'param_detail': '容值: 10nF', 'package': '0402'},
                 'C200': {'ok': True, 'c_code': 'C200', 'stock': '0', 'param_detail': '阻值: 100kΩ', 'package': '0603'}}
        save_stock_cache(app.stock_cache_path, cache)

        def fake_stock(code):
            assert code == 'C100'
            cache[code] = {**cache[code], 'param_detail': '容值: 0.1uF', 'package': '0603'}
            save_stock_cache(app.stock_cache_path, cache)
            return cache[code]

        app.stock = fake_stock
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            result = subprocess.run(['node', '-e', BROWSER], capture_output=True, text=True, encoding='utf-8',
                env={**os.environ, 'SPEC_TEST_URL': f'http://127.0.0.1:{server.server_port}'}, timeout=70, **options)
            print(result.stdout)
            if result.returncode:
                raise AssertionError(result.stderr)
            assert (root / 'test.kicad_sch').read_text(encoding='utf-8') == schematic
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            app.search_jobs.close()
            app.selection_jobs.close()


if __name__ == '__main__':
    main()

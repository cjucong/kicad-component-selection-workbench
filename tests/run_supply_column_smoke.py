"""Merged supply column in Chromium, using a disposable project and local stock."""
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
  const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.setDefaultTimeout(12000);
  await page.goto(process.env.SUPPLY_TEST_URL);
  const row=page.locator('#rows tr[data-refs="C1"]');
  const code=row.getByRole('textbox',{name:'主料 C 编码'});
  await row.waitFor();
  assert.equal(await page.locator('th[data-col="c_code"]').count(),0);
  assert.equal(await page.locator('th[data-col="stock"]').innerText(),'选用物料 / 供货');
  assert.equal(await row.locator('select[data-field="status"]').count(),0);
  assert.equal(await code.inputValue(),'C100');
  assert.equal(await row.locator('[data-col="local_inventory"]').count(),1);
  const details=row.locator('.stock-details');
  assert.equal(await details.getAttribute('open'),null);
  await details.locator('summary').click();
  assert.match(await details.innerText(),/深圳 200.*查询时间/s);
  // A stock response cannot replace a focused, unsaved primary-code editor.
  await code.focus();
  await code.evaluate(el=>{el.value='C101';window.supplyEditor=el;});
  await page.evaluate(async()=>{
   const stock=(await (await fetch('/api/stock?code=C100')).json()).stock;
   setStockCells(document.querySelector('#rows tr[data-refs="C1"]'),stock);
  });
  assert.equal(await code.evaluate(el=>el===window.supplyEditor&&el===document.activeElement),true);
  assert.equal(await code.inputValue(),'C101');
  await code.fill('C102');
  await page.waitForFunction(()=>currentRows[0].effective_c_code==='C102');
  await page.waitForFunction(()=>!document.querySelector('#rows tr').classList.contains('changed'));
  assert.equal(await row.locator('[data-field="review_status"]').inputValue(),'Pending');
  assert.match(await row.locator('[data-primary-code-link]').getAttribute('href'),/C102/);
  await page.reload();
  await row.waitFor();
  assert.equal(await code.inputValue(),'C102');
  await row.getByRole('button',{name:'清空主料 C 编码'}).click();
  await page.waitForFunction(()=>!currentRows[0].effective_c_code);
  await page.reload();
  await row.waitFor();
  assert.equal(await code.inputValue(),'');
  assert.equal(await row.locator('[data-primary-code-link]').isVisible(),false);
  // Order-only supply remains useful without inventing a C code.
  await page.evaluate(()=>{
   currentRows[0].procurement_choice={supplier:'Mouser',order_url:'https://example.com/order?sku=123&qty=1'};
   renderRows(currentRows);
  });
  assert.match(await row.locator('[data-col="stock"] .supply-order').innerText(),/Mouser.*订货链接/);
  assert.equal(await row.locator('[data-col="stock"] .supply-order a').getAttribute('href'),
    'https://example.com/order?sku=123&qty=1');
  assert.equal(await code.inputValue(),'');
  // Both the active layout and saved default layout accept pre-merge settings.
  const migrated=await page.evaluate(()=>{
   localStorage.setItem(UI_STORAGE_KEY,JSON.stringify({schemaVersion:6,
    columnOrder:['note','c_code','param','stock','review'],
    hiddenColumns:{c_code:true,stock:false,note:false},columnWidths:{c_code:180,stock:260}}));
   const live=loadUiSettings();
   localStorage.setItem(DEFAULT_VIEW_STORAGE_KEY,JSON.stringify({
    columnOrder:['c_code','param','stock'],hiddenColumns:{c_code:true,stock:false},columnWidths:{c_code:230}}));
   loadDefaultViewConfig();
   const defaults=loadUiSettings();
   localStorage.setItem(UI_STORAGE_KEY,JSON.stringify({schemaVersion:6,columnOrder:['c_code','param']}));
   const minimal=loadUiSettings();
   return {live,defaults,minimal};
  });
  assert.deepEqual(migrated.live.columnOrder,['alternates','note','stock','param','review']);
  assert.equal(migrated.live.hiddenColumns.note,false);
  assert.equal(migrated.live.hiddenColumns.stock,false);
  assert.equal(migrated.live.columnWidths.stock,260);
  assert.deepEqual(migrated.defaults.columnOrder,['stock','param']);
  assert.equal(migrated.defaults.columnWidths.stock,230);
  assert.deepEqual(migrated.minimal.columnOrder,['stock','param','alternates']);
  for (const width of [1440,768,390]) {
   await page.setViewportSize({width,height:900});
   await row.locator('[data-col="stock"]').scrollIntoViewIfNeeded();
   const bounds=await row.locator('[data-col="stock"]').boundingBox();
   for(const control of await row.locator('.supply-primary > :visible').all()) {
    const box=await control.boundingBox();
    assert(box.x>=bounds.x-1&&box.x+box.width<=bounds.x+bounds.width+1);
   }
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({merged_column:true,stock_editor_focus:true,code_save_clear:true,
    order_only:true,legacy_views:true,viewports:[1440,768,390],page_errors:errors}));
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def main():
    with temporary_project(prefix='browser-supply-column-') as directory:
        root = Path(directory)
        schematic = ('(kicad_sch (symbol (property "Reference" "C1") '
                     '(property "Comment" "capacitor") (property "Value" "100nF") '
                     '(property "Footprint" "DSMU:0603") (property "LCSC" "C100")))')
        (root / 'test.kicad_sch').write_text(schematic, encoding='utf-8')
        config = root / 'settings.json'
        config.write_text(json.dumps({'schematic_dir': '.', 'source_bom': '__schematic__',
                                      'bom_dir': 'data'}), encoding='utf-8')
        app = PartSelectionApp(config)
        stock = {'ok': True, 'c_code': 'C100', 'stock': '200', 'stock_sz': 200,
                 'status': '现货', 'delivery': '4小时发货', 'channel': '立创自营',
                 'fetched_at': '2026-10-22T12:00:00', 'param_detail': '容值: 100nF', 'package': '0603'}
        save_stock_cache(app.stock_cache_path, {'C100': stock})

        def fake_stock(code):
            assert code == 'C100'
            return stock

        app.stock = fake_stock
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            result = subprocess.run(['node', '-e', BROWSER], capture_output=True, text=True, encoding='utf-8',
                env={**os.environ, 'SUPPLY_TEST_URL': f'http://127.0.0.1:{server.server_port}'}, timeout=90, **options)
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

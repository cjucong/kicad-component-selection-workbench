"""Real Chromium local-stock regression; all workbook/CAD data is disposable."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

import openpyxl

from _scratch import temporary_project
from part_selection_server import PartSelectionApp, make_handler

ROOT = Path(__file__).resolve().parents[1]
BROWSER = r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict'),path=require('node:path');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1600,height:1000}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.setDefaultTimeout(12000);
  await page.goto(process.env.INVENTORY_TEST_URL);
  await page.waitForSelector('#rows tr[data-refs="R1"]');
  const cell=ref=>page.locator(`#rows tr[data-refs="${ref}"] [data-local-inventory]`);
  await page.waitForFunction(()=>document.querySelector('#rows [data-local-inventory]').textContent.includes('未匹配'));
  assert.equal(await page.locator('#localInventorySetCount').inputValue(),'3');
  await page.getByRole('button',{name:'数据',exact:true}).click();
  await page.locator('[data-command="importLocalInventory"]').click();
  await page.locator('#inventorySource').fill(process.env.INVENTORY_TEST_SOURCE);
  assert.equal(await page.locator('#inventoryConfirm').isEnabled(),false);
  await page.locator('#inventoryPreview').click();
  await page.waitForFunction(()=>!document.querySelector('#inventoryConfirm').disabled);
  assert.equal(await page.locator('#inventoryPreviewRows tr').count(),4);
  assert.equal(await page.locator('#inventoryPreviewRows img').count(),0);
  assert.match(await page.locator('#inventoryStatus').innerText(),/新增 2.*待核实 2.*无效行 1/);
  assert.match(await page.locator('#inventoryStatus').innerText(),/待匹配 1/);
  assert.equal(await page.locator('[data-inventory-match]').count(),1);
  if(process.env.LOCAL_INVENTORY_SCREENSHOTS) await page.screenshot({path:path.join(process.env.LOCAL_INVENTORY_SCREENSHOTS,'inventory-preview-desktop.png')});
  await page.locator('#inventoryConfirm').click();
  await page.waitForFunction(()=>document.querySelector('#inventoryStatus').textContent.includes('库存已导入'));
  await page.locator('#localInventoryDialog [data-inventory-close]').first().click();
  assert.match(await cell('R1').innerText(),/现存 5.*本行需求 3/);
  assert.match(await cell('R1').innerText(),/同编码合计需求 6/);
  assert.match(await cell('R1').innerText(),/缺口 1/);
  assert.match(await cell('R3').innerText(),/NC，不计需求/);
  assert.match(await cell('C1').innerText(),/现存 0/);
  await page.locator('#localInventorySetCount').fill('2');
  assert.match(await cell('R1').innerText(),/余量 1/);
  await page.locator('#localInventorySetCount').fill('0');
  assert.equal(await page.locator('#localInventorySetCount').getAttribute('aria-invalid'),'true');
  assert.match(await cell('R1').innerText(),/余量 1/);
  await page.locator('#bomSearch').focus();
  assert.equal(await page.locator('#localInventorySetCount').inputValue(),'2');
  await page.locator('#toggleColumns').click();
  await page.locator('[data-column-toggle="local_inventory"]').uncheck();
  await page.reload();
  await page.waitForSelector('#rows tr[data-refs="R1"]');
  assert.equal(await page.locator('#localInventorySetCount').inputValue(),'2');
  assert.equal(await page.locator('th[data-col="local_inventory"]').isVisible(),false);
  await page.locator('#toggleColumns').click();
  await page.locator('[data-column-toggle="local_inventory"]').check();
  await page.locator('#toggleColumns').click();
  await page.locator('#rows tr[data-refs="R1"] [data-field="replacement_c_code"]').fill('C200');
  await page.waitForFunction(()=>document.querySelector('#rows tr[data-refs="R1"] [data-local-inventory]').textContent.includes('现存 0'));
  assert.match(await cell('R1').innerText(),/同编码合计需求 4/);
  assert.match(await cell('R1').innerText(),/缺口 4/);
  assert.match(await cell('R2').innerText(),/余量 3/);
  await page.locator('#importLocalInventory').click();
  await page.locator('#inventorySource').fill(process.env.INVENTORY_TEST_SOURCE);
  await page.locator('#inventoryPreview').click();
  await page.waitForFunction(()=>!document.querySelector('#inventoryConfirm').disabled);
  assert.match(await page.locator('#inventoryStatus').innerText(),/重复 2/);
  await page.locator('[data-inventory-match]').fill('C900');
  await page.locator('#inventoryPreview').click();
  await page.waitForFunction(()=>document.querySelector('#inventoryStatus').textContent.includes('新增 1') && !document.querySelector('#inventoryConfirm').disabled);
  assert.match(await page.locator('#inventoryStatus').innerText(),/待匹配 0/);
  await page.setViewportSize({width:390,height:844});
  const bounds=await page.locator('#localInventoryDialog').boundingBox();
  assert(bounds.x>=0 && bounds.x+bounds.width<=391);
  if(process.env.LOCAL_INVENTORY_SCREENSHOTS) await page.screenshot({path:path.join(process.env.LOCAL_INVENTORY_SCREENSHOTS,'inventory-preview-mobile.png')});
  await page.locator('#inventoryConfirm').click();
  await page.waitForFunction(()=>document.querySelector('#inventoryStatus').textContent.includes('库存已导入'));
  const inventory=await page.evaluate(async()=>await (await fetch('/api/local_inventory')).json());
  assert.equal(inventory.items.C900.quantity,36);
  assert.equal(Object.keys(inventory.unmatched || {}).length,0);
  await page.locator('#inventoryPreview').click();
  await page.waitForFunction(()=>!document.querySelector('#inventoryConfirm').disabled);
  assert.equal(await page.locator('[data-inventory-match]').inputValue(),'C900');
  assert.match(await page.locator('#inventoryStatus').innerText(),/待匹配 0/);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({preview:true,confirm:true,dedupe:true,shared_demand:true,zero_stock:true,nc:true,
    immediate_set_count:true,persistence:true,column_settings:true,code_edit:true,manual_supplier_matching:true,mobile_bounds:true,page_errors:errors}));
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def main():
    screenshots = os.environ.get('LOCAL_INVENTORY_SCREENSHOTS')
    if screenshots:
        target = Path(screenshots).resolve()
        if not target.is_absolute() or target.is_relative_to(ROOT):
            raise ValueError('Screenshots must be outside the repository/project')
        target.mkdir(parents=True, exist_ok=True)
    with temporary_project(prefix='browser-local-inventory-') as directory:
        root = Path(directory)
        symbols = []
        for ref, comment, value, code in [('R1', 'resistor', '100R', 'C100'),
                ('R2', 'resistor', '200R', 'C100'), ('R3', 'resistor', 'NC', 'C100'),
                ('C1', 'capacitor', '100nF', 'C200')]:
            symbols.append(f'(symbol (property "Reference" "{ref}") (property "Comment" "{comment}") '
                           f'(property "Value" "{value}") (property "Footprint" "Library:0603") (property "LCSC" "{code}"))')
        schematic = '(kicad_sch ' + ' '.join(symbols) + ')'
        (root / 'example.kicad_sch').write_text(schematic)
        (root / 'example.kicad_pro').write_text('{}')
        config = root / 'settings.json'
        config.write_text(json.dumps({'source_bom': '__schematic__', 'schematic_dir': '.', 'bom_dir': 'data'}))
        source = root / 'inventory.xlsx'
        workbook = openpyxl.Workbook()
        workbook.active.append(['商品编号', '同型号已购合计', '厂家型号'])
        for row in [['C100', 5, '<img src=x onerror=alert(1)>'], ['C100', 5, 'SAMPLE'],
                    ['C200', 0, 'ZERO'], ['C300', 4, 'CONFLICT'], ['C300', 9, 'CONFLICT'], ['C400', '=1+1', 'NO-CACHE'],
                    ['D100', 36, 'SUPPLIER-MUX'], ['D200', 36, 'SUPPLIER-MUX']]:
            workbook.active.append(row)
        workbook.save(source)
        workbook.close()
        source_bytes = source.read_bytes()
        app = PartSelectionApp(config)
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            result = subprocess.run(['node', '-e', BROWSER], capture_output=True, text=True, encoding='utf-8',
                env={**os.environ, 'INVENTORY_TEST_URL': f'http://127.0.0.1:{server.server_port}',
                     'INVENTORY_TEST_SOURCE': str(source)}, timeout=90, **options)
            print(result.stdout)
            if result.returncode:
                raise AssertionError(result.stderr)
            assert (root / 'example.kicad_sch').read_text() == schematic
            assert source.read_bytes() == source_bytes
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            app.search_jobs.close()
            app.selection_jobs.close()


if __name__ == '__main__':
    main()

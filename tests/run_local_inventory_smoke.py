"""Real Chromium local-stock regression; all workbook/CAD data is disposable."""
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

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
  const qty=ref=>cell(ref).locator('[data-inventory-quantity]');
  await qty('R1').waitFor();
  assert.equal(await qty('R1').inputValue(),'0');
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
  assert.equal(await qty('R1').inputValue(),'5');
  assert.equal(await qty('R3').inputValue(),'5');
  assert.equal(await qty('C1').inputValue(),'0');
  await page.locator('#localInventorySetCount').fill('2');
  assert.equal(await qty('R1').inputValue(),'5');
  await page.locator('#localInventorySetCount').fill('0');
  assert.equal(await page.locator('#localInventorySetCount').getAttribute('aria-invalid'),'true');
  assert.equal(await qty('R1').inputValue(),'5');
  await page.locator('#bomSearch').focus();
  assert.equal(await page.locator('#localInventorySetCount').inputValue(),'2');
  await page.locator('#toggleColumns').click();
  await page.locator('[data-column-toggle="stock"]').uncheck();
  await page.reload();
  await page.waitForSelector('#rows tr[data-refs="R1"]');
  assert.equal(await page.locator('#localInventorySetCount').inputValue(),'2');
  assert.equal(await page.locator('th[data-col="stock"]').isVisible(),false);
  await page.locator('#toggleColumns').click();
  await page.locator('[data-column-toggle="stock"]').check();
  await page.locator('#toggleColumns').click();
  await page.locator('#rows tr[data-refs="R1"] [data-field="replacement_c_code"]').fill('C200');
  await page.waitForFunction(()=>document.querySelector('#rows tr[data-refs="R1"] [data-inventory-code="C200"]')?.value==='0');
  assert.equal(await qty('R2').inputValue(),'5');
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
  const importedInventory=await page.evaluate(async()=>await (await fetch('/api/local_inventory')).json());
  assert.equal(importedInventory.items.C900.quantity,36);
  assert.equal(Object.keys(importedInventory.unmatched || {}).length,0);
  await page.locator('#inventoryPreview').click();
  await page.waitForFunction(()=>!document.querySelector('#inventoryConfirm').disabled);
  assert.equal(await page.locator('[data-inventory-match]').inputValue(),'C900');
  assert.match(await page.locator('#inventoryStatus').innerText(),/待匹配 0/);
  await page.locator('#localInventoryDialog [data-inventory-close]').first().click();
  await page.setViewportSize({width:1600,height:1000});
  const overridesBefore=await page.evaluate(()=>JSON.stringify(currentRows.map(row=>[row.ref,row.review_status,row.status,row.schematic_sync])));
  const inventory=()=>page.evaluate(async()=>await (await fetch('/api/local_inventory')).json());
  // A single main code shares one physical balance, even across NC rows.
  await qty('R2').fill('24');
  assert.equal(await qty('R3').inputValue(),'24');
  await page.waitForFunction(async()=>(await (await fetch('/api/local_inventory')).json()).items.C100.quantity===24);
  await page.evaluate(()=>flushRowEdits());
  // In-flight edits serialize; the old response must not replace newer input.
  let release, held, interceptedOnce=false;
  const intercepted=new Promise(resolve=>held=resolve);
  await page.route('**/api/local_inventory/update',async route=>{
    if(!interceptedOnce) {
      interceptedOnce=true;
      await new Promise(resolve=>{release=resolve;held();});
    }
    await route.continue();
  });
  await qty('R2').fill('30');
  await intercepted;
  await qty('R2').fill('31');
  release();
  await page.evaluate(()=>flushRowEdits());
  await page.unroute('**/api/local_inventory/update');
  assert.equal((await inventory()).items.C100.quantity,31);
  assert.equal(await qty('R3').inputValue(),'31');
  await qty('R2').fill('');
  assert.equal(await qty('R2').getAttribute('aria-invalid'),'true');
  assert.equal((await inventory()).items.C100.quantity,31);
  await qty('R2').press('Escape');
  assert.equal(await qty('R2').inputValue(),'31');
  await qty('R2').fill('0');
  await qty('R2').press('Enter');
  await page.evaluate(()=>flushRowEdits());
  assert.equal((await inventory()).items.C100.quantity,0);
  // A failed write remains editable and blocks normal exit/export flush.
  await page.route('**/api/local_inventory/update',route=>route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({ok:false,error:'测试保存失败'})}));
  await qty('R2').fill('9');
  await qty('R2').press('Enter');
  await page.waitForFunction(()=>document.querySelector('[data-inventory-retry="C100"]').textContent==='未保存');
  assert.equal(await qty('R2').inputValue(),'9');
  assert.equal(await page.evaluate(()=>flushRowEdits().then(()=>false,()=>true)),true);
  assert.equal((await inventory()).items.C100.quantity,0);
  await page.unroute('**/api/local_inventory/update');
  await cell('R2').locator('[data-inventory-retry]').click();
  await page.evaluate(()=>flushRowEdits());
  // A remote edit requires explicit acknowledgement before overwriting.
  let latest=(await inventory()).items.C100;
  await page.request.post(process.env.INVENTORY_TEST_URL+'/api/local_inventory/update',{data:{c_code:'C100',quantity:18,expected:latest}});
  await qty('R2').fill('20');
  await qty('R2').press('Enter');
  await page.waitForFunction(()=>document.querySelector('[data-inventory-retry="C100"]').title.includes('其他窗口'));
  assert.equal((await inventory()).items.C100.quantity,18);
  page.once('dialog',dialog=>dialog.accept());
  await cell('R2').locator('[data-inventory-retry]').click();
  await page.waitForFunction(async()=>(await (await fetch('/api/local_inventory')).json()).items.C100.quantity===20);
  await qty('C1').fill('16');
  await page.locator('#bomSearch').focus();
  await page.evaluate(()=>flushRowEdits());
  assert.equal((await inventory()).items.C200.quantity,16);
  await qty('R2').focus();
  await page.evaluate(()=>updateRowsForStock('C100',{ok:true,c_code:'C100',stock:700}));
  assert.equal(await qty('R2').evaluate(input=>input===document.activeElement),true);
  assert.equal(await qty('R2').inputValue(),'20');
  assert.equal(await page.evaluate(()=>JSON.stringify(currentRows.map(row=>[row.ref,row.review_status,row.status,row.schematic_sync]))),overridesBefore);
  if(process.env.LOCAL_INVENTORY_SCREENSHOTS) await page.screenshot({path:path.join(process.env.LOCAL_INVENTORY_SCREENSHOTS,'inventory-inline-desktop.png')});
  await page.reload(); await qty('R2').waitFor();
  assert.equal(await qty('R2').inputValue(),'20');
  assert.equal(await qty('C1').inputValue(),'16');
  // Real toolbar exports must await unsaved balances, not the debounce timer.
  await page.locator('#localInventorySetCount').fill('100');
  let exportRequests=0, releaseExportSave, exportSaveHeld;
  page.on('request',request=>{if(request.url().endsWith('/api/export_pump_table')) exportRequests++;});
  const exportSaveStarted=new Promise(resolve=>exportSaveHeld=resolve);
  await page.route('**/api/local_inventory/update',async route=>{
    if(route.request().postDataJSON().c_code==='C100') {
      await new Promise(resolve=>{releaseExportSave=resolve;exportSaveHeld();});
    }
    await route.continue();
  });
  const exportBom=async()=>{
    await page.locator('#exportPumpMenu').click();
    await page.locator('#pumpExportMode').selectOption('all_non_nc');
    await page.locator('#exportPumpTable').click();
  };
  await qty('C1').fill('41');
  await qty('R2').fill('37');
  await exportBom();
  await exportSaveStarted;
  assert.equal(exportRequests,0);
  const exported=page.waitForResponse('**/api/export_pump_table');
  releaseExportSave();
  assert.equal((await (await exported).json()).count,3);
  await page.unroute('**/api/local_inventory/update');
  assert.equal((await inventory()).items.C100.quantity,37);
  assert.equal((await inventory()).items.C200.quantity,41);
  assert.equal(await qty('R1').inputValue(),'41');
  // A real zero must export as zero, not an unknown or demand-derived quantity.
  await qty('R2').fill('0');
  const zeroExport=page.waitForResponse('**/api/export_pump_table');
  await exportBom();
  assert.equal((await (await zeroExport).json()).ok,true);
  // Failed inventory saves must prevent stale BOM output from being created.
  await page.route('**/api/local_inventory/update',route=>route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({ok:false,error:'测试导出前保存失败'})}));
  await qty('R2').fill('99');
  await exportBom();
  await page.waitForFunction(()=>document.querySelector('[data-inventory-retry="C100"]').textContent==='未保存');
  assert.equal(await page.evaluate(()=>flushRowEdits().then(()=>false,()=>true)),true);
  assert.equal(exportRequests,2);
  await page.unroute('**/api/local_inventory/update');
  await qty('R2').press('Escape');
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({preview:true,confirm:true,dedupe:true,shared_demand:true,zero_stock:true,nc:true,
    inline_edit:true,inflight_edits:true,save_failure:true,conflict_confirmation:true,focus_preserved:true,
    immediate_set_count:true,persistence:true,column_settings:true,code_edit:true,manual_supplier_matching:true,mobile_bounds:true,
    immediate_bom_export:true,zero_bom_export:true,failed_save_blocks_export:true,page_errors:errors}));
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
        exports = []
        export_bom = app.export_pump_table

        def capture_export(payload):
            result = export_bom(payload)
            with Path(result['path']).open(encoding='utf-8-sig', newline='') as file:
                reader = csv.DictReader(file)
                records = list(reader)
                assert reader.fieldnames[10:12] == ['本地库存', '本地库存说明']
            exports.append(records)
            return result

        app.export_pump_table = capture_export
        app._open_directory = Mock(return_value={'ok': True})
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
            assert len(exports) == 2
            for records, expected in zip(exports, ({'C100': '37', 'C200': '41'}, {'C100': '0', 'C200': '41'})):
                assert len(records) == 3
                assert {row['位号'] for row in records} == {'R1', 'R2', 'C1'}
                for row in records:
                    assert row['本地库存'] == expected[row['商品编号']]
                    assert row['单套用量'] == '1'
                    assert '未扣减需求' in row['本地库存说明']
                    if row['商品编号'] == 'C200':
                        assert '共享' in row['本地库存说明']
            assert app._open_directory.call_count == 2
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            app.search_jobs.close()
            app.selection_jobs.close()


if __name__ == '__main__':
    main()

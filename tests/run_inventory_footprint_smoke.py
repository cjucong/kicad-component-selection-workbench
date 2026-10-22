"""Chromium inventory/row-footprint workflow with synthetic, auto-cleaned CAD."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from collections import Counter
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from _scratch import temporary_project
from part_selection_inventory import save_inventory
from part_selection_overrides import save_overrides
from part_selection_server import PartSelectionApp, make_handler
from test_footprint_choices import module
from test_row_footprint import stage_conversion


BROWSER = r'''
const {chromium}=require('playwright'), assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:950}}), errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.setDefaultTimeout(20000);
  await page.goto(process.env.WORKFLOW_URL);
  const row=ref=>page.locator(`#rows tr[data-refs="${ref}"]`);
  const code=ref=>row(ref).locator('[data-field="replacement_c_code"]');
  await row('R1').waitFor();
  await page.waitForFunction(()=>document.querySelector('[data-local-inventory]').textContent==='本地库存 12');
  // No Enter: a late old code must not overwrite the newer result or editor.
  await code('R1').fill('C201');
  await page.waitForFunction(()=>currentRows.find(r=>r.ref==='R1').effective_c_code==='C201');
  await code('R1').fill('C202');
  await page.waitForFunction(()=>currentRows.find(r=>r.ref==='R1').stock?.stock==='202');
  await page.waitForTimeout(1500);
  assert.equal(await code('R1').inputValue(),'C202');
  assert.match(await row('R1').locator('[data-stock-cell]').innerText(),/现货 202/);
  assert.equal(await code('R1').evaluate(el=>el===document.activeElement),true);
  await row('R1').locator('[data-field="review_status"]').selectOption('Confirmed');
  await code('R1').fill('c202');
  await page.waitForTimeout(650);
  assert.equal(await row('R1').locator('[data-field="review_status"]').inputValue(),'Confirmed');
  // Two rows save the same code: one upstream call, no cross-row cancellation.
  await code('R1').fill('C300');
  await code('R2').fill('C300');
  await page.waitForFunction(()=>currentRows.every(r=>r.stock?.stock==='300'));
  await code('R1').fill('C500');
  await row('R1').getByRole('button',{name:'现货 查询失败，重试'}).waitFor();
  await row('R1').getByRole('button',{name:'现货 查询失败，重试'}).click();
  await page.waitForFunction(()=>currentRows.find(r=>r.ref==='R1').stock?.stock==='500');
  // Local-only management and rename are usable without a selected C code.
  await code('R2').fill('');
  await page.waitForFunction(()=>!currentRows.find(r=>r.ref==='R2').effective_c_code);
  await row('R2').locator('[data-field="review_status"]').selectOption('Confirmed');
  await page.waitForFunction(()=>currentRows.find(r=>r.ref==='R2').review_status==='Confirmed');
  await row('R2').getByRole('button',{name:'预览／管理封装'}).click();
  await page.getByText('当前绑定的本地库封装 · mm',{exact:true}).waitFor();
  assert.match(await page.locator('[data-fp-background]').innerText(),/无唯一 C 编码/);
  await page.locator('[data-fp-rename]').click();
  await page.locator('[data-fp-detail]').filter({hasText:'焊盘 2'}).waitFor();
  await page.locator('[data-fp-name]').fill('Renamed_R2');
  await page.locator('[data-fp-ack]').check();
  await page.locator('.fp-apply').click();
  await page.waitForFunction(()=>currentRows.find(r=>r.ref==='R2').footprint==='Local:Renamed_R2');
  assert.equal(await row('R2').locator('[data-field="review_status"]').inputValue(),'Confirmed',
    JSON.stringify(await page.evaluate(()=>currentRows.find(r=>r.ref==='R2'))));
  assert.equal(await row('R2').locator('[data-field="value"]').inputValue(),'200k');
  assert.equal(await code('R2').inputValue(),'');
  // Real KiCad-rendered layers, background conversion, comparison and cancellation.
  await row('R1').getByRole('button',{name:'预览／管理封装'}).click();
  await page.waitForFunction(()=>document.querySelector('[data-fp-background]')?.textContent.includes('已就绪'));
  await page.waitForFunction(()=>document.querySelector('[data-fp-alignment]')?.dataset.ok==='true');
  assert((await page.locator('[data-fp-layer-image]').count())>=2);
  await page.locator('[data-fp-view="side"]').check();
  assert.equal(await page.locator('[data-fp-layout]').getAttribute('data-fp-layout'),'side');
  await page.locator('[data-fp-view="overlay"]').check();
  await page.locator('[data-fp-opacity]').fill('65');
  if(process.env.WORKFLOW_SCREENSHOT) await page.screenshot({path:process.env.WORKFLOW_SCREENSHOT});
  await page.locator('[data-fp-cancel]').click();
  assert.equal(await row('R1').locator('[data-field="footprint"]').inputValue(),'Original');
  // Apply through the independent interface, never through candidate selection.
  await row('R1').getByRole('button',{name:'预览／管理封装'}).click();
  await page.waitForFunction(()=>document.querySelector('[data-fp-alignment]')?.dataset.ok==='true');
  await page.locator('[data-fp-name]').fill('Converted_R1');
  await page.locator('[data-fp-ack]').check();
  await page.locator('.fp-apply').click();
  await page.waitForFunction(()=>currentRows.find(r=>r.ref==='R1').footprint==='Local:Converted_R1');
  assert.equal(await code('R1').inputValue(),'C500');
  assert.equal(await row('R1').locator('[data-field="value"]').inputValue(),'100k');
  assert.equal(await row('R1').locator('[data-field="review_status"]').inputValue(),'Pending');
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({automatic_stock:true,shared_request:true,late_response:true,retry:true,
    same_code_confirmation:true,local_preview:true,rename_without_code:true,converted_overlay:true,
    cancel_readonly:true,independent_footprint_save:true,page_errors:errors}));
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def main():
    with temporary_project(prefix='wf-', short_paths=True) as directory:
        root = Path(directory)
        pretty = root/'Local.pretty'
        pretty.mkdir()
        (pretty/'Original.kicad_mod').write_text(module('Original'),encoding='utf-8')
        (root/'fp-lib-table').write_text('(fp_lib_table (version 7) (lib (name "Local") '
            '(type "KiCad") (uri "${KIPRJMOD}/Local.pretty")))',encoding='utf-8')
        schematic = '(kicad_sch '+''.join('(symbol (property "Reference" "R%d") '
            '(property "Comment" "resistor") (property "Value" "%dk") '
            '(property "Footprint" "Local:Original") (property "LCSC" "C100"))' % (i,i*100)
            for i in (1,2))+')'
        (root/'test.kicad_sch').write_text(schematic,encoding='utf-8')
        config = root/'settings.json'
        config.write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.','bom_dir':'data'}),encoding='utf-8')
        app = PartSelectionApp(config)
        save_overrides(app.overrides_path,{ref:{'review_status':'Confirmed'} for ref in ('R1','R2')})
        save_inventory(app.local_inventory_path,{'schema_version':1,'items':{'C100':{'quantity':12,'status':'ready'}}})
        calls = Counter()
        lock = threading.Lock()
        def stock(code):
            with lock:
                calls[code] += 1
                attempt = calls[code]
            time.sleep(1.2 if code in ('C201','C300') else .03)
            if code == 'C500' and attempt == 1:
                return {'ok':False,'c_code':code,'status':'查询失败','error':'temporary upstream failure'}
            return {'ok':True,'c_code':code,'stock':code[1:],'param_detail':'阻值: 100kΩ; 功率: 100mW','package':'0603'}
        app.stock = stock
        server = ThreadingHTTPServer(('127.0.0.1',0),make_handler(app))
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            def convert(code, progress=None):
                time.sleep(.6)
                return stage_conversion(app.footprint_choices,code,progress)
            with patch('part_selection_footprints.LibraryImporter.convert',side_effect=convert):
                flags = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
                result = subprocess.run(['node','-e',BROWSER],capture_output=True,text=True,encoding='utf-8',
                    env={**os.environ,'WORKFLOW_URL':f'http://127.0.0.1:{server.server_port}'},timeout=120,**flags)
                print(result.stdout)
                if result.returncode:
                    raise AssertionError(result.stderr)
            assert calls['C300'] == 1, calls
            assert (root/'test.kicad_sch').read_text(encoding='utf-8') == schematic
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            app.search_jobs.close()
            app.selection_jobs.close()


if __name__ == '__main__':
    main()

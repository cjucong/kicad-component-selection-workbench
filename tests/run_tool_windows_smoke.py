"""Disposable HTTP utility-page checks; does not claim native window or CAD QA."""
import json
import os
from pathlib import Path
import subprocess
import threading
from http.server import ThreadingHTTPServer

from _scratch import temporary_project
from part_selection_server import PartSelectionApp, make_handler

BROWSER = r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage(), errors=[];
  page.setDefaultTimeout(12000);
  page.on('pageerror',error=>errors.push(error.message));
  page.on('dialog',dialog=>dialog.accept());
  async function open(kind,payload={}) {
   await page.goto(process.env.TOOL_WINDOWS_URL+'?view=tool&tool='+kind);
   await page.evaluate(({kind,payload})=>{
    window.pywebview={api:{tool_window:async(action,value)=>{
     if(action==='propose') setTimeout(()=>window.receiveNativeToolResult({request_id:value.request_id,
      result:{ok:true,current_version:'test',mode:'off',installed:false}}),0);
     return {ok:true};
    }}};
    const result=window.receiveNativeToolRequest({kind,payload,host:'test',project:'fixture',request_id:'one'});
    if(!result.ok) throw Error(result.error);
   },{kind,payload});
  }
  await open('settings');
  await page.locator('#pathSettingsDialog').waitFor({state:'visible'});
  await page.locator('[name="jump_cursor_behavior"]').selectOption('stay');
  assert.equal(await page.evaluate(()=>window.prepareNativeToolExit()),false);
  await page.locator('#pathSettingsDialog [data-close-paths]').last().click();
  assert.equal(await page.evaluate(()=>window.prepareNativeToolExit()),true);
  await open('inventory');
  await page.locator('#localInventoryDialog').waitFor({state:'visible'});
  await page.locator('#inventorySource').fill('C:/disposable-only.xlsx');
  assert.equal(await page.evaluate(()=>window.prepareNativeToolExit()),false);
  await page.locator('[data-inventory-close]').last().click();
  assert.equal(await page.evaluate(()=>window.prepareNativeToolExit()),true);
  await open('field-mapping');
  await page.locator('#pathMappingFields').waitFor({state:'visible'});
  assert.equal(await page.locator('#pathDataFields').isVisible(),false);
  await open('list-view');
  await page.locator('#columnList [data-column-toggle]').first().waitFor({state:'visible'});
  const toggle=page.locator('#columnList [data-column-toggle="note"]');
  await toggle.setChecked(true);
  assert.equal(await page.evaluate(()=>window.prepareNativeToolExit()),true);
  await page.evaluate(()=>{window.nativeToolDidClose();window.cancelNativeToolExit();
    window.receiveNativeToolRequest({kind:'list-view',payload:{},host:'test',project:'fixture',request_id:'two'});});
  await page.locator('#columnList [data-column-toggle]').first().waitFor({state:'visible'});
  await open('value-parameters',{refs:'R1',bom:'__schematic__'});
  await page.locator('#valueParametersDialog').waitFor({state:'visible'});
  await page.locator('[data-value-key="tolerance"]').check();
  assert.equal(await page.evaluate(()=>window.prepareNativeToolExit()),false);
  await page.locator('[data-value-cancel]').click();
  await open('undo',{refs:'R1',bom:'__schematic__'});
  await page.getByRole('button',{name:'确认撤回本行修改',exact:true}).waitFor();
  await page.getByRole('button',{name:'确认撤回本行修改',exact:true}).click();
  await page.getByText('已撤回工作台修改；原理图和 PCB 未改动',{exact:true}).waitFor();
  await open('footprint',{refs:'R1',bom:'__schematic__'});
  await page.locator('.fp-workflow').waitFor({state:'visible'});
  await page.locator('[data-fp-close]:visible').last().click();
  await open('plugin-update');
  await page.getByText('当前版本：test',{exact:true}).waitFor();
  assert.deepEqual(await page.evaluate(()=>Object.keys(window.pywebview.api)),['tool_window']);
  await page.goto(process.env.TOOL_WINDOWS_URL);
  await page.locator('#rows tr[data-refs="R1"]').waitFor();
  await page.evaluate(()=>{window.opened=[];window.pywebview={api:{tool_window:async(action,value)=>{
    if(action==='open') opened.push(value);return {ok:true};
  }}};});
  await page.locator('#rows tr[data-refs="R1"] [data-action="manage-footprint"]').click();
  await page.waitForFunction(()=>opened.length===1);
  assert.equal(await page.evaluate(()=>opened[0].kind),'footprint');
  assert.equal(await page.evaluate(()=>opened[0].payload.refs),'R1');
  assert.equal(await page.locator('.fp-overlay').count(),0);
  await page.evaluate(async()=>{
    window.retainedRow=document.querySelector('#rows tr[data-refs="R2"]');
    window.retainedInput=retainedRow.querySelector('[data-field="note"]');
    retainedInput.focus();
    const result=await fetch('/api/field_override',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({refs:'R1',bom:'__schematic__',field_overrides:{value:'47k'}})});
    if(!result.ok) throw Error('Fixture update failed');
    await window.nativeToolChanged({kind:'value-parameters',payload:{refs:'R1',bom:'__schematic__'}});
  });
  await page.waitForFunction(()=>document.querySelector('#rows tr[data-refs="R1"] [data-field="value"]').value==='47k');
  assert.equal(await page.evaluate(()=>document.querySelector('#rows tr[data-refs="R2"]')===retainedRow),true,
    'Utility save replaced an unrelated row');
  assert.equal(await page.evaluate(()=>document.activeElement===retainedInput),true,
    'Utility save lost the focused editor');
  const unintended=[];
  page.on('request',request=>{
    if(request.method()==='POST' && /\/api\/(override|field_override)$/.test(request.url())) unintended.push(request.postDataJSON());
  });
  await page.locator('#rows tr[data-refs="R1"] [data-field="replacement_c_code"]').focus();
  await page.evaluate(()=>{
    const rows=structuredClone(currentRows);
    rows.find(row=>row.refs==='R1').footprint='Local:Renamed';
    renderRows(rows,{patch:true});
  });
  await page.evaluate(()=>flushRowEdits());
  assert.deepEqual(unintended,[],'Refreshing a renamed footprint saved a focused C-code editor');
  assert.equal(await page.locator('#rows tr[data-refs="R1"] [data-field="replacement_c_code"]').evaluate(el=>el.dataset.savedValue===el.value),true);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({ok:true,settings:true,dirty_protection:true,inventory:true,mapping:true,list:true,update:true}));
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def run_checks():
    with temporary_project(prefix='utility-routes-') as root:
        root = Path(root)
        (root/'example.kicad_sch').write_text('(kicad_sch (symbol (property "Reference" "R1") '
            '(property "Value" "10k") (property "Comment" "resistor") '
            '(property "LCSC" "C101") (property "Footprint" "Local:R")) '
            '(symbol (property "Reference" "R2") (property "Value" "33k") '
            '(property "Comment" "resistor") (property "Footprint" "Local:R")))',encoding='utf-8')
        config = root/'settings.json'
        config.write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.','bom_dir':'data'}))
        app = PartSelectionApp(config)
        from part_stock_lookup import save_stock_cache
        save_stock_cache(app.stock_cache_path, {'C101':{'ok':True,'c_code':'C101',
            'param_detail':'阻值: 10k; 精度: ±1%; 最大工作电压: 50V'}})
        app.save_field_override({'refs':'R1','bom':'__schematic__','field_overrides':{'value':'22k'}})
        server = ThreadingHTTPServer(('127.0.0.1',0),make_handler(app))
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            result = subprocess.run(['node','-e',BROWSER],env={**os.environ,
                'TOOL_WINDOWS_URL':f'http://127.0.0.1:{server.server_port}/'},
                capture_output=True,text=True,encoding='utf-8',timeout=90)
            assert result.returncode == 0,result.stderr
            return result.stdout
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            app.design_sync.close(); app.search_jobs.close(); app.selection_jobs.close()


if __name__ == '__main__':
    print(run_checks())

"""Large table loading checks using synthetic rows and isolated Chromium."""
import json
import os
import subprocess
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

from _scratch import temporary_project
from part_selection_server import PartSelectionApp, make_handler

BROWSER = r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1600,height:1000}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const rows=Array.from({length:650},(_,i)=>({ref:`R${i+1}`,refs:`R${i+1}`,refs_list:[`R${i+1}`],quantity:1,
    comment:`Synthetic ${i}`,value:`${i}R`,footprint:'Library:0603',effective_c_code:'C100',review_status:'Pending'}));
  let release;
  await page.route('**/api/rows?**',async route=>{await new Promise(r=>release=r);await route.fulfill({json:{rows}});});
  await page.goto(process.env.TABLE_TEST_URL);
  await page.waitForFunction(()=>typeof rowsLoading!=='undefined' && rowsLoading);
  assert.equal(await page.locator('#tableLoadProgress').isVisible(),true);
  assert.equal(await page.locator('#tableLoadProgress progress').getAttribute('value'),null);
  if(process.env.TABLE_SCREENSHOTS) await page.screenshot({path:process.env.TABLE_SCREENSHOTS+'/loading.png'});
  const start=Date.now(); release();
  await page.waitForFunction(()=>document.querySelectorAll('#rows tr[data-ref]').length>0);
  const first=Date.now()-start;
  await page.waitForFunction(()=>!rowsLoading,{},{timeout:30000});
  const total=Date.now()-start;
  assert.equal(await page.locator('#rows tr[data-ref]').count(),650);
  assert.equal(await page.locator('#tableLoadProgress').isVisible(),false);
  assert.equal(await page.locator('#rows').getAttribute('inert'),null);
  if(process.env.TABLE_SCREENSHOTS) await page.screenshot({path:process.env.TABLE_SCREENSHOTS+'/loaded.png'});
  await page.setViewportSize({width:390,height:844});
  if(process.env.TABLE_SCREENSHOTS) await page.screenshot({path:process.env.TABLE_SCREENSHOTS+'/mobile.png'});
  await page.setViewportSize({width:1600,height:1000});
  await page.locator('#bomSearch').fill('Synthetic 649R');
  await page.waitForFunction(()=>document.querySelectorAll('#rows tr:not(.hidden-row)[data-ref]').length===1);
  await page.locator('#bomSearch').fill('');
  await page.unroute('**/api/rows?**');
  await page.route('**/api/rows?**',route=>route.fulfill({status:500,json:{error:'synthetic failure'}}));
  await page.evaluate(()=>loadRows());
  assert.match(await page.locator('#state').innerText(),/synthetic failure/);
  assert.equal(await page.locator('#tableLoadProgress').isVisible(),false);
  assert.equal(await page.locator('#rows').getAttribute('inert'),null);
  await page.unroute('**/api/rows?**');
  await page.route('**/api/rows?**',route=>route.fulfill({json:{rows:[]}}));
  await page.evaluate(()=>loadRows());
  assert.equal(await page.locator('#rows tr[data-ref]').count(),0);
  assert.equal(await page.locator('#tableLoadProgress').isVisible(),false);
  await page.unroute('**/api/rows?**');
  let releaseOld, oldArrived;
  const arrived=new Promise(r=>oldArrived=r);
  let calls=0;
  await page.route('**/api/rows?**',async route=>{
    if(++calls===1) {await new Promise(r=>{releaseOld=r;oldArrived();});await route.fulfill({json:{rows}});}
    else await route.fulfill({json:{rows:rows.slice(0,1)}});
  });
  await page.evaluate(()=>{window.oldLoad=loadRows();});
  await arrived;
  await page.evaluate(()=>loadRows());
  releaseOld(); await page.evaluate(()=>oldLoad);
  assert.equal(await page.locator('#rows tr[data-ref]').count(),1);
  assert.equal(await page.evaluate(()=>currentRows.length),1);
  assert.equal(await page.locator('#tableLoadProgress').isVisible(),false);
  await page.unroute('**/api/rows?**');
  await page.route('**/api/rows?**',route=>route.fulfill({json:{rows:[rows[0],null]}}));
  await page.evaluate(()=>loadRows());
  assert.equal(await page.locator('#rows tr[data-ref]').count(),1);
  assert.equal(await page.evaluate(()=>currentRows.length),1);
  assert.equal(await page.locator('#rows').getAttribute('inert'),null);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({first_rows_ms:first,all_rows_ms:total,rows:650,errors}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


def main():
    with temporary_project(prefix='table-loading-') as directory:
        root = Path(directory)
        (root / 'example.kicad_sch').write_text('(kicad_sch)', encoding='utf-8')
        (root / 'settings.json').write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.','bom_dir':'data'}))
        app = PartSelectionApp(root / 'settings.json')
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(['node','-e',BROWSER], capture_output=True, text=True, encoding='utf-8',
                env={**os.environ,'TABLE_TEST_URL':f'http://127.0.0.1:{server.server_port}'}, timeout=65)
            print(result.stdout)
            if result.returncode:
                raise AssertionError(result.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            app.search_jobs.close()
            app.selection_jobs.close()


if __name__ == '__main__':
    main()

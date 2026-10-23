"""Synthetic workbench B menu and native-tool route in real offline Chromium."""
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
  const page=await browser.newPage({viewport:{width:1440,height:920}}), errors=[], requests=[];
  page.on('pageerror',error=>errors.push(error.message));
  let pendingPreview=null, previewPolls=0;
  await page.route('**/api/start_design_sync_preview',async route=>{
   const body=route.request().postDataJSON(); requests.push(body);
   pendingPreview={ok:true,preview_id:'synthetic',refs:body.refs,destination:body.destination,
    schematic_changes:[],blocked:false,pcb_preview:body.destination==='schematic'?null:{ok:true,preview_id:'pcb',
     items:[{ref:body.refs[0],kind:'rename',before:'Local:Original',after:'Local:0603',needs_ack:false,
      field_changes:[{name:'Value',before:'10k',after:'22k'}]}]}};
   previewPolls=0;
   await route.fulfill({json:{ok:true,job_id:'preflight',done:false,stage:'queued',message:'Waiting for verified PCB IPC'}});
  });
  await page.route('**/api/design_sync_job?*',async route=>{
   previewPolls++;
   await route.fulfill({json:{ok:true,job_id:'preflight',done:previewPolls>1,
    stage:previewPolls>1?'preview_ready':'preflight:connecting',message:'Waiting for verified PCB IPC',
    elapsed_seconds:1,stage_elapsed_seconds:.5,
    timings:[{stage:'source',message:'Read design data',seconds:.25}],preview:previewPolls>1?pendingPreview:null}});
  });
  await page.goto(process.env.DESIGN_SYNC_URL);
  await page.locator('#rows tr[data-ref]').first().waitFor();
  await page.locator('#designSyncMenu').click();
  await page.locator('[data-design-destination="both"]').click();
  await page.getByText('请先选择需要同步的器件行',{exact:true}).waitFor();
  assert.equal(requests.length,0);
  await page.locator('#rows tr[data-refs="R1"] [data-select-row]').check();
  await page.locator('#designSyncMenu').click();
  await page.locator('[data-design-destination="pcb"]').click();
  await page.getByText('Waiting for verified PCB IPC',{exact:true}).waitFor();
  assert.equal(await page.locator('[data-design-bar]').getAttribute('value'),null);
  assert.match(await page.locator('[data-design-elapsed]').textContent(),/s/);
  await page.getByText('封装仅改名',{exact:true}).waitFor();
  assert.deepEqual(requests.at(-1).refs,['R1']);
  assert.equal(await page.locator('[data-design-compare]').count(),0);
  assert.equal(await page.locator('[data-design-bar]').getAttribute('value'),'1');
  await page.locator('[data-design-timing] summary').click();
  await page.getByText('Read design data',{exact:false}).waitFor();
  for(const width of [1440,768,390]) {
   await page.setViewportSize({width,height:920});
   const bounds=await page.locator('[data-design-panel]').boundingBox();
   assert(bounds.x>=0 && bounds.x+bounds.width<=width+1);
   assert(await page.locator('[data-design-apply]').evaluate(el=>el.getBoundingClientRect().right<=innerWidth));
   assert(await page.locator('[data-design-changes] table').evaluate(el=>
    el.querySelector('th').getBoundingClientRect().bottom <= el.querySelector('td').getBoundingClientRect().top + 1));
   if(process.env.DESIGN_SYNC_SCREENSHOTS) await page.screenshot({path:process.env.DESIGN_SYNC_SCREENSHOTS+'/'+width+'.png'});
  }
  await page.locator('[data-design-close]').click();
  await page.setViewportSize({width:1440,height:920});
  await page.locator('#designSyncScope').selectOption('all');
  await page.locator('#rows tr[data-refs="R2"] .row-sync-menu summary').click();
  await page.locator('#rows tr[data-refs="R2"] [data-action="sync-both"]').click();
  await page.getByRole('dialog',{name:'同步设计'}).waitFor();
  assert.deepEqual(requests.at(-1).refs,['R2']);
  assert.equal(requests.at(-1).destination,'both');
  await page.locator('[data-design-close]').click();
  await page.locator('#designSyncMenu').click();
  await page.locator('[data-design-destination="schematic"]').click();
  await page.getByRole('dialog',{name:'同步设计'}).waitFor();
  assert.deepEqual(requests.at(-1).refs,['R1','R2']);
  await page.locator('[data-design-close]').click();
  await page.locator('#designSyncScope').selectOption('filtered');
  await page.locator('#bomSearch').fill('22k');
  await page.locator('#designSyncMenu').click();
  await page.locator('[data-design-destination="schematic"]').click();
  await page.getByRole('dialog',{name:'同步设计'}).waitFor();
  assert.deepEqual(requests.at(-1).refs,['R2']);
  await page.locator('[data-design-close]').click();
  await page.goto(process.env.DESIGN_SYNC_URL+'?view=tool&tool=design-sync');
  await page.evaluate(()=>{
   window.pywebview={api:{tool_window:async()=>({ok:true})}};
   const result=window.receiveNativeToolRequest({kind:'design-sync',request_id:'one',host:'test',project:'fixture',
    payload:{refs:'R1',destination:'pcb',bom:'__schematic__'}});
   if(!result.ok) throw Error(result.error);
  });
  await page.getByText('封装仅改名',{exact:true}).waitFor();
  assert.equal(await page.locator('.workbench-chrome').isVisible(),false);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({ok:true,selected:true,filtered:true,all:true,row_scope:true,tool_route:true,widths:[1440,768,390]}));
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def run_checks():
    screenshots = os.environ.get('DESIGN_SYNC_SCREENSHOTS')
    if screenshots:
        target = Path(screenshots).resolve()
        if target.is_relative_to(Path(__file__).resolve().parents[1]):
            raise ValueError('Screenshots must stay outside the source/project')
        target.mkdir(parents=True,exist_ok=True)
    with temporary_project(prefix='design-sync-browser-') as root:
        root = Path(root)
        path = root / 'example.kicad_sch'
        path.write_text('(kicad_sch '+''.join(f'(symbol (property "Reference" "{ref}") '
            f'(property "Comment" "resistor") (property "Value" "{value}") '
            '(property "Footprint" "Local:Original"))' for ref,value in [('R1','10k'),('R2','22k')])+')')
        config = root / 'settings.json'
        config.write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.','bom_dir':'data'}))
        app = PartSelectionApp(config)
        server = ThreadingHTTPServer(('127.0.0.1',0),make_handler(app))
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            result = subprocess.run(['node','-e',BROWSER],capture_output=True,text=True,encoding='utf-8',timeout=90,
                env={**os.environ,'DESIGN_SYNC_URL':f'http://127.0.0.1:{server.server_port}/'})
            if result.returncode: raise AssertionError(result.stderr)
            return result.stdout
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            app.design_sync.close(); app.search_jobs.close(); app.selection_jobs.close()


if __name__ == '__main__':
    print(run_checks())

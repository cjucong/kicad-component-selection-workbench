"""Public design-sync renderer interactions in offline Chromium, not native CAD QA."""
import os
from pathlib import Path
import subprocess


def test_design_sync_renderer_consent_stages_and_late_responses():
    source = Path(__file__).resolve().parents[1] / 'tools/web/design-sync.js'
    script = r'''
const {chromium}=require('playwright'), assert=require('node:assert/strict'), fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage();
  await page.route('http://workbench.test/',route=>route.fulfill({body:'<html><body></body></html>'}));
  await page.goto('http://workbench.test/');
  await page.evaluate(()=>{
   window.calls=[]; window.mode='rename'; window.phase=0;
   window.setState=()=>{}; window.flushRowEdits=async()=>{};
   window.registerWorkbenchTool=(name,handler)=>{window.handler=handler;};
   window.fetch=async(path,options)=>{
    const body=options?.body ? JSON.parse(options.body) : null;
    calls.push({path,body});
    if(path==='/api/start_design_sync_preview') {
     if(mode==='late') await new Promise(resolve=>window.finishPreview=resolve);
     return {ok:true,json:async()=>({ok:true,job_id:'preflight',done:true,stage:'preview_ready',preview:{ok:true,preview_id:'design-1',refs:['R1'],destination:body.destination,
      schematic_changes:[],pcb_preview:body.destination==='both' ? null : {preview_id:'pcb-1',blocked:false,
       items:[{ref:'R1',kind:mode==='replace'?'replace':'rename',before:'Local:Old',after:'Local:New',
        needs_ack:mode==='replace',max_error_mm:mode==='replace'?.2:0,field_changes:[]}]}}})};
    }
    if(path.includes('start_design_sync')) {
     if(mode==='rejected') return {ok:false,json:async()=>({ok:false,error:'预检后数据已改变，请重新预检'})};
     return {ok:true,json:async()=>({ok:true,job_id:'job-1'})};
    }
    if(path.includes('continue_design_sync')) {phase=1;return {ok:true,json:async()=>({ok:true,job_id:'job-1'})};}
    if(path==='/api/retry_design_navigation') {mode='navigation-done';return {ok:true,json:async()=>({ok:true,job_id:'job-1'})};}
    if(path.includes('design_sync_job')) {
     if(mode==='rejected') return {ok:true,json:async()=>({ok:false,stage:'expired',done:true})};
     if(mode==='disconnect') throw Error('offline');
     if(mode==='navigation'||mode==='navigation-done') return {ok:true,json:async()=>({
      ok:mode==='navigation-done',job_id:'job-1',done:true,stage:mode==='navigation'?'navigation_failed':'complete',
      phase_results:{pcb:{ok:true,saved:true}},message:mode==='navigation'?'设计已同步；定位未完成':'重新打开及定位完成',
      steps:[{stage:'pcb:save_verify',message:'PCB 磁盘核验',status:'succeeded',seconds:1.2},
       {stage:'navigation',message:'定位器件',status:mode==='navigation'?'failed':'succeeded',seconds:.3}]})};
     return {ok:true,json:async()=> phase ? {ok:true,job_id:'job-1',done:true,stage:'complete',
      phase_results:{schematic:{ok:true},pcb:{ok:true}},message:'设计同步完成；PCB 改动尚未保存'} :
      {ok:true,job_id:'job-1',done:true,stage:'awaiting_pcb_confirmation',needs_confirmation:true,
       phase_results:{schematic:{ok:true}},message:'原理图已完成，请核对 PCB 更新后继续',
       pcb_preview:{ok:true,preview_id:'pcb-2',items:[{ref:'R1',kind:'fields',field_changes:[
        {name:'Value',before:'10k',after:'22k'}]}]}}};
    }
    throw Error('unexpected '+path);
   };
  });
  await page.addScriptTag({content:fs.readFileSync(process.env.DESIGN_SYNC_JS,'utf8')});
  await page.evaluate(()=>window.openDesignSync({refs:['R1'],destination:'pcb',bom:'__schematic__'}));
  await page.getByText('封装仅改名',{exact:true}).waitFor();
  assert.equal(await page.locator('[data-design-compare]').count(),0);
  assert.equal(await page.locator('[data-design-geometry]').isVisible(),false);
  assert.equal(await page.locator('[data-design-apply]').isEnabled(),true);
  await page.locator('[data-design-close]').click();
  await page.evaluate(()=>{mode='replace';return window.openDesignSync({refs:['R1'],destination:'pcb'});});
  await page.getByText('更换封装',{exact:true}).waitFor();
  assert.equal(await page.locator('[data-design-apply]').isEnabled(),false);
  await page.locator('[data-design-geometry]').check();
  assert.equal(await page.locator('[data-design-apply]').isEnabled(),false);
  await page.locator('[data-design-movement]').check();
  assert.equal(await page.locator('[data-design-apply]').isEnabled(),true);
  await page.locator('[data-design-alignment]').selectOption('rigid');
  await page.waitForFunction(()=>!document.querySelector('[data-design-geometry]').checked);
  assert.equal(await page.locator('[data-design-apply]').isEnabled(),false);
  await page.locator('[data-design-close]').click();
  await page.evaluate(()=>{mode='rename';return window.openDesignSync({refs:['R1'],destination:'both'});});
  await page.locator('[data-design-apply]').click();
  await page.getByRole('button',{name:'继续同步 PCB',exact:true}).waitFor();
  assert.match(await page.locator('[data-design-phases]').innerText(),/原理图.*完成/s);
  await page.locator('[data-design-apply]').click();
  await page.waitForFunction(()=>document.querySelector('[data-design-status]').textContent.includes('尚未保存'));
  assert.equal(await page.evaluate(()=>calls.filter(x=>x.path==='/api/start_design_sync').length),1);
  assert.equal(await page.evaluate(()=>calls.filter(x=>x.path==='/api/continue_design_sync').length),1);
  await page.locator('[data-design-close]').click();
  await page.evaluate(()=>{mode='late';void window.openDesignSync({refs:['R1'],destination:'pcb'});});
  await page.waitForFunction(()=>typeof finishPreview==='function');
  await page.locator('[data-design-close]').click();
  await page.evaluate(()=>finishPreview());
  await page.waitForTimeout(100);
  assert.equal(await page.locator('[data-design-panel]').count(),0);
  assert(await page.evaluate(()=>calls.some(x=>x.path==='/api/cancel_design_sync_preview' && x.body.job_id==='preflight')));
  await page.evaluate(()=>{mode='rename';return window.openDesignSync({refs:['R1'],destination:'pcb'});});
  await page.evaluate(()=>mode='rejected');
  await page.locator('[data-design-apply]').click();
  await page.getByRole('button',{name:'重新预检',exact:true}).waitFor();
  assert.match(await page.locator('[data-design-status]').innerText(),/数据已改变/);
  assert.equal(await page.locator('[data-design-close]').isEnabled(),true);
  assert.equal(await page.locator('[data-design-apply]').isEnabled(),false);
  await page.locator('[data-design-close]').click();
  await page.evaluate(()=>{mode='rename';return window.openDesignSync({refs:['R1'],destination:'pcb'});});
  await page.evaluate(()=>mode='disconnect');
  await page.locator('[data-design-apply]').click();
  await page.getByRole('button',{name:'查询任务状态',exact:true}).waitFor();
  assert.equal(await page.locator('[data-design-apply]').isEnabled(),false);
  await page.getByRole('button',{name:'查询任务状态',exact:true}).click();
  assert.equal(await page.evaluate(()=>calls.filter(x=>x.path==='/api/start_design_sync').length),3);
  await page.evaluate(()=>mode='navigation');
  await page.getByRole('button',{name:'查询任务状态',exact:true}).click();
  await page.locator('[data-design-navigation]').waitFor();
  assert.match(await page.locator('[data-design-phases]').innerText(),/PCB.*完成/);
  assert.equal(await page.locator('[data-design-steps] [data-step-status=failed]').count(),1);
  if(process.env.DESIGN_SYNC_SCREENSHOTS) {
   await page.addStyleTag({path:require('node:path').join(require('node:path').dirname(process.env.DESIGN_SYNC_JS),'design-sync.css')});
   await page.screenshot({path:require('node:path').join(process.env.DESIGN_SYNC_SCREENSHOTS,'navigation-retry.png')});
  }
  await page.locator('[data-design-navigation]').click();
  await page.waitForFunction(()=>document.querySelector('[data-design-status]').textContent==='重新打开及定位完成');
  assert.equal(await page.locator('[data-design-steps] [data-step-status=failed]').count(),0);
  assert.equal(await page.evaluate(()=>calls.filter(x=>x.path==='/api/start_design_sync').length),3);
  assert.equal(await page.evaluate(()=>calls.filter(x=>x.path==='/api/retry_design_navigation').length),1);
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    env = {**os.environ, 'DESIGN_SYNC_JS':str(source)}
    result = subprocess.run(['node','-e',script],env=env,capture_output=True,text=True,timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


def test_workbench_scope_menu_and_native_tool_route():
    from run_design_sync_smoke import run_checks
    assert '"ok":true' in run_checks()

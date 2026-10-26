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
     return {ok:true,json:async()=>({ok:true,job_id:'preflight',done:true,stage:'preview_ready',preview:{ok:true,preview_id:'design-1',refs:['R1'],destination:body.destination,automatic:mode==='auto',
      blocked:mode==='locked',schematic_changes:[],pcb_preview:body.destination==='both' ? null : {preview_id:'pcb-1',blocked:mode==='locked',
       items:[{ref:'R1',kind:['replace','locked'].includes(mode)?'replace':'rename',before:'Local:Old',after:'Local:New',
        locked:mode==='locked',blocked_reason:mode==='locked'?'R1 已锁定；请在 PCB 解锁后重新预检':'',
        needs_ack:mode==='replace',max_error_mm:mode==='replace'?.2:0,field_changes:[]},
        {ref:'R2',kind:'none',field_changes:[]}]}}})};
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
  assert(!((await page.locator('[data-design-changes]').innerText()).includes('R2')));
  assert.equal(await page.locator('[data-design-compare]').count(),0);
  assert.equal(await page.locator('[data-design-geometry]').isVisible(),false);
  assert.equal(await page.locator('[data-design-apply]').isEnabled(),true);
  await page.locator('[data-design-close]').click();
  await page.evaluate(()=>{mode='locked';return window.openDesignSync({refs:['R1'],destination:'pcb'});});
  await page.getByText('已锁定',{exact:true}).waitFor();
  assert.match(await page.locator('[data-design-status]').innerText(),/锁定.*解锁/);
  assert.equal(await page.locator('[data-design-apply]').isEnabled(),false);
  assert.equal(await page.locator('[data-design-refresh]').isVisible(),true);
  assert.equal(await page.locator('[data-design-compare]').count(),0);
  assert(!((await page.locator('[data-design-changes]').innerText()).includes('中心偏差')));
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
  await page.locator('[data-design-close]').click();
  await page.evaluate(()=>{mode='auto';return window.openDesignSync({refs:['R1'],destination:'design'});});
  await page.waitForFunction(()=>calls.filter(x=>x.path==='/api/start_design_sync').length===4);
  await page.waitForFunction(()=>document.querySelector('[data-design-close]').disabled===false);
  assert.equal(await page.evaluate(()=>calls.filter(x=>x.path==='/api/start_design_sync').length),4);
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    env = {**os.environ, 'DESIGN_SYNC_JS':str(source)}
    result = subprocess.run(['node','-e',script],env=env,capture_output=True,text=True,timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


def test_workbench_scope_menu_and_native_tool_route():
    from run_design_sync_smoke import run_checks
    assert '"ok":true' in run_checks()


def test_design_retry_automatically_resumes_once_and_keeps_managed_alignment():
    source = Path(__file__).resolve().parents[1] / 'tools/web/design-sync.js'
    script = r'''
const {chromium}=require('playwright'), assert=require('node:assert/strict'), fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage();
  await page.route('http://workbench.test/',r=>r.fulfill({body:'<html><body></body></html>'}));
  await page.goto('http://workbench.test/');
  await page.evaluate(()=>{
   window.calls=[];window.continued=false;window.preflights=0;window.risk=false;window.switching=false;
   window.flushRowEdits=async()=>{};
   window.fetch=async(path,options)=>{
    const body=options?.body?JSON.parse(options.body):null; calls.push({path,body});
    const pcb={preview_id:'pcb-'+preflights,blocked:false,items:[{ref:'R1',kind:'replace',
      before:'Local:Old',after:'Local:New',needs_ack:true,selection_verified:true,
      manual_adaptation:true,alignment_source:'management',max_error_mm:.2,field_changes:[],
      ...(risk?{dropped_terminals:[{number:'9',net_assigned:true,same_net_objects:{segment:0,arc:0,via:0,zone:0}}]}:{})}]};
    let value;
    if(path==='/api/start_design_sync_preview') {
     ++preflights;
     value={ok:true,job_id:'preflight-'+preflights,done:true,preview:{ok:true,preview_id:'design-'+preflights,
      destination:body.destination,automatic:preflights===1,pcb_automatic:!risk,blocked:false,
      schematic_changes:[],pcb_preview:preflights===1?null:pcb}};
     if(switching && preflights===2) Object.assign(value.preview,{blocked:true,pcb_automatic:false,
       suggested_destination:'both',pcb_preview:null,schematic_changes:[{ref:'R1',name:'value',before:'10k',after:'22k'}]});
     if(switching && preflights>=3) value.preview.automatic=false;
    } else if(path==='/api/start_design_sync') value={ok:true,job_id:'job'};
    else if(path==='/api/continue_design_sync') {continued=true;value={ok:true,job_id:'job'};}
    else if(path.startsWith('/api/design_sync_job')) value=continued?
      {ok:true,job_id:'job',done:true,stage:'complete',phase_results:{schematic:{ok:true},pcb:{ok:true,saved:true}},message:'同步完成'}:
      {ok:false,job_id:'job',done:true,stage:'partial',phase_results:{schematic:{ok:true}},message:'PCB 连接中断，请重新预检'};
    else throw Error('unexpected '+path);
    return {ok:true,json:async()=>value};
   };
  });
  await page.addScriptTag({content:fs.readFileSync(process.env.DESIGN_SYNC_JS,'utf8')});
  await page.evaluate(()=>window.openDesignSync({refs:['R1'],destination:'design'}));
  await page.getByRole('button',{name:'重新预检 PCB',exact:true}).waitFor();
  await page.getByRole('button',{name:'重新预检 PCB',exact:true}).click();
  await page.waitForFunction(()=>calls.some(c=>c.path==='/api/continue_design_sync'),{},{timeout:3000});
  await page.waitForFunction(()=>document.querySelector('[data-design-status]').textContent==='同步完成');
  assert.equal(await page.locator('[data-design-alignment]').isVisible(),false);
  assert.equal(await page.evaluate(()=>calls.filter(c=>c.path==='/api/start_design_sync').length),1);
  assert.equal(await page.evaluate(()=>calls.filter(c=>c.path==='/api/continue_design_sync').length),1);
  await page.locator('[data-design-close]').click();
  await page.evaluate(()=>{continued=false;preflights=0;risk=true;return window.openDesignSync({refs:['R1'],destination:'design'});});
  await page.getByRole('button',{name:'重新预检 PCB',exact:true}).click();
  await page.locator('[data-design-drops]').waitFor();
  assert.equal(await page.locator('[data-design-apply]').isEnabled(),false);
  assert.equal(await page.evaluate(()=>calls.filter(c=>c.path==='/api/continue_design_sync').length),1);
  await page.locator('[data-design-close]').click();
  await page.evaluate(()=>{continued=false;preflights=0;risk=false;switching=true;return window.openDesignSync({refs:['R1'],destination:'design'});});
  await page.getByRole('button',{name:'重新预检 PCB',exact:true}).click();
  await page.getByRole('button',{name:'改为全部同步',exact:true}).click();
  await page.waitForFunction(()=>preflights===3);
  assert.equal(await page.evaluate(()=>calls.filter(c=>c.path==='/api/start_design_sync_preview').at(-1).body.destination),'design');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run(['node', '-e', script], env={**os.environ, 'DESIGN_SYNC_JS': str(source)},
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


def test_scope_is_not_the_write_count_and_partial_job_keeps_both_phase_counts():
    source = Path(__file__).resolve().parents[1] / 'tools/web/design-sync.js'
    script = r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage();
  await page.route('http://workbench.test/',r=>r.fulfill({body:'<html><body></body></html>'}));
  await page.goto('http://workbench.test/');
  await page.evaluate(()=>{
   window.refreshes=0;window.flushRowEdits=async()=>{};
   addEventListener('workbench-design-synced',()=>refreshes++);
   window.fetch=async(path,options)=>{
    const body=options?.body?JSON.parse(options.body):null;
    const changes=[{ref:'U71',name:'footprint',before:'Local:Old',after:'Local:New',kind:'rename'},
      {ref:'U11',name:'footprint',before:'Local:Old2',after:'Local:New2',kind:'replace'}];
    let value;
    if(path==='/api/start_design_sync_preview') value={ok:true,job_id:'preview',done:true,preview:{ok:true,
      preview_id:'design',destination:body.destination,automatic:false,schematic_changes:changes,pcb_preview:null}};
    else if(path==='/api/start_design_sync') value={ok:true,job_id:'sync'};
    else if(path.startsWith('/api/design_sync_job')) value={ok:false,job_id:'sync',done:true,stage:'partial',
      message:'PCB staged rejection',phase_results:{schematic:{ok:true},pcb:{ok:false}},schematic_changes:changes,
      pcb_preview:{ok:true,preview_id:'pcb',items:[
       ...Array.from({length:66},(_,i)=>({ref:'C'+i,kind:'fields',field_changes:[{name:'Value',before:'1',after:'2'}]})),
       ...Array.from({length:36},(_,i)=>({ref:'Q'+i,kind:'rename',before:'Local:Old',after:'Local:New'})),
       ...Array.from({length:11},(_,i)=>({ref:'U'+i,kind:'replace',before:'Local:A',after:'Local:B'})),
       ...Array.from({length:1133},(_,i)=>({ref:'R'+i,kind:'none'}))]}};
    else throw Error(path);
    return {ok:true,json:async()=>value};
   };
  });
  await page.addScriptTag({content:fs.readFileSync(process.env.DESIGN_SYNC_JS,'utf8')});
  await page.evaluate(()=>window.openDesignSync({refs:Array.from({length:1246},(_,i)=>'R'+i),destination:'design'}));
  assert.match(await page.locator('[data-design-scope]').innerText(),/核对范围.*1246/);
  assert.match(await page.locator('[data-design-counts]').innerText(),/原理图.*2.*PCB.*未核验/);
  await page.locator('[data-design-apply]').click();
  await page.waitForFunction(()=>document.querySelector('[data-design-status]').textContent==='PCB staged rejection');
  const counts=await page.locator('[data-design-counts]').innerText();
  assert.match(counts,/原理图.*2.*已完成/);
  for(const expected of [/PCB.*113/,/字段更新 66/,/封装仅改名 36/,/更换封装 11/,/无需更新 1133/]) assert.match(counts,expected);
  assert.equal(await page.locator('[data-design-changes] tbody tr').count(),115);
  assert.equal(await page.evaluate(()=>refreshes),1,'Completed schematic must refresh the main table even after PCB failure');
  assert.equal(await page.locator('[data-design-apply]').isEnabled(),false);
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run(['node','-e',script],env={**os.environ,'DESIGN_SYNC_JS':str(source)},
                            capture_output=True,text=True,timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr

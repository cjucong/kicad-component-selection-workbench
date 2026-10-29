"""A verified success closes itself; active/error sync stays observable."""
import os
from pathlib import Path
import subprocess


def test_compact_native_progress_and_verified_auto_close():
    root=Path(__file__).resolve().parents[1]
    script=r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:800,height:600}});
  await page.route('http://workbench.test/**',r=>r.fulfill({body:'<html><body></body></html>'}));
  await page.goto('http://workbench.test/?tool=design-sync');
  await page.evaluate(()=>{
   window.finished=false;window.fail=false;window.unsaved=false;window.closeFails=false;window.offline=false;window.closes=0;window.notifications=0;
   window.flushRowEdits=async()=>{};window.closeNativeTool=async()=>{closes++;return closeFails?{ok:false,error:'close blocked'}:{ok:true};};
   window.notifyNativeToolChanged=async()=>{notifications++;};
   window.fetch=async path=>{if(offline && path.includes('design_sync_job')) throw new Error('offline');return {ok:true,json:async()=>path.includes('start_design_sync_preview')?
    {ok:true,done:true,job_id:'preflight',preview:{ok:true,preview_id:'preview',automatic:true,schematic_changes:[]}}:
    path.includes('start_design_sync')?{ok:true,job_id:'job'}:
    path.includes('cancel_design_sync_preview')?{ok:true}:
    {ok:!fail,job_id:'job',done:finished,stage:finished?(fail?'partial':'complete'):'pcb:readback',
     message:finished?(fail?'回读未通过':'已保存核验'):'正在回读核验',
     phase_results:{schematic:{ok:true},...(finished&&!fail?{pcb:{ok:true,saved:!unsaved}}:{})},
     steps:[{message:'旧步骤',status:'succeeded',seconds:1},{message:'正在回读核验',status:'running',seconds:1}]}};};
  });
  await page.addStyleTag({path:process.env.SYNC_CSS});
  await page.addScriptTag({content:fs.readFileSync(process.env.SYNC_JS,'utf8')});
  await page.evaluate(()=>openDesignSync({refs:['R1'],destination:'design'}));
  await page.locator('[data-design-status]').filter({hasText:'正在回读核验'}).waitFor();
  assert.equal(await page.locator('[data-design-steps] li').count(),0,'history must not be rendered');
  assert(await page.locator('[data-design-bar]').isVisible());
  assert.equal(await page.locator('[data-design-bar]').getAttribute('value'),null,'no invented percentage');
  assert.equal(await page.locator('[data-design-changes]').isVisible(),false);
  if(process.env.COMPACT_SYNC_SCREENSHOT) await page.screenshot({path:process.env.COMPACT_SYNC_SCREENSHOT});
  await page.evaluate(()=>offline=true);
  await page.locator('[data-design-status]').filter({hasText:'连接中断'}).waitFor();
  assert(await page.locator('[data-design-query]').isVisible(),'uncertain result needs a visible recovery action');
  assert(await page.locator('[data-design-apply]').isDisabled(),'do not submit a second write');
  assert(await page.locator('[data-design-close]').isDisabled(),'unknown write result remains protected');
  await page.evaluate(()=>offline=false);
  await page.locator('[data-design-query]').click();
  await page.locator('[data-design-status]').filter({hasText:'正在回读核验'}).waitFor();
  assert.equal(await page.locator('[data-design-changes]').isVisible(),false);
  await page.evaluate(()=>finished=true);
  await page.waitForFunction(()=>closes===1 && !document.querySelector('[data-design-panel]'));
  assert(await page.evaluate(()=>notifications>0));
  await page.evaluate(()=>{finished=false;fail=true;return openDesignSync({refs:['R1'],destination:'design'});});
  await page.locator('[data-design-status]').filter({hasText:'正在回读核验'}).waitFor();
  await page.evaluate(()=>finished=true);
  await page.getByText('回读未通过',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>closes),1,'failure must not silently close');
  assert(await page.locator('[data-design-panel]').isVisible());
  await page.evaluate(()=>{finished=false;fail=false;unsaved=true;return openDesignSync({refs:['R1'],destination:'design'});});
  await page.locator('[data-design-status]').filter({hasText:'正在回读核验'}).waitFor();
  await page.evaluate(()=>finished=true);
  await page.getByText('已保存核验',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>closes),1,'missing save evidence must not silently close');
  assert(await page.locator('[data-design-panel]').isVisible());
  await page.evaluate(()=>{finished=false;unsaved=false;closeFails=true;return openDesignSync({refs:['R1'],destination:'design'});});
  await page.locator('[data-design-status]').filter({hasText:'正在回读核验'}).waitFor();
  await page.evaluate(()=>finished=true);
  await page.getByText('同步已完成；窗口关闭未完成：close blocked',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>closes),2);
  assert(await page.locator('[data-design-panel]').isVisible(),'close failure must not leave a blank native window');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run(['node','-e',script],env={**os.environ,
        'SYNC_JS':str(root/'tools/web/design-sync.js'),'SYNC_CSS':str(root/'tools/web/design-sync.css')},
        capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert result.returncode==0,result.stdout+result.stderr

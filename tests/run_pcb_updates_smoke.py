"""Offline Chromium contract for PCB preflight, cancellation and one-shot updates."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]

BROWSER = r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.setContent('<button id="updateSelectedPcb">PCB</button>');
  await page.addStyleTag({path:process.env.PCB_CSS});
  await page.evaluate(()=>{
   window.bulkSyncRunning=false;window.loadedRowsBom='__schematic__';window.refs=[];window.calls=[];
   window.flushRowEdits=async()=>{};window.selectedSyncRefs=()=>refs;window.setState=()=>{};
   window.sample={ok:true,preview_id:'token',items:[{ref:'R1',before:'Local:SMD-2',after:'Local:1206',
    old_position_mm:[80,90],position_mm:[81,92],old_rotation_deg:37,rotation_deg:127,back:false,
    max_error_mm:.000001,differences:[{number:'1',fields:['size_mm']}]}]};
   window.fetch=async(url,options)=>{
    calls.push({url,body:options?.body?JSON.parse(options.body):null});
    if(url==='/api/pcb_update_preview') {
     if(window.hold) await new Promise(resolve=>window.release=resolve);
     return {ok:true,json:async()=>sample};
    }
    if(url==='/api/start_pcb_update')return {ok:true,json:async()=>({ok:true,job_id:'job'})};
    return {ok:true,json:async()=>({ok:true,done:true,result:{ok:true,saved:false}})};
   };
  });
  await page.addScriptTag({path:process.env.PCB_JS});
  await page.getByRole('button',{name:'PCB',exact:true}).click();
  await page.getByText('请先勾选需要更新 PCB 的器件行',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>calls.length),0);
  await page.getByRole('button',{name:'取消',exact:true}).click();
  await page.evaluate(()=>{window.refs=['R1'];window.hold=true;});
  await page.getByRole('button',{name:'PCB',exact:true}).click();
  await page.getByRole('dialog').waitFor();
  await page.getByText('正在读取当前 PCB 并预检…',{exact:true}).waitFor();
  await page.getByRole('button',{name:'取消',exact:true}).click();
  await page.evaluate(()=>{window.hold=false;window.release();});
  await page.waitForTimeout(50);
  assert.equal(await page.getByRole('dialog').count(),0);
  assert.equal(await page.evaluate(()=>window.pcbUpdateBusy),false);
  await page.getByRole('button',{name:'PCB',exact:true}).click();
  await page.getByText('已预检 1 个器件',{exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:'更新 PCB',exact:true}).isDisabled(),true);
  if(process.env.PCB_SCREENSHOT)await page.screenshot({path:process.env.PCB_SCREENSHOT+'.desktop.png'});
  await page.setViewportSize({width:390,height:850});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  if(process.env.PCB_SCREENSHOT)await page.screenshot({path:process.env.PCB_SCREENSHOT+'.mobile.png'});
  await page.getByRole('checkbox').check();
  await page.getByRole('button',{name:'更新 PCB',exact:true}).click();
  await page.getByText('已更新到 PCB 编辑器，尚未保存。',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>calls.filter(c=>c.url==='/api/start_pcb_update').length),1);
  assert.deepEqual(await page.evaluate(()=>calls.filter(c=>c.url==='/api/pcb_update_preview').at(-1).body.refs),'R1');
  await page.getByRole('button',{name:'关闭',exact:true}).last().click();
  assert.equal(await page.evaluate(()=>window.pcbUpdateBusy),false);
  assert.deepEqual(errors,[]);
  process.stdout.write(JSON.stringify({ok:true,checks:10}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


def run_checks():
    env = dict(os.environ, PCB_JS=str(ROOT / 'tools/web/pcb-updates.js'),
               PCB_CSS=str(ROOT / 'tools/web/pcb-updates.css'))
    done = subprocess.run(['node', '-e', BROWSER], env=env, capture_output=True,
                          text=True, encoding='utf-8', timeout=45)
    assert done.returncode == 0, done.stderr
    return done.stdout


if __name__ == '__main__':
    print(run_checks())

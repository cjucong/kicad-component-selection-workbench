"""Real Chromium checks for renderer request isolation and dirty-window lifecycle."""
import os
from pathlib import Path
import subprocess


def test_tool_renderer_keeps_state_rejects_cross_host_and_retains_dirty_form():
    script = r'''
const {chromium}=require('playwright'), assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://tools.test/**',route=>route.fulfill({contentType:'text/html',body:'<main id="toolMount"></main>'}));
  await page.goto('http://tools.test/?view=tool&tool=footprint');
  await page.evaluate(()=>{window.bridgeCalls=[];window.pywebview={api:{tool_window:async(...args)=>{
   bridgeCalls.push(args);return {ok:true};}}};});
  await page.addScriptTag({path:process.env.TOOL_JS});
  const result=await page.evaluate(async()=>{
   let mounts=0;window.dirty=false;
   registerWorkbenchTool('footprint',{
    mount(payload){mounts++;document.querySelector('#toolMount').innerHTML='<input value="'+payload.refs+'">';},
    prepareExit(){return !dirty;},cancelExit(){}
   });
   const first={kind:'footprint',request_id:'one',payload:{refs:'R1'},host:'owner',project:'one-project'};
   const opened=receiveNativeToolRequest(first);
   document.querySelector('input').value='draft';
   const reused=receiveNativeToolRequest(first);
   dirty=true;
   const replaced=receiveNativeToolRequest({...first,request_id:'two',payload:{refs:'R2'}});
   const cross=receiveNativeToolRequest({...first,request_id:'three',host:'another-owner'});
   const close=prepareNativeToolExit();
   const value=document.querySelector('input').value;
   dirty=false;
   const prepared=prepareNativeToolExit();cancelNativeToolExit();
   return {opened,reused,replaced,cross,close,value,prepared,inert:document.body.inert,mounts};
  });
  assert.equal(result.opened.ok,true);assert.equal(result.reused.ok,true);
  assert.equal(result.replaced.ok,false);assert.equal(result.cross.ok,false);
  assert.equal(result.close,false);assert.equal(result.value,'draft');assert.equal(result.mounts,1);
  assert.equal(result.prepared,true);assert.equal(result.inert,false);assert.deepEqual(errors,[]);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    env = dict(os.environ, TOOL_JS=str(Path(__file__).resolve().parents[1] / 'tools/web/tool-desktop.js'))
    result = subprocess.run(['node','-e',script],env=env,capture_output=True,text=True,timeout=40)
    assert result.returncode == 0, result.stderr


def test_candidate_uses_restricted_tool_bridge_for_child_actions():
    script = r'''
const {chromium}=require('playwright'), assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage();
  await page.route('http://tools.test/**',route=>route.fulfill({contentType:'text/html',body:'<main></main>'}));
  await page.goto('http://tools.test/?view=candidate');
  await page.evaluate(()=>{
   window.candidateOnly=true;window.calls=[];
   window.candidateRows=document.createElement('tbody');
   window.candidateTargetRow=null;window.candidateSearchProgress=document.createElement('p');
   window.pywebview={api:{tool_window:async(...args)=>{calls.push(args);return {ok:true};}}};
  });
  await page.addScriptTag({path:process.env.CANDIDATE_JS});
  await page.addScriptTag({path:process.env.TOOL_JS});
  await page.evaluate(async()=>{await notifyNativeCandidateSaved();closeNativeCandidates();});
  await page.waitForFunction(()=>calls.some(c=>c[0]==='close'));
  assert.deepEqual(await page.evaluate(()=>calls.map(c=>c[0])),['ready','changed','close']);
  assert.equal(await page.evaluate(()=>candidateSearchProgress.textContent),'');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    base = Path(__file__).resolve().parents[1] / 'tools/web'
    env = dict(os.environ, TOOL_JS=str(base / 'tool-desktop.js'),
               CANDIDATE_JS=str(base / 'candidate-desktop.js'))
    result = subprocess.run(['node','-e',script],env=env,capture_output=True,text=True,timeout=40)
    assert result.returncode == 0, result.stderr

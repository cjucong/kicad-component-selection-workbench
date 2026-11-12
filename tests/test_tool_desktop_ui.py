"""Real Chromium checks for renderer request isolation and dirty-window lifecycle."""
import os
from pathlib import Path
import subprocess
import pytest


@pytest.mark.parametrize("kind", ["settings", "inventory", "field-mapping", "list-view",
    "value-parameters", "undo", "footprint", "plugin-update", "design-sync", "candidate"])
def test_escape_uses_guarded_close_and_respects_inner_popup_and_ime(kind):
    script = r'''
const {chromium}=require("playwright"),assert=require("node:assert/strict");
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage(),kind=process.env.TOOL_KIND;
  await page.route("http://tools.test/**",r=>r.fulfill({contentType:"text/html",body:"<main><input></main>"}));
  await page.goto("http://tools.test/?view="+(kind==="candidate"?"candidate":"tool&tool="+kind));
  await page.evaluate(()=>{
   window.dirty=false;window.closes=0;window.failures=0;
   window.receiveNativeCandidateRequest=()=>({ok:true});
   window.prepareNativeCandidateExit=()=>!dirty;
   window.pywebview={api:{tool_window:async name=>{
    if(name==="close") {if(!prepareNativeToolExit()) return {ok:false,error:"unsaved"};closes++;cancelNativeToolExit();}
    return {ok:true};
   }}};
   window.addEventListener("workbench-tool-error",()=>failures++);
  });
  await page.addScriptTag({path:process.env.TOOL_JS});
  await page.evaluate(kind=>{
   registerWorkbenchTool(kind,{mount(){},prepareExit:()=>!dirty,cancelExit(){}});
   assertOpened=receiveNativeToolRequest({kind,request_id:"one",payload:{},host:"fixture",project:"fixture"});
  },kind);
  await page.keyboard.press("Escape");
  await page.waitForFunction(()=>closes===1);
  await page.evaluate(()=>dirty=true);
  await page.keyboard.press("Escape");
  await page.waitForFunction(()=>failures===1);
  assert.equal(await page.evaluate(()=>closes),1);
  await page.evaluate(()=>{dirty=false;document.querySelector("input").value="retained";
   window.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",isComposing:true,bubbles:true,cancelable:true}));
   document.querySelector("input").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",keyCode:229,bubbles:true,cancelable:true}));
  });
  assert.equal(await page.evaluate(()=>closes),1);
  await page.evaluate(()=>{const d=document.createElement("dialog");d.innerHTML="<input>";
   document.body.append(d);d.showModal();});
  await page.keyboard.press("Escape");
  await page.waitForFunction(()=>!document.querySelector("dialog").open);
  assert.equal(await page.evaluate(()=>closes),1,"Esc closed both the dialog and its owner");
  await page.evaluate(()=>document.querySelector("input").addEventListener("keydown",e=>{
   if(e.key==="Escape")e.preventDefault();},{once:true}));
  await page.locator("main input").focus();await page.keyboard.press("Escape");
  assert.equal(await page.evaluate(()=>closes),1);
  assert.equal(await page.locator("main input").inputValue(),"retained");
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    env = dict(os.environ, TOOL_JS=str(Path(__file__).resolve().parents[1] / "tools/web/tool-desktop.js"),
               TOOL_KIND=kind)
    result = subprocess.run(["node", "-e", script], env=env, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr


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


def test_candidate_parameter_retry_releases_exit_guard(monkeypatch):
    import run_candidate_search_smoke as fixture

    script = r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage(),errors=[];
  page.setDefaultTimeout(5000);
  page.on('pageerror',error=>errors.push(error.message));
  let failDetails=true,saveRequests=0,releaseSave;
  const saved=new Promise(resolve=>releaseSave=resolve);
  const item={c_code:'C101',product_id:'321',model:'FIXTURE',package:'0603',stock:'100'};
  await page.route('**/api/lcsc_search?*',route=>route.fulfill({json:{
   ok:true,done:true,revision:1,items:[item],sources:{domestic:'ready'}}}));
  await page.route('**/api/candidate_parameters',route=>route.fulfill({json:failDetails
   ? {ok:false,status:'failed',error:'offline fixture'}
   : {ok:true,...item,parameters:[{name:'工作电压',value:'50V'}],param_detail:'工作电压: 50V'}}));
  await page.route('**/api/select_candidate',async route=>{
   saveRequests++;await saved;await route.fulfill({json:{ok:true}});
  });
  await page.goto(process.env.CANDIDATE_TEST_URL+'/?view=candidate');
  await page.evaluate(()=>{
   window.pywebview={api:{tool_window:async()=>({ok:true})}};
   const result=receiveNativeToolRequest({kind:'candidate',request_id:'one',host:'fixture',project:'fixture',
    payload:{ref:'C1',refs:'C1',bom:'__schematic__',comment:'capacitor',value:'4.7uF',footprint:'Library:0805'}});
   if(!result.ok)throw Error(result.error);
  });
  await page.locator('[data-action="retry-candidate-parameters"]').waitFor();
  failDetails=false;
  await page.locator('[data-action="retry-candidate-parameters"]').click();
  await page.waitForFunction(()=>document.querySelector('#candidateRows').textContent.includes('50V'));
  assert.equal(await page.evaluate(()=>nativeCandidateCanExit()),true,
   'Read-only parameter retry left the candidate window permanently busy');
  assert.equal(await page.evaluate(()=>prepareNativeToolExit(true)),true);
  await page.evaluate(()=>cancelNativeToolExit());
  await page.locator('[data-action="choose-candidate"]').click();
  await page.waitForFunction(()=>candidateTargetRow.dataset.candidateSaving==='1');
  assert.equal(await page.evaluate(()=>prepareNativeToolExit(true)),false,
   'A real selection write must still block shutdown');
  assert.equal(saveRequests,1);
  releaseSave();
  await page.waitForFunction(()=>nativeCandidateCanExit());
  assert.equal(await page.evaluate(()=>prepareNativeToolExit(true)),true);
  assert.deepEqual(errors,[]);
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    monkeypatch.setattr(fixture, 'BROWSER', script)
    fixture.main()

"""Real Chromium update UI audit with a fake native bridge; no GitHub or install."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]

BROWSER = r'''
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const source=process.env.WORKBENCH_SOURCE_ROOT;
const script=fs.readFileSync(path.join(source,'tools/web/plugin-updates.js'),'utf8');
const css=fs.readFileSync(path.join(source,'tools/web/path-settings.css'),'utf8');
(async()=>{
 const browser=await chromium.launch({headless:true});
 const results=[];
 async function check(name,run,initialMode='off'){
  const page=await browser.newPage({viewport:{width:1366,height:768}});
  page.setDefaultTimeout(4000);
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  try {
   await page.setContent('<style>body{font:14px Arial;margin:16px}button,select{font:inherit}'+css+'</style><button id="openPluginUpdates">插件更新…</button>');
   await page.evaluate(()=>{
    window.updateState={ok:true,installed:true,current_version:'0.1.5',latest_version:'9.0.0',
      available:true,ready:false,mode:'off',notes:'<img src=x onerror=alert(1)> release notes',
      last_checked:'2026-10-20T12:00:00+00:00',last_error:''};
    window.calls=[];window.exitCalls=[];
    window.closeToolbarMenus=()=>{};window.setState=()=>{};
    window.requestDesktopExit=async(...args)=>window.exitCalls.push(args);
    window.pywebview={api:{plugin_update:async(action,value)=>{
     window.calls.push([action,value]);
     if(window.holdAction===action) await new Promise(resolve=>window.releaseAction=resolve);
     if(window.failAction===action) return {ok:false,error:'simulated '+action+' failure'};
     if(action==='preferences') window.updateState.mode=value;
     if(action==='prepare') window.updateState.ready=true;
     return {...window.updateState};
    }}};
   });
   await page.evaluate(mode=>window.updateState.mode=mode,initialMode);
   await page.addScriptTag({content:script});
   await page.waitForFunction(()=>!window.pluginUpdateBusy);
   await page.locator('#openPluginUpdates').click();
   await page.waitForFunction(()=>!window.pluginUpdateBusy);
   await run(page);
   assert.deepEqual(errors,[]);
   results.push({name,ok:true});
  }catch(error){results.push({name,ok:false,error:error.message,page_errors:errors});}
  finally{await page.close();}
 }
 try{
  await check('preferences_failure_reverts',async page=>{
   await page.evaluate(()=>window.failAction='preferences');
   await page.locator('#pluginUpdateMode').selectOption('auto');
   await page.waitForFunction(()=>!window.pluginUpdateBusy);
   assert.equal(await page.locator('#pluginUpdateMode').inputValue(),'off');
   assert.match(await page.locator('#pluginUpdateStatus').innerText(),/simulated preferences failure/);
  });
  await check('prepared_update_not_downloaded_again',async page=>{
   await page.locator('#pluginUpdatesDialog footer [data-close-update]').click();
   await page.evaluate(()=>window.updateState.ready=true);
   await page.locator('#openPluginUpdates').click();
   await page.waitForFunction(()=>!window.pluginUpdateBusy);
   page.once('dialog',dialog=>dialog.accept());
   await page.locator('#installPluginUpdate').click();
   await page.waitForFunction(()=>window.exitCalls.length===1);
   assert.equal(await page.evaluate(()=>window.calls.filter(c=>c[0]==='prepare').length),0);
   assert.deepEqual(await page.evaluate(()=>window.exitCalls[0]),[true,true]);
  });
  await check('download_later_progress_and_retry',async page=>{
   assert.equal(await page.locator('#installPluginUpdate').isHidden(),true);
   assert.match(await page.locator('#pluginUpdateChecked').innerText(),/上次成功检查/);
   assert.equal(await page.locator('#pluginReleaseNotes img').count(),0);
   await page.evaluate(()=>{window.holdAction='prepare';window.failAction='prepare';});
   await page.locator('#downloadPluginUpdate').click();
   await page.waitForFunction(()=>typeof window.releaseAction==='function');
   assert.equal(await page.locator('#pluginUpdateProgress').isVisible(),true);
   assert.equal(await page.locator('#checkPluginUpdate').isDisabled(),true);
   await page.locator('#pluginUpdatesDialog footer [data-close-update]').click();
   await page.locator('#openPluginUpdates').click();
   assert.match(await page.locator('#pluginUpdateStatus').innerText(),/下载/);
   await page.evaluate(()=>window.releaseAction());
   await page.waitForFunction(()=>!window.pluginUpdateBusy);
   assert.match(await page.locator('#pluginUpdateStatus').innerText(),/simulated prepare failure/);
   assert.equal(await page.locator('#pluginUpdateProgress').isHidden(),true);
   assert.equal(await page.locator('#downloadPluginUpdate').isEnabled(),true);
   await page.evaluate(()=>{window.holdAction=null;window.failAction=null;});
   await page.locator('#downloadPluginUpdate').click();
   await page.waitForFunction(()=>!window.pluginUpdateBusy);
   assert.equal(await page.locator('#installPluginUpdate').isVisible(),true);
   assert.equal(await page.evaluate(()=>window.exitCalls.length),0);
   page.once('dialog',dialog=>dialog.dismiss());
   await page.locator('#installPluginUpdate').click();
   assert.equal(await page.evaluate(()=>window.exitCalls.length),0);
   await page.evaluate(()=>window.hasUnsavedPathSettings=()=>true);
   await page.locator('#installPluginUpdate').click();
   assert.match(await page.locator('#pluginUpdateStatus').innerText(),/工程设置/);
   assert.equal(await page.evaluate(()=>window.exitCalls.length),0);
   const directory=process.env.PLUGIN_UPDATE_SCREENSHOTS;
   if(directory){
    await page.screenshot({path:directory+'/updates-desktop.png'});
    await page.setViewportSize({width:390,height:844});
    const bounds=await page.locator('#pluginUpdatesDialog').boundingBox();
    assert.ok(bounds.x>=0 && bounds.y>=0 && bounds.x+bounds.width<=390 && bounds.y+bounds.height<=844);
    assert.equal(await page.locator('#pluginUpdatesDialog').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
    await page.screenshot({path:directory+'/updates-narrow.png'});
   }
  });
  await check('source_run_never_claims_latest',async page=>{
   await page.locator('#pluginUpdatesDialog footer [data-close-update]').click();
   await page.evaluate(()=>Object.assign(window.updateState,{installed:false,available:false,latest_version:'',ready:false}));
   await page.locator('#openPluginUpdates').click();
   await page.waitForFunction(()=>!window.pluginUpdateBusy);
   await page.locator('#checkPluginUpdate').click();
   await page.waitForFunction(()=>!window.pluginUpdateBusy);
   assert.match(await page.locator('#pluginUpdateStatus').innerText(),/源码/);
   assert.equal(await page.locator('#downloadPluginUpdate').isHidden(),true);
  });
  await check('automatic_download_never_forces_restart',async page=>{
   assert.equal(await page.locator('#installPluginUpdate').isVisible(),true);
   assert.equal(await page.evaluate(()=>window.calls.filter(c=>c[0]==='prepare').length),1);
   assert.equal(await page.evaluate(()=>window.exitCalls.length),0);
   await page.locator('#pluginUpdatesDialog footer [data-close-update]').click();
   assert.equal(await page.evaluate(()=>window.exitCalls.length),0);
  },'auto');
 }finally{await browser.close();}
 console.log(JSON.stringify(results,null,2));
 if(results.some(result=>!result.ok)) process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def main():
    screenshots = os.environ.get('PLUGIN_UPDATE_SCREENSHOTS')
    if screenshots:
        target = Path(screenshots)
        if not target.is_absolute() or target.resolve().is_relative_to(ROOT):
            raise ValueError('Screenshots require an external absolute directory')
        target.mkdir(parents=True, exist_ok=True)
    flags = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    result = subprocess.run(['node','-e',BROWSER],capture_output=True,text=True,encoding='utf-8',
        env={**os.environ,'WORKBENCH_SOURCE_ROOT':str(ROOT)},timeout=60,**flags)
    print(result.stdout.encode('ascii',errors='backslashreplace').decode())
    if result.returncode:
        raise AssertionError(result.stderr.encode('ascii',errors='backslashreplace').decode())


if __name__ == '__main__':
    main()

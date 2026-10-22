"""Offline Chromium checks for the footprint comparison UI; no CAD or network access."""
import json
import os
from pathlib import Path
import subprocess

from _scratch import temporary_project
from part_selection_runtime import runtime_directory

ROOT = Path(__file__).resolve().parents[1]

BROWSER = r'''
const {chromium} = require('playwright'), assert = require('node:assert/strict');
(async () => {
 const browser = await chromium.launch({headless:true});
 try {
  const page = await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
  page.on('pageerror', e=>errors.push(e.message));
  page.setDefaultTimeout(4000);
  await page.route('**/*', route=>route.fulfill({contentType:'text/html',body:'<!doctype html><html><body><button id="launch">Open</button></body></html>'}));
  await page.goto('http://footprint.test/');
  await page.addStyleTag({path:process.env.FP_CSS});
  await page.addScriptTag({path:process.env.FP_JS});
  await page.evaluate(() => {
   window.calls=[]; window.pending={}; window.aborted=0; window.hold=false; window.bad=false;
   const svg = color=>`<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="500" viewBox="-5 -2.5 10 5"><rect x="-2" y="-1" width="1" height="2" fill="${color}"/><script>window.untrustedRan=true</script></svg>`;
   window.sample = id=>({alignment:{ok:!window.bad,rotation_deg:id==='Local:B'?180:90,offset_mm:[1,2],max_error_mm:0.002,reason:window.bad?'焊盘编号无法配准':''},
    view_box:[-5,-2.5,10,5],layers:[{id:'F.Cu',label:'铜层',default_visible:true},{id:'F.SilkS',label:'丝印',default_visible:false}],
    existing_layers:{'F.Cu':svg('#159b52'),'F.SilkS':svg('#159b52')},converted_layers:{'F.Cu':svg('#d52798'),'F.SilkS':svg('#d52798')},
    origin_mm:[0,0],existing_pad_one:[-1.5,0],converted_pad_one:[-1.5,0],ref:id});
   window.fetch = async (url, options) => {
    const payload=JSON.parse(options.body); calls.push({url,payload});
    options.signal?.addEventListener('abort',()=>window.aborted++);
    if(url==='/api/footprint_comparison') {
     const detail=window.sample(payload.id);
     if(window.hold) return new Promise(resolve=>{pending[payload.id]=()=>resolve({ok:true,json:async()=>({ok:true,detail})});});
     return {ok:true,json:async()=>({ok:true,detail})};
    }
    if(url==='/api/library_footprint') {
     if(window.failMetadata) {window.failMetadata=false;return {ok:false,json:async()=>({ok:false,error:'fixture metadata failure'})};}
     return {ok:true,json:async()=>({ok:true,detail:{id:payload.id,compatible:!!window.libraryCompatible,pads:2,numbers:['1','2'],svg:svg('#159b52')}})};
    }
    throw Error('Unexpected fixture API '+url);
   };
   window.preview={preview_id:'synthetic-preview',c_code:'C123',compatible:true,original_ids:['Local:A','Local:B'],
    suggested_name:'Selected_0603',default_library:'Local',libraries:['Local','Shared','LCSC_C999'],
    converted_info:{pads:2,numbers:['1','2'],pad_size:'1 x 2',pad_envelope:'4 x 2',geometry_hash:'new'},
    comparison_available:true,converted_svg:'',existing:['A','B','Other'].map(name=>({id:'Local:'+name,pads:2,numbers:['1','2'],svg:'',geometry_hash:'old'}))};
   window.openComparison = ()=>{window.result=undefined;window.chooseFootprint(structuredClone(preview)).then(value=>window.result=value);};
   window.openLibrary = kind=>{window.result=undefined;window.chooseLibraryFootprint({...structuredClone(preview),kind,default_id:'Local:A',items:preview.existing.slice(0,2).map(e=>({...e,library:'Local'}))}).then(value=>window.result=value);};
  });
  const check=process.env.FP_CASE || 'all';
  const close=async()=>{await page.locator('[data-fp-close]').click(); await page.waitForFunction(()=>!document.querySelector('.fp-overlay'));};
  if(check==='all'||check==='pane-radios') {
   await page.evaluate(()=>{
    window.libraryCompatible=true;window.result=undefined;
    window.openFootprintManager({load:async kind=>kind==='convert'?structuredClone(preview):{
      ...structuredClone(preview),kind:'library',default_id:'Local:A',items:preview.existing.map(e=>({...e,library:'Local'}))
    }}).then(value=>window.result=value);
   });
   await page.locator('[data-fp-page="convert"] [data-fp-layer-image]').first().waitFor({state:'attached'});
   const lib=page.locator('[data-fp-page="library"]'), converted=page.locator('[data-fp-page="convert"]');
   await lib.locator('[data-fp-ack]').check();
   assert.equal(await lib.locator('.fp-apply').isEnabled(),true,'background pane must not clear library radios');
   await lib.locator('[data-fp-view="side"]').check();
   await page.getByRole('tab',{name:'替换立创封装',exact:true}).click();
   assert.equal(await converted.locator('[data-fp-view="overlay"]').isChecked(),true);
   await converted.locator('[data-fp-view="side"]').check();
   await page.getByRole('button',{name:'返回上一页',exact:true}).click();
   assert.equal(await lib.locator('[data-fp-view="side"]').isChecked(),true);
   await lib.locator('.fp-apply').click();
   await page.waitForFunction(()=>window.result?.source==='existing');
   await page.evaluate(()=>window.libraryCompatible=false);
  }
  if(check==='all'||check==='rename-retry') {
   await page.evaluate(()=>{window.failMetadata=true;calls=[];openLibrary('rename');});
   await page.getByText('fixture metadata failure',{exact:true}).waitFor();
   await page.getByLabel('改名后',{exact:true}).fill('Shared:RetainDraft');
   await page.getByRole('button',{name:'重试',exact:true}).click();
   await page.waitForFunction(()=>!document.querySelector('.fp-apply').disabled);
   assert.equal(await page.getByLabel('改名后',{exact:true}).inputValue(),'Shared:RetainDraft');
   await page.locator('.fp-apply').click();
   await page.waitForFunction(()=>window.result?.name==='RetainDraft');
  }
  if(check==='all'||check==='navigation') {
   await page.evaluate(()=>{
    window.loads=[];window.releaseLibrary=null;
    const lib={...structuredClone(preview),kind:'library',default_id:'Local:A',
      items:preview.existing.map(e=>({...e,library:'Local'}))};
    const rename={...structuredClone(lib),kind:'rename',current_footprints:['Local:A']};
    window.result=undefined;
    window.openFootprintManager({load:async kind=>{
      loads.push(kind);
      if(kind==='library') return new Promise(resolve=>window.releaseLibrary=()=>resolve(lib));
      if(kind==='rename') return rename;
      throw Error('upstream conversion unavailable');
    }}).then(value=>window.result=value);
   });
   assert.equal(await page.getByRole('dialog',{name:'封装管理',exact:true}).count(),1,'shell opens before data');
   await page.getByRole('tab',{name:'库封装改名',exact:true}).click();
   await page.getByLabel('改名后',{exact:true}).waitFor();
   assert.equal(await page.locator('.fp-overlay').count(),1,'navigation must not create a new dialog');
   assert.equal(await page.locator('.fp-page:not([hidden]) input[type=text]').count(),2);
   assert.equal(await page.locator('.fp-page:not([hidden]) select').count(),0,'rename has no catalog');
   await page.getByLabel('改名后',{exact:true}).fill('Shared:KeptGeometry');
   await page.evaluate(()=>releaseLibrary());
   await page.getByRole('button',{name:'返回上一页',exact:true}).click();
   await page.locator('[data-fp-search]').fill('Local:B');
   await page.getByRole('tab',{name:'替换立创封装',exact:true}).click();
   await page.getByText('upstream conversion unavailable',{exact:false}).waitFor();
   await page.getByRole('button',{name:'返回上一页',exact:true}).click();
   assert.equal(await page.locator('[data-fp-search]').inputValue(),'Local:B','search survives navigation');
   await page.getByRole('tab',{name:'库封装改名',exact:true}).click();
   assert.equal(await page.getByLabel('改名后',{exact:true}).inputValue(),'Shared:KeptGeometry');
   assert.equal(await page.evaluate(()=>loads.filter(k=>k==='rename').length),1);
   for(const width of [1440,390]) {
    await page.setViewportSize({width,height:850});
    const bounds=await page.getByRole('dialog',{name:'封装管理',exact:true}).boundingBox();
    assert.ok(bounds.x>=0&&bounds.x+bounds.width<=width+1);
    await page.screenshot({path:process.env.FP_SHOTS+`/footprint-manager-rename-${width}.png`});
   }
   await page.getByRole('button',{name:'应用改名',exact:true}).click();
   await page.waitForFunction(()=>window.result!==undefined);
   assert.equal(await page.evaluate(()=>result.target_library),'Shared');
   assert.equal(await page.evaluate(()=>result.name),'KeptGeometry');
   assert.equal(await page.evaluate(()=>result.source),'rename');
   assert.equal(await page.locator('.fp-overlay').count(),0);
   await page.evaluate(()=>{
    window.result=undefined;window.latePreview=null;
    window.openFootprintManager({load:()=>new Promise(resolve=>window.latePreview=()=>resolve({
      ...structuredClone(preview),kind:'library',default_id:'Local:A',items:preview.existing.map(e=>({...e,library:'Local'}))
    }))}).then(value=>window.result=value);
   });
   await page.getByRole('button',{name:'关闭',exact:true}).click();
   await page.evaluate(()=>latePreview());
   await page.waitForFunction(()=>window.result===null);
   assert.equal(await page.locator('.fp-overlay').count(),0,'late load must not reopen a closed manager');
   await page.evaluate(()=>{
    window.attempt=0;window.result=undefined;
    window.openFootprintManager({initialMode:'rename',load:async()=>{
      if(++attempt===1) throw Error('fixture read failed');
      return {...structuredClone(preview),kind:'rename',default_id:'Local:A',current_footprints:['Local:A']};
    }}).then(value=>window.result=value);
   });
   await page.getByRole('button',{name:'重试',exact:true}).click();
   await page.getByLabel('改名后',{exact:true}).waitFor();
   await page.getByRole('button',{name:'关闭',exact:true}).click();
   assert.equal(await page.evaluate(()=>attempt),2,'failed page is retryable');
   await page.setViewportSize({width:1440,height:1000});
  }
  if(check==='all'||check==='conflict') {
   await page.evaluate(()=>{calls=[];});
   const open=async(changed=false)=>page.evaluate(changed=>{
    window.result=undefined;
    window.resolveFootprintConflict({footprint:'Local:QSOP-16',name:'QSOP-16',token:'bound-token',changed},
      {preview_id:'p1',source:'rename',name:'QSOP-16',collision:'overwrite',collision_token:'old-token'})
      .then(value=>window.result=value);
   },changed);
   for(const width of [1440,390]) {
    await page.setViewportSize({width,height:800});
    await open();
    assert.equal(await page.locator('.fp-apply').isEnabled(),false);
    assert.equal(await page.locator('[data-fp-overwrite-warning]').isVisible(),false);
    await page.locator('[data-fp-new-name]').fill('QSOP-16');
    assert.equal(await page.locator('.fp-apply').isEnabled(),false);
    await page.locator('[data-fp-new-name]').fill('../bad');
    assert.equal(await page.locator('.fp-apply').isEnabled(),false);
    await page.locator('[data-fp-new-name]').fill('QSOP-16_Original');
    assert.equal(await page.locator('.fp-apply').isEnabled(),true);
    const bounds=await page.locator('.fp-dialog').boundingBox();
    assert.ok(bounds.x>=0&&bounds.x+bounds.width<=width+1);
    assert.equal(await page.locator('.fp-body').evaluate(e=>e.scrollWidth<=e.clientWidth+1),true);
    await page.screenshot({path:process.env.FP_SHOTS+`/footprint-conflict-${width}.png`});
    await page.locator('.fp-apply').click();
    await page.waitForFunction(()=>window.result!==undefined);
    const saved=await page.evaluate(()=>window.result);
    assert.equal(saved.name,'QSOP-16_Original');
    assert.equal(saved.collision,undefined);assert.equal(saved.collision_token,undefined);
    await open(true);
    assert.match(await page.locator('[data-fp-conflict-note]').innerText(),/已变化/);
    await page.locator('[name="fp-conflict-mode"][value="overwrite"]').check();
    assert.equal(await page.locator('[data-fp-new-name]').isVisible(),false);
    assert.equal(await page.locator('.fp-apply').isEnabled(),false);
    await page.locator('[data-fp-overwrite-ack]').check();
    await page.locator('.fp-apply').click();
    await page.waitForFunction(()=>window.result!==undefined);
    const overwritten=await page.evaluate(()=>window.result);
    assert.equal(overwritten.name,'QSOP-16');
    assert.equal(overwritten.collision,'overwrite');assert.equal(overwritten.collision_token,'bound-token');
   }
   await open();
   await page.keyboard.press('Escape');
   await page.waitForFunction(()=>window.result===null);
   await open();
   await page.locator('[data-fp-cancel]').click();
   await page.waitForFunction(()=>window.result===null);
   assert.equal(await page.locator('.fp-overlay').count(),0);
   assert.equal(await page.evaluate(()=>calls.length),0,'collision resolution does not call converter or save by itself');
   await page.setViewportSize({width:1440,height:1000});
  }
  if(check==='all'||check==='pcb') {
   await page.evaluate(()=>{
    preview.alignment_ids=['Local:A','Local:Other','PCB:U3'];
    preview.rename_ids=['PCB:U3'];
    preview.default_reference_id='PCB:U3';
    preview.existing.push({id:'PCB:U3',source:'pcb',compatible:true,
      placement:{ref:'U3',footprint:'Local:Old',position_mm:[90.5,96.735],rotation_deg:37,back:true}});
    openComparison();
   });
   await page.waitForFunction(()=>document.querySelector('[data-fp-alignment]')?.textContent.includes('中心已对齐'));
   assert.equal(await page.locator('[data-fp-existing]').inputValue(),'PCB:U3');
   assert.equal(await page.locator('[name="fp-source"][value="existing"]').isEnabled(),false,'PCB snapshots are not registered libraries');
   assert.equal(await page.locator('[value="rename"]').isEnabled(),true,'rename preserves actual PCB geometry');
   assert.match(await page.locator('[data-fp-reference-context]').innerText(),/37°.*背面/);
   await page.locator('[data-fp-ack]').check();
   assert.equal(await page.locator('.fp-apply').isEnabled(),true);
   await page.locator('[data-fp-existing]').selectOption('Local:Other');
   await page.waitForFunction(()=>document.querySelector('[data-fp-alignment]')?.textContent.includes('中心已对齐'));
   assert.equal(await page.locator('[data-fp-ack]').isChecked(),false,'changing recovery reference requires new acknowledgement');
   assert.match(await page.locator('[data-fp-reference-context]').innerText(),/手动库基准/);
   await page.locator('[data-fp-ack]').check();
   assert.equal(await page.locator('.fp-apply').isEnabled(),true,'explicit recovery reference may align converted geometry');
   await page.locator('.fp-apply').click();
   await page.waitForFunction(()=>window.result!==undefined);
   assert.equal(await page.evaluate(()=>window.result.existing_id),'Local:Other');
   await page.evaluate(()=>{delete preview.alignment_ids;delete preview.rename_ids;delete preview.default_reference_id;preview.existing=preview.existing.filter(e=>e.source!=='pcb');});
  }
  if(check==='all'||check==='overlay') {
   await page.evaluate(()=>{calls=[];});
   await page.evaluate(()=>openComparison());
   await page.waitForFunction(()=>document.querySelectorAll('[data-fp-layer-image]').length===4);
   await page.locator('[data-fp-ack]').check();
   assert.equal(await page.locator('.fp-apply').isEnabled(),true);
   assert.equal(await page.getByRole('slider',{name:'立创不透明度'}).count(),1);
   assert.equal(await page.evaluate(()=>calls.filter(e=>e.url==='/api/footprint_comparison').length),1);
   assert.equal(await page.evaluate(()=>window.untrustedRan),undefined,'SVG must never execute');
   assert.equal(await page.locator('.fp-comparison svg').count(),0,'upstream SVG may only be an img');
   const dimensions=await page.locator('[data-fp-layer-image]').evaluateAll(images=>images.filter(e=>e.getClientRects().length).map(e=>({src:e.src,w:e.getBoundingClientRect().width,h:e.getBoundingClientRect().height})));
   assert.ok(dimensions.every(d=>d.src.startsWith('blob:')));
   assert.ok(dimensions.every(d=>Math.abs(d.w/d.h-2)<0.01));
   assert.equal(dimensions.length,2,'only default-visible layers are shown');
   assert.equal(dimensions[0].w,dimensions[1].w);
   await page.locator('[data-fp-view="side"]').check();
   const side=await page.locator('[data-fp-stage]').evaluateAll(es=>es.map(e=>e.getBoundingClientRect().width));
   assert.equal(side[0],side[1]);
   await page.locator('[data-fp-opacity]').fill('27');
   assert.equal(await page.locator('[data-fp-source-layers="converted"]').evaluate(e=>getComputedStyle(e).opacity),'0.27');
   await page.locator('[data-fp-layer="existing"][value="F.Cu"]').uncheck();
   assert.equal(await page.locator('[data-fp-layer-image][data-source="existing"][data-layer="F.Cu"]').isVisible(),false);
   await page.locator('[data-fp-markers]').uncheck();
   assert.equal(await page.locator('[data-fp-origin]').first().isVisible(),false);
   assert.equal(await page.evaluate(()=>calls.filter(e=>e.url==='/api/footprint_comparison').length),1,'display controls must not render again');
   await page.locator('[data-fp-layer="existing"][value="F.Cu"]').check();
   await page.locator('[data-fp-markers]').check();
   await page.locator('[data-fp-view="overlay"]').check();
   await page.locator('[data-fp-opacity]').fill('50');
   await page.waitForFunction(()=>[...document.querySelectorAll('[data-fp-layer-image]')].every(e=>e.complete&&e.naturalWidth>0));
   const pixel=await page.locator('[data-fp-layer-image]').first().evaluate(image=>{const c=document.createElement('canvas');c.width=1000;c.height=500;c.getContext('2d').drawImage(image,0,0,1000,500);return [...c.getContext('2d').getImageData(350,250,1,1).data];});
   assert.equal(pixel[3],255,'native SVG image must contain real geometry');
   for(const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    const bounds=await page.locator('.fp-dialog').boundingBox();
    assert.ok(bounds.x>=0&&bounds.x+bounds.width<=width+1);
    assert.equal(await page.locator('.fp-body').evaluate(e=>e.scrollWidth<=e.clientWidth+1),true,'no horizontal overflow');
    assert.equal(await page.locator('[data-fp-stage]').first().evaluate(e=>Math.abs(e.clientWidth/e.clientHeight-2)<0.03),true);
    await page.screenshot({path:process.env.FP_SHOTS+`/footprint-overlay-${width}.png`});
    await page.locator('[data-fp-view="side"]').check();
    const panes=await page.locator('[data-fp-stage]').evaluateAll(es=>es.map(e=>({w:e.clientWidth,h:e.clientHeight})));
    assert.equal(panes[0].w,panes[1].w);
    assert.ok(panes.every(p=>Math.abs(p.w/p.h-2)<0.03));
    assert.equal(await page.locator('.fp-body').evaluate(e=>e.scrollWidth<=e.clientWidth+1),true);
    await page.screenshot({path:process.env.FP_SHOTS+`/footprint-side-${width}.png`});
    await page.locator('[data-fp-view="overlay"]').check();
   }
   await page.locator('.fp-apply').click();
   await page.waitForFunction(()=>window.result!==undefined);
   assert.equal(await page.evaluate(()=>window.result.source),'converted');
   assert.equal(await page.evaluate(()=>window.result.existing_id),'Local:A');
   await page.setViewportSize({width:1440,height:1000});
  }
  if(check==='all'||check==='rename') {
   await page.evaluate(()=>{bad=true;preview.compatible=false;openComparison();});
   await page.waitForFunction(()=>document.querySelector('[data-fp-alignment]')?.textContent.includes('无法对齐'));
   await page.locator('[data-fp-ack]').check();
   assert.equal(await page.locator('.fp-apply').isEnabled(),false);
   await page.locator('[name="fp-source"][value="rename"]').check();
   assert.equal(await page.locator('.fp-destination').isVisible(),true);
   await page.locator('[data-fp-library]').selectOption('Shared');
   await page.locator('[data-fp-name]').fill('KeptGeometry');
   assert.equal(await page.locator('.fp-apply').isEnabled(),true,'rename is independent of converted compatibility');
   await page.locator('[data-fp-existing]').selectOption('Local:Other');
   assert.equal(await page.locator('[data-fp-ack]').isChecked(),false);
   assert.equal(await page.locator('[value="rename"]').isEnabled(),false,'only original bindings can be renamed');
   await page.locator('[data-fp-existing]').selectOption('Local:A');
   await page.locator('[name="fp-source"][value="rename"]').check();
   await page.locator('[data-fp-ack]').check();
   await page.locator('.fp-apply').click();
   await page.waitForFunction(()=>window.result!==undefined);
   const result=await page.evaluate(()=>window.result);
   assert.equal(result.source,'rename');assert.equal(result.existing_id,'Local:A');
   assert.equal(result.target_library,'Shared');assert.equal(result.name,'KeptGeometry');assert.equal(result.acknowledged,true);
   await page.evaluate(()=>openComparison());
   await page.locator('[name="fp-source"][value="existing"]').check();
   assert.equal(await page.locator('.fp-apply').isEnabled(),true,'existing keeps old safety rules');
   await close();
  }
  if(check==='all'||check==='stale') {
   await page.evaluate(()=>{calls=[];pending={};hold=true;bad=false;preview.compatible=true;
    preview.existing.push({...preview.existing[0],id:'Local:Slow'});preview.original_ids.push('Local:Slow');openComparison();});
   await page.waitForFunction(()=>pending['Local:A']);
   await page.locator('[data-fp-ack]').check();
   assert.equal(await page.locator('.fp-apply').isEnabled(),false,'alignment must finish before conversion');
   await page.locator('[data-fp-existing]').selectOption('Local:B');
   await page.waitForFunction(()=>pending['Local:B']);
   assert.equal(await page.locator('[data-fp-ack]').isChecked(),false);
   await page.evaluate(()=>pending['Local:B']());
   await page.waitForFunction(()=>document.querySelectorAll('[data-fp-layer-image]').length===4);
   await page.evaluate(()=>pending['Local:A']());
   await page.waitForTimeout(50);
   assert.equal(await page.locator('[data-fp-existing]').inputValue(),'Local:B');
   assert.match(await page.locator('[data-fp-alignment]').innerText(),/180°/,'late A must not replace B alignment');
   assert.equal(await page.evaluate(()=>aborted>0),true);
   await page.locator('[data-fp-existing]').selectOption('Local:A');
   await page.waitForTimeout(50);
   assert.equal(await page.evaluate(()=>calls.length),2,'completed late responses may be cached but never replace current selection');
   assert.match(await page.locator('[data-fp-alignment]').innerText(),/90°/);
   await page.locator('[data-fp-existing]').selectOption('Local:Other');
   assert.equal(await page.evaluate(()=>calls.length),2,'unbound similar footprints must not be alignment references');
   await page.locator('[data-fp-existing]').selectOption('Local:Slow');
   await page.waitForFunction(()=>pending['Local:Slow']);
   await close();
   await page.evaluate(()=>pending['Local:Slow']());
   await page.waitForTimeout(30);
   assert.equal(await page.locator('.fp-overlay').count(),0);
   await page.evaluate(()=>{hold=false;preview.existing=preview.existing.filter(e=>e.id!=='Local:Slow');
     preview.original_ids=preview.original_ids.filter(id=>id!=='Local:Slow');});
  }
  if(check==='all'||check==='library') {
   await page.evaluate(()=>{calls=[];openLibrary('rename');});
   await page.waitForFunction(()=>calls.length===1);
   assert.deepEqual(await page.evaluate(()=>calls.map(e=>({url:e.url,render:e.payload.render}))),[{url:'/api/library_footprint',render:false}]);
   assert.equal(await page.locator('select').count(),0,'rename has only before and after fields');
   assert.equal(await page.locator('[data-fp-before]').inputValue(),'Local:A / Local:B');
   await page.getByLabel('改名后',{exact:true}).fill('LCSC_C999:RenameOnly');
   await page.waitForFunction(()=>!document.querySelector('.fp-apply').disabled);
   assert.equal(await page.locator('.fp-apply').isEnabled(),true,'rename needs no conversion or image preview');
   await page.locator('.fp-apply').click();
   await page.waitForFunction(()=>window.result!==undefined);
   assert.deepEqual(await page.evaluate(()=>window.result),{preview_id:'synthetic-preview',source:'rename',existing_id:'Local:A',acknowledged:true,target_mode:'existing',target_library:'LCSC_C999',name:'RenameOnly'});
   await page.evaluate(()=>{calls=[];openLibrary('library');});
   await page.waitForFunction(()=>calls.some(e=>e.url==='/api/library_footprint'));
   await page.locator('[data-fp-ack]').check();
   assert.equal(await page.locator('.fp-apply').isEnabled(),false,'ordinary library selection still blocks incompatible footprints');
   await close();
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({scenario:check,passed:true,page_errors:errors}));
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def run_checks(scenario="all"):
    with temporary_project(prefix="footprint-overlay-") as directory:
        shots = runtime_directory(ROOT) / "footprint-overlay-smoke"
        shots.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "FP_CASE": scenario, "FP_JS": str(ROOT / "tools/web/footprint-compare.js"),
               "FP_CSS": str(ROOT / "tools/web/footprint-compare.css"), "FP_SHOTS": str(shots)}
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        result = subprocess.run(["node", "-e", BROWSER], cwd=directory, env=env, capture_output=True,
                                text=True, encoding="utf-8", timeout=90, **options)
        if result.returncode:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip()), shots


if __name__ == "__main__":
    result, shots = run_checks()
    print(json.dumps(result))
    print(f"Screenshots: {shots}")

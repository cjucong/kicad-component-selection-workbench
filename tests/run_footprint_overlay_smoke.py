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
  await page.evaluate(svg=>document.body.insertAdjacentHTML('afterbegin',svg),require('node:fs').readFileSync(process.env.FP_ICONS,'utf8'));
  await page.addStyleTag({path:process.env.FP_CSS});
  await page.addStyleTag({path:process.env.TOOL_CSS});
  if (process.env.FP_ADJUST_JS) await page.addScriptTag({path:process.env.FP_ADJUST_JS});
  await page.addScriptTag({path:process.env.FP_JS});
  await page.evaluate(() => {
   window.calls=[]; window.pending={}; window.aborted=0; window.hold=false; window.bad=false;
   const svg = color=>`<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="500" viewBox="-5 -2.5 10 5"><rect x="-2" y="-1" width="1" height="2" fill="${color}"/><script>window.untrustedRan=true</script></svg>`;
   window.sample = id=>({alignment:{ok:!window.bad,rotation_deg:id==='Local:B'?180:90,offset_mm:[1,2],max_error_mm:0.002,reason:window.bad?'焊盘编号无法配准':''},
    view_box:[-5,-2.5,10,5],layers:[{id:'F.Cu',label:'铜层',default_visible:true},{id:'F.SilkS',label:'丝印',default_visible:false}],
    existing_layers:{'F.Cu':svg('#159b52'),'F.SilkS':svg('#159b52')},converted_layers:{'F.Cu':svg('#d52798'),'F.SilkS':svg('#d52798')},
    origin_mm:[0,0],existing_pad_one:[-1.5,0],converted_pad_one:[-1.5,0],
    existing_pads:[{number:'1',position_mm:[-1.5,0]},{number:'2',position_mm:[1.5,0]}],
    converted_pads:[{number:'1',position_mm:[-1.5,0]},{number:'2',position_mm:[1.5,0]}],ref:id});
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
  if(check==='all'||check==='library-pcb-reference') {
   await page.evaluate(()=>{
    window.previousFetch=window.fetch;
    window.fetch=async(url,options)=>{
     const payload=JSON.parse(options.body);
     if(url==='/api/footprint_comparison' && payload.id!=='PCB:Q6') {
      calls.push({url,payload});
      return {ok:false,json:async()=>({ok:false,error:'请选择原绑定库封装作为比较基准'})};
     }
     return previousFetch(url,options);
    };
    window.result=undefined; calls=[];
    window.chooseLibraryFootprint({...structuredClone(preview),kind:'library',default_id:'Local:A',
     default_reference_id:'PCB:Q6',baseline_ids:['PCB:Q6','PCB:Q7'],
     items:preview.existing.slice(0,2).map(e=>({...e,library:'Local'}))}).then(value=>window.result=value);
   });
   await page.waitForFunction(()=>calls.some(e=>e.url==='/api/footprint_comparison'));
   assert.equal(await page.evaluate(()=>calls.find(e=>e.url==='/api/footprint_comparison').payload.id),'PCB:Q6',
    'keep the saved PCB instance baseline instead of replacing it with the library default');
   await page.locator('[data-fp-adjust]').waitFor();
   assert.equal(await page.locator('[data-fp-canvas-status]').isVisible(),false,'saved PCB baseline renders the comparison');
   await page.locator('.fp-pad-mapping summary').click();
   await page.locator('[data-fp-mapping-enabled]').check();
   await page.locator('[data-fp-ack]').check();
   await page.locator('.fp-apply').click();
   await page.waitForFunction(()=>window.result!==undefined);
   assert.equal(await page.evaluate(()=>result.reference_id),'PCB:Q6','apply keeps the same baseline as the preview');
   await page.evaluate(()=>{fetch=previousFetch;});
  }
  if(check==='all'||check==='pad-mapping') {
   for(const library of [false,true]) {
    await page.evaluate(library=>{
     window.mappingSample=window.sample;
     window.sample=id=>({...mappingSample(id),alignment:{ok:false,rotation_deg:0,offset_mm:[0,0],reason:'different pad sets'},
      existing_pads:Array.from({length:10},(_,i)=>({number:String(i+1),position_mm:[i-4,0]})),
      converted_pads:Array.from({length:8},(_,i)=>({number:String(i+1),position_mm:[i-4,0]}))});
     preview.compatible=false;library?openLibrary('library'):openComparison();
    },library);
    await page.locator('[data-fp-adjust]').waitFor();
    await page.locator('.fp-pad-mapping summary').click();
    await page.locator('[data-fp-mapping-enabled]').check();
    assert.equal(await page.getByLabel('原端子 9 对应目标焊盘',{exact:true}).inputValue(),'','unmatched originals never silently drop');
    await page.locator('[data-fp-ack]').check();
    assert.equal(await page.locator('.fp-apply').isEnabled(),false);
    for(const n of [9,10])await page.getByLabel(`原端子 ${n} 对应目标焊盘`,{exact:true}).selectOption('__drop__');
    await page.locator('[data-fp-ack]').check();
    assert.equal(await page.locator('.fp-apply').isEnabled(),false,'dropping needs separate consent');
    await page.locator('[data-fp-drop-ack]').check();
    await page.getByLabel('原端子 8 参与对齐',{exact:true}).uncheck();
    await page.locator('[data-fp-drop-ack]').check();
    await page.getByLabel('立创 X 偏移 mm',{exact:true}).fill('0.75');
    if(library){
     await page.locator('[data-fp-name]').fill('MappedCopy');
     const callsBefore=await page.evaluate(()=>calls.length);
     await page.getByLabel('搜索封装名称',{exact:true}).fill('Local:');
     assert.equal(await page.evaluate(()=>calls.length),callsBefore,'filtering without changing target keeps the loaded draft');
     assert.equal(await page.locator('[data-fp-name]').inputValue(),'MappedCopy');
     assert.equal(await page.getByLabel('立创 X 偏移 mm',{exact:true}).inputValue(),'0.75');
     assert.equal(await page.getByLabel('原端子 9 对应目标焊盘',{exact:true}).inputValue(),'__drop__');
     for(const width of [1440,390]) {
      await page.setViewportSize({width,height:1000});
      const destination=await page.locator('.fp-destination').boundingBox();
      const input=await page.locator('[data-fp-name]').boundingBox();
      assert.ok(input.width>=140,'adapted footprint name input must remain usable: '+input.width);
      assert.ok(input.x+input.width<=destination.x+destination.width+1);
     }
     await page.setViewportSize({width:1440,height:1000});
    }
    await page.locator('[data-fp-ack]').check();
    await page.locator('.fp-apply').click();
    await page.waitForFunction(()=>window.result!==undefined);
    const mapped=await page.evaluate(()=>window.result);
    assert.equal(mapped.source,library?'existing':'converted');
    assert.deepEqual(mapped.mapping.dropped,['9','10']);
    assert.equal(mapped.mapping.pairs.find(p=>p.source==='8').align,false,'alignment exclusion still maps electrical identity');
    assert.deepEqual(mapped.alignment_transform.offset_mm,[0.75,0]);
    assert.equal(mapped.dropped_acknowledged,true);
    await page.evaluate(()=>{sample=mappingSample;preview.compatible=true;});
   }
   await page.evaluate(()=>{
    window.mappingSample=window.sample;
    window.sample=id=>({...mappingSample(id),alignment:{ok:false,rotation_deg:0,offset_mm:[0,0],reason:'different pad sets'},
     existing_pads:Array.from({length:8},(_,i)=>({number:String(i+1),position_mm:[i-4,0]})),
     converted_pads:Array.from({length:10},(_,i)=>({number:String(i+1),position_mm:[i-4,0]}))});
    preview.compatible=false;openComparison();
   });
   await page.locator('[data-fp-adjust]').waitFor();
   await page.locator('.fp-pad-mapping summary').click();await page.locator('[data-fp-mapping-enabled]').check();
   assert.match(await page.locator('[data-fp-mapping-status]').innerText(),/9, 10.*未分配网络/);
   await page.locator('[data-fp-ack]').check();assert.equal(await page.locator('.fp-apply').isEnabled(),true);
   await page.getByLabel('原端子 1 对应目标焊盘',{exact:true}).selectOption('2');
   await page.locator('[data-fp-ack]').check();assert.equal(await page.locator('.fp-apply').isEnabled(),false,'many to one rejects');
   await page.getByLabel('原端子 2 对应目标焊盘',{exact:true}).selectOption('1');
   assert.equal(await page.locator('[data-fp-side=converted][data-fp-pad-number="2"]').innerText(),'2→1');
   await page.locator('[data-fp-ack]').check();await page.locator('.fp-apply').click();
   await page.waitForFunction(()=>window.result!==undefined);
   assert.deepEqual(await page.evaluate(()=>result.mapping.pairs[0]),{source:'1',target:'2',align:true});
   await page.evaluate(()=>{sample=mappingSample;preview.compatible=true;});
   await page.setViewportSize({width:800,height:640});
   await page.evaluate(()=>{
    document.documentElement.classList.add('native-utility-page');
    window.openFootprintManager({load:async()=>({...structuredClone(preview),kind:'library',default_id:'Local:A',
      items:preview.existing.slice(0,2).map(e=>({...e,library:'Local'}))})});
   });
   const libraryPane=page.locator('[data-fp-page=library]');
   await libraryPane.locator('.fp-pad-mapping summary').click();
   await libraryPane.locator('[data-fp-mapping-enabled]').check();
   const stage=libraryPane.locator('[data-fp-stage=converted]');
   await stage.scrollIntoViewIfNeeded();
   assert.equal(await stage.evaluate(e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}),true,
    'native library preview remains reachable for dragging below the mapping editor');
   await page.keyboard.press('Escape');await page.waitForFunction(()=>!document.querySelector('.fp-overlay'));
   await page.evaluate(()=>document.documentElement.classList.remove('native-utility-page'));
   await page.setViewportSize({width:1440,height:1000});
  }
  if(check==='all'||check==='native-layout') {
   await page.evaluate(()=>{
    document.documentElement.classList.add('native-utility-page');
    window.openFootprintManager({load:async kind=>({...structuredClone(preview),kind:'rename',current_footprints:['Local:A']})});
   });
   await page.getByLabel('改名后',{exact:true}).waitFor();
   assert.equal(await page.locator('.fp-workflow > .fp-title').isVisible(),false,'native title bar must not have an inner duplicate');
   assert.equal(await page.locator('.fp-history [data-fp-back]').count(),1);
   const tabsBounds=await page.locator('.fp-tabs').boundingBox(), historyBounds=await page.locator('.fp-history').boundingBox();
   assert.ok(historyBounds.y>=tabsBounds.y+tabsBounds.height-1,'history sits below operation tabs');
   assert.ok(historyBounds.height<=32,'history buttons stay compact');
   await page.getByRole('tab',{name:'库封装改名',exact:true}).click();
   await page.getByRole('button',{name:'返回上一页',exact:true}).click();
   await page.getByRole('button',{name:'前进',exact:true}).click();
   const bounds=await page.locator('.fp-workflow').boundingBox();
   assert.equal(bounds.x,0); assert.equal(bounds.y,0);
   assert.equal(bounds.height,1000); assert.equal(bounds.width,1440);
   await page.keyboard.press('Escape');
   await page.waitForFunction(()=>!document.querySelector('.fp-overlay'));
   await page.evaluate(()=>document.documentElement.classList.remove('native-utility-page'));
  }
  if(check==='all'||check==='manual') {
   await page.evaluate(()=>{calls=[];openComparison();});
   await page.locator('[data-fp-adjust]').waitFor();
   assert.equal(await page.locator('[data-fp-pad-number]').count(),4,'all numbered pads are labeled');
   const canvas=page.locator('[data-fp-stage="converted"]');
   const point=await canvas.boundingBox();
   const x=point.x+point.width/2,y=point.y+point.height/2;
   await page.locator('[data-fp-ack]').check();
   await page.mouse.move(x,y);await page.mouse.down();await page.mouse.move(x+point.width/10,y+point.height/5,{steps:5});await page.mouse.up();
   assert.equal(await page.locator('[data-fp-ack]').isChecked(),false,'moving requires fresh review');
   assert.ok(Math.abs(Number(await page.getByLabel('立创 X 偏移 mm',{exact:true}).inputValue())-1)<0.01);
   assert.ok(Math.abs(Number(await page.getByLabel('立创 Y 偏移 mm',{exact:true}).inputValue())-1)<0.01);
   assert.match(await page.locator('[data-fp-source-layers="converted"]').getAttribute('style'),/transform:/);
   await page.getByRole('button',{name:'1 号引脚中心对齐',exact:true}).click();
   assert.ok(Math.abs(Number(await page.getByLabel('立创 X 偏移 mm',{exact:true}).inputValue()))<0.001);
   await page.getByLabel('立创旋转角度',{exact:true}).fill('90');
   assert.equal(await page.getByLabel('立创旋转角度',{exact:true}).inputValue(),'90');
   await page.getByRole('button',{name:'按引脚编号自动定向',exact:true}).click();
   assert.ok(Math.abs(Number(await page.getByLabel('立创旋转角度',{exact:true}).inputValue()))<0.001);
   await page.getByLabel('立创 X 偏移 mm',{exact:true}).fill('2');
   await page.getByLabel('立创 Y 偏移 mm',{exact:true}).fill('1');
   await page.getByRole('button',{name:'封装中心对齐',exact:true}).click();
   assert.ok(Math.abs(Number(await page.getByLabel('立创 X 偏移 mm',{exact:true}).inputValue()))<0.001);
   assert.ok(Math.abs(Number(await page.getByLabel('立创 Y 偏移 mm',{exact:true}).inputValue()))<0.001);
   await page.getByRole('button',{name:'设置旋转中心',exact:true}).click();
   const pivotBounds=await canvas.boundingBox();
   await page.mouse.click(pivotBounds.x+pivotBounds.width*.6,pivotBounds.y+pivotBounds.height/2);
   assert.equal(await page.getByRole('button',{name:'旋转目标封装',exact:true}).getAttribute('aria-pressed'),'true');
   await page.getByLabel('立创旋转角度',{exact:true}).fill('90');
   assert.ok(Math.abs(Number(await page.getByLabel('立创 X 偏移 mm',{exact:true}).inputValue())-1)<0.01,'rotation around a user pivot moves the origin: '+await page.getByLabel('立创 X 偏移 mm',{exact:true}).inputValue());
   assert.ok(Math.abs(Number(await page.getByLabel('立创 Y 偏移 mm',{exact:true}).inputValue())-1)<0.01);
   await page.getByRole('button',{name:'重置手动调整',exact:true}).click();
   await page.getByRole('button',{name:'旋转目标封装',exact:true}).click();
   const rotationBounds=await canvas.boundingBox();
   await page.mouse.move(rotationBounds.x+rotationBounds.width*.6,rotationBounds.y+rotationBounds.height/2);await page.mouse.down();
   await page.mouse.move(rotationBounds.x+rotationBounds.width/2,rotationBounds.y+rotationBounds.height*.7,{steps:8});await page.mouse.up();
   assert.ok(Math.abs(Number(await page.getByLabel('立创旋转角度',{exact:true}).inputValue())+90)<0.1,'canvas rotation uses KiCad counterclockwise degrees');
   await page.getByRole('button',{name:'重置手动调整',exact:true}).click();
   await page.getByLabel('立创 X 偏移 mm',{exact:true}).fill('1.25');
   await page.getByLabel('立创 Y 偏移 mm',{exact:true}).fill('-0.5');
   await page.locator('[data-fp-view="side"]').check();
   assert.equal(await page.getByLabel('立创 X 偏移 mm',{exact:true}).inputValue(),'1.25','view modes retain adjustments');
   await page.getByLabel('引脚编号',{exact:true}).uncheck();
   assert.equal(await page.locator('[data-fp-pad-label-stack]').first().isVisible(),false);
   await page.getByLabel('引脚编号',{exact:true}).check();
   assert.equal(await page.evaluate(()=>calls.filter(c=>c.url==='/api/footprint_comparison').length),1,'manual gestures never re-render over HTTP');
   await page.locator('[data-fp-ack]').check();await page.locator('.fp-apply').click();
   await page.waitForFunction(()=>window.result!==undefined);
   const result=await page.evaluate(()=>result);
   assert.equal(result.alignment_transform.rotation_deg,90);
   assert.deepEqual(result.alignment_transform.offset_mm,[2.25,1.5],'persist the composed raw-to-reference transform');
   assert.equal(result.mapping.pairs.length,2,'compatible converted manual gestures retain durable adaptation evidence');
   await page.evaluate(()=>openComparison());await page.locator('[data-fp-adjust]').waitFor();
   await page.getByRole('button',{name:'设置旋转中心',exact:true}).click();
   const customPivot=await canvas.boundingBox();
   await page.mouse.click(customPivot.x+customPivot.width*.6,customPivot.y+customPivot.height/2);
   const axisPoint=await page.locator('.fp-rotation-axis').evaluate(e=>[parseFloat(e.style.left)/100*10-5,parseFloat(e.style.top)/100*5-2.5]);
   await page.getByLabel('立创旋转角度',{exact:true}).fill('37');
   const radians=37*Math.PI/180,c=Math.cos(radians),s=Math.sin(radians);
   const labelPoint=await page.locator('[data-fp-side=converted][data-fp-pad-number="2"]').evaluate(e=>[parseFloat(e.style.left)/100*10-5,parseFloat(e.style.top)/100*5-2.5]);
   const around=([x,y])=>[c*(x-axisPoint[0])+s*(y-axisPoint[1])+axisPoint[0],-s*(x-axisPoint[0])+c*(y-axisPoint[1])+axisPoint[1]];
   const expectedLabel=around([1.5,0]);
   assert.ok(labelPoint.every((v,i)=>Math.abs(v-expectedLabel[i])<1e-5),'labels follow the custom pivot within CSS percentage serialization precision');
   await page.locator('[data-fp-ack]').check();await page.locator('.fp-apply').click();
   await page.waitForFunction(()=>window.result!==undefined);
   const composed=await page.evaluate(()=>result.alignment_transform);
   assert.ok(Math.abs(composed.rotation_deg-127)<1e-6);
   const expectedOffset=around([1,2]);
   assert.ok(composed.offset_mm.every((v,i)=>Math.abs(v-expectedOffset[i])<1e-6),'nonzero base rotation and custom pivot compose into the published transform');
   await page.evaluate(()=>{
    const sample=window.sample;window.originalSample=sample;
    window.sample=id=>({...sample(id),alignment:{ok:false,rotation_deg:0,offset_mm:[0,0],reason:'Pad number sets do not match exactly'},
      existing_pads:Array.from({length:9},(_,i)=>({number:String(i+1),position_mm:[i-4,0]})),
      converted_pads:Array.from({length:5},(_,i)=>({number:String(i+1),position_mm:[i-2,0]}))});
    preview.compatible=false;preview.alignment_ids=[];openComparison();
   });
   await page.locator('[data-fp-adjust]').waitFor();
   assert.equal(await page.locator('[data-fp-pad-number]').count(),14);
   await page.getByRole('button',{name:'封装中心对齐',exact:true}).click();
   await page.getByRole('button',{name:'按引脚编号自动定向',exact:true}).click();
   await page.getByLabel('立创 X 偏移 mm',{exact:true}).fill('1');
   await page.locator('[data-fp-ack]').check();
   assert.equal(await page.locator('.fp-apply').isEnabled(),false,'manual transforms cannot authorize 5 to 9 pins');
   assert.match(await page.locator('[data-fp-adjust-status]').innerText(),/仅预览.*9.*5/);
   await page.locator('[name=fp-source][value=rename]').check();
   await page.locator('.fp-apply').click();
   await page.waitForFunction(()=>window.result?.source==='rename');
   assert.equal(await page.evaluate(()=>Object.hasOwn(result,'alignment_transform')),false,'rename never includes a geometric adjustment');
   await page.evaluate(()=>{sample=originalSample;preview.compatible=true;delete preview.alignment_ids;});
   for(const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    await page.evaluate(()=>openComparison());await page.locator('[data-fp-adjust]').waitFor();
    await page.waitForFunction(()=>[...document.querySelectorAll('[data-fp-layer-image]')].every(e=>e.complete&&e.naturalWidth>0));
    assert.equal(await page.locator('.fp-body').evaluate(e=>e.scrollWidth<=e.clientWidth+1),true);
    await page.screenshot({path:process.env.FP_SHOTS+`/footprint-manual-${width}.png`});
    await close();
   }
  }
  if(check==='all'||check==='manual-safety') {
   await page.evaluate(()=>openComparison());await page.locator('[data-fp-adjust]').waitFor();
   await page.getByLabel('立创 X 偏移 mm',{exact:true}).fill('2.5');
   assert.match(await page.locator('[data-fp-alignment]').innerText(),/手动配准/);
   await page.getByLabel('立创 X 偏移 mm',{exact:true}).fill('');
   await page.locator('[data-fp-ack]').check();
   assert.equal(await page.locator('.fp-apply').isEnabled(),false,'empty numeric input cannot apply a hidden prior value');
   await page.getByLabel('立创 X 偏移 mm',{exact:true}).fill('1001');
   await page.locator('[data-fp-ack]').check();
   assert.equal(await page.locator('.fp-apply').isEnabled(),false);
   await page.getByLabel('立创 X 偏移 mm',{exact:true}).fill('900');
   await page.getByLabel('立创 Y 偏移 mm',{exact:true}).fill('900');
   const safetyCanvas=page.locator('[data-fp-stage="converted"]'),safetyBounds=await safetyCanvas.boundingBox();
   await page.getByRole('button',{name:'设置旋转中心',exact:true}).click();
   await page.mouse.click(safetyBounds.x+safetyBounds.width/2,safetyBounds.y+safetyBounds.height/2);
   await page.getByLabel('立创旋转角度',{exact:true}).fill('45');
   await page.locator('[data-fp-ack]').check();
   assert.equal(await page.locator('.fp-apply').isEnabled(),false,'derived offset outside bounds cannot close and lose the draft');
   await page.getByRole('button',{name:'重置手动调整',exact:true}).click();
   await page.getByLabel('立创 X 偏移 mm',{exact:true}).fill('1000');
   await page.locator('[data-fp-ack]').check();
   assert.equal(await page.locator('.fp-apply').isEnabled(),false,'composed absolute offset also respects backend bounds');
   await page.getByRole('button',{name:'重置手动调整',exact:true}).click();
   await page.getByRole('button',{name:'拖动目标封装',exact:true}).click();
   await page.mouse.move(safetyBounds.x+safetyBounds.width/2,safetyBounds.y+safetyBounds.height/2);await page.mouse.down();
   await page.mouse.move(safetyBounds.x+safetyBounds.width*.6,safetyBounds.y+safetyBounds.height/2);
   await safetyCanvas.evaluate(e=>e.releasePointerCapture(1));await page.mouse.up();
   assert.equal(await page.getByLabel('立创 X 偏移 mm',{exact:true}).inputValue(),'0','lost pointer capture restores the gesture baseline');
   await page.getByLabel('立创 X 偏移 mm',{exact:true}).fill('1');
   await page.locator('[data-fp-existing]').selectOption('Local:B');
   await page.waitForFunction(()=>document.querySelector('[data-fp-alignment]').textContent.includes('180°'));
   assert.equal(await page.getByLabel('立创 X 偏移 mm',{exact:true}).inputValue(),'0','new baseline resets old draft');
   await page.getByRole('button',{name:'重置手动调整',exact:true}).click();
   await page.locator('[data-fp-ack]').check();await page.locator('.fp-apply').click();
   await page.waitForFunction(()=>window.result!==undefined);
   assert.equal(await page.evaluate(()=>Object.hasOwn(result,'alignment_transform')),false,'reset preserves existing automatic path');
   await page.evaluate(()=>{
    const old=window.sample;window.savedSample=old;
    window.sample=id=>({...old(id),converted_pads:[{number:'2',position_mm:[0,0]},{number:'2',position_mm:[1,0]}]});
    preview.compatible=false;openComparison();
   });
   await page.locator('[data-fp-adjust]').waitFor();
   assert.equal(await page.getByRole('button',{name:'1 号引脚中心对齐',exact:true}).isEnabled(),false);
   assert.equal(await page.getByRole('button',{name:'按引脚编号自动定向',exact:true}).isEnabled(),false,'duplicate numbers are ambiguous');
   await page.getByRole('button',{name:'封装中心对齐',exact:true}).click();
   await page.keyboard.press('Escape');await page.waitForFunction(()=>!document.querySelector('.fp-overlay'));
   await page.evaluate(()=>{sample=savedSample;preview.compatible=true;openComparison();});
   await page.locator('[data-fp-adjust]').waitFor();
   assert.equal(await page.getByLabel('立创 X 偏移 mm',{exact:true}).inputValue(),'0','cancel never retains geometry for a different session');
   await close();
   await page.evaluate(()=>{window.chooseFootprint(structuredClone(preview),{readonly:true,loadComparison:async()=>sample('Local:A')});});
   await page.locator('[data-fp-pad-number]').first().waitFor();
   assert.equal(await page.locator('[data-fp-adjust]').isVisible(),false,'PCB read-only comparison is never editable');
   await close();
  }
  if(check==='all'||check==='incompatible-preview') {
   await page.evaluate(()=>{
    preview.alignment_ids=[]; preview.compatible=false; bad=true;
    preview.default_reference_id='Local:A';
    window.openComparison();
   });
   await page.locator('[data-fp-layer-image]').first().waitFor({state:'attached'});
   assert.equal(await page.locator('.fp-apply').isDisabled(),true);
   assert.equal(await page.locator('[data-fp-canvas-status]').isVisible(),false);
   assert.match(await page.locator('[data-fp-alignment]').innerText(),/仅预览，未配准.*焊盘编号/);
   await page.locator('[data-fp-ack]').check();
   assert.equal(await page.locator('.fp-apply').isDisabled(),true);
   await close();
   await page.evaluate(()=>{ delete preview.alignment_ids;preview.compatible=true;bad=false; });
  }
  if(check==='all'||check==='escape') {
   await page.evaluate(()=>{
    window.outerResult=undefined;window.innerResult=undefined;
    window.openFootprintManager({load:async()=>new Promise(()=>{})}).then(value=>outerResult=value);
    window.resolveFootprintConflict({footprint:'Local:A',name:'A',token:'test'},{}).then(value=>innerResult=value);
   });
   assert.equal(await page.locator('.fp-overlay').count(),2);
   await page.getByLabel('新封装名称').evaluate(el=>el.dispatchEvent(new KeyboardEvent('keydown',
     {key:'Escape',isComposing:true,bubbles:true,cancelable:true})));
   assert.equal(await page.locator('.fp-overlay').count(),2,'IME Escape must not dismiss a footprint popup');
   await page.keyboard.press('Escape');
   await page.waitForFunction(()=>innerResult===null);
   assert.equal(await page.locator('.fp-overlay').count(),1,'Only the top popup closes');
   assert.equal(await page.evaluate(()=>outerResult===undefined),true);
   await page.keyboard.press('Escape');
   await page.waitForFunction(()=>outerResult===null);
   await page.evaluate(()=>{
    const dialog=document.createElement('dialog');document.body.append(dialog);dialog.showModal();
    const mount=document.createElement('div');dialog.append(mount);window.result=undefined;
    chooseFootprint(structuredClone(preview),{mount,readonly:true,loadComparison:async()=>sample('Local:A')})
      .then(value=>result=value);
   });
   await page.keyboard.press('Escape');
   await page.waitForFunction(()=>result===null);
   assert.equal(await page.evaluate(()=>document.querySelector('dialog').open),true,'Comparison returns to its owner');
   await page.evaluate(()=>document.querySelector('dialog').remove());
  }
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
   assert.equal(await page.locator('.fp-layer-stack svg').count(),0,'upstream SVG may only be an img');
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
   await page.waitForFunction(()=>document.querySelector('[data-fp-alignment]')?.textContent.includes('仅预览，未配准'));
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
   await page.waitForFunction(()=>pending['Local:Other']);
   assert.equal(await page.evaluate(()=>calls.length),3,'unbound footprints remain inspectable without granting alignment permission');
   await page.evaluate(()=>pending['Local:Other']());
   await page.waitForFunction(()=>document.querySelectorAll('[data-fp-layer-image]').length===4);
   await page.locator('[data-fp-ack]').check();
   assert.equal(await page.locator('.fp-apply').isEnabled(),false,'preview must not authorize an unbound alignment reference');
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
               "FP_ICONS": str(ROOT / "tools/web/toolbar-icons.svg"),
               "FP_ADJUST_JS": str(ROOT / "tools/web/footprint-adjust.js"),
               "TOOL_CSS": str(ROOT / "tools/web/tool-pages.css"),
               "FP_CSS": str(ROOT / "tools/web/footprint-compare.css"), "FP_SHOTS": str(shots)}
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        result = subprocess.run(["node", "-"], input=BROWSER, cwd=directory, env=env, capture_output=True,
                                text=True, encoding="utf-8", timeout=90, **options)
        if result.returncode:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip()), shots


if __name__ == "__main__":
    result, shots = run_checks()
    print(json.dumps(result))
    print(f"Screenshots: {shots}")

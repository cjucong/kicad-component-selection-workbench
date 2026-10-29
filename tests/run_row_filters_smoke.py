"""Chromium audit of generated workbench HTML with entirely synthetic HTTP data.

Requires Node Playwright (NODE_PATH may point at the bundled node_modules).
No backend, CAD, records, installation or external network is used.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from _scratch import temporary_project

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from part_selection_server import render_index


BROWSER = r'''
const {chromium} = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const html = fs.readFileSync(process.env.ROW_FILTER_HTML,'utf8');
const project = process.env.ROW_FILTER_PROJECT;
const origin = 'http://127.0.0.1:18763';
const dangerous = '<img src=x onerror="window.filterInjection=true">';
const group = (ref,value,comment,pages,footprint='Library:0603') => ({
 group_id:ref,ref:ref+'0',refs:pages.map((page,i)=>ref+i).join(','),
 refs_list:pages.map((page,i)=>ref+i),
 ref_links:pages.map((page,i)=>({ref:ref+i,page,url:''})),
 page:pages[0],page_refs:pages.join('; '),quantity:pages.length,
 comment,value,footprint,stock:{},override:{},review_status:'Pending',
 effective_c_code:'',schematic_sync_needed:true
});
const rows = [group('R1','10pF','capacitor',['/Power','/Logic']),
 group('R3','22pF','capacitor',['/Logic']),group('R4','10pF','resistor',['/Other']),
 group('R5','',dangerous,['/Other'],'')];
(async()=>{
 const browser = await chromium.launch({headless:true});
 const context = await browser.newContext({viewport:{width:1440,height:1000}});
 const errors=[],requests=[];
 let rowRequests=0;
 try {
  await context.route('**/*',async route=>{
   const url=new URL(route.request().url());
   assert.equal(url.origin,origin,'Unexpected external request');
   if (!url.pathname.startsWith('/api/')) return route.fulfill({contentType:'text/html',body:html});
   assert.equal(route.request().method(),'GET','No CAD or record writes are expected');
   requests.push(url.pathname);
   let data={ok:true};
   if (url.pathname==='/api/boms') data={boms:[{path:'__schematic__',name:'Synthetic'}],default_bom:'__schematic__',project_root:project};
   if (url.pathname==='/api/rows') { rowRequests++; data={rows}; }
   if (url.pathname==='/api/footprint_libraries') data={ok:true,libraries:[]};
   if (url.pathname==='/api/local_inventory') data={ok:true,records:{},pending_records:{}};
   await route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  });
  const page = await context.newPage();
  page.on('pageerror',error=>errors.push(error.message));
  page.setDefaultTimeout(7000);
  await page.goto(origin);
  await page.waitForSelector('#rows tr[data-ref]',{state:'attached'});
  assert.equal(await page.locator('#rows tr[data-ref]').count(),4);
  assert.equal(await page.locator('.row-filter-trigger').count(),4);
  assert.equal(await page.locator('#bomSearchCount').textContent(),'4 / 4 行');
  assert.equal(await page.evaluate(()=>document.querySelector('.filter-tabs').nextElementSibling.id),'rowFilters');
  const initial = await page.evaluate(()=>JSON.stringify(currentRows));
  const visible = ()=>page.locator('#rows tr[data-ref]:visible').count();
  const choose = async field=>page.locator('#rowFilterField').selectOption(field);
  const select = async value=>page.locator('#rowFilterOptions input').evaluateAll((inputs,value)=>{
    const input=inputs.find(input=>input.value===value);
    if (!input) throw Error('Missing option '+value);
    input.click();
  },value);
  await page.locator('[data-row-filter="value"]').click();
  await page.locator('#rowFilterOptionSearch').fill('10P');
  assert.equal(await page.locator('#rowFilterOptions input').count(),1);
  await select('10pF');
  assert.equal(await visible(),2);
  await page.locator('#rowFilterOptionSearch').fill('');
  await select('22pF');
  assert.equal(await visible(),3);
  await choose('comment');
  await select('capacitor');
  assert.equal(await visible(),2);
  await choose('page_refs');
  await select('/Power');
  assert.equal(await visible(),1);
  assert.equal(await page.locator('#rows tr[data-ref]:visible').getAttribute('data-ref'),'R10');
  assert.match(await page.locator('#rows tr[data-ref]:visible [data-col="refs"]').innerText(),/R10.*R11/s);
  assert.equal(await page.evaluate(()=>JSON.stringify(currentRows)),initial);
  assert.equal(await page.evaluate(()=>currentRows.reduce((total,row)=>total+row.quantity,0)),5);
  assert.equal(rowRequests,1);
  await page.locator('#bomSearch').fill('no-such-row');
  await page.waitForSelector('#rowsEmptyState:visible');
  assert.equal(await page.locator('#resetRowFilters').isVisible(),true);
  await page.locator('#resetRowFilters').click();
  assert.equal(await visible(),4);
  assert.equal(await page.locator('#rowFilterTags').textContent(),'');
  assert.equal(await page.evaluate(()=>workbenchRowFilters.active()),false);
  await page.locator('[data-filter="pending_review"]').click();
  await choose('value');
  await select('22pF');
  assert.equal(await visible(),1);
  await page.locator('#bomSearch').fill('10pF');
  assert.equal(await visible(),0);
  await page.locator('#resetRowFilters').click();
  assert.equal(await visible(),4);
  // Hidden headers retain an accessible chooser and active tags.
  await page.evaluate(()=>{updateUiSettings({hiddenColumns:{value:true}});applyColumnVisibility();});
  assert.equal(await page.locator('[data-row-filter="value"]').isVisible(),false);
  await choose('value');
  await select('22pF');
  assert.match(await page.locator('#rowFilterTags').innerText(),/Value: 22pF/);
  await page.reload();
  await page.waitForSelector('#rows tr[data-ref]',{state:'attached'});
  assert.equal(await visible(),1);
  assert.match(await page.locator('#rowFilterTags').innerText(),/22pF/);
  await page.locator('#clearColumnFilters').click();
  assert.equal(await visible(),4);
  await choose('footprint');
  await select('');
  assert.equal(await visible(),1);
  await page.locator('#clearColumnFilters').click();
  await choose('comment');
  await page.locator('#rowFilterOptionSearch').fill('<img');
  assert.equal(await page.locator('#rowFilterOptions img').count(),0);
  assert.match(await page.locator('#rowFilterOptions').innerText(),/<img/);
  await select(dangerous);
  assert.equal(await page.locator('#rowFilterTags img').count(),0);
  assert.equal(await page.evaluate(()=>Boolean(window.filterInjection)),false);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#rowFilterPanel').isHidden(),true);
  // A stale selection is preserved before asynchronous initial rows arrive.
  await page.evaluate(project=>{
    updateUiSettings({rowFiltersByProject:{[project]:{value:['stale-value']},other:{comment:['untouched']}}});
  },project);
  await page.reload();
  await page.waitForSelector('#rowsEmptyState:visible');
  assert.equal(await visible(),0);
  await choose('value');
  assert.equal(await page.getByRole('checkbox',{name:'stale-value',exact:true}).isChecked(),true);
  await page.keyboard.press('Escape');
  await page.locator('#resetRowFilters').click();
  assert.equal(await visible(),4);
  assert.deepEqual(await page.evaluate(()=>loadUiSettings().rowFiltersByProject.other),{comment:['untouched']});
  // Browser storage failure cannot prevent live filtering.
  await page.evaluate(()=>{window.originalSetItem=Storage.prototype.setItem;Storage.prototype.setItem=()=>{throw Error('blocked');};});
  await choose('comment');
  await select('capacitor');
  assert.equal(await visible(),2);
  await page.evaluate(()=>{Storage.prototype.setItem=window.originalSetItem;});
  for (const width of [1440,800,390]) {
    await page.setViewportSize({width,height:1000});
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    const searchBounds=await page.locator('.bom-search').boundingBox();
    const filterBounds=await page.locator('#rowFilters').boundingBox();
    const chooser=await page.locator('#rowFilterField').boundingBox();
    const tabs=await page.locator('.filter-tabs').boundingBox();
    assert.ok(await page.locator('#rowFilters').evaluate(el=>Boolean(el.closest('.filter-bar'))),'Column filters must be in the upper filter bar');
    if(width>=1200) assert.ok(Math.abs(chooser.y-tabs.y)<8,'Column filter chooser belongs beside the quick filters');
    for (const selector of ['#rowFilterField','#rowFilterPanel','#rowFilterOptionSearch','#rowFilterTags']) {
      const box=await page.locator(selector).boundingBox();
      assert.ok(box && box.width>0 && box.x>=0 && box.x+box.width<=width+1,`${selector} overflows at ${width}: ${JSON.stringify(box)} scrollX=${await page.evaluate(()=>scrollX)}`);
    }
    if (process.env.ROW_FILTER_SCREENSHOTS) await page.screenshot({path:process.env.ROW_FILTER_SCREENSHOTS+`/row-filters-${width}.png`});
  }
  const candidate=await context.newPage();
  candidate.on('pageerror',error=>errors.push(error.message));
  await candidate.goto(origin+'/?view=candidate');
  assert.equal(await candidate.locator('#rowFilters').count(),0);
  assert.equal(await candidate.locator('.row-filter-trigger').count(),0);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({ok:true,groups:rows.length,demand:5,rowRequests,viewports:[1440,800,390],apiRequests:[...new Set(requests)]},null,2));
 } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def main():
    with temporary_project(prefix='row-filter-smoke-') as directory:
        project = Path(directory)
        html = project.parent / 'workbench.html'
        html.write_text(render_index(SimpleNamespace(project_root=project, config={})), encoding='utf-8')
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        result = subprocess.run(['node', '-e', BROWSER], capture_output=True, text=True, encoding='utf-8',
            timeout=90, env={**os.environ, 'ROW_FILTER_HTML': str(html), 'ROW_FILTER_PROJECT': str(project)}, **flags)
        print(result.stdout.encode('ascii', errors='backslashreplace').decode())
        if result.returncode:
            raise AssertionError(result.stderr.encode('ascii', errors='backslashreplace').decode())


if __name__ == '__main__':
    main()

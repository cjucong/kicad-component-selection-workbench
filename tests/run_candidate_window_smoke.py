"""Chromium interaction smoke for native candidate pages and blank-click fallback."""
import run_candidate_search_smoke as fixture


fixture.BROWSER = r'''
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1475,height:900}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  let searches=0, saves=0, failSave=false;
  const item={c_code:'C123456',model:'TEST-CAP',brand:'Test Maker',package:'0805',stock:'1200',
    param:'4.7uF X7R 50V',param_detail:'容值: 4.7uF; 温度系数: X7R',moq:'1',mpq:'100'};
  await page.route('**/api/lcsc_search?*',async route=>{
   searches++;
   await route.fulfill({json:{ok:true,done:true,items:[item],sources:{domestic:'ready',partner:'empty'},revision:1}});
  });
  await page.route('**/api/select_candidate',async route=>{
   saves++;
   await route.fulfill({status:failSave?500:200,json:failSave?{ok:false,error:'test save failed'}:{ok:true}});
  });
  await page.goto(process.env.CANDIDATE_TEST_URL);
  await page.locator('#rows [data-action="open-candidates"]').first().click();
  await page.locator('#candidateRows [data-action="choose-candidate"]').waitFor();
  await page.locator('#candidateKeyword').fill('retained keyword');
  const box=await page.locator('#candidateKeyword').boundingBox();
  await page.mouse.move(box.x+20,box.y+15); await page.mouse.down();
  await page.mouse.move(2,2); await page.mouse.up();
  assert.equal(await page.locator('#candidateModal').evaluate(el=>el.classList.contains('minimized')),false);
  await page.mouse.click(2,2);
  assert.equal(await page.locator('#candidateModal').evaluate(el=>el.classList.contains('minimized')),true);
  await page.locator('#candidateMinimizedBar').click();
  assert.equal(await page.locator('#candidateKeyword').inputValue(),'retained keyword');
  await page.locator('#closeCandidate').click();

  const calls=[];
  await page.exposeFunction('nativeStub',(action,value)=>{calls.push({action,value});return {ok:true};});
  await page.evaluate(()=>{window.pywebview={api:{candidate_window:window.nativeStub}};});
  await page.locator('#rows [data-action="open-candidates"]').first().click();
  await page.waitForFunction(()=>document.querySelector('#candidateModal').classList.contains('open'));
  assert.equal(calls.at(-1).action,'open');
  assert.equal(calls.at(-1).value.value,'4.7uF');
  await page.locator('#candidateMinimizedBar').click();
  await page.waitForTimeout(50);
  assert.equal(calls.at(-1).action,'restore');
  await page.mouse.click(10,750);
  await page.waitForTimeout(50);
  assert.equal(calls.at(-1).action,'minimize');

  await page.goto(process.env.CANDIDATE_TEST_URL+'/?view=candidate');
  await page.evaluate(()=>{window.pywebview={api:{candidate_window:window.nativeStub}};});
  assert.equal(await page.locator('#rows tr').count(),0);
  const payload={ref:'C1',refs:'C1',bom:'__schematic__',comment:'capacitor',value:'4.7uF',footprint:'Library:0805',code:'C100'};
  assert.equal((await page.evaluate(p=>window.receiveNativeCandidateRequest(p),payload)).ok,true);
  await page.locator('#candidateRows [data-action="choose-candidate"]').waitFor();
  const windowBox=await page.locator('#candidateWindow').boundingBox();
  assert.equal(windowBox.x,0); assert.equal(windowBox.y,0); assert.equal(windowBox.width,1475);
  assert.equal(await page.locator('#candidateWindow .modal-head').isVisible(),false);
  assert.equal(await page.locator('body > header').isVisible(),false);
  await page.locator('#candidateKeyword').fill('preserve custom query');
  await page.locator('#candidateKeyword').press('Enter');
  await page.locator('#candidateRows [data-action="choose-candidate"]').waitFor();
  await page.locator('[data-candidate-filter="identity"]').click();
  assert.match(await page.locator('#candidateFilterPanel').innerText(),/Test Maker/);
  await page.locator('[data-candidate-facet="brand"]').check();
  await page.locator('#closeCandidateFilters').click();
  const before=searches;
  assert.equal((await page.evaluate(p=>window.receiveNativeCandidateRequest(p),payload)).ok,true);
  await page.waitForTimeout(100);
  assert.equal(searches,before);
  assert.equal(await page.locator('#candidateKeyword').inputValue(),'preserve custom query');
  assert.match(await page.locator('#candidateFilterTags').innerText(),/Test Maker/);
  await page.evaluate(()=>{candidateTargetRow.dataset.candidateSaving='1';});
  assert.equal((await page.evaluate(p=>window.receiveNativeCandidateRequest(p),payload)).ok,true);
  assert.equal((await page.evaluate(p=>window.receiveNativeCandidateRequest({...p,ref:'C2',refs:'C2'}),payload)).ok,false);
  await page.evaluate(()=>{delete candidateTargetRow.dataset.candidateSaving;});
  if(process.env.CANDIDATE_WINDOW_SCREENSHOT) await page.screenshot({path:process.env.CANDIDATE_WINDOW_SCREENSHOT});
  failSave=true;
  await page.locator('#candidateRows [data-action="choose-candidate"]').click();
  await page.waitForFunction(()=>document.querySelector('#candidateSearchProgress').textContent.includes('test save failed'));
  assert.equal(await page.evaluate(()=>window.nativeCandidateCanExit()),true);
  failSave=false;
  await page.locator('#candidateRows [data-action="choose-candidate"]').click();
  await page.waitForFunction(()=>!document.querySelector('#candidateModal').classList.contains('open'));
  assert.equal(saves,2);
  assert.equal(calls.at(-2).action,'changed'); assert.equal(calls.at(-1).action,'close');
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({native_bridge:'passed',retained_query_filters_results:'passed',
    no_duplicate_search:'passed',save_refresh_and_failure:'passed',blank_click:'passed',text_drag:'passed',page_errors:errors}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


if __name__ == '__main__':
    fixture.main()

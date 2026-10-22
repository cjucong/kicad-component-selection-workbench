"""Full-detail completion, stale response protection and save in an offline Chromium fixture."""
import json
import os
from pathlib import Path
import subprocess
import threading
from http.server import ThreadingHTTPServer

from _scratch import temporary_project
from part_selection_server import PartSelectionApp, make_handler
from part_selection_runtime import runtime_directory
from part_selection_overrides import load_overrides


BROWSER = r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.setDefaultTimeout(12000);
  let model='SYNTHETIC-SLOW',fail=false,release;
  const held=new Promise(resolve=>{release=resolve;});
  let slowRequested=false;
  let holdAlternate=false,releaseAlternate,alternatePayload;
  const alternateHeld=new Promise(resolve=>{releaseAlternate=resolve;});
  page.on('request',request=>{if(request.url().endsWith('/api/alternate_parts'))alternatePayload=request.postDataJSON();});
  await page.route('**/api/lcsc_search?*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({
    ok:true,done:true,revision:1,items:[{c_code:'C101',product_id:'321',model,category:'贴片电阻',brand:'Synthetic',package:'0603',
      parameters:[{name:'电阻类型',value:'薄膜电阻'},{name:'阻值',value:'10kΩ'},{name:'精度',value:'±0.1%'},{name:'功率',value:'100mW'}],
      param_detail:'电阻类型: 薄膜电阻; 阻值: 10kΩ; 精度: ±0.1%; 功率: 100mW',stock:'100'}],sources:{domestic:'ready'}})}));
  await page.route('**/api/candidate_parameters',async route=>{
    const input=route.request().postDataJSON(),slow=input.model==='SYNTHETIC-SLOW';
    if(slow){slowRequested=true;await held;}
    if(holdAlternate)await alternateHeld;
    const failed=fail;
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(failed?{ok:false,status:'failed',error:'fixture failure'}:{
      ok:true,c_code:'C101',product_id:'321',model:input.model,detail_url:'https://item.szlcsc.com/321.html',
      parameters:[{name:'商品目录',value:'贴片电阻'},{name:'电阻类型',value:'薄膜电阻'},{name:'阻值',value:'10kΩ'},
       {name:'精度',value:'±0.1%'},{name:'功率',value:'100mW'},{name:'工作电压',value:slow?'900V':'75V'},
       {name:'温度系数',value:'±10ppm/℃'},{name:'工作温度',value:'-55℃~+155℃'}],
      param_detail:'电阻类型: 薄膜电阻; 阻值: 10kΩ; 精度: ±0.1%; 功率: 100mW; 工作电压: '+(slow?'900V':'75V')+'; 温度系数: ±10ppm/℃; 工作温度: -55℃~+155℃'} )});
  });
  await page.goto(process.env.COMPLETION_TEST_URL);
  await page.locator('#rows [data-action="open-candidates"]').first().click();
  await page.waitForFunction(()=>document.querySelector('#candidateRows').textContent.includes('SYNTHETIC-SLOW'));
  while(!slowRequested)await page.waitForTimeout(20);
  assert.match(await page.locator('#candidateRows').innerText(),/补取详情|尚未读取/);
  assert.doesNotMatch(await page.locator('#candidateRows').innerText(),/未提供/);
  model='SYNTHETIC-CURRENT';
  await page.locator('#candidateKeyword').fill('current model');
  await page.locator('#candidateKeyword').press('Enter');
  await page.waitForFunction(()=>document.querySelector('#candidateRows').textContent.includes('75V'));
  release();
  await page.waitForTimeout(150);
  assert.doesNotMatch(await page.locator('#candidateRows').innerText(),/900V/);
  const parameters=page.locator('#candidateRows [data-candidate-col="param"]');
  assert.match(await parameters.innerText(),/75V/);
  assert.match(await parameters.innerText(),/10ppm/);
  assert.match(await parameters.innerText(),/工作温度/);
  assert.match(await parameters.innerText(),/参数来源：商品详情/);
  await page.locator('[data-candidate-filter="parameters"]').click();
  await page.locator('[data-candidate-facet="param:工作电压"]').check();
  await page.locator('[data-candidate-facet="param:温度系数"]').check();
  assert.match(await page.locator('#candidateFilterCount').innerText(),/显示 1/);
  await page.locator('#closeCandidateFilters').click();
  await page.locator('#clearCandidateFilters').click();
  holdAlternate=true;model='SYNTHETIC-ALTERNATE';
  await page.locator('#candidateKeyword').fill('alternate');
  await page.locator('#candidateKeyword').press('Enter');
  await page.waitForFunction(()=>document.querySelector('#candidateRows').textContent.includes('SYNTHETIC-ALTERNATE'));
  await page.locator('[data-action="add-candidate-alternate"]').click();
  await page.waitForTimeout(150);
  assert.equal(alternatePayload,undefined,'alternate must wait for pending full parameters');
  holdAlternate=false;releaseAlternate();
  await page.waitForFunction(()=>!document.querySelector('#candidateModal').classList.contains('open'));
  assert.match(alternatePayload.part.param_detail,/75V/);
  assert.match(alternatePayload.part.param_detail,/10ppm/);
  fail=true;model='SYNTHETIC-RETRY';
  await page.locator('#rows [data-action="open-candidates"]').first().click();
  await page.locator('#candidateKeyword').fill('retry');
  await page.locator('#candidateKeyword').press('Enter');
  await page.waitForFunction(()=>document.querySelector('#candidateRows').textContent.includes('详情读取失败'));
  assert.doesNotMatch(await page.locator('#candidateRows').innerText(),/未提供/);
  fail=false;
  await page.locator('[data-action="retry-candidate-parameters"]').click();
  await page.waitForFunction(()=>document.querySelector('#candidateRows').textContent.includes('75V'));
  await page.screenshot({path:process.env.COMPLETION_SCREENSHOT});
  await page.locator('[data-action="choose-candidate"]').click();
  await page.waitForFunction(()=>!document.querySelector('#candidateModal').classList.contains('open'));
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({first_results_unblocked:true,full_attributes:true,stale_detail_ignored:true,
    failure_not_vendor_absence:true,retry:true,voltage_tcr_filters:true,alternate_waits_for_parameters:true,saved_full_parameters:true,page_errors:errors}));
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def main():
    with temporary_project(prefix="browser-detail-completion-") as directory:
        root = Path(directory)
        schematic = ('(kicad_sch (symbol (property "Reference" "R1") (property "Comment" "resistor") '
                     '(property "Value" "10k") (property "Footprint" "Lib:0603")))')
        (root / "test.kicad_sch").write_text(schematic, encoding="utf-8")
        config = root / "config.json"
        config.write_text(json.dumps({"schematic_dir": ".", "source_bom": "__schematic__", "open_lcsc_search_on_replace": False}))
        app = PartSelectionApp(config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            output = runtime_directory(Path(__file__).resolve().parents[1]) / "full-candidate-parameters.png"
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            result = subprocess.run(["node", "-e", BROWSER], capture_output=True, text=True, encoding="utf-8",
                env={**os.environ, "COMPLETION_TEST_URL": f"http://127.0.0.1:{server.server_port}",
                     "COMPLETION_SCREENSHOT": str(output)}, timeout=90, **options)
            print(result.stdout)
            if result.returncode:
                raise AssertionError(result.stderr)
            record = load_overrides(app.overrides_path)["R1"]
            assert "75V" in record["field_overrides"]["description"]
            assert "10ppm" in record["field_overrides"]["description"]
            assert (root / "test.kicad_sch").read_text(encoding="utf-8") == schematic
            print(f"Screenshot: {output}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            app.search_jobs.close()
            app.selection_jobs.close()


if __name__ == "__main__":
    main()

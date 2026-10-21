"""Chromium search-state/layout smoke with mocked upstream and a disposable project."""
import json
import os
from pathlib import Path
import subprocess
import threading
from http.server import ThreadingHTTPServer

from _scratch import temporary_project
from part_selection_server import PartSelectionApp, make_handler


BROWSER = r'''
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1475,height:900}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  let phase='waiting';
  const item={c_code:'C123456',model:'TEST-0805-475',brand:'Test Manufacturer',package:'0805',
    param:'4.7uF X7R 50V 0805',param_detail:'容值: 4.7uF; 温度系数: X7R; 额定电压: 50V; 精度: ±10%',
    stock:'1200',channel:'现货',delivery:'最快4小时发货',moq:'5',mpq:'3000',price_ladder:'5+: ¥0.62; 50+: ¥0.51'};
  await page.route('**/api/lcsc_search?*',async route=>{
   const done=['complete','empty','failed'].includes(phase);
   const items=['partial','complete'].includes(phase)?[item]:[];
   const sources={domestic:'empty',partner:'empty',fallback:done?(phase==='empty'?'empty':phase==='failed'?'failed':'ready'):'querying'};
   await route.fulfill({status:phase==='failed'?503:200,contentType:'application/json',body:JSON.stringify({
    ok:phase!=='failed',error:phase==='failed'?'simulated upstream failure':'',done,items,sources,
    job_id:'smoke',revision:phase==='waiting'?1:phase==='partial'?2:3,checked_at:done?'2026-10-21T00:00:00':''})});
  });
  await page.goto(process.env.CANDIDATE_TEST_URL);
  await page.locator('#rows [data-action="open-candidates"]').first().click();
  await page.waitForFunction(()=>document.querySelector('#candidateSearchProgress').textContent.includes('补充检索：查询中'));
  const status=page.locator('#candidateSearchProgress');
  assert.doesNotMatch(await status.innerText(),/无结果|查询完成|未找到/);
  phase='partial';
  await page.waitForFunction(()=>document.querySelector('#candidateSearchProgress').textContent.includes('部分结果'));
  assert.match(await page.locator('#candidateRows').innerText(),/TEST-0805-475/);
  assert.doesNotMatch(await page.locator('#candidateRows').innerText(),/没有候选结果/);
  phase='complete';
  await page.waitForFunction(()=>document.querySelector('#candidateSearchProgress').textContent.includes('查询完成'));
  assert.match(await status.innerText(),/补充检索：已返回/);
  for(const [width,height] of [[1475,900],[800,700],[390,844]]) {
   await page.setViewportSize({width,height});
   await page.waitForTimeout(150);
   const bar=await page.locator('.candidate-status-bar').boundingBox();
   const query=await page.locator('#candidateWindow .modal-query').boundingBox();
   const body=await page.locator('#candidateWindow .candidate-body-wrap').boundingBox();
   const progress=await status.boundingBox();
   const count=await page.locator('#candidateFilterCount').boundingBox();
   assert.ok(bar.height<=(width>=800?52:120),JSON.stringify({width,bar}));
   assert.ok(bar.y>=query.y+query.height-1);
   assert.ok(body.y>=bar.y+bar.height-1);
   assert.ok(progress.y+progress.height<=count.y || progress.x+progress.width<=count.x+1);
   assert.ok(count.x+count.width<=width);
   if(process.env.CANDIDATE_SCREENSHOTS) await page.screenshot({path:path.join(process.env.CANDIDATE_SCREENSHOTS,`candidate-${width}.png`)});
  }
  await page.setViewportSize({width:1475,height:900});
  await page.locator('[data-candidate-filter="identity"]').click();
  await page.locator('[data-candidate-facet="brand"]').check();
  assert.match(await page.locator('#candidateFilterTags').innerText(),/Test Manufacturer/);
  await page.locator('#closeCandidateFilters').click();
  await page.locator('#inStockOnly').check();
  assert.match(await page.locator('#candidateFilterCount').innerText(),/显示 1/);
  await page.locator('#clearCandidateFilters').click();
  Object.assign(item,{category:'贴片电阻',model:'TEST-RESISTOR',param:'',
    param_detail:'阻值: 100kΩ; 温度系数: ±25ppm/℃; 工作电压: 75V; 厂商扩展参数: literal'});
  await page.locator('#candidateKeyword').fill('100k 0603');
  await page.locator('#candidateKeyword').press('Enter');
  await page.waitForFunction(()=>document.querySelector('#candidateRows').textContent.includes('TEST-RESISTOR'));
  assert.match(await page.locator('#candidateRows [data-candidate-col="param"]').innerText(),/温度系数\s*±25ppm\/℃/);
  assert.match(await page.locator('#candidateRows [data-candidate-col="param"]').innerText(),/工作电压\s*75V/);
  await page.locator('[data-candidate-filter="parameters"]').click();
  await page.locator('[data-candidate-facet="param:温度系数"]').check();
  assert.match(await page.locator('#candidateFilterCount').innerText(),/显示 1/);
  await page.locator('#closeCandidateFilters').click();
  await page.locator('#clearCandidateFilters').click();
  Object.assign(item,{category:'功率电感',model:'TEST-INDUCTOR',
    param_detail:'感值: 10uH; 直流电阻(DCR): 42mΩ; 类型: 绕线; 额定电流: 2A'});
  await page.locator('#candidateKeyword').fill('10uH 0805');
  await page.locator('#candidateKeyword').press('Enter');
  await page.waitForFunction(()=>document.querySelector('#candidateRows').textContent.includes('TEST-INDUCTOR'));
  const inductorParam=await page.locator('#candidateRows [data-candidate-col="param"]').innerText();
  assert.match(inductorParam,/直流电阻\(DCR\)\s*42mΩ/);
  assert.match(inductorParam,/电感类型\s*绕线/);
  assert.match(inductorParam,/额定电流\s*2A/);
  await page.locator('[data-candidate-filter="parameters"]').click();
  await page.locator('[data-candidate-facet="param:直流电阻(DCR)"]').check();
  assert.match(await page.locator('#candidateFilterCount').innerText(),/显示 1/);
  await page.locator('#closeCandidateFilters').click();
  if(process.env.CANDIDATE_SCREENSHOTS) await page.screenshot({path:path.join(process.env.CANDIDATE_SCREENSHOTS,'inductor-parameters.png')});
  phase='empty';
  await page.locator('#candidateKeyword').fill('no match');
  await page.locator('#candidateKeyword').press('Enter');
  await page.waitForFunction(()=>document.querySelector('#candidateSearchProgress').textContent.includes('未找到匹配器件'));
  assert.match(await page.locator('#candidateRows').innerText(),/没有候选结果/);
  phase='failed';
  await page.locator('#candidateKeyword').fill('network failure');
  await page.locator('#candidateKeyword').press('Enter');
  await page.waitForFunction(()=>document.querySelector('#candidateSearchProgress').textContent.includes('查询失败'));
  assert.match(await status.innerText(),/simulated upstream failure/);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({progress:'passed',partial:'passed',fallback_source:'passed',empty_and_failure:'passed',
    filters:'passed',resistor_and_inductor_parameters:'passed',layout:[1475,800,390],page_errors:errors}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


def main():
    with temporary_project(prefix="candidate-search-smoke-") as directory:
        root = Path(directory)
        (root / "test.kicad_pro").write_text("{}")
        (root / "test.kicad_sch").write_text(
            '(kicad_sch (symbol (property "Reference" "C1") (property "Comment" "capacitor") '
            '(property "Value" "4.7uF") (property "Footprint" "Library:0805")))')
        config = root / "settings.json"
        config.write_text(json.dumps({"source_bom": "__schematic__", "schematic_dir": ".",
                                      "open_lcsc_search_on_replace": False}))
        app = PartSelectionApp(config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            result = subprocess.run(["node", "-e", BROWSER], capture_output=True, text=True, encoding="utf-8",
                env={**os.environ, "CANDIDATE_TEST_URL": f"http://127.0.0.1:{server.server_port}"}, timeout=60, **flags)
            print(result.stdout)
            if result.returncode:
                raise AssertionError(result.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            app.search_jobs.close()
            app.selection_jobs.close()


if __name__ == "__main__":
    main()

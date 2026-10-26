"""Real Chromium + HTTP queue, pause/reload/backend reconstruction and resume."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer

from _scratch import temporary_project
from part_selection_server import PartSelectionApp, make_handler
from part_stock_lookup import check_stock_cancelled, lookup_stock_with_cache, save_stock_cache


BROWSER = r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.setDefaultTimeout(10000);
  await page.goto(process.env.STOCK_TEST_URL);
  await page.locator('#rows tr[data-ref]').first().waitFor();
  assert.match(await page.locator('[data-stock-freshness]').first().innerText(),/近期已查/);
  await page.getByRole('button',{name:'开始库存查询',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('#pauseStockQuery').disabled);
  await page.getByRole('button',{name:'暂停库存查询',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#queryMissingStock').innerText.includes('继续查询'));
  const paused=await page.evaluate(async()=>await (await fetch('/api/stock_refresh')).json());
  assert.equal(paused.state,'paused');
  assert.equal(paused.skipped_fresh,1);
  assert(paused.pending>0);
  const succeeded=Object.entries(paused.items).filter(([,v])=>v.state==='succeeded').map(([k])=>k);
  // Reconstruct the backend queue from disk, without any in-memory checkpoint.
  assert((await (await page.request.post(process.env.STOCK_TEST_URL+'__test_reopen')).json()).ok);
  await page.reload();
  await page.getByRole('button',{name:'继续库存查询',exact:true}).waitFor();
  await page.getByRole('button',{name:'继续库存查询',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#stockProgressText').innerText.includes('库存已完成'));
  const result=await (await page.request.get(process.env.STOCK_TEST_URL+'__test_seen')).json();
  assert(!result.seen.includes('C100'),'Fresh inventory was re-queried');
  for(const code of succeeded) assert.equal(result.seen.filter(c=>c===code).length,1);
  const state=await (await page.request.get(process.env.STOCK_TEST_URL+'api/stock_refresh')).json();
  assert.equal(state.succeeded,7);
  assert.equal(state.pending,0);
  assert.equal(state.failed,0);
  for(const width of [1440,768,390]) {
   await page.setViewportSize({width,height:900});
   assert(await page.locator('#stockFreshness').isVisible());
   const badge=page.locator('[data-stock-freshness]').first();
   await badge.scrollIntoViewIfNeeded();
   assert((await badge.innerText()).includes('近期已查'));
   if(process.env.STOCK_SCREENSHOT_DIR) await page.screenshot({path:process.env.STOCK_SCREENSHOT_DIR+'/'+width+'.png'});
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({pause:true,reopen:true,resume:true,fresh_skipped:true,unique_completed:true,
    viewports:[1440,768,390],page_errors:errors}));
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def main():
    with temporary_project(prefix='stock-refresh-browser-') as directory:
        root = Path(directory)
        schematic = '(kicad_sch ' + ' '.join(
            f'(symbol (property "Reference" "C{i}") (property "Comment" "capacitor") '
            f'(property "Value" "{i}nF") (property "Footprint" "Test:0603") '
            f'(property "LCSC" "C{i}00"))' for i in range(1,9)) + ')'
        (root/'test.kicad_sch').write_text(schematic, encoding='utf-8')
        config = root/'settings.json'
        config.write_text(json.dumps({'schematic_dir':'.','source_bom':'__schematic__','bom_dir':'data'}))
        app = PartSelectionApp(config)
        assert not app.stock_refresh.path.is_relative_to(root)
        before_records = app.overrides_path.read_bytes() if app.overrides_path.exists() else None
        fresh = {'ok':True,'status':'OK','c_code':'C100','stock':'200',
                 'fetched_at':datetime.now().isoformat(timespec='seconds')}
        save_stock_cache(app.stock_cache_path, {'C100':fresh})
        seen = []
        def fetch(code):
            seen.append(code)
            for _ in range(8 if code=='C200' else 70):
                check_stock_cancelled()
                time.sleep(.01)
            return {**fresh, 'c_code':code}
        def configure():
            app.stock_refresh.fetcher = lambda code: lookup_stock_with_cache(app.stock_cache_path, code, fetch)
        configure()
        base = make_handler(app)
        class Handler(base):
            def do_GET(self):
                if self.path == '/__test_seen':
                    from part_selection_server import json_response
                    return json_response(self, {'seen':seen})
                return super().do_GET()
            def do_POST(self):
                if self.path == '/__test_reopen':
                    from part_selection_server import json_response
                    assert not app.stock_refresh.busy()
                    app.stock_refresh = app.create_stock_refresh()
                    configure()
                    return json_response(self, {'ok':True})
                return super().do_POST()
        server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            options = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
            result = subprocess.run(['node','-e',BROWSER],capture_output=True,text=True,encoding='utf-8',timeout=90,
                env={**os.environ,'STOCK_TEST_URL':f'http://127.0.0.1:{server.server_port}/'},**options)
            print(result.stdout)
            if result.returncode:
                raise AssertionError(result.stderr)
            assert (root/'test.kicad_sch').read_text(encoding='utf-8') == schematic
            after_records = app.overrides_path.read_bytes() if app.overrides_path.exists() else None
            assert after_records == before_records, 'Stock reads changed selection records'
        finally:
            app.stock_refresh.pause()
            for worker in app.stock_refresh.threads:
                worker.join(timeout=3)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            app.search_jobs.close()
            app.selection_jobs.close()


if __name__ == '__main__':
    main()

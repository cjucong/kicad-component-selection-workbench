"""Exercise autosave and Enter merges in Chromium using the regression's disposable fixture."""
import os
from pathlib import Path
import subprocess
import threading
from http.server import ThreadingHTTPServer

from _scratch import temporary_project
from part_selection_overrides import load_overrides
from part_selection_server import make_handler
from test_directed_row_merge import make_app


BROWSER = r'''
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.setDefaultTimeout(10000);
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.MERGE_TEST_URL);
  await page.waitForFunction(()=>document.querySelectorAll('#rows tr').length===2);
  const target=page.locator('#rows tr[data-refs="C9"]');
  assert.match(await target.locator('[data-effective-code]').innerText(),/C200/);
  const source=page.locator('#rows tr[data-refs="C1,C2"]');
  await source.locator('[data-field="value"]').fill('4.7nF 50V');
  if(process.env.MERGE_TEST_ENTER==='1') await source.locator('[data-field="value"]').press('Enter');
  await page.waitForSelector('#rows tr[data-refs="C1,C2,C9"]');
  const merged=page.locator('#rows tr[data-refs="C1,C2,C9"]');
  assert.equal(await page.locator('#rows tr').count(),1);
  assert.match(await merged.locator('[data-effective-code]').innerText(),/C200/);
  assert.equal(await merged.locator('[data-field="review_status"]').inputValue(),'Confirmed');
  const marker=await merged.locator('[data-sync-marker]').innerText();
  assert.match(marker,/4.7nF.*4.7nF 50V/);
  assert.match(marker,/C100.*C200/);
  assert.match(await page.evaluate(()=>currentRows[0].description),/TARGET 50V/);
  await page.reload();
  await page.waitForSelector('#rows tr[data-refs="C1,C2,C9"]');
  assert.match(await merged.locator('[data-effective-code]').innerText(),/C200/);
  assert.equal(await merged.locator('[data-field="review_status"]').inputValue(),'Confirmed');
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({mode:process.env.MERGE_TEST_ENTER==='1'?'Enter':'autosave',
    merged_code:'C200',confirmation:'preserved',sync_diffs:'passed',reload:'passed',page_errors:errors}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


def main():
    for enter in (False, True):
        with temporary_project(prefix="directed-row-merge-smoke-") as directory:
            app, schematic = make_app(Path(directory))
            cad = schematic.read_bytes()
            before = load_overrides(app.overrides_path)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                result = subprocess.run(["node", "-e", BROWSER], capture_output=True, text=True, encoding="utf-8",
                    env={**os.environ, "MERGE_TEST_URL": f"http://127.0.0.1:{server.server_port}",
                         "MERGE_TEST_ENTER": "1" if enter else "0"}, timeout=45, **flags)
                print(result.stdout)
                if result.returncode:
                    raise AssertionError(result.stderr)
                assert schematic.read_bytes() == cad
                records = load_overrides(app.overrides_path)
                assert records["C9"] == before["C9"]
                assert records["C1"]["note"] == "source note"
                assert records["C1"]["alternate_parts"] == before["C9"]["alternate_parts"]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)
                app.search_jobs.close()
                app.selection_jobs.close()


if __name__ == "__main__":
    main()

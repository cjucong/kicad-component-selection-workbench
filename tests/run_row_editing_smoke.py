"""Real browser editing regression using only a disposable schematic project.

Requires Node's playwright package (NODE_PATH may point to an existing runtime).
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

from _scratch import temporary_project

ROOT = Path(__file__).resolve().parents[1]

BROWSER = r'''
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    page.setDefaultTimeout(12000);
    const errors = [];
    page.on('pageerror', e=>errors.push(e.message));
    await page.goto(process.env.ROW_TEST_URL);
    await page.waitForFunction(()=>document.querySelectorAll('#rows tr').length===1, null, {timeout:12000}).catch(async e=>{
      throw Error(JSON.stringify({error:e.message,errors,state:await page.locator('#state').innerText()}));
    });
    // Clicking a page immediately after typing waits for that edit before rendering the picker.
    await page.locator('#rows [data-field="value"]').fill('100nF ');
    await page.locator('[data-start-split-page="POWER"]').click();
    await page.waitForSelector('[data-multi-split-ref="C103"]');
    assert.equal(await page.locator('[data-multi-split-ref="C103"]').isChecked(),true);
    assert.equal(await page.locator('[data-multi-split-ref="C104"]').isChecked(),true);
    await page.locator('[data-multi-split-page="ADC"]').check();
    await page.locator('[data-action="split-selected-refs"]').click();
    assert.equal(await page.locator('#rows tr').count(),2);
    const selected = page.locator('#rows tr[data-refs="C103,C104,C105"]');
    assert.equal(await selected.count(),1);
    await selected.locator('[data-field="value"]').fill('220nF');
    await page.waitForTimeout(1000);
    // Saving while focused must keep the same DOM/focus, and only this group changes.
    assert.equal(await selected.locator('[data-field="value"]').inputValue(),'220nF');
    assert.equal(await page.locator('#rows tr[data-refs="C106"] [data-field="value"]').inputValue(),'100nF');
    await selected.locator('[data-field="footprint"]').fill('Library:0805');
    await selected.locator('[data-field="comment"]').fill('capacitor');
    await page.waitForTimeout(900);
    // Notes are hidden by default; type directly into the real control and dispatch input.
    await selected.locator('[data-field="note"]').evaluate(el=>{el.value='已核对耐压';el.dispatchEvent(new Event('input',{bubbles:true}));});
    await page.waitForTimeout(900);
    assert.equal(await page.evaluate(()=>rowForRefs(currentRows.find(r=>r.refs==='C103,C104,C105'),['C103']).note),'已核对耐压');
    await selected.locator('[data-field="replacement_c_code"]').fill('C123456');
    await page.waitForTimeout(900);
    await page.reload();
    await page.waitForSelector('#rows tr[data-refs="C103,C104,C105"]');
    assert.equal(await selected.locator('[data-field="value"]').inputValue(),'220nF');
    assert.equal(await selected.locator('[data-field="footprint"]').inputValue(),'Library:0805');
    assert.equal(await selected.locator('[data-field="note"]').inputValue(),'已核对耐压');
    assert.match(await selected.locator('[data-effective-code]').innerText(),/C123456/);
    // IME composition must not save intermediate content or treat Enter as submit.
    const value = selected.locator('[data-field="value"]');
    await value.focus();
    await value.evaluate(el=>{
      el.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));
      el.value='IME intermediate';
      el.dispatchEvent(new InputEvent('input',{bubbles:true,isComposing:true}));
      el.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true,key:'Enter',isComposing:true}));
    });
    await page.waitForTimeout(900);
    assert.equal(await value.inputValue(),'IME intermediate');
    await value.evaluate(el=>{
      el.value='330nF';el.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'330nF'}));
    });
    await page.waitForTimeout(900);
    await page.reload();
    await page.waitForSelector('#rows tr[data-refs="C103,C104,C105"]');
    assert.equal(await value.inputValue(),'330nF');
    let failed = false;
    await page.route('**/api/field_override', async route=>{
      if (!failed) { failed=true; await route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({ok:false,error:'simulated disk failure'})}); }
      else await route.continue();
    });
    await value.fill('470nF');
    await page.waitForTimeout(900);
    assert.equal(await value.inputValue(),'470nF');
    assert.match(await page.locator('#state').innerText(), /simulated disk failure/);
    await value.fill('330nF');
    await page.waitForTimeout(900);
    await page.unroute('**/api/field_override');
    // A confirmation chosen while C-code autosave is in flight must not become Pending.
    await page.route('**/api/override',async route=>{
      const payload=route.request().postDataJSON();
      if (payload.manual_c_code==='C123457') await new Promise(r=>setTimeout(r,500));
      await route.continue();
    });
    await selected.locator('[data-field="replacement_c_code"]').fill('C123457');
    await page.waitForTimeout(750);
    await selected.locator('[data-field="review_status"]').selectOption('Confirmed');
    await page.waitForTimeout(1100);
    await page.unroute('**/api/override');
    assert.equal(await selected.locator('[data-field="review_status"]').inputValue(),'Confirmed');
    // Reverting while an older write is still in flight must persist the last input.
    let delayed=false;
    await page.route('**/api/field_override',async route=>{
      if (!delayed) { delayed=true; await new Promise(r=>setTimeout(r,500)); }
      await route.continue();
    });
    await value.fill('680nF');
    await page.waitForTimeout(750);
    await value.fill('330nF');
    await page.waitForTimeout(1300);
    await page.unroute('**/api/field_override');
    await page.reload();
    await page.waitForSelector('#rows tr[data-refs="C103,C104,C105"]');
    assert.equal(await value.inputValue(),'330nF');
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({page_selection:'passed',group_split:'passed',autosave_without_enter:'passed',
      notes_and_code:'passed',composition:'passed',failure_retry:'passed',inflight_revert:'passed',page_errors:errors,viewport:'1440x1000'}));
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


def main():
    with temporary_project(prefix='browser-row-editing-') as directory:
        root = Path(directory)
        (root / 'test.kicad_pro').write_text('{}')
        (root / 'test.kicad_sch').write_text('(kicad_sch)')
        (root / 'DOC').mkdir()
        for page, refs in [('POWER', ['C103', 'C104']), ('ADC', ['C105']), ('DAC', ['C106'])]:
            symbols = ''.join('(symbol (property "Reference" "' + ref + '") '
                '(property "Comment" "capacitor") (property "Value" "100nF") '
                '(property "Footprint" "Library:0603"))' for ref in refs)
            (root / 'DOC' / (page + '.kicad_sch')).write_text('(kicad_sch ' + symbols + ')')
        config = root / 'settings.json'
        config.write_text(json.dumps({'source_bom': '__schematic__', 'schematic_dir': 'DOC'}))
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        with (root / 'server.log').open('wb') as output:
            process = subprocess.Popen([sys.executable, '-B', str(ROOT / 'tools/part_selection_server.py'),
                '--config', str(config), '--port', str(port)], stdout=output, stderr=output, **flags)
            try:
                base = f'http://127.0.0.1:{port}'
                deadline = time.monotonic() + 20
                while True:
                    try:
                        with urlopen(base + '/api/config', timeout=1):
                            break
                    except OSError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise AssertionError((root / 'server.log').read_text(errors='replace'))
                        time.sleep(0.1)
                result = subprocess.run(['node', '-e', BROWSER], capture_output=True, text=True, encoding='utf-8',
                    env={**os.environ, 'ROW_TEST_URL': base}, timeout=90, **flags)
                print(result.stdout.encode('ascii', errors='backslashreplace').decode())
                if result.returncode:
                    raise AssertionError(result.stderr.encode('ascii', errors='backslashreplace').decode())
                records = json.loads((root / 'BOM/part_selection_overrides.json').read_text(encoding='utf-8'))
                assert all(records[ref]['field_overrides']['value'] == '330nF' for ref in ('C103','C104','C105'))
                assert records['C106']['field_overrides']['value'] == '100nF'
                assert all(records[ref]['review_status'] == 'Confirmed' for ref in ('C103','C104','C105'))
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    main()

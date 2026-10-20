"""Repeatable Chromium interaction audit; synthetic project, no KiCad/network writes."""
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
  const results = [];
  async function check(name, run) {
    const page = await browser.newPage({viewport:{width:1440,height:900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.setDefaultTimeout(5000);
    try { await run(page); assert.deepEqual(errors,[]); results.push({name,ok:true}); }
    catch (error) { results.push({name,ok:false,error:error.message,page_errors:errors}); }
    finally { await page.close(); }
  }
  async function load(page) {
    await page.goto(process.env.WORKBENCH_TEST_URL);
    await page.waitForSelector('#rows tr[data-ref]');
  }
  try {
    await check('keyboard_menus', async page => {
      await load(page);
      const file = page.locator('.app-menubar [data-menu-trigger]').nth(0);
      const data = page.locator('.app-menubar [data-menu-trigger]').nth(1);
      await file.focus();
      await page.keyboard.press('ArrowDown');
      assert.equal(await file.getAttribute('aria-expanded'),'true');
      assert.equal(await page.locator('.app-menu.open .menu-popup > button').first().evaluate(el=>el===document.activeElement),true);
      await page.keyboard.press('End');
      assert.equal(await page.locator('#exitWorkbench').evaluate(el=>el===document.activeElement),true);
      await page.keyboard.press('Home');
      await page.keyboard.press('ArrowUp');
      assert.equal(await page.locator('#exitWorkbench').evaluate(el=>el===document.activeElement),true);
      await page.keyboard.press('ArrowRight');
      assert.equal(await data.getAttribute('aria-expanded'),'true');
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('.app-menu.open').count(),0);
      assert.equal(await data.evaluate(el=>el===document.activeElement),true);
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('.app-menu.open .menu-popup > button').first().evaluate(el=>el===document.activeElement),true);
      await page.keyboard.press('Escape');
      await page.keyboard.press('ArrowDown');
      await page.keyboard.press('Tab');
      assert.equal(await page.locator('.app-menu.open').count(),0);
      // The export range remains a normal select, not a simulated menu item.
      await page.locator('#exportPumpMenu').focus();
      await page.keyboard.press('ArrowDown');
      assert.equal(await page.locator('#pumpExportMode').evaluate(el=>el===document.activeElement),true);
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#exportPumpMenu').evaluate(el=>el===document.activeElement),true);
      await page.locator('#defaultViewMenu').focus();
      await page.keyboard.press('Space');
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('.app-menu.open').count(),0);
      assert.equal(await page.locator('#defaultViewMenu').evaluate(el=>el===document.activeElement),true);
    });
    await check('startup_failure_retry', async page => {
      let requests = 0;
      await page.route('**/api/boms', async route => {
        requests += 1;
        if (requests === 1) await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'audit source unavailable'})});
        else await route.continue();
      });
      await page.goto(process.env.WORKBENCH_TEST_URL);
      await page.waitForFunction(()=>document.querySelector('#state').textContent.includes('audit source unavailable'));
      assert.equal(await page.locator('#syncAllSchematic').isDisabled(),true);
      assert.equal(await page.locator('#queryAllStock').isDisabled(),true);
      await page.locator('#refreshSchematicRows').click();
      await page.waitForSelector('#rows tr[data-ref]');
      assert.equal(requests,2);
      assert.equal(await page.locator('#bomSelect').inputValue(),'__schematic__');
      assert.equal(await page.locator('#syncAllSchematic').isEnabled(),true);
    });
    await check('filter_empty_reset_and_selection', async page => {
      await load(page);
      assert.equal(await page.locator('#rows tr[data-ref]').count(),2);
      await page.locator('#selectVisibleRows').check();
      await page.locator('#bomSearch').fill('C101');
      assert.equal(await page.locator('#rows tr[data-ref]:visible').count(),1);
      assert.equal(await page.locator('#syncSelectedSchematic [data-button-count]').innerText(),'1');
      await page.locator('#bomSearch').fill('does-not-exist');
      await page.waitForSelector('#rowsEmptyState:visible');
      assert.match(await page.locator('#rowsEmptyState').innerText(),/没有匹配/);
      assert.equal(await page.locator('#syncSelectedSchematic').isDisabled(),true);
      await page.locator('#resetRowFilters').click();
      assert.equal(await page.locator('#rows tr[data-ref]:visible').count(),2);
      assert.equal(await page.locator('#syncSelectedSchematic [data-button-count]').innerText(),'2');
      assert.equal(await page.locator('#rowsEmptyState').isHidden(),true);
      await page.locator('[data-filter="needs_sync"]').click();
      await page.locator('#bomSearch').fill('does-not-exist');
      await page.locator('#resetRowFilters').click();
      assert.equal(await page.locator('#bomSearch').inputValue(),'');
      assert.equal(await page.locator('[data-filter="all"]').getAttribute('class'),'active');
      const directory = process.env.WORKBENCH_AUDIT_SCREENSHOTS;
      if (directory) {
        await page.screenshot({path:directory+'/desktop.png'});
        await page.setViewportSize({width:800,height:900});
        await page.locator('.app-menubar [data-menu-trigger]').first().click();
        const bounds = await page.locator('.app-menu.open .menu-popup').boundingBox();
        assert.ok(bounds.x>=0 && bounds.x+bounds.width<=800);
        await page.screenshot({path:directory+'/narrow-menu.png'});
      }
    });
  } finally { await browser.close(); }
  console.log(JSON.stringify(results,null,2));
  if (results.some(result=>!result.ok)) process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def main():
    with temporary_project(prefix='workbench-interactions-') as directory:
        root = Path(directory)
        (root / 'audit.kicad_pro').write_text('{}', encoding='utf-8')
        symbols = ''.join(
            f'(symbol (property "Reference" "{ref}") (property "Comment" "capacitor") '
            f'(property "Value" "{value}") (property "Footprint" "Library:0603"))'
            for ref, value in [('C101', '100nF'), ('C102', '1uF')])
        (root / 'audit.kicad_sch').write_text('(kicad_sch '+symbols+')', encoding='utf-8')
        config = root / 'settings.json'
        config.write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.'}), encoding='utf-8')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        flags = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        with (root / 'server.log').open('wb') as output:
            process = subprocess.Popen([sys.executable, '-B', str(ROOT / 'tools/part_selection_server.py'),
                '--config', str(config), '--port', str(port)], stdout=output, stderr=output, **flags)
            try:
                base = f'http://127.0.0.1:{port}'
                deadline = time.monotonic()+20
                while True:
                    try:
                        with urlopen(base+'/api/config', timeout=1):
                            break
                    except OSError:
                        if process.poll() is not None or time.monotonic()>=deadline:
                            raise AssertionError((root / 'server.log').read_text(errors='replace'))
                        time.sleep(0.1)
                result = subprocess.run(['node', '-e', BROWSER], capture_output=True, text=True,
                    encoding='utf-8', env={**os.environ,'WORKBENCH_TEST_URL':base}, timeout=90, **flags)
                print(result.stdout.encode('ascii', errors='backslashreplace').decode())
                if result.returncode:
                    raise AssertionError(result.stderr.encode('ascii', errors='backslashreplace').decode())
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    main()

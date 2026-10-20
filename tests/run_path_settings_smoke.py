"""Real browser settings regression, using an auto-cleaned disposable project."""
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
const path = require('node:path');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.setDefaultTimeout(10000);
    await page.goto(process.env.SETTINGS_TEST_URL);
    await page.waitForSelector('#rows tr');
    const open = async () => {
      await page.getByRole('button', {name:'设置', exact:true}).click();
      await page.locator('#openPathConfig').click();
      await page.locator('#pathSettingsDialog').waitFor({state:'visible'});
    };
    await open();
    assert.equal(await page.locator('#pathAdvanced').getAttribute('open'),null);
    assert.equal(await page.locator('#pathSourceSchematic').isChecked(),true);
    assert.equal(await page.locator('#pathImportRow').isVisible(),false);
    assert.equal(await page.locator('#path-output_overrides').isVisible(),false);
    if (process.env.PATH_SETTINGS_SCREENSHOTS) {
      await page.screenshot({path:path.join(process.env.PATH_SETTINGS_SCREENSHOTS,'settings-compact.png')});
    }
    await page.locator('#pathDataCustom').check();
    assert.equal(await page.locator('#path-bom_dir').inputValue(),'BOM');
    await page.locator('#path-bom_dir').fill('discarded');
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#pathSettingsDialog footer [data-close-paths]').click();
    await open();
    assert.equal(await page.locator('#path-bom_dir').inputValue(),'BOM');
    await page.locator('#pathDataCustom').check();
    await page.locator('#path-bom_dir').fill('reverted');
    await page.locator('#pathDataDefault').check();
    assert.equal(await page.locator('#path-output_overrides').inputValue(),'BOM/part_selection_overrides.json');
    // Exercise the browse button without opening a blocking native Windows dialog.
    await page.route('**/api/path_picker', route => route.fulfill({contentType:'application/json',body:JSON.stringify({ok:true,path:'exports'})}));
    await page.getByRole('button',{name:'选择工程选料数据目录',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#path-bom_dir').value==='exports');
    await page.unroute('**/api/path_picker');
    assert.equal(await page.locator('#path-output_overrides').inputValue(),'exports/part_selection_overrides.json');
    await page.locator('#pathAdvanced summary').click();
    await page.locator('#path-output_overrides').fill('conflict.json');
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#savePathSettings').click();
    await page.waitForFunction(()=>document.querySelector('#pathSettingsStatus').textContent.includes('目标已有不同数据'));
    await page.locator('#pathUnifyData').click();
    if (process.env.PATH_SETTINGS_SCREENSHOTS) {
      await page.screenshot({path:path.join(process.env.PATH_SETTINGS_SCREENSHOTS,'paths-desktop.png')});
    }
    // Declining migration leaves both the form and active records untouched.
    page.once('dialog', dialog => dialog.dismiss());
    await page.locator('#savePathSettings').click();
    assert.equal(await page.locator('#pathSettingsDialog').isVisible(),true);
    const retained=await page.request.get(process.env.SETTINGS_TEST_URL+'/api/path_settings');
    assert.equal((await retained.json()).values.bom_dir,'BOM');
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#savePathSettings').click();
    await page.waitForFunction(()=>document.querySelector('#pathSettingsStatus').textContent.includes('已保存并应用'));
    await page.locator('#pathSettingsDialog footer [data-close-paths]').click();
    await page.waitForSelector('#rows tr');
    await open();
    assert.equal(await page.locator('#path-bom_dir').inputValue(),'exports');
    assert.equal(await page.locator('#path-output_overrides').inputValue(),'exports/part_selection_overrides.json');
    // A full reload ignores any remembered CSV selection.
    assert.equal(await page.locator('#bomSelect').inputValue(),'__schematic__');
    await page.locator('#pathAdvanced summary').click();
    await page.setViewportSize({width:390,height:844});
    const bounds = await page.locator('#pathSettingsDialog').boundingBox();
    assert(bounds.x>=0 && bounds.y>=0 && bounds.x+bounds.width<=390 && bounds.y+bounds.height<=844);
    assert.equal(await page.locator('#pathSettingsDialog').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
    const overlaps = await page.locator('.path-field').evaluateAll(rows=>rows.some(row=>{
      const label=row.querySelector('label').getBoundingClientRect();
      const input=row.querySelector('input:not([type=radio])').getBoundingClientRect();
      if (!input.width || !label.width) return false;
      return label.bottom>input.top+1 && label.right>input.left+1;
    }));
    assert.equal(overlaps,false);
    if (process.env.PATH_SETTINGS_SCREENSHOTS) {
      await page.screenshot({path:path.join(process.env.PATH_SETTINGS_SCREENSHOTS,'paths-mobile.png')});
    }
    await page.getByRole('button',{name:'关闭路径配置',exact:true}).click();
    await page.setViewportSize({width:1440,height:1000});
    // Explicit import takes effect after save, but never becomes the next startup source.
    await open();
    await page.locator('#pathSourceImport').check();
    assert.equal(await page.locator('#pathImportRow').isVisible(),true);
    assert.equal(await page.locator('#path-source_bom').inputValue(),'BOM/legacy.csv');
    // Windows file pickers return absolute paths; match the option's canonical key.
    const view=await (await page.request.get(process.env.SETTINGS_TEST_URL+'/api/path_settings')).json();
    await page.locator('#path-source_bom').fill(view.project_root+'/BOM/legacy.csv');
    await page.locator('#savePathSettings').click();
    await page.waitForFunction(()=>document.querySelector('#pathSettingsStatus').textContent.includes('已保存并应用'));
    await page.locator('#pathSettingsDialog footer [data-close-paths]').click();
    await page.waitForFunction(()=>document.querySelector('#bomSelect').value==='BOM/legacy.csv');
    await page.reload();
    await page.waitForSelector('#rows tr');
    assert.equal(await page.locator('#bomSelect').inputValue(),'__schematic__');
    // Unrelated saves preserve advanced legacy paths and the retained import file.
    const endpoint=process.env.SETTINGS_TEST_URL+'/api/path_settings';
    const before=await (await page.request.get(endpoint)).json();
    const customized=await page.request.post(endpoint,{data:{revision:before.revision,
      values:{output_overrides:'retained/selection.json'},copy_existing:true}});
    assert.equal(customized.ok(),true);
    await open();
    assert.equal(await page.locator('#pathCustomFilesNotice').isVisible(),true);
    await page.locator('#pathAdvanced summary').click();
    await page.locator('#path-project_name').fill('Renamed disposable project');
    await page.locator('#savePathSettings').click();
    await page.waitForFunction(()=>document.querySelector('#pathSettingsStatus').textContent.includes('已保存并应用'));
    const after=await (await page.request.get(endpoint)).json();
    assert.equal(after.values.output_overrides,'retained/selection.json');
    assert.equal(after.import_source,'BOM/legacy.csv');
    await page.locator('#pathSettingsDialog footer [data-close-paths]').click();
    await page.waitForSelector('#rows tr');
    // Runtime changes offer the same native restart path without restarting production.
    await open();
    await page.locator('#pathAdvanced summary').click();
    await page.locator('#path-desktop_home').fill(process.env.SETTINGS_TEST_HOME);
    await page.locator('#savePathSettings').click();
    await page.waitForFunction(()=>document.querySelector('#pathSettingsStatus').textContent.includes('已保存并应用'));
    assert.equal(await page.locator('#pathRestart').isVisible(),true);
    await page.evaluate(()=>{window.pywebview={api:{finish_exit:async restart=>{window.testRestart=restart;return {ok:true};}}};});
    await page.locator('#pathRestart').click();
    await page.waitForFunction(()=>window.testRestart===true);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({menu:'passed',cancel:'passed',defaults:'passed',browse_mock:'passed',
      conflict:'passed',migration_cancel:'passed',migration_and_reload:'passed',
      explicit_import_and_schematic_startup:'passed',custom_paths_retained:'passed',
      runtime_restart_button:'passed',desktop_mobile_layout:'passed',page_errors:errors}));
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


def main():
    screenshots = os.environ.get('PATH_SETTINGS_SCREENSHOTS')
    if screenshots:
        target = Path(screenshots)
        if not target.is_absolute() or target.resolve().is_relative_to(ROOT):
            raise ValueError('Screenshots require an absolute directory outside the repository/project')
        target.mkdir(parents=True, exist_ok=True)
    with temporary_project(prefix='browser-path-settings-') as directory:
        root = Path(directory)
        schematic = '(kicad_sch (symbol (property "Reference" "R1") (property "Comment" "resistor") (property "Value" "100R") (property "Footprint" "Library:0603")))'
        (root / 'example.kicad_pro').write_text('{}')
        (root / 'example.kicad_sch').write_text(schematic)
        config = root / 'settings.json'
        config.write_text(json.dumps({'source_bom':'BOM/legacy.csv','schematic_dir':'.'}))
        (root / 'BOM').mkdir()
        (root / 'BOM/legacy.csv').write_text('Designator,Comment,Value,Footprint\nR1,resistor,100R,Library:0603\n')
        original = {'R1':{'note':'preserved selection'}}
        (root / 'BOM/part_selection_overrides.json').write_text(json.dumps(original))
        (root / 'conflict.json').write_text('{"different":true}')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0))
            port = listener.getsockname()[1]
        flags = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        with (root / 'server.log').open('wb') as output:
            process = subprocess.Popen([sys.executable,'-B',str(ROOT / 'tools/part_selection_server.py'),
                '--config',str(config),'--port',str(port)],stdout=output,stderr=output,**flags)
            try:
                base = f'http://127.0.0.1:{port}'
                deadline = time.monotonic()+20
                while True:
                    try:
                        with urlopen(base+'/api/config',timeout=1):
                            break
                    except OSError:
                        if process.poll() is not None or time.monotonic()>=deadline:
                            raise AssertionError((root / 'server.log').read_text(errors='replace'))
                        time.sleep(0.1)
                result = subprocess.run(['node','-e',BROWSER],capture_output=True,text=True,encoding='utf-8',
                    env={**os.environ,'SETTINGS_TEST_URL':base,'SETTINGS_TEST_HOME':str(root.parent / 'desktop')},timeout=90,**flags)
                print(result.stdout.encode('ascii',errors='backslashreplace').decode())
                if result.returncode:
                    raise AssertionError(result.stderr.encode('ascii',errors='backslashreplace').decode())
                assert json.loads((root / 'BOM/part_selection_overrides.json').read_text())['R1']['note']=='preserved selection'
                assert json.loads((root / 'exports/part_selection_overrides.json').read_text())['R1']['note']=='preserved selection'
                assert (root / 'example.kicad_sch').read_text()==schematic
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    main()

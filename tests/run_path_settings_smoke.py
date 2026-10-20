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
      await page.getByRole('button', {name:'文件', exact:true}).click();
      await page.locator('#openPathConfig').click();
      await page.locator('#pathSettingsDialog').waitFor({state:'visible'});
    };
    await open();
    assert.equal(await page.locator('#path-bom_dir').inputValue(),'BOM');
    await page.locator('#path-bom_dir').fill('discarded');
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#pathSettingsDialog footer [data-close-paths]').click();
    await open();
    assert.equal(await page.locator('#path-bom_dir').inputValue(),'BOM');
    await page.locator('#path-bom_dir').fill('reverted');
    await page.locator('#pathDefaults').click();
    assert.equal(await page.locator('#path-output_overrides').inputValue(),'BOM/part_selection_overrides.json');
    // Exercise the browse button without opening a blocking native Windows dialog.
    await page.route('**/api/path_picker', route => route.fulfill({contentType:'application/json',body:JSON.stringify({ok:true,path:'exports'})}));
    await page.getByRole('button',{name:'选择BOM 导出与同步日志目录',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#path-bom_dir').value==='exports');
    await page.unroute('**/api/path_picker');
    await page.locator('#pathUnifyData').click();
    await page.locator('#path-output_overrides').fill('conflict.json');
    await page.locator('#savePathSettings').click();
    await page.waitForFunction(()=>document.querySelector('#pathSettingsStatus').textContent.includes('目标已有不同数据'));
    await page.locator('#pathUnifyData').click();
    if (process.env.PATH_SETTINGS_SCREENSHOTS) {
      await page.screenshot({path:path.join(process.env.PATH_SETTINGS_SCREENSHOTS,'paths-desktop.png')});
    }
    await page.locator('#savePathSettings').click();
    await page.waitForFunction(()=>document.querySelector('#pathSettingsStatus').textContent.includes('已保存并应用'));
    await page.locator('#pathSettingsDialog footer [data-close-paths]').click();
    await page.waitForSelector('#rows tr');
    await open();
    assert.equal(await page.locator('#path-bom_dir').inputValue(),'exports');
    assert.equal(await page.locator('#path-output_overrides').inputValue(),'exports/part_selection_overrides.json');
    await page.getByRole('tab',{name:'转换与运行',exact:true}).click();
    await page.setViewportSize({width:390,height:844});
    const bounds = await page.locator('#pathSettingsDialog').boundingBox();
    assert(bounds.x>=0 && bounds.y>=0 && bounds.x+bounds.width<=390 && bounds.y+bounds.height<=844);
    assert.equal(await page.locator('#pathSettingsDialog').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
    const overlaps = await page.locator('#pathRuntimeFields .path-field').evaluateAll(rows=>rows.some(row=>{
      const label=row.querySelector('label').getBoundingClientRect();
      const input=row.querySelector('input').getBoundingClientRect();
      return label.bottom>input.top+1 && label.right>input.left+1;
    }));
    assert.equal(overlaps,false);
    if (process.env.PATH_SETTINGS_SCREENSHOTS) {
      await page.screenshot({path:path.join(process.env.PATH_SETTINGS_SCREENSHOTS,'paths-mobile.png')});
    }
    await page.getByRole('button',{name:'关闭路径配置',exact:true}).click();
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({menu:'passed',cancel:'passed',defaults:'passed',browse_mock:'passed',
      conflict:'passed',migration_and_reload:'passed',desktop_mobile_layout:'passed',page_errors:errors}));
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
        config.write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.'}))
        (root / 'BOM').mkdir()
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
                    env={**os.environ,'SETTINGS_TEST_URL':base},timeout=90,**flags)
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

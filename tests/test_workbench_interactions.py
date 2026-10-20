"""Fast startup/toolbar boundary tests, independent of browser smoke and test order."""
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from part_selection_server import render_index


def test_packager_import_in_a_fresh_python_process():
    result = subprocess.run([sys.executable, '-B', '-c',
        'import sys; sys.path.insert(0, sys.argv[1]); '
        'from kicad_plugin.package_plugin import package_files; '
        'assert "plugins/app/part_selection_server.py" in package_files()', str(ROOT / 'tools')],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which('node'), reason='Node required for real startup functions')
def test_source_loading_checks_http_and_payload_and_allows_retry():
    html = render_index(SimpleNamespace(project_root=ROOT, config={}))
    functions = re.search(r'async function loadBomOptions\(.*?(?=const rowEditQueue)', html, re.S).group()
    script = r'''
const assert=require('node:assert/strict');
const refresh={disabled:false}, bomSelect={innerHTML:'original options'};
const states=[];
let response, loaded=0;
const document={getElementById:()=>refresh};
const sessionStorage={getItem:()=>null,removeItem:()=>{}};
const esc=x=>x, updateUiSettings=()=>{};
const setState=message=>states.push(message);
const fetch=async()=>response;
const loadRows=async()=>{loaded+=1;};
FUNCTIONS
(async()=>{
 for(const body of [{error:'offline'},{boms:[]},{boms:null}]) {
   response={ok:!body.error,json:async()=>body};
   await initializeWorkbenchRows();
   assert.equal(loaded,0);
   assert.equal(bomSelect.innerHTML,'original options');
   assert.equal(refresh.disabled,false);
   assert.match(states.at(-1),/失败.*重新读取/);
 }
 response={ok:true,json:async()=>{throw Error('invalid JSON');}};
 await initializeWorkbenchRows();
 assert.match(states.at(-1),/invalid JSON/);
 response={ok:true,json:async()=>({boms:[{path:'__schematic__',name:'schematic'}],default_bom:'__schematic__'})};
 await initializeWorkbenchRows();
 assert.equal(loaded,1);
 assert.match(bomSelect.innerHTML,/__schematic__.*selected/);
 assert.equal(refresh.disabled,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''.replace('FUNCTIONS', functions)
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True,
                            text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which('node'), reason='Node required for real button state function')
def test_stock_commands_only_enable_for_loaded_current_rows():
    html = render_index(SimpleNamespace(project_root=ROOT, config={}))
    function = re.search(r'function resetQueryAllButton\(.*?(?=function cancelStockQueries)', html, re.S).group()
    script = r'''
const assert=require('node:assert/strict');
const queryMissingButton={},queryAllButton={},bomSelect={value:'A'};
let loadedRowsBom=null,rowsLoading=false;
const setToolbarLabel=()=>{};
FUNCTION
function check(expected) {
 resetQueryAllButton();
 assert.equal(queryMissingButton.disabled,expected);
 assert.equal(queryAllButton.disabled,expected);
}
check(true);
loadedRowsBom='A';check(false);
rowsLoading=true;check(true);
rowsLoading=false;bomSelect.value='B';check(true);
'''.replace('FUNCTION', function)
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True,
                            text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr

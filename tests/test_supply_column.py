"""Merged supply column and legacy view migration, using synthetic data only."""
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from part_selection_server import render_index

ROOT = Path(__file__).resolve().parents[1]


def page():
    return render_index(SimpleNamespace(project_root=ROOT, config={}))


def test_one_supply_column_and_no_visible_code_status_controls():
    html = page()
    assert '<th data-col="c_code"' not in html
    assert re.search(r'<th data-col="stock"[^>]*>选料 / 库存</th>', html)
    assert '<th data-col="local_inventory"' not in html
    assert '<select class="edit" data-field="status">' not in html
    assert 'data-action="clear-primary"' not in html
    assert '>查询库存</button>' not in html
    assert 'aria-label="主料 C 编码"' in html
    assert '["stock", "选料 / 库存"]' in html
    assert '["c_code", "C编码"]' not in html


@pytest.mark.skipif(not shutil.which('node'), reason='Node.js required')
def test_legacy_supply_view_migration_preserves_unrelated_preferences():
    html = page()
    match = re.search(r'function migrateSupplyColumns\(view\) \{.*?\n\}', html, re.S)
    assert match
    cases = [
        ({'columnOrder':['note','c_code','param','stock','review'],
          'hiddenColumns':{'c_code':True,'stock':False,'note':False},
          'columnWidths':{'c_code':180,'stock':260,'note':310},'candidateColumnWidths':{'code':123}},
         {'columnOrder':['note','stock','param','review'], 'hiddenColumns':{'stock':False,'note':False},
          'columnWidths':{'stock':260,'note':310}, 'candidateColumnWidths':{'code':123}}),
        ({'columnOrder':['stock','value','c_code'], 'hiddenColumns':{'c_code':True,'stock':True},
          'columnWidths':{'c_code':185}},
         {'columnOrder':['stock','value'],'hiddenColumns':{'stock':True},'columnWidths':{'stock':220}}),
        ({'columnOrder':['value','local_inventory','note','stock'],
          'hiddenColumns':{'local_inventory':False,'stock':True,'note':True},
          'columnWidths':{'local_inventory':180,'stock':290,'note':150}},
         {'columnOrder':['value','stock','note'],'hiddenColumns':{'stock':False,'note':True},
          'columnWidths':{'stock':290,'note':150}}),
    ]
    script = match.group() + '\nconst assert=require("node:assert/strict");\n'
    script += 'for (const [input,expected] of '+json.dumps(cases)+') {\n'
    script += 'assert.deepEqual(migrateSupplyColumns(input),expected);assert.deepEqual(migrateSupplyColumns(input),expected);}\n'
    result = subprocess.run(['node','-e',script],capture_output=True,text=True,encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_live_and_saved_default_views_both_migrate_supply_columns():
    html = page()
    for name in ('loadUiSettings', 'loadDefaultViewConfig'):
        function = re.search(r'function '+name+r'\(\) \{.*?\n\}', html, re.S).group()
        assert 'migrateSupplyColumns(' in function


@pytest.mark.skipif(not shutil.which('node'), reason='Node.js required')
def test_stock_exit_abort_keeps_shared_queries_and_new_retry_isolated():
    source = page().split('const pendingStockRequests = new Map();', 1)[1].split('\nasync function refreshRowStock', 1)[0]
    script = r'''
const assert=require('node:assert/strict');
const window={}, requests=[];
const fetch=(url, options)=>new Promise((resolve,reject)=>{
 requests.push({url,resolve});
 options.signal.addEventListener('abort',()=>reject(new DOMException('cancelled','AbortError')),{once:true});
});
const pendingStockRequests = new Map();
''' + source + r'''
(async()=>{
 const first=requestStock('C100'), shared=requestStock('C100');
 assert.strictEqual(first,shared);
 assert.equal(requests.length,1);
 const observed=first.catch(error=>error.name);
 cancelRowStockQueries();
 const retry=requestStock('C100');
 assert.notStrictEqual(retry,first);
 assert.equal(await observed,'AbortError');
 assert.strictEqual(pendingStockRequests.get('C100'),retry,'old cleanup removed retry');
 assert.equal(pendingStockControllers.size,1);
 requests[1].resolve({ok:true,json:async()=>({stock:{stock:'123',c_code:'C100'}})});
 assert.equal((await retry).stock,'123');
 await Promise.resolve();
 assert.equal(pendingStockRequests.size,0);
 assert.equal(pendingStockControllers.size,0);
 window.desktopExitPreparing=true;
 await assert.rejects(requestStock('C200'),{name:'AbortError'});
 assert.equal(requests.length,2,'exit started another query');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run(['node','-e',script],capture_output=True,text=True,encoding='utf-8',timeout=10)
    assert result.returncode == 0, result.stderr


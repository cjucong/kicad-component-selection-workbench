"""Exercise the actual stock/candidate renderers with synthetic material data."""
import json
import re
import shutil
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from part_selection_server import render_index


def run_js(assertions):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for material display checks')
    html = render_index(SimpleNamespace(project_root=Path(__file__).resolve().parents[1], config={}))
    blocks = [re.search(pattern, html, re.S).group() for pattern in (
        r'function stockParamText\(.*?(?=function stockAlreadyQueried\()',
        r'function normalizedCandidateParam\(.*?(?=function renderCandidateEntryParam\()')]
    script = "const assert = require('node:assert/strict');\n" + '\n'.join(blocks) + r'''
const esc = value => String(value ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const keys = html => [...html.matchAll(/class="candidate-param-key">([^<]*)</g)].map(m=>m[1]);
''' + assertions
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_stock_query_separates_summary_and_labels_with_consistent_colons():
    run_js(r'''
const stock = {model:'TEST267K', package:'0603', brand:'TEST',
 param:'RES 267kΩ ±0.1% 100mW 0603',
 param_detail:'功率: 100mW; 阻值: 267kΩ; 精度: ±0.1%\n工作温度: -55℃~+155℃'};
const before=JSON.stringify(stock), html=renderStockParam(stock);
assert.deepEqual(keys(html),['型号:','功率:','阻值:','精度:','工作温度:','封装:','品牌:']);
assert.ok(html.includes('class="candidate-param-value">RES 267kΩ ±0.1% 100mW 0603</div>'));
assert.equal(JSON.stringify(stock),before);
''')


def test_saved_and_fresh_stock_render_identically_without_duplicate_model():
    run_js(r'''
const stock = {model:'TEST',package:'0603',brand:'ACME',param:'RES 100kΩ',
 param_detail:'阻值: 100kΩ; 功率: 100mW; 精度: ±1%'};
const saved = {...stock,param:'型号: TEST\nRES 100kΩ\n阻值: 100kΩ; 功率: 100mW; 精度: ±1%\n封装: 0603\n品牌: ACME',param_detail:''};
assert.equal(renderStockParam(stock),renderStockParam(saved));
assert.equal(keys(renderStockParam(saved)).filter(key=>key==='型号:').length,1);
''')


def test_candidate_pairs_and_legacy_lines_share_colons_and_escape_values():
    run_js(r'''
const html=renderCandidateParam('摘要\r\n功率：100mW；精度: ±1%、工作温度: -55℃~+155℃',
 {name:'工作电压',value:'<50V>'});
assert.deepEqual(keys(html),['功率:','精度:','工作温度:','工作电压:']);
assert.ok(html.includes('&lt;50V&gt;'));
assert.ok(!html.includes('摘要\r\n功率'));
''')

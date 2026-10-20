import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from part_selection_server import render_index


class AutoColumnWidthsTest(unittest.TestCase):
    def setUp(self):
        self.html = render_index(SimpleNamespace(project_root=ROOT, config={}))

    def run_js(self, script):
        if not shutil.which("node"):
            self.skipTest("Node is required for column sizing behavior checks")
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_manual_auto_size_is_inside_view_settings_and_keeps_double_click_handles(self):
        panel = re.search(r'<div id="columnPanel".*?<div id="columnList">', self.html, re.S)
        self.assertIsNotNone(panel)
        self.assertIn('id="autoSizeColumns"', panel.group())
        self.assertIn('自动调整列宽', panel.group())
        self.assertEqual(self.html.count('id="autoSizeColumns"'), 1)
        self.assertNotIn('id="autoSizeCandidateColumns"', self.html)
        for token in (
                      'autoSizeColumns(candidate)',
                      'autoSizeColumns(false, th.dataset.col)',
                      'autoSizeColumns(true, th.dataset.candidateCol)', 'fitDefaultColumnWidths(true)'):
            self.assertTrue(token in self.html, token)

    def test_view_settings_button_resizes_only_the_workbench(self):
        listener = re.search(
            r'document.getElementById\("autoSizeColumns"\).addEventListener\("click",.*?\n\}\);',
            self.html, re.S,
        )
        self.assertIsNotNone(listener)
        self.run_js('''
const assert = require('node:assert/strict');
let callback = null, calls = [];
const document = {getElementById(id) {
  assert.equal(id, 'autoSizeColumns');
  return {addEventListener(event, handler) {
    assert.equal(event, 'click'); callback = handler;
  }};
}};
function autoSizeColumns(...args) {calls.push(args);}
LISTENER
callback();
assert.deepEqual(calls, [[false]]);
'''.replace('LISTENER', listener.group()))

    def test_width_budget_respects_minimums_caps_and_narrow_screens(self):
        function = re.search(r'function fitColumnWidthBudget\(.*?(?=\nfunction )', self.html, re.S)
        self.assertIsNotNone(function)
        self.run_js('''
const assert = require('node:assert/strict');
FUNCTION
const columns = [{key:'short',min:60,max:100,width:20},
                 {key:'param',min:160,max:300,width:999}];
const wide = fitColumnWidthBudget(columns, 1000);
assert.deepEqual(wide, {short:60,param:300});
const fit = fitColumnWidthBudget(columns, 280);
assert.equal(fit.short, 60);
assert.equal(fit.param, 220);
assert.deepEqual(fitColumnWidthBudget(columns, 100), {short:60,param:300});
assert.deepEqual(fitColumnWidthBudget([], 500), {});
assert.deepEqual(columns[1], {key:'param',min:160,max:300,width:999});
'''.replace('FUNCTION', function.group()))

    def test_auto_sizing_preserves_hidden_columns_and_other_settings(self):
        function = re.search(r'function autoSizeColumns\(.*?(?=\nfunction )', self.html, re.S)
        self.assertIsNotNone(function)
        self.run_js('''
const assert = require('node:assert/strict');
let defaultColumnWidthFrame = 0;
let candidateColumnWidthFrame = 0;
let settings = {columnWidths:{note:277,comment:999}, candidateColumnWidths:{param:333},
                hiddenColumns:{note:true}, columnOrder:['note','comment']};
let candidate = false, measuredKeys = [];
function header(key, hidden=false) {
  return {dataset:{col:key,candidateCol:key},classList:{contains:()=>hidden}};
}
const body = {querySelectorAll:()=>[]};
const table = {tBodies:[body],parentElement:{clientWidth:900},
               querySelectorAll:()=>[header('comment'),header('note',true)]};
table.closest=()=>table;
const document = {querySelector:()=>table, documentElement:{clientWidth:900}};
function loadUiSettings() {return settings;}
function updateUiSettings(update) {Object.assign(settings,update);}
function measureColumnWidth(table,key) {measuredKeys.push(key);return 123;}
function fitColumnWidthBudget(columns) {return Object.fromEntries(columns.map(c=>[c.key,c.width]));}
function columnWidthLimits() {return [60,300];}
function applySavedColumnWidths() {}
function applyCandidateSavedWidths() {}
function cancelAnimationFrame() {}
FUNCTION
autoSizeColumns(false);
assert.deepEqual(measuredKeys,['comment']);
assert.deepEqual(settings.columnWidths,{note:277,comment:123});
assert.deepEqual(settings.candidateColumnWidths,{param:333});
assert.deepEqual(settings.columnOrder,['note','comment']);
assert.equal(settings.columnWidthMode,'auto');
measuredKeys=[];
autoSizeColumns(false,'missing');
assert.deepEqual(measuredKeys,[]);
table.querySelectorAll=()=>[header('param'),header('code')];
autoSizeColumns(true,'code');
assert.deepEqual(measuredKeys,['code']);
assert.deepEqual(settings.candidateColumnWidths,{param:333,code:123});
assert.equal(settings.candidateColumnWidthMode,'manual');
'''.replace('FUNCTION', function.group()))

    def test_default_sizing_does_not_overwrite_existing_layout(self):
        function = re.search(r'function fitDefaultColumnWidths\(.*?(?=\nfunction )', self.html, re.S).group()
        self.run_js('''
const assert = require('node:assert/strict');
let defaultColumnWidthFrame = 0, candidateColumnWidthFrame = 0, calls = [], pending = null;
let settings = {columnWidths:{value:147}};
function loadUiSettings() {return settings;}
function autoSizeColumns(...args) {calls.push(args);}
function cancelAnimationFrame() {}
function requestAnimationFrame(fn) {pending=fn;return 1;}
FUNCTION
fitDefaultColumnWidths();
assert.equal(pending,null);
settings={}; fitDefaultColumnWidths();
settings={columnWidths:{value:155}}; pending();
assert.deepEqual(calls,[]);
settings={}; fitDefaultColumnWidths(); pending();
assert.deepEqual(calls,[[false]]);
calls=[]; pending=null; settings={candidateColumnWidths:{model:199}};
fitDefaultColumnWidths(true);
assert.equal(pending,null);
settings={candidateColumnWidths:{model:199},candidateColumnWidthMode:'auto'};
fitDefaultColumnWidths(true); pending();
assert.deepEqual(calls,[[true]]);
calls=[]; pending=null; settings={columnWidths:{value:147},columnWidthMode:'auto'};
fitDefaultColumnWidths(); pending();
assert.deepEqual(calls,[[false]]);
'''.replace('FUNCTION', function))


if __name__ == "__main__":
    unittest.main()

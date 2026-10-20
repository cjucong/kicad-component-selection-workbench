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


class CandidateFiltersTest(unittest.TestCase):
    def setUp(self):
        self.html = render_index(SimpleNamespace(project_root=ROOT, config={}))

    def run_js(self, assertions):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for candidate filter checks")
        block = re.search(r'function candidateResultEntries\(.*?(?=function renderCandidateFilterPanel\()', self.html, re.S)
        self.assertIsNotNone(block, "Candidate filter helpers are missing")
        script = "const assert = require('node:assert/strict');\n" + block.group() + assertions
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_headers_expose_brand_package_and_parameters_without_model_filter(self):
        self.assertTrue('data-candidate-filter="identity"' in self.html)
        self.assertTrue('data-candidate-filter="parameters"' in self.html)
        self.assertTrue('id="candidateFilterPanel"' in self.html)
        self.assertTrue('id="candidateFilterTags"' in self.html)
        self.assertTrue('id="candidateFilterCount"' in self.html)
        header = re.search(r'<th data-candidate-col="channel_stock".*?</th>', self.html, re.S).group()
        self.assertIn('id="inStockOnly"', header)
        query = re.search(r'<div class="modal-query">.*?id="candidateSearchProgress"', self.html, re.S).group()
        self.assertNotIn('id="inStockOnly"', query)

    def test_parameter_fields_parse_lines_aliases_and_preserve_unit_case(self):
        self.run_js(r'''
const entry = {item:{brand:' Murata ',package:'0603',model:'DO_NOT_FILTER',
 param:'CAP 10pF', param_detail:'容量：10pF、容差: ±5%; 耐压: 50V\n功率: 1mW, 温度系数: C0G; 型号: DO_NOT_FILTER'}};
const values = candidateEntryFields(entry);
assert.deepEqual([...values.get('brand')], ['Murata']);
assert.deepEqual([...values.get('package')], ['0603']);
assert.deepEqual([...values.get('param:容值')], ['10pF']);
assert.deepEqual([...values.get('param:精度')], ['±5%']);
assert.deepEqual([...values.get('param:功率')], ['1mW']);
assert.deepEqual([...values.get('param:耐压')], ['50V']);
assert.deepEqual([...values.get('param:温度系数')], ['C0G']);
assert.ok(![...values.keys()].some(key=>key.includes('型号') || key==='model'));
assert.equal(candidateEntryFields({item:{param:'CAP 10pF ±5% 0603'}}).size,0);
''')

    def test_multi_values_union_and_fields_intersection_without_mutation(self):
        self.run_js(r'''
const items = [
 {c_code:'C1',brand:'A',package:'0603',param_detail:'容量: 10pF; 精度: ±5%',stock:'10'},
 {c_code:'C2',brand:'B',package:'0603',param_detail:'容值: 10pF; 精度: ±5%',stock:'0'},
 {c_code:'C3',brand:'C',package:'0603',param_detail:'容量: 10pF; 精度: ±5%',stock:'20'},
 {c_code:'C4',brand:'A',package:'0805',param_detail:'容量: 10pF; 精度: ±5%',stock:'20'},
 {c_code:'C5',brand:'A',package:'0603',param_detail:'容量: 22pF; 精度: ±5%',stock:'20'}];
const before = JSON.stringify(items);
const selected = new Map([['brand',new Set(['A','B'])],['package',new Set(['0603'])],['param:容值',new Set(['10pF'])]]);
const entries = candidateResultEntries(items);
assert.deepEqual(filterCandidateEntries(entries,selected,false).map(e=>e.item.c_code),['C1','C2']);
assert.deepEqual(filterCandidateEntries(entries,selected,true).map(e=>e.item.c_code),['C1']);
assert.equal(JSON.stringify(items),before);
assert.equal(selected.get('brand').size,2);
''')

    def test_partner_quote_filters_use_the_quote_brand_package_parameters_and_stock(self):
        self.run_js(r'''
const entries = candidateResultEntries([{c_code:'C1',brand:'Main',package:'0603',stock:'0',
 param_detail:'精度: ±5%',order_channels:[
 {url:'https://example.com/one',brand:'Partner',package:'0805',param_detail:'精度: ±1%',stock:'1.2K'},
 {url:'https://example.com/two',brand:'Other',stock:'0'},
 {url:'https://example.com/one',brand:'Partner',stock:'1200'}]}]);
assert.equal(entries.length,3);
const selected = new Map([['brand',new Set(['Partner'])],['package',new Set(['0805'])],['param:精度',new Set(['±1%'])]]);
const matches = filterCandidateEntries(entries,selected,true);
assert.equal(matches.length,1);
assert.equal(matches[0].channel.url,'https://example.com/one');
assert.equal(candidateEntryStock({item:{stock:'待核实'}}),0);
assert.equal(candidateEntryStock({item:{stock:'26K'}}),26000);
assert.equal(candidateEntryStock({item:{stock:'1,784'}}),1784);
assert.equal(candidateEntryStock({item:{stock:0}}),0);
''')

    def test_facets_keep_selected_zero_count_and_count_against_other_fields(self):
        self.run_js(r'''
const entries = candidateResultEntries([
 {brand:'A',package:'0603',param_detail:'精度: ±5%',stock:1},
 {brand:'B',package:'0805',param_detail:'精度: ±1%',stock:1}]);
const selected = new Map([['brand',new Set(['A'])],['param:精度',new Set(['±1%'])]]);
const facets = buildCandidateFacets(entries,selected,false);
assert.equal(facets.get('brand').get('A'),0);
assert.equal(facets.get('brand').get('B'),1);
assert.equal(facets.get('param:精度').get('±5%'),1);
assert.equal(facets.get('param:精度').get('±1%'),0);
const missing = buildCandidateFacets([],selected,false);
assert.equal(missing.get('brand').get('A'),0);
assert.equal(missing.get('param:精度').get('±1%'),0);
''')

    def test_parameter_values_are_exact_missing_is_not_a_match_and_units_are_case_sensitive(self):
        self.run_js(r'''
const entries = candidateResultEntries([
 {param_detail:'功率: 1mW; 阻值: 50kΩ'},
 {param_detail:'功率: 1MW; 阻值: 50kΩ'},
 {param_detail:'描述: 50kΩ'},
 {param_detail:'功率: 1mW; 阻值: 50kΩ~100kΩ'}]);
const selected = new Map([['param:功率',new Set(['1mW'])],['param:阻值',new Set(['50kΩ'])]]);
assert.deepEqual(filterCandidateEntries(entries,selected,false),[entries[0]]);
''')


if __name__ == "__main__":
    unittest.main()

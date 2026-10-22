import json
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
from part_stock_lookup import build_candidate_item, candidate_stock_result, order_channel_from_lcbook


class CandidateFiltersTest(unittest.TestCase):
    def setUp(self):
        self.html = render_index(SimpleNamespace(project_root=ROOT, config={}))

    def run_js(self, assertions):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for candidate filter checks")
        block = re.search(r'function normalizedCandidateParam\(.*?(?=function renderCandidateFilterPanel\()', self.html, re.S)
        self.assertIsNotNone(block, "Candidate filter helpers are missing")
        script = "const assert = require('node:assert/strict');\n" + block.group() + r'''
const esc = value => String(value ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const candidateRows = {innerHTML:''};
let currentCandidateItems, currentProcurementQuotes;
const candidateRawItems = [], candidateTargetRow = {};
let targetRow = {};
const rowDataForElement = () => targetRow;
const currentLcscCode = () => '';
const normalizeCCode = value => value || '';
const alternateCodes = () => [];
const applyCandidateColumnOrder = () => {};
const applyCandidateSavedWidths = () => {};
const makeCandidateColumnsConfigurable = () => {};
const fitDefaultColumnWidths = () => {};
''' + assertions
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

    def test_resistor_structured_aliases_reach_details_and_filters_without_defaults(self):
        item = build_candidate_item("100k 0603", {
            "productCode": "C123", "productModel": "RC0603FR-07100KL",
            "paramPOList": [
                {"paramName": "Resistance", "paramValue": "100kOhm"},
                {"paramName": "Temperature Coefficient", "paramValue": "±100ppm/℃"},
                {"paramName": "Working Voltage", "paramValue": "75V"},
            ],
        })
        self.assertIn("温度系数: ±100ppm/℃", item["param_detail"])
        self.assertIn("工作电压: 75V", item["param_detail"])
        missing = build_candidate_item("100k 0603", {"productModel": "RC0603FR-07100KL"})
        self.assertEqual(missing["param_detail"], "")
        self.run_js("const items = " + json.dumps([item, missing]) + r''';
const entries = candidateResultEntries(items);
const filters = new Map([['param:温度系数',new Set(['±100ppm/°C'])],['param:工作电压',new Set(['75V'])]]);
assert.deepEqual(filterCandidateEntries(entries,filters,false), [entries[0]]);
assert.ok(!candidateEntryFields(entries[1]).has('param:工作电压'));
''')

    def test_resistor_alias_filters_respect_partner_overrides_and_rated_voltage(self):
        self.run_js(r'''
const entries = candidateResultEntries([{param_detail:'温度系数: ±100ppm/°C; 工作电压: 75V; 额定电压: 50V',
 order_channels:[{url:'https://example.com/quote',
 param_detail:'Temperature Coefficient: ±50ppm/°C; Working Voltage: 100V'}]}]);
const filters = new Map([['param:温度系数',new Set(['±50ppm/°C'])],['param:工作电压',new Set(['100V'])]]);
assert.deepEqual(filterCandidateEntries(entries,filters,false), [entries[1]]);
const fields = candidateEntryFields(entries[1]);
assert.ok(!fields.has('param:额定电压'));
assert.ok(!fields.get('param:工作电压').has('75V'));
''')

    def test_resistor_structured_labels_with_units_keep_source_values(self):
        for source_key in ("paramVOList", "paramPOList"):
            with self.subTest(source_key=source_key):
                item = build_candidate_item("100k 0603", {source_key: [
                    {"name": "温度系数(ppm/℃)", "value": "±50"},
                    {"name": "工作电压(V)", "value": "100"},
                    {"name": "额定电压", "value": "50V"},
                ]})
                self.assertEqual(item["param_detail"], "温度系数(ppm/℃): ±50; 工作电压(V): 100; 额定电压: 50V")
        self.run_js(r'''
const fields = candidateEntryFields({item:{param_detail:
 'Temperature Coefficient of Resistance (ppm/°C): ±50; Operating Voltage (V): 100'}});
assert.deepEqual([...fields.get('param:温度系数')], ['±50ppm/°C']);
assert.deepEqual([...fields.get('param:工作电压')], ['100V']);
''')

    def test_maximum_working_voltage_aliases_filter_without_rewriting_details(self):
        items = []
        for label in ("最大工作电压", "Maximum Working Voltage", "Max Working Voltage"):
            item = build_candidate_item("100k 0603", {"paramVOList": [
                {"paramName": label, "paramValue": "75V"},
            ]})
            self.assertEqual(item["param_detail"], label + ": 75V")
            items.append(item)
        items.append({"param_detail": "额定电压: 75V"})
        self.run_js("const items = " + json.dumps(items) + r''';
const entries = candidateResultEntries(items);
const filters = new Map([['param:工作电压',new Set(['75V'])]]);
assert.deepEqual(filterCandidateEntries(entries,filters,false), entries.slice(0,3));
assert.ok(!candidateEntryFields(entries[3]).has('param:工作电压'));
assert.deepEqual([...candidateEntryFields(entries[3]).get('param:额定电压')], ['75V']);
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

    def test_all_structured_lists_and_zero_values_are_retained_without_summary_truncation(self):
        specs = [{"name": "Vendor field " + str(i), "value": "value " + str(i)} for i in range(12)]
        specs += [{"name": "Notes", "value": "A; B: C"}, {"name": "Zero", "value": 0}]
        item = build_candidate_item("synthetic", {
            "catalogName": "功率电感", "paramVOList": specs,
            "paramPOList": [{"paramName": "DCR", "paramValue": "42mΩ"}],
        })
        self.assertIn("Zero: 0", item["param_detail"])
        self.assertIn("DCR: 42mΩ", item["param_detail"])
        self.assertEqual(len(item["parameters"]), 15)
        self.run_js("const item = " + json.dumps(item) + r''';
const fields = candidateEntryFields({item});
assert.deepEqual([...fields.get('param:Notes')], ['A; B: C']);
renderCandidateRows([item]);
assert.ok(candidateRows.innerHTML.includes('Vendor field 11'));
assert.ok(candidateRows.innerHTML.includes('A; B: C'));
''')

    def test_required_fields_show_missing_for_source_category_or_explicit_target_kind(self):
        self.run_js(r'''
for (const item of [{category:'贴片电阻',model:'synthetic'}, {category:'Chip Resistors - Surface Mount'}]) {
  renderCandidateRows([item]);
  assert.match(candidateRows.innerHTML, /温度系数:<\/span><span[^>]*>详情尚未读取/);
  assert.match(candidateRows.innerHTML, /工作电压:<\/span><span[^>]*>详情尚未读取/);
  assert.ok(!candidateRows.innerHTML.includes('直流电阻(DCR)'));
}
targetRow = {kind:'inductor',refs:'U1'};
renderCandidateRows([{param_detail:'感值: 10uH'}]);
assert.match(candidateRows.innerHTML, /直流电阻\(DCR\):<\/span><span[^>]*>详情尚未读取/);
assert.match(candidateRows.innerHTML, /电感类型:<\/span><span[^>]*>详情尚未读取/);
targetRow = {refs:'U1'};
renderCandidateRows([{category:'Microcontrollers',param_detail:'频率: 48MHz'}]);
assert.ok(!candidateRows.innerHTML.includes('未提供'));
targetRow = {kind:'resistor'};
renderCandidateRows([{category:'Power Inductors'}]);
assert.ok(candidateRows.innerHTML.includes('电感类型'));
assert.ok(!candidateRows.innerHTML.includes('工作电压'));
''')

    def test_missing_markers_do_not_become_known_facets_or_matches(self):
        self.run_js(r'''
const markers = ['未提供','待核实','待确认','未知','N/A','not specified','--','-',''];
const entries = candidateResultEntries(markers.map(value=>({param_detail:'工作电压: '+value})));
for (const entry of entries) assert.ok(!candidateEntryFields(entry).has('param:工作电压'));
assert.ok(!buildCandidateFacets(entries,new Map(),false).has('param:工作电压'));
assert.equal(filterCandidateEntries(entries,new Map([['param:工作电压',new Set(['未提供'])]]),false).length,0);
renderCandidateRows([{category:'Resistors',param_detail:'Working Voltage: N/A'}]);
assert.match(candidateRows.innerHTML, /工作电压:<\/span><span[^>]*>详情尚未读取/);
''')

    def test_inductor_aliases_preserve_units_and_type_without_guessing(self):
        self.run_js(r'''
for (const label of ['DCR','DC Resistance','直流电阻','直流电阻(DCR)']) {
  const entry = {item:{category:'电感',param_detail:label+': 42mΩ; Inductor Type: Shielded; Vendor Extra: literal'}};
  const fields = candidateEntryFields(entry);
  assert.deepEqual([...fields.get('param:直流电阻(DCR)')],['42mΩ']);
  assert.deepEqual([...fields.get('param:电感类型')],['Shielded']);
  renderCandidateRows([entry.item]);
  assert.ok(candidateRows.innerHTML.includes('Shielded'));
  assert.ok(candidateRows.innerHTML.includes('Vendor Extra'));
}
const fields = candidateEntryFields({item:{param_detail:'DC Resistance (mΩ): 42; Inductor Type: Wirewound'}});
assert.deepEqual([...fields.get('param:直流电阻(DCR)')], ['42mΩ']);
''')

    def test_supplier_parameters_never_inherit_parent_ratings(self):
        parent = {"catalogName": "贴片电阻", "productKeyAttributes": "Working Voltage: 75V",
                  "paramVOList": [{"name": "温度系数", "value": "±100ppm/℃"}]}
        quote = order_channel_from_lcbook({"provider": "Synthetic", "selfProductIntro": "",
                                          "paramPOList": [{"name": "Working Voltage", "value": "100V"}]}, parent)
        self.assertEqual(quote["param"], "")
        self.assertIn("100V", quote["param_detail"])
        self.assertNotIn("100ppm", quote["param_detail"])
        self.run_js(r'''
const item = {category:'贴片电阻',param_detail:'工作电压: 75V; 温度系数: ±100ppm/°C',order_channels:[
 {url:'https://example.com/a',param_detail:'Working Voltage: 100V'},
 {url:'https://example.com/b',param_detail:''}]};
const entries = candidateResultEntries([item]);
assert.ok(!candidateEntryFields(entries[1]).has('param:温度系数'));
assert.ok(!candidateEntryFields(entries[2]).has('param:工作电压'));
const html = renderCandidatePartnerRows([item]);
assert.ok(!html.includes('75V'));
assert.ok(!html.includes('100ppm'));
assert.ok(html.includes('100V'));
assert.match(html, /温度系数:<\/span><span[^>]*>渠道未返回/);
''')

    def test_category_scoped_type_alias_and_dcr_unit_labels(self):
        self.run_js(r'''
const item = {category:'功率电感',param_detail:'直流电阻(DCR)(mΩ): 42; 类型: 绕线; 额定电流: 2A'};
const fields = candidateEntryFields({item});
assert.deepEqual([...(fields.get('param:直流电阻(DCR)') || [])], ['42mΩ']);
assert.deepEqual([...(fields.get('param:电感类型') || [])], ['绕线']);
renderCandidateRows([item]);
assert.ok(!candidateRows.innerHTML.includes('未提供'));
assert.ok(candidateRows.innerHTML.includes('额定电流'));
const icFields = candidateEntryFields({item:{category:'Microcontrollers',param_detail:'Type: MCU'}});
assert.deepEqual([...icFields.get('param:Type')], ['MCU']);
assert.ok(!icFields.has('param:电感类型'));
targetRow = {comment:'inductor',refs:'U1'};
renderCandidateRows([{param_detail:'Inductance: 10uH; Type: Wirewound'}]);
assert.match(candidateRows.innerHTML, /电感类型:<\/span><span[^>]*>Wirewound/);
''')

    def test_structured_empty_values_override_summary_and_preserve_vendor_labels(self):
        item = build_candidate_item("synthetic", {"catalogName": "Resistors", "paramVOList": [
            {"name": "Working Voltage", "value": ""},
            {"name": "Vendor Note", "value": "<safe>; Alpha: Beta"},
        ], "productKeyAttributes": "Working Voltage: 75V"})
        self.run_js("const item = " + json.dumps(item) + r''';
renderCandidateRows([item]);
assert.match(candidateRows.innerHTML, /工作电压:<\/span><span[^>]*>详情尚未读取/);
assert.ok(candidateRows.innerHTML.includes('&lt;safe&gt;; Alpha: Beta'));
assert.ok(!candidateRows.innerHTML.includes('75V'));
assert.ok(!candidateEntryFields({item}).has('param:工作电压'));
const facets = buildCandidateFacets([{item}],new Map([['param:工作电压',new Set(['未提供'])]]),false);
assert.ok(!facets.get('param:工作电压')?.has('未提供'));
''')

    def test_official_bilingual_parameter_fields_use_source_names_and_never_search_sort_values(self):
        item = build_candidate_item("synthetic", {"catalogName": "Power Inductors", "paramVOList": [
            {"paramName": None, "paramNameEn": "DC Resistance(DCR)", "paramValueEn": "31mΩ", "paramValueEnForSearch": 0.031},
            {"paramNameEn": "Type", "paramValueEn": "Molded Inductor"},
            {"paramName": "测试属性", "paramNameEn": "Test", "paramValue": "原值", "paramValueEn": "translated"},
        ]})
        self.assertIn("DC Resistance(DCR): 31mΩ", item["param_detail"])
        self.assertIn("测试属性: 原值", item["param_detail"])
        self.assertNotIn("0.031", item["param_detail"])
        self.run_js("const item = " + json.dumps(item) + r''';
renderCandidateRows([item]);
assert.match(candidateRows.innerHTML, /直流电阻\(DCR\):<\/span><span[^>]*>31mΩ/);
assert.match(candidateRows.innerHTML, /电感类型:<\/span><span[^>]*>Molded Inductor/);
assert.ok(!candidateRows.innerHTML.includes('未提供'));
''')

    def test_rendered_missing_values_do_not_mutate_or_export_as_ratings(self):
        item = build_candidate_item("synthetic", {"catalogName": "Resistors"})
        self.assertEqual(candidate_stock_result(item)["param_detail"], "")
        self.assertNotIn("未提供", json.dumps(candidate_stock_result(item), ensure_ascii=False))
        self.run_js("const item = " + json.dumps(item) + r''';
const before = JSON.stringify(item);
renderCandidateRows([item]);
assert.ok(candidateRows.innerHTML.includes('详情尚未读取'));
assert.equal(JSON.stringify(item), before);
assert.equal(candidateEntryFields({item}).size, 0);
''')


    def test_detail_statuses_distinguish_unread_failure_and_authoritative_absence(self):
        self.run_js(r'''
for (const [status,label] of [['pending','正在补取详情'],['failed','详情读取失败'],['ready','详情页未列出']]) {
  renderCandidateRows([{c_code:'C101',category:'贴片电阻',model:'SYNTHETIC',detail_parameter_status:status}]);
  assert.ok(candidateRows.innerHTML.includes(label));
  assert.ok(candidateRows.innerHTML.includes('参数来源'));
  assert.ok(!candidateRows.innerHTML.includes('未提供'));
}
''')


if __name__ == "__main__":
    unittest.main()

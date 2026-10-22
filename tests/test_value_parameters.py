"""Selective Value composition uses synthetic electrical parameters only."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for Value parameter composition')
    result = subprocess.run([node, '-e', "const assert=require('node:assert/strict');const vp=require("
        + json.dumps(str(ROOT / 'tools/web/value-parameters.js')) + ');\n' + script],
        capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr


def test_resistor_preselection_and_selective_replacement_without_duplicates():
    run_js(r'''
const pairs=[['阻值','5.1kΩ'],['温度系数','±5ppm/℃'],['精度','±0.1%'],['工作电压','100V'],['功率','100mW']]
 .map(([key,value])=>({key,value}));
const before=JSON.stringify(pairs), original='5.1k, 5ppm';
const model=vp.buildModel('resistor',original,pairs);
assert.deepEqual(model.options.filter(o=>o.selected).map(o=>o.key),['nominal','tcr']);
assert.equal(vp.compose(model,['nominal','tcr']),'5.1kΩ, ±5ppm/℃');
assert.equal(vp.compose(model,['nominal','tcr','tolerance']),'5.1kΩ, ±5ppm/℃, ±0.1%');
assert.equal(vp.compose(vp.buildModel('resistor','100kΩ, ±1%, 10ppm, 5ppm, 特殊低噪声',pairs),
 ['tcr','tolerance']),'100kΩ, ±0.1%, ±5ppm/℃, 特殊低噪声');
assert.equal(vp.compose(model,[]),original);
assert.equal(JSON.stringify(pairs),before);
''')


def test_capacitor_equivalent_spelling_and_special_requirements_are_preserved():
    run_js(r'''
const pairs=[{key:'容值',value:'100nF'},{key:'额定电压',value:'50V'},{key:'温度系数',value:'X7R'}];
let model=vp.buildModel('capacitor','0.1µF 250V C0G, 高压专用',pairs);
assert.deepEqual(model.options.filter(o=>o.selected).map(o=>o.key),['nominal','voltage','dielectric']);
assert.equal(vp.compose(model,['nominal']),'100nF, 250V, C0G, 高压专用');
assert.equal(vp.compose(model,['nominal','voltage','dielectric']),'100nF, 50V, X7R, 高压专用');
assert.equal(vp.compose(vp.buildModel('capacitor','100nF, 50V min',pairs),['nominal']), '100nF, 50V min');
''')


def test_inductor_current_dcr_and_type_are_labeled():
    run_js(r'''
const pairs=[['感值','10uH'],['温升电流','2A'],['饱和电流(Isat)','3A'],['直流电阻(DCR)','50mΩ'],['电感类型','屏蔽电感']]
 .map(([key,value])=>({key,value}));
const model=vp.buildModel('inductor','10uH, Rated Current: 1A, Saturation Current: not specified, DCR: 80mΩ, Type: 非屏蔽',pairs);
assert.equal(vp.compose(model,model.options.filter(o=>o.available).map(o=>o.key)),
 '10uH, RMS Current: 2A, Sat Current: 3A, DCR: 50mΩ, Type: 屏蔽电感');
''')


def test_missing_conflicting_and_non_electrical_properties_are_not_copyable():
    run_js(r'''
const model=vp.buildModel('resistor','10k, ±1%',[
 {key:'阻值',value:'10kΩ'},{key:'阻值',value:'20kΩ'},{key:'温度系数',value:'未提供'},
 {key:'精度',value:'±1%'},{key:'型号',value:'PART'},{key:'品牌',value:'BRAND'}]);
assert(!model.options.find(o=>o.key==='nominal').available);
assert.match(model.options.find(o=>o.key==='nominal').reason,/冲突/);
assert(!model.options.find(o=>o.key==='tcr').available);
assert(!model.options.some(o=>['型号','品牌'].includes(o.key)));
assert.equal(vp.compose(model,['nominal','tcr']),'10k, ±1%');
assert.equal(vp.buildModel('unknown','PART',[]).options.length,0);
''')


def test_only_the_current_selected_code_can_supply_parameters():
    run_js(r'''
const row={stock:{c_code:'C101',param_detail:'阻值: 10kΩ'}};
assert.equal(vp.selectedSource(row,'C101').param_detail,'阻值: 10kΩ');
assert.throws(()=>vp.selectedSource(row,'C202'),/当前/);
assert.throws(()=>vp.selectedSource({...row,selection_codes_mixed:true},'C101'),/混合/);
assert.throws(()=>vp.selectedSource(row,''),/C 编码/);
row.override={field_overrides:{description:'阻值: 20kΩ',description_c_code:'C202'}};
assert.equal(vp.selectedSource(row,'C101').param_detail,'阻值: 10kΩ');
assert.equal(vp.selectedSource(row,'C202').param,'阻值: 20kΩ');
assert.equal(vp.selectedSource(row,'C202').parameters,undefined);
''')


def test_units_in_labels_are_retained_and_unknown_placeholders_disabled():
    run_js(r'''
const model=vp.buildModel('inductor','10uH',[
 {key:'感值(uH)',value:'10'},{key:'温升电流(A)',value:'2'},
 {key:'饱和电流(Isat)(mA)',value:'3000'},{key:'直流电阻(DCR)',value:'渠道未返回'}]);
assert.equal(vp.compose(model,['nominal','rms','sat','dcr']),
 '10uH, RMS Current: 2A, Sat Current: 3000mA');
assert(!model.options.find(o=>o.key==='dcr').available);
const resistor=vp.buildModel('resistor','100k',[
 {key:'温度系数(ppm/℃)',value:'±5'},{key:'精度(%)',value:'±0.1'}]);
assert.equal(vp.compose(resistor,['tcr','tolerance']),'100k, ±0.1%, ±5ppm/℃');
assert.equal(vp.compose(vp.buildModel('capacitor','100nF 250V min',[{key:'容值',value:'100nF'}]),['nominal']),
 '100nF, 250V min');
''')


def test_picker_is_packaged_and_integrated_with_normal_row_save():
    from kicad_plugin.package_plugin import CORE_FILES
    from part_selection_server import render_index
    from types import SimpleNamespace
    html = render_index(SimpleNamespace(project_root=ROOT, config={}))
    assert 'web/value-parameters.js' in CORE_FILES
    assert 'web/value-parameters.css' in CORE_FILES
    assert 'data-action="value-parameters"' in html
    assert 'toolbar-icon-arrow-left' in html
    assert 'window.openValueParameters(tr.dataset.refs)' in html
    assert 'queueRowInput(input);\n      await flushRowEdits();' in html


def test_shared_parameter_parser_retains_capacitance_and_tcr_units():
    import re
    from types import SimpleNamespace
    from part_selection_server import render_index
    html = render_index(SimpleNamespace(project_root=ROOT, config={}))
    functions = re.search(r'function candidateKnownParamValue\(.*?(?=function candidateEntryFields\()', html, re.S).group()
    run_js(functions + r'''
const pairs=candidateParameterPairs({parameters:[
 {name:'电容量(uF)',value:'0.1'}, {name:'TCR(ppm/℃)',value:'±5'}]},'capacitor');
assert.equal(pairs[0].value,'0.1uF');
assert.equal(pairs[1].value,'±5ppm/℃');
''')


def test_engineering_nominal_notation_is_replaced_not_duplicated():
    run_js(r'''
for(const original of ['100n','4n7','0.1µF','100nF','100纳法','0.1微法拉']) {
 const model=vp.buildModel('capacitor',original,[{key:'容值',value:'100nF'}]);
 assert(model.options.find(o=>o.key==='nominal').selected,original);
 assert.equal(vp.compose(model,['nominal']),'100nF');
}
for(const original of ['100K','4k7','100','100R','5.1kΩ','100千欧姆']) {
 const model=vp.buildModel('resistor',original,[{key:'阻值',value:'100kΩ'}]);
 assert.equal(vp.compose(model,['nominal']),'100kΩ');
}
''')

"""Main BOM facets use grouped rows and browser UI settings, never CAD records."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'tools/web/row-filters.js'


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for row-filter behavior checks')
    result = subprocess.run(
        [node, '-e', "const assert = require('node:assert/strict');\n"
         + 'const filters = require(' + json.dumps(str(MODULE)) + ');\n' + script],
        capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr


def rendered():
    sys.path.insert(0, str(ROOT / 'tools'))
    from part_selection_server import render_index
    return render_index(SimpleNamespace(project_root=ROOT, config={}))


def function(html, name):
    return re.search(r'function ' + name + r'\(.*?(?=\nfunction |\ndocument\.)', html, re.S).group()


def test_default_all_and_or_within_fields_and_across_fields_without_mutation():
    run_js(r'''
const rows = [
 {comment:'capacitor', value:'10pF', footprint:'Library:0603'},
 {comment:'capacitor', value:'22pF', footprint:'Library:0603'},
 {comment:'resistor', value:'10pF', footprint:'Library:0603'},
 {comment:'capacitor', value:'10pF', footprint:'Library:0805'}];
const before = JSON.stringify(rows);
const selected = filters.restoreRowFilters({comment:['capacitor'],value:['10pF','22pF'],footprint:['Library:0603']});
assert.deepEqual(rows.filter(row=>filters.rowMatchesFilters(row,new Map())),rows);
assert.deepEqual(rows.filter(row=>filters.rowMatchesFilters(row,selected)),rows.slice(0,2));
assert.equal(selected.get('value').size,2);
assert.equal(JSON.stringify(rows),before);
''')


def test_page_filter_keeps_entire_group_and_counts_duplicate_pages_once():
    run_js(r'''
const group = {refs_list:['R1','R2','R3'],quantity:3,required_quantity:12,
 page_refs:'untrusted formatted summary',ref_links:[
 {ref:'R1',page:'/Power'}, {ref:'R2',page:'/Power'}, {ref:'R3',page:'/Logic'}]};
const before = JSON.stringify(group);
const selected = filters.restoreRowFilters({page_refs:['/Logic']});
assert.equal(filters.rowMatchesFilters(group,selected),true);
assert.deepEqual(filters.rowFilterValues(group,'page_refs'),['/Power','/Logic']);
assert.deepEqual([...filters.rowFilterFacets([group],selected,'page_refs')],[['/Logic',1],['/Power',1]]);
assert.deepEqual([group].filter(row=>filters.rowMatchesFilters(row,selected)),[group]);
assert.equal(JSON.stringify(group),before);
assert.equal(filters.rowMatchesFilters(group,filters.restoreRowFilters({page_refs:['R3']})),false);
assert.deepEqual(filters.rowFilterValues({page:'/Legacy'},'page_refs'),['/Legacy']);
assert.deepEqual(filters.rowFilterValues({},'page_refs'),['']);
''')


def test_exact_values_preserve_unit_case_and_blank_is_selectable():
    run_js(r'''
const selected = filters.restoreRowFilters({value:['1mW'],footprint:['']});
assert.equal(filters.rowMatchesFilters({value:'1mW',footprint:'  '},selected),true);
assert.equal(filters.rowMatchesFilters({value:'1MW'},selected),false);
assert.equal(filters.rowMatchesFilters({value:'1mW~2mW'},selected),false);
assert.deepEqual(filters.rowFilterValues({comment:' <img onerror=alert(1)> '},'comment'),['<img onerror=alert(1)>']);
assert.deepEqual(filters.rowFilterValues({},'not_a_field'),[]);
''')


def test_option_search_is_immediate_case_insensitive_and_keeps_counts():
    run_js(r'''
const facets = new Map([['CAPACITOR',2],['resistor',3],['',1]]);
assert.deepEqual(filters.searchRowFilterOptions(facets,' cap '),[['CAPACITOR',2]]);
assert.deepEqual(filters.searchRowFilterOptions(facets,'no-match'),[]);
assert.deepEqual(filters.searchRowFilterOptions(facets,''),[...facets]);
assert.equal(facets.size,3);
''')


def test_facets_ignore_their_own_selection_and_keep_stale_zero_count_values():
    run_js(r'''
const rows = [{comment:'A',value:'10pF'}, {comment:'B',value:'22pF'}, {comment:'C',value:'10pF'}];
const selected = filters.restoreRowFilters({comment:['A','gone'],value:['22pF']});
const counts = filters.rowFilterFacets(rows,selected,'comment');
assert.equal(counts.get('A'),0);
assert.equal(counts.get('B'),1);
assert.equal(counts.get('C'),0);
assert.equal(counts.get('gone'),0);
assert.deepEqual([...filters.rowFilterFacets([],selected,'comment')],[['A',0],['gone',0]]);
assert.equal(filters.rowFilterFacets(rows,selected,'value').get('10pF'),1);
''')


def test_settings_round_trip_is_project_scoped_and_preserves_other_settings():
    run_js(r'''
const a = 'E:\\projects\\alpha', b = 'E:\\projects\\beta';
let settings = {columnWidths:{value:123},rowFiltersByProject:{[b]:{value:['22pF']}}};
const before = JSON.stringify(settings);
const selected = filters.restoreRowFilters({comment:['A'],value:['22pF','10pF']});
settings = {...settings,...filters.rowFilterSettingsUpdate(settings,a,selected)};
assert.deepEqual(settings.columnWidths,{value:123});
assert.deepEqual(filters.serializeRowFilters(filters.projectRowFilters(settings,a)),{comment:['A'],value:['10pF','22pF']});
assert.deepEqual(filters.serializeRowFilters(filters.projectRowFilters(settings,b)),{value:['22pF']});
assert.equal(filters.projectRowFilters(settings,'other').size,0);
assert.deepEqual(JSON.parse(before).rowFiltersByProject[b],settings.rowFiltersByProject[b]);
settings = {...settings,...filters.rowFilterSettingsUpdate(settings,a,new Map())};
assert.equal(filters.projectRowFilters(settings,a).size,0);
assert.equal(filters.projectRowFilters(settings,b).get('value').size,1);
''')


def test_corrupt_settings_and_unknown_fields_are_ignored():
    run_js(r'''
for (const saved of [null,0,'bad',[],{value:'text'},{value:[null,{},22],unknown:['x']}]) {
 assert.equal(filters.restoreRowFilters(saved).size,0);
}
assert.deepEqual(filters.serializeRowFilters(filters.restoreRowFilters({value:[' A ','A',''],comment:[]})),{value:['','A']});
assert.equal(filters.projectRowFilters({rowFiltersByProject:{}},'toString').size,0);
const map = new Map([['value',new Set(['B','A'])],['comment',new Set(['x'])]]);
assert.equal(JSON.stringify(filters.serializeRowFilters(map)),JSON.stringify(filters.serializeRowFilters(new Map([...map].reverse()))));
''')


@pytest.mark.parametrize('with_module', [False, True])
def test_real_filter_loop_combines_status_search_and_columns_and_keeps_totals(with_module):
    html = rendered()
    script = r'''
const selected = filters.restoreRowFilters({value:['10pF'],page_refs:['/Logic']});
const currentRows = [
 {ref:'R1',comment:'A',value:'10pF',footprint:'0603',quantity:3,ref_links:[{page:'/Power'},{page:'/Logic'}]},
 {ref:'R4',comment:'A',value:'22pF',footprint:'0603',quantity:1,ref_links:[{page:'/Logic'}]},
 {ref:'R5',comment:'B',value:'10pF',footprint:'0603',quantity:1,ref_links:[{page:'/Logic'}]}];
const original = JSON.stringify(currentRows);
const elements = currentRows.map((row,i)=>({row,dataset:{filterKey:i===2?'done':'pending'},
 classList:{toggle:(_name,hidden)=>{elements[i].hidden=hidden;}}}));
const rowsEl = {querySelectorAll:()=>elements};
const rowDataForElement = tr=>tr.row, fieldValue = (tr,key)=>tr.row[key], currentLcscCode=()=>'';
const activeFilters = new Map([['review','pending']]), bomSearch={value:'A'}, bomSelect={value:'schematic'}, refsInput={value:''};
const bomRowMatchesSearch = (row,term)=>row.comment.includes(term);
const bomSearchCount={}, updateRowsEmptyState=()=>{}, updateSyncSelection=()=>{}, updateWorkbenchSummary=()=>{};
const sessionStorage={getItem:()=>null}, window={scrollTo:()=>{}}, rememberFilterScroll=()=>{};
const FILTER_SCROLL_STORAGE_PREFIX='test';
let activeFilterViewKey='', refreshed;
MODULE_SETUP
FUNCTIONS
const beforeKey=filterViewKey();
applyActiveFilter();
assert.equal(JSON.stringify(currentRows),original);
assert.equal(currentRows.reduce((total,row)=>total+row.quantity,0),5);
assert.equal(bomSearchCount.textContent,EXPECTED_COUNT);
assert.deepEqual(elements.map(tr=>tr.hidden),EXPECTED_HIDDEN);
MODULE_ASSERTIONS
'''
    setup = "window.workbenchRowFilters={key:()=>filters.serializeRowFilters(selected),matches:row=>filters.rowMatchesFilters(row,selected),refresh:rows=>{refreshed=rows;}};"
    script = script.replace('MODULE_SETUP', setup if with_module else '')
    script = script.replace('FUNCTIONS', function(html, 'filterViewKey') + function(html, 'applyActiveFilter'))
    script = script.replace('EXPECTED_COUNT', json.dumps('1 / 3 行' if with_module else '2 / 3 行'))
    script = script.replace('EXPECTED_HIDDEN', '[false,true,true]' if with_module else '[false,false,true]')
    script = script.replace('MODULE_ASSERTIONS', r'''
assert.deepEqual(refreshed.map(row=>row.ref),['R1','R4']);
selected.set('comment',new Set(['no-match']));
assert.notEqual(filterViewKey(),beforeKey);
''' if with_module else '')
    run_js(script)


def test_assets_initialize_before_async_load_and_reset_and_empty_state_are_integrated():
    html = rendered()
    assert html.index('window.workbenchRowFilters = window.RowFilters?.createRowFilters') < html.index('if (!candidateOnly && !toolOnly) initializeWorkbenchRows();')
    assert 'if (!candidateOnly && !toolOnly) window.workbenchRowFilters' in html
    assert 'window.workbenchRowFilters?.reset();' in html
    assert 'window.workbenchRowFilters?.active()' in function(html, 'updateRowsEmptyState')
    source = MODULE.read_text(encoding='utf-8')
    assert 'innerHTML' not in source
    assert 'fetch(' not in source
    assert 'optionSearch.addEventListener(\'input\', renderOptions)' in source
    assert 'aria-label' in source and 'aria-expanded' in source

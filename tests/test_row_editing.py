import re
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from part_selection_server import render_index
from _scratch import temporary_project


@unittest.skipUnless(shutil.which('node'), 'Node required')
class RowEditingTests(unittest.TestCase):
    def run_js(self, script):
        result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_selected_pages_and_refs_split_into_one_scoped_row(self):
        html = render_index(SimpleNamespace(config={}, project_root=ROOT))
        functions = re.search(r'function refButton\(.*?(?=function stockCode\()', html, re.S).group()
        self.run_js('''
const assert = require('node:assert/strict');
let currentRows, multiSplitGroupId = 'G1', multiSplitSource = 'page', multiSplitRefs = new Set();
const esc = String, renderRows = rows => { currentRows = rows; }, setState = () => {};
''' + functions + '''
const links = [{ref:'C1',page:'POWER'}, {ref:'C2',page:'POWER'},
               {ref:'C3',page:'ADC'}, {ref:'C4',page:'DAC'}];
const row = {group_id:'G1',ref:'C1',refs:'C1,C2,C3,C4',refs_list:links.map(x=>x.ref),
  ref_links:links, quantity:4, value:'100nF', field_changed_refs:'C1,C3',
  field_diffs:{value:{old:'0.1uF',new:'100nF',refs:'C1,C3'}},
  per_ref_sync_fields:{C1:{value:'100nF'},C2:{value:'100nF'},C3:{value:'100nF'},C4:{value:'100nF'}}};
currentRows = [row];
multiSplitRefs = new Set(['C1','C2','C3']);
const picker = renderMultiSplitPicker(row);
assert.match(picker, /data-multi-split-page="POWER"/);
assert.match(picker, /data-multi-split-ref="C3"/);
splitSelectedRefs('G1');
assert.equal(currentRows.length,2);
assert.equal(currentRows[0].refs,'C4');
assert.equal(currentRows[1].refs,'C1,C2,C3');
assert.equal(currentRows[1].quantity,3);
assert.deepEqual(Object.keys(currentRows[1].per_ref_sync_fields),['C1','C2','C3']);
assert.deepEqual(Object.keys(currentRows[0].per_ref_sync_fields),['C4']);
assert.equal(currentRows[1].field_changed_refs,'C1,C3');
assert.match(currentRows[1].page_refs,/POWER: C1,C2/);
currentRows = [rowForRefs(row,['C1','C2','C3','C4'])];
multiSplitRefs = new Set(['C1','C2','C3','C4']);
splitSelectedRefs(currentRows[0].group_id);
assert.equal(currentRows.length,1);
assert.equal(currentRows[0].quantity,4);
''')

    def test_autosave_debounces_serializes_and_preserves_newer_edits_and_failures(self):
        self.run_js('''
const assert = require('node:assert/strict');
const {createRowEditQueue} = require('./tools/web/row-edits.js');
(async () => {
  const writes = [], errors = [];
  let release;
  const q = createRowEditQueue({delay:5, save:async item => {
    writes.push(item.value);
    if (item.value==='first') await new Promise(r=>release=r);
    if (item.value==='fail') throw Error('disk full');
  }, onError:e=>errors.push(e.message)});
  q.schedule('value',{value:'100'});
  q.schedule('value',{value:'100nF'});
  await new Promise(r=>setTimeout(r,20));
  assert.deepEqual(writes,['100nF']);
  q.schedule('value',{value:'first'});
  const pending = q.flush();
  await Promise.resolve();
  q.schedule('value',{value:'newer'});
  release(); await pending;
  assert.deepEqual(writes,['100nF','first','newer']);
  q.pause('comment');
  q.schedule('note',{value:'memo'});
  await q.flush();
  assert.equal(writes.at(-1),'memo');
  q.schedule('value',{value:'fail'});
  q.schedule('independent',{value:'unrelated row'});
  await assert.rejects(q.flush(), /disk full/);
  assert.equal(writes.at(-1),'unrelated row');
  assert.equal(q.pending,true);
  q.schedule('value',{value:'retry'});
  await q.flush();
  assert.equal(q.pending,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')

    def test_saved_diffs_keep_per_reference_baselines_and_unrelated_changes(self):
        html = render_index(SimpleNamespace(config={}, project_root=ROOT))
        function = html.split('function updateSavedFieldDiffs', 1)[1].split('function markRowNeedsSchematicSync', 1)[0]
        function = 'function updateSavedFieldDiffs' + function
        self.run_js("const assert=require('node:assert/strict');" + function + r'''
const row={refs:'C1,C2',refs_list:['C1','C2'],value:'220nF',
 schematic_fields_by_ref:{C1:{value:'100nF',footprint:'Lib:0603'},C2:{value:'0.1uF',footprint:'Lib:0603'}},
 per_ref_sync_fields:{C1:{value:'220nF'},C2:{value:'220nF'}},
 field_diffs_by_ref:{C1:{footprint:{old:'Lib:0603',new:'Lib:0805'}}}};
updateSavedFieldDiffs(row,['value']);
assert.equal(row.field_diffs.value.old,'100nF; 0.1uF');
assert.equal(row.field_diffs_by_ref.C2.value.old,'0.1uF');
row.per_ref_sync_fields.C1.value='470nF';row.per_ref_sync_fields.C2.value='470nF';
updateSavedFieldDiffs(row,['value']);
assert.equal(row.field_diffs.value.old,'100nF; 0.1uF');
row.per_ref_sync_fields.C1.value='100nF';row.per_ref_sync_fields.C2.value='0.1uF';
updateSavedFieldDiffs(row,['value']);
assert.equal(row.field_diffs.value,undefined);
assert.equal(row.field_diffs_by_ref.C1.value,undefined);
assert.equal(row.field_changed_refs,'C1');
assert.equal(row.field_edit_status,'NeedsSchematicUpdate');
row.per_ref_sync_fields.C1.footprint='Lib:0603';
row.per_ref_sync_fields.C2.footprint='Lib:0603';
updateSavedFieldDiffs(row,['footprint']);
assert.equal(row.field_changed_refs,'');
row.missing_schematic_refs='C3';
updateSavedFieldDiffs(row,['value']);
assert.equal(row.field_changed_refs,'C3');
''')

    def test_partial_field_save_preserves_notes_and_other_fields(self):
        from part_selection_server import PartSelectionApp
        from part_selection_overrides import save_overrides, load_overrides
        with temporary_project() as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text(json.dumps({'output_overrides': 'records.json'}))
            app = PartSelectionApp(config)
            try:
                save_overrides(app.overrides_path, {
                    'C1': {'note': 'voltage checked', 'field_overrides': {'footprint': 'Lib:0603'}},
                    'C2': {'note': 'different note'}})
                app.save_field_override({'refs': 'C1,C2', 'field_overrides': {'value': '100nF'}})
                records = load_overrides(app.overrides_path)
                self.assertEqual(records['C1']['note'], 'voltage checked')
                self.assertEqual(records['C2']['note'], 'different note')
                self.assertEqual(records['C1']['field_overrides']['footprint'], 'Lib:0603')
                self.assertTrue(all(x['field_overrides']['value'] == '100nF' for x in records.values()))
            finally:
                app.search_jobs.close()
                app.selection_jobs.close()

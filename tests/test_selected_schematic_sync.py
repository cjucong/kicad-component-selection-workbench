import json
import re
import shutil
import subprocess
import sys
from _scratch import temporary_project
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from part_selection_server import PartSelectionApp, make_handler, render_index
from part_selection_helper import read_schematic_symbol_field_values
from part_selection_overrides import save_override


class SelectedSchematicSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_project()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "BOM").mkdir()
        (root / "DOC").mkdir()
        config = root / "BOM/config.json"
        config.write_text(json.dumps({"source_bom": "BOM/source.csv", "schematic_dir": "DOC",
                                      "output_overrides": "BOM/overrides.json"}), encoding="utf-8")
        (root / "BOM/source.csv").write_text(
            'Comment,Description,Designator,Footprint,Value\n'
            'Resistor,new,"R1,R2",0402,20k\nResistor,new,R3,0603,30k\n', encoding="utf-8")
        self.schematic = root / "DOC/test.kicad_sch"
        symbols = "".join(f'(symbol (lib_id "Device:R") (property "Reference" "{ref}" (at 0 0 0)) '
                          '(property "Comment" "Resistor" (at 0 0 0)) '
                          '(property "Value" "10k" (at 0 0 0)) '
                          '(property "Footprint" "0402" (at 0 0 0)))' for ref in ["R1", "R2", "R3"])
        self.schematic.write_text(f'(kicad_sch {symbols})', encoding="utf-8")
        self.app = PartSelectionApp(config)
        for ref, value in [("R1", "20k"), ("R2", "20k"), ("R3", "30k")]:
            save_override(self.app.overrides_path, ref, {"field_overrides": {"value": value}})

    def test_selected_refs_write_only_selected_symbols_even_within_group(self):
        result = self.app.bulk_sync_bom_state_to_schematic(
            "BOM/source.csv", selected_refs=["r1", "R1"], notify=False, prepare=False)
        fields = read_schematic_symbol_field_values(self.schematic.parent, ["R1", "R2", "R3"], ["value"])
        self.assertEqual(fields["R1"]["value"], "20k")
        self.assertEqual(fields["R2"]["value"], "10k")
        self.assertEqual(fields["R3"]["value"], "10k")
        self.assertEqual(result["target_refs"], 1)
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["updated_refs"], ["R1"])

    def test_empty_invalid_or_unknown_selection_never_closes_editor_or_writes(self):
        before = self.schematic.read_bytes()
        for refs in ([], ["R999"], "R1", [None], ["R1", "R999"]):
            with self.subTest(refs=refs), mock.patch.object(self.app, "prepare_kicad_for_external_schematic_write") as prepare:
                with self.assertRaises(ValueError):
                    self.app.bulk_sync_bom_state_to_schematic("BOM/source.csv", selected_refs=refs)
                prepare.assert_not_called()
                self.assertEqual(self.schematic.read_bytes(), before)

    def test_missing_selected_symbol_is_reported_without_writing_other_symbols(self):
        self.schematic.write_text(
            self.schematic.read_text(encoding="utf-8").replace('"R3"', '"R9"'), encoding="utf-8")
        before = self.schematic.read_bytes()
        result = self.app.bulk_sync_bom_state_to_schematic(
            "BOM/source.csv", selected_refs=["R3"], notify=False, prepare=False)
        self.assertEqual(result["missing_refs"], ["R3"])
        self.assertEqual(result["updated_refs"], [])
        self.assertEqual(self.schematic.read_bytes(), before)

    def test_dry_run_is_scoped_and_never_writes(self):
        before = self.schematic.read_bytes()
        result = self.app.bulk_sync_bom_state_to_schematic(
            "BOM/source.csv", selected_refs=["R1", "R2"], dry_run=True)
        self.assertEqual(result["scope"], "selected")
        self.assertEqual(result["target_refs"], 2)
        self.assertEqual(result["rows"], 1)
        self.assertEqual(self.schematic.read_bytes(), before)

    def test_already_synced_bulk_never_saves_closes_or_reopens_editor(self):
        self.app.bulk_sync_bom_state_to_schematic("BOM/source.csv", notify=False, prepare=False)
        with mock.patch.object(self.app, "run_kicad_script", return_value={"ok": True}) as run:
            result = self.app.bulk_sync_bom_state_to_schematic("BOM/source.csv", notify=True)
        self.assertEqual(result["updated_refs"], [])
        self.assertEqual(result["changes"], {})
        self.assertTrue(result["kicad_notify"]["ok"])
        self.assertTrue(result["already_synced"])
        self.assertTrue(result["schematic_verify"]["ok"])
        run.assert_not_called()

    def test_save_close_subprocess_budget_covers_long_project_backup(self):
        with mock.patch("part_selection_server.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            result = self.app.run_kicad_script("kicad-sch-search://R1", ["-SaveOnly"])
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(run.call_args.kwargs["timeout"], 450)

    def test_already_synced_single_request_does_not_operate_editor(self):
        payload = {"refs": "R1", "field_overrides": {"value": "10k"}, "notify": True}
        with mock.patch.object(self.app, "run_kicad_script") as run:
            result = self.app.sync_schematic(payload)
        self.assertTrue(result["already_synced"])
        self.assertEqual(result["updated_refs"], [])
        run.assert_not_called()

    def test_reopen_subprocess_budget_covers_large_project_startup(self):
        with mock.patch("part_selection_server.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            result = self.app.run_kicad_script("kicad-sch-search://R1", ["-NotifyOnly"])
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(run.call_args.kwargs["timeout"], 60)

    def test_sync_notification_reopens_without_automatic_find(self):
        with mock.patch.object(self.app, 'run_kicad_script', return_value={'ok': True}) as run:
            self.app.notify_kicad_schematic_changed({'updated_refs':['R1'],'pages':{'R1':'test'}})
        self.assertIn('-NotifyOnly',run.call_args.args[1])
        with mock.patch.object(self.app, 'run_kicad_script', return_value={'ok': True}) as run:
            self.app.jump_to_schematic('kicad-sch-search://R1?page=test')
        self.assertNotIn('-NotifyOnly',run.call_args.args[1])

    @unittest.skipUnless(shutil.which("node"), "Node is required for UI behavior checks")
    def test_selection_uses_visible_rows_and_keeps_partial_group_scope(self):
        html = render_index(self.app)
        functions = re.search(r'function visibleSyncRows\(\).*?(?=selectVisibleRows.addEventListener)', html, re.S).group()
        script = '''
const assert = require('node:assert/strict');
const selectedRowRefs = new Set(['R1', 'R2', 'R3']);
let bulkSyncRunning = false;
let rowsLoading = false;
function row(refs) {
  return {dataset: {refs}, hidden: false, input: {},
    classList: {contains() { return this.owner.hidden; }},
    querySelector() {return this.input;}};
}
const rows = [row('R1,R2'), row('R3')];
rows.forEach(row => row.classList.owner = row);
const rowsEl = {querySelectorAll() {return rows;}};
const countLabel = {};
const syncSelectedButton = {textContent: 'ICON', querySelector() {return countLabel;},
  setAttribute(name, value) {this[name] = value;}};
const selectVisibleRows = {};
FUNCTIONS
updateSyncSelection();
assert.equal(countLabel.textContent, 2);
assert.equal(syncSelectedButton['aria-label'], '同步所选行（2）');
assert.equal(syncSelectedButton.textContent, 'ICON');
assert.equal(selectVisibleRows.checked, true);
rows[0].hidden = true;
assert.deepEqual(selectedSyncRefs(), ['R3']);
updateSyncSelection();
assert.equal(countLabel.textContent, 1);
assert.equal(syncSelectedButton['aria-label'], '同步所选行（1）');
rows[0].hidden = false;
selectedRowRefs.delete('R2');
updateSyncSelection();
assert.equal(rows[0].input.indeterminate, true);
assert.equal(selectVisibleRows.checked, false);
assert.equal(selectVisibleRows.indeterminate, true);
assert.deepEqual(selectedSyncRefs(), ['R1', 'R3']);
bulkSyncRunning = true;
updateSyncSelection();
assert.equal(syncSelectedButton.disabled, true);
'''.replace('FUNCTIONS', functions)
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_api_forwards_selection_and_rejects_empty_scope(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(self.app, "bulk_sync_bom_state_to_schematic", return_value={"ok": True}) as sync:
                request = Request(f"http://127.0.0.1:{server.server_port}/api/bulk_sync_schematic",
                                  data=json.dumps({"bom": "BOM/source.csv", "selected_refs": ["R1"]}).encode(),
                                  headers={"Content-Type": "application/json"})
                with urlopen(request) as response:
                    self.assertTrue(json.load(response)["ok"])
                self.assertEqual(sync.call_args.kwargs["selected_refs"], ["R1"])
                self.assertEqual(sync.call_args.kwargs["ref_map"], {})
                request = Request(f"http://127.0.0.1:{server.server_port}/api/bulk_sync_schematic",
                                  data=json.dumps({"bom": "__schematic__"}).encode(),
                                  headers={"Content-Type": "application/json"})
                with urlopen(request) as response:
                    self.assertTrue(json.load(response)["ok"])
                self.assertEqual(sync.call_args.kwargs["ref_map"], {}, "Current schematic designators must not use historical BOM remapping")
            for selection in ([], None):
                request = Request(f"http://127.0.0.1:{server.server_port}/api/bulk_sync_schematic",
                                  data=json.dumps({"bom": "BOM/source.csv", "selected_refs": selection}).encode(),
                                  headers={"Content-Type": "application/json"})
                with self.assertRaises(HTTPError):
                    urlopen(request)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    @unittest.skipUnless(shutil.which("node"), "Node is required for UI behavior checks")
    def test_sync_transport_reports_disconnect_without_replaying_write(self):
        html = render_index(self.app)
        match = re.search(r'async function postSchematicSync\(.*?(?=async function syncSchematicWithProgress)', html, re.S)
        self.assertIsNotNone(match)
        script = "const assert = require('node:assert/strict');\n" + match.group() + r'''
const window = {location:{origin:'http://127.0.0.1:28777'}};
let calls = 0;
let fetch = async () => { calls++; throw new TypeError('Failed to fetch'); };
(async () => {
  await assert.rejects(postSchematicSync('/api/bulk_sync_schematic', {bom:'__schematic__'}),
    error => error.message.includes('28777') && error.message.includes('不要重复点击'));
  assert.equal(calls, 1);
  fetch = async () => ({ok:false, status:400, json:async () => ({ok:false, error:'save failed', sync_attempt_id:'test-123'})});
  await assert.rejects(postSchematicSync('/api/bulk_sync_schematic', {}), /save failed.*test-123/);
  fetch = async () => ({ok:true, json:async () => ({ok:true, updated_refs:['R1']})});
  assert.deepEqual(await postSchematicSync('/api/sync_schematic', {}), {ok:true, updated_refs:['R1']});
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is required for UI behavior checks")
    def test_loading_binds_rows_to_source_and_ignores_stale_success_and_failure(self):
        html = render_index(self.app)
        functions = re.search(r'async function loadRows\(.*?(?=async function loadBomOptions)', html, re.S).group()
        script = '''
const assert = require('node:assert/strict');
let rowsLoadVersion = 0, rowsLoading = false, loadedRowsBom = 'A';
let bulkSyncRunning = false;
const bomSelect = {value:'B'}, refsInput = {value:''};
const queryMissingButton = {}, queryAllButton = {}, syncAllButton = {};
const rowsEl = {innerHTML:'old rows'};
let currentRows = ['old'], rendered = [], states = [], pending = [];
const selectedRowRefs = new Set(['R1']);
function cancelStockQueries() {}
function setState(message) {states.push(message);}
function updateSyncSelection() {}
function updateRowsEmptyState() {}
function resetQueryAllButton() {}
function restoreRowAnchor() {}
function renderRows(rows) {rendered = rows;}
async function flushRowEdits() {}
function fetch(url) {return new Promise((resolve, reject) => pending.push({url,resolve,reject}));}
FUNCTIONS
(async () => {
  const b = loadRows();
  await Promise.resolve();
  assert.equal(rowsEl.innerHTML, '');
  assert.deepEqual(currentRows, []);
  assert.equal(rowsLoading, true);
  assert.equal(syncAllButton.disabled, true);
  bomSelect.value = 'C';
  const c = loadRows();
  await Promise.resolve();
  pending[1].resolve({ok:true,json:async()=>({rows:[{ref:'R3'}]})});
  await c;
  assert.equal(loadedRowsBom, 'C');
  pending[0].resolve({ok:true,json:async()=>({rows:[{ref:'R1'}]})});
  await b;
  assert.deepEqual(rendered, [{ref:'R3'}]);
  assert.equal(rowsLoading, false);
  bomSelect.value = 'D';
  const d = loadRows();
  await Promise.resolve();
  pending[2].reject(new Error('offline'));
  await d;
  assert.equal(syncAllButton.disabled, true);
  assert.equal(rowsEl.innerHTML, '');
  bomSelect.value = 'E'; const e = loadRows();
  await Promise.resolve();
  bomSelect.value = 'F'; const f = loadRows();
  await Promise.resolve();
  pending[4].resolve({ok:true,json:async()=>({rows:[{ref:'R6'}]})}); await f;
  pending[3].reject(new Error('stale error')); await e;
  assert.equal(loadedRowsBom, 'F');
  assert.equal(states.at(-1).includes('加载 BOM 失败'), false);
})().catch(error => {console.error(error); process.exitCode=1;});
'''.replace('FUNCTIONS', functions)
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_page_has_selected_sync_and_filter_select_all_controls(self):
        html = render_index(self.app)
        for token in ('id="syncSelectedSchematic"', 'id="selectVisibleRows"', 'data-select-row',
                      'selected_refs: selectedRefs', 'selectedSyncRefs()', 'selectedRowRefs.clear()'):
            self.assertTrue(token in html, token)

    def test_sync_ui_explicitly_explains_automatic_save_close(self):
        html = render_index(self.app)
        self.assertIn('已一致则直接完成', html)
        self.assertIn('自动关闭已保存的原理图', html)
        self.assertIn('自动保存不可用，会停止并提示先手动保存', html)
        self.assertIn('无需手动关闭', html)
        self.assertNotIn('请先保存/关闭 KiCad 后重试', html)

    def test_save_close_failure_leaves_schematic_untouched_for_both_sync_paths(self):
        before = self.schematic.read_bytes()
        for message in ('save command timed out', 'No KiCad schematic editor window was found',
                        'KICAD_BUSY: project window inspection timed out',
                        'KiCad manual save required: automatic save IPC is unavailable'):
            with self.subTest(message=message), mock.patch.object(
                self.app, 'prepare_kicad_for_external_schematic_write',
                return_value={'ok': False, 'message': message},
            ):
                for sync in (
                    lambda: self.app.sync_schematic({'refs': 'R1', 'field_overrides': {'value': '20k'}, 'notify': False}),
                    lambda: self.app.bulk_sync_bom_state_to_schematic('BOM/source.csv', notify=False),
                ):
                    with self.assertRaisesRegex(RuntimeError, '未写入原理图'):
                        sync()
                    self.assertEqual(self.schematic.read_bytes(), before)

    def test_unsaved_unrelated_edits_are_saved_before_sync_for_both_paths(self):
        original = self.schematic.read_bytes()
        original_overrides = self.app.overrides_path.read_bytes()
        for bulk in (False, True):
            with self.subTest(bulk=bulk):
                self.schematic.write_bytes(original)
                self.app.overrides_path.write_bytes(original_overrides)
                def save_editor(refs):
                    text = self.schematic.read_text(encoding='utf-8')
                    text = text.replace('(property "Reference" "R3"', '(property "UserNote" "unsaved edit" (at 0 0 0)) (property "Reference" "R3"', 1)
                    self.schematic.write_text(text, encoding='utf-8')
                    return {'ok': True}

                with mock.patch.object(self.app, 'prepare_kicad_for_external_schematic_write', side_effect=save_editor):
                    if bulk:
                        result = self.app.bulk_sync_bom_state_to_schematic('BOM/source.csv', selected_refs=['R1'], notify=False)
                    else:
                        result = self.app.sync_schematic({'refs': 'R1', 'field_overrides': {'value': '20k'}, 'notify': False})
                self.assertTrue(result['schematic_verify']['ok'])
                self.assertIn('(property "UserNote" "unsaved edit"', self.schematic.read_text(encoding='utf-8'))


if __name__ == "__main__":
    unittest.main()

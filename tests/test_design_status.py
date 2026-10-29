"""Main-table status must not lose an unfinished PCB phase."""
import json
import os
from pathlib import Path
import subprocess
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

import pytest

from part_selection_server import PartSelectionApp, row_filter_keys
from part_selection_overrides import save_overrides
from part_selection_design_status import DesignStatus


@pytest.fixture
def status_app(scratch_project):
    root = scratch_project
    (root / 'Design.kicad_pro').write_text('{}')
    (root / 'settings.json').write_text(json.dumps({
        'source_bom':'__schematic__', 'schematic_dir':'.', 'bom_dir':'data'}))
    (root / 'Design.kicad_sch').write_text('''(kicad_sch (uuid "root")
      (symbol (uuid "symbol") (property "Reference" "U1")
        (property "Value" "NEW") (property "Footprint" "Local:New")
        (instances (project "Design" (path "/root" (reference "U1") (unit 1))))))''')
    board = root / 'Design.kicad_pcb'
    board.write_text('''(kicad_pcb (footprint "Local:Old" (uuid "fp")
      (path "/root/symbol") (property "Reference" "U1") (property "Value" "OLD")))''')
    app = PartSelectionApp(root / 'settings.json')
    app.pcb_updates.session.snapshot = Mock(side_effect=AssertionError('No native IPC for table reads'))
    try:
        yield app, board
    finally:
        app.design_sync.close(); app.search_jobs.close(); app.selection_jobs.close()


def test_schematic_current_pcb_pending_is_not_sync_done(status_app):
    app, _ = status_app
    row, = app.rows('', '__schematic__', include_stock=False)
    assert not row.get('field_changed_refs')
    assert row.get('pcb_changed_refs') == 'U1'
    assert 'needs_sync' in row_filter_keys(row)
    assert 'sync_done' not in row_filter_keys(row)
    assert row['pcb_sync_by_ref']['U1']['field_changes'][0]['after'] == 'NEW'


def current_board(board):
    board.write_text(board.read_text().replace('Local:Old', 'Local:New').replace('"OLD"', '"NEW"'))


def replacement_record(app):
    save_overrides(app.overrides_path, {'U1':{'review_status':'Confirmed',
        'footprint_change':{'operation':'replace','before':'Local:Old','after':'Local:New'}}})


def observation(app, board, kind='none', targets=None):
    source = app.design_sync.sources.capture(['U1'])
    return {'refs':['U1'], 'requested_refs':['U1'], 'source':{'design':source},
            'snapshot':{'text':board.read_text()}, 'items':[{'ref':'U1','kind':kind}],
            'targets':targets or []}


def status(app):
    row, = app.rows('', '__schematic__', include_stock=False)
    return row


def test_restart_retains_pending_without_mutating_cad_or_confirmation(status_app):
    app, board = status_app
    replacement_record(app)
    before = board.read_bytes(), app.overrides_path.read_bytes()
    assert status(app)['pcb_changed_refs'] == 'U1'
    app.design_status = DesignStatus(app)
    assert status(app)['pcb_changed_refs'] == 'U1'
    assert (board.read_bytes(), app.overrides_path.read_bytes()) == before


def test_same_name_geometry_needs_verification_until_real_preflight(status_app):
    app, board = status_app
    replacement_record(app); current_board(board)
    assert status(app)['pcb_unverified_refs'] == 'U1'
    state = observation(app, board)
    assert app.design_status.observe(state)
    app.design_status = DesignStatus(app)
    row = status(app)
    assert not row['pcb_unverified_refs'] and not row['pcb_changed_refs']
    assert 'sync_done' in row_filter_keys(row)
    # Editing an unrelated track must not erase this instance's certificate.
    board.write_text(board.read_text()[:-1] + ' (segment (start 1 2) (end 3 4)))')
    assert not status(app)['pcb_unverified_refs']
    # Editing this instance must invalidate it.
    board.write_text(board.read_text().replace('(uuid "fp")', '(uuid "fp2")'))
    assert status(app)['pcb_unverified_refs'] == 'U1'


def test_changed_library_invalidates_geometry_certificate(status_app):
    import hashlib
    app, board = status_app
    replacement_record(app); current_board(board)
    target = board.parent / 'Local.pretty' / 'New.kicad_mod'
    target.parent.mkdir()
    target.write_text('(footprint "New")')
    (board.parent / 'fp-lib-table').write_text('(fp_lib_table (lib (name "Local") '
        '(type "KiCad") (uri "${KIPRJMOD}/Local.pretty")))')
    state = observation(app, board, targets=[{'ref':'U1','path':str(target),
                                           'hash':hashlib.sha256(target.read_bytes()).hexdigest()}])
    app.design_status.observe(state)
    assert not status(app)['pcb_unverified_refs']
    target.write_text('(footprint "Changed")')
    assert status(app)['pcb_unverified_refs'] == 'U1'


def test_remapped_library_invalidates_geometry_certificate(status_app):
    app, board = status_app
    replacement_record(app); current_board(board)
    table = board.parent / 'fp-lib-table'
    table.write_text('(fp_lib_table (lib (name "Local") (type "KiCad") (uri "${KIPRJMOD}/First.pretty")))')
    app.design_status.observe(observation(app, board))
    assert not status(app)['pcb_unverified_refs']
    table.write_text(table.read_text().replace('First.pretty', 'Second.pretty'))
    assert status(app)['pcb_unverified_refs'] == 'U1'


def test_cancelled_failed_and_unsaved_previews_cannot_certify_geometry(status_app):
    app, board = status_app
    replacement_record(app); current_board(board)
    app.design_status.observe(observation(app, board, 'replace'))
    assert status(app)['pcb_changed_refs'] == 'U1'
    state = observation(app, board)
    state['snapshot']['text'] = state['snapshot']['text'].replace('"NEW"', '"UNSAVED"')
    app.design_status.observe(state)
    assert status(app)['pcb_changed_refs'] == 'U1'


def test_save_uncertainty_survives_restart_and_readonly_preflight_resolves_it(status_app):
    app, board = status_app
    current_board(board)
    state = observation(app, board, 'fields')
    app.design_status.uncertain(state, 'Timed out')
    app.design_status = DesignStatus(app)
    assert status(app)['pcb_unverified_refs'] == 'U1'
    app.design_status.observe(state)
    assert not status(app)['pcb_unverified_refs']


@pytest.mark.parametrize('kind,expected', [('unknown', 'pcb_unverified_refs'), ('replace', 'pcb_changed_refs')])
def test_same_name_preflight_failure_is_not_hidden_and_restart_retains_it(status_app, kind, expected):
    app, board = status_app
    current_board(board)
    state = observation(app, board, kind)
    state['items'][0]['reason'] = '原封装几何与改名来源不一致' if kind == 'unknown' else '几何与目标库不同'
    app.design_status.observe(state)
    app.design_status = DesignStatus(app)
    row = status(app)
    assert row[expected] == 'U1'
    assert state['items'][0]['reason'] in row['pcb_sync_by_ref']['U1']['reason']
    assert 'sync_done' not in row_filter_keys(row)
    app.design_status.observe(observation(app, board))
    assert 'needs_sync' not in row_filter_keys(status(app))


def test_actual_save_clears_only_verified_scope(status_app):
    app, board = status_app
    replacement_record(app)
    state = observation(app, board, 'replace')
    current_board(board)
    assert app.design_status.observe(state, saved=True, verified={'text':board.read_text()})
    assert not status(app)['pcb_changed_refs']
    assert not status(app)['pcb_unverified_refs']


def test_disk_edit_after_verified_save_cannot_be_certified(status_app):
    app, board = status_app
    replacement_record(app); current_board(board)
    state = observation(app, board, 'replace')
    verified = {'text':board.read_text()}
    board.write_text(board.read_text().replace('(uuid "fp")', '(uuid "externally-edited")'))
    app.design_status.observe(state, saved=True, verified=verified)
    assert status(app)['pcb_unverified_refs'] == 'U1'


def test_unrelated_unsaved_items_do_not_hide_verified_instance(status_app):
    app, board = status_app
    replacement_record(app); current_board(board)
    state = observation(app, board)
    state['snapshot']['text'] = state['snapshot']['text'][:-1] + ' (segment (start 1 2) (end 3 4)))'
    app.design_status.observe(state)
    assert not status(app)['pcb_unverified_refs']


@pytest.mark.parametrize('change,synced', [
    ('format_order', True), ('coordinate', False), ('uuid', False),
    ('field_number', False), ('pad_number', False), ('duplicate', False),
    ('polygon_order', False), ('unknown_numeric', False),
])
def test_saved_live_certificate_ignores_only_exact_serialization_equivalence(status_app, change, synced):
    app, board = status_app
    replacement_record(app); current_board(board)
    objects = '''(fp_line (start 0.838200 -3.200400) (end -0.341026 -3.200400)
       (stroke (width 0.101600) (type solid)) (layer "F.SilkS") (uuid "line"))
      (pad "01" smd rect (at 0.000000 1.000000) (size 1.000000 1.000000) (uuid "pad"))
      (fp_poly (pts (xy 0 0) (xy 1 0) (xy 0 1)) (uuid "polygon"))
      (property "Number" "001.00") (future_numeric 001.00)'''
    board.write_text(board.read_text()[:-2] + objects + '))')
    state = observation(app, board)
    text = state['snapshot']['text']
    if change == 'format_order':
        # KiCad sorts footprint objects on read and trims decimal zeroes.
        first, second = objects.split('\n      (pad ', 1)
        text = text.replace(objects, '(pad ' + second + '\n' + first)
        for old, new in [('0.838200','0.8382'),('-3.200400','-3.2004'),
                         ('0.101600','0.1016'),('0.000000','0'),('1.000000','1')]:
            text = text.replace(old, new)
    elif change == 'coordinate': text = text.replace('0.838200', '0.838201')
    elif change == 'uuid': text = text.replace('"line"', '"other-line"')
    elif change == 'field_number': text = text.replace('"001.00"', '"1"')
    elif change == 'pad_number': text = text.replace('"01"', '"1"')
    elif change == 'duplicate': text = text.replace(objects, objects + '\n' + objects.split('\n')[0] + ')')
    elif change == 'polygon_order': text = text.replace('(xy 1 0) (xy 0 1)', '(xy 0 1) (xy 1 0)')
    elif change == 'unknown_numeric': text = text.replace('(future_numeric 001.00)', '(future_numeric 1)')
    state['snapshot']['text'] = text
    assert app.design_status.observe(state)
    assert (not status(app)['pcb_unverified_refs']) is synced


@pytest.mark.parametrize('failure', ['malformed', 'duplicate', 'identity', 'ambiguous', 'missing'])
def test_unreadable_or_unassociated_pcb_never_looks_synced(status_app, failure):
    app, board = status_app
    if failure == 'malformed':
        board.write_text('(invalid')
    elif failure == 'duplicate':
        board.write_text(board.read_text()[:-1] + board.read_text().split('(kicad_pcb ', 1)[1])
    elif failure == 'identity':
        board.write_text(board.read_text().replace('/root/symbol', '/other/symbol'))
    elif failure == 'ambiguous':
        board.with_name('Another.kicad_pcb').write_text(board.read_text())
    else:
        board.write_text('(kicad_pcb)')
    row = status(app)
    assert {'needs_sync', 'needs_verification'} & row_filter_keys(row)
    assert 'sync_done' not in row_filter_keys(row)
    assert row['pcb_changed_refs'] or row['pcb_unverified_refs']


def test_missing_board_is_schematic_only_and_cached_reads_do_not_reparse(status_app, monkeypatch):
    import part_selection_design_status as module
    app, board = status_app
    status(app)
    monkeypatch.setattr(module, 'parse_sexpr', Mock(side_effect=AssertionError('Unnecessary PCB parse')))
    assert status(app)['pcb_changed_refs'] == 'U1'
    board.unlink()
    assert not status(app).get('pcb_changed_refs')


def test_custom_mapped_fields_match_native_sync_field_names(status_app):
    app, board = status_app
    schematic = board.with_suffix('.kicad_sch')
    schematic.write_text(schematic.read_text().replace('"Value" "NEW"', '"值" "NEW"'))
    app.config['field_mapping'] = {'value':'值'}
    current_board(board)
    row = status(app)
    assert row['pcb_changed_refs'] == 'U1'
    assert {'name':'值','before':'','after':'NEW'} in row['pcb_sync_by_ref']['U1']['field_changes']
    board.write_text(board.read_text().replace('(property "Value" "NEW")',
                                              '(property "Value" "NEW") (property "值" "NEW")'))
    assert not status(app)['pcb_changed_refs']


def test_actual_table_pending_filter_refresh_and_split_in_chromium(status_app):
    from part_selection_server import make_handler
    app, _ = status_app
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    script = r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:920}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.STATUS_URL);
  const row=page.locator('#rows tr[data-ref="U1"]');
  await row.locator('[data-sync-marker] strong').filter({hasText:'待同步 PCB'}).waitFor();
  assert.match(await page.locator('#summaryPending').textContent(),/待同步 1 组 \/ 1 器件/);
  await page.locator('[data-filter="needs_sync"]').click();
  assert(await row.isVisible());
  await row.locator('[data-sync-marker] summary').click();
  assert.match(await row.locator('[data-sync-marker]').innerText(),/Local:Old → Local:New/);
  assert.match(await row.locator('[data-sync-marker]').innerText(),/OLD → NEW/);
  await row.locator('[data-field="value"]').focus();
  await page.evaluate(()=>{window.originalStatusRow=document.querySelector('#rows tr[data-ref="U1"]');});
  const initial=await page.request.get(process.env.STATUS_URL+'api/rows?bom=__schematic__&stock=0');
  const data=await initial.json();
  const item=data.rows[0];
  item.pcb_changed_refs='';item.pcb_unverified_refs='U1';
  item.pcb_sync_by_ref={U1:{status:'unverified',reason:'上次 PCB 结果待核验：Timed out'}};
  await page.route('**/api/rows?*',route=>route.fulfill({json:data}));
  await page.evaluate(()=>refreshDesignSyncRows());
  assert.match(await row.locator('[data-sync-marker] strong').innerText(),/PCB 待核验/);
  assert.match(await page.locator('#summaryPending').textContent(),/待同步 0 组 \/ 0 器件/);
  assert.match(await page.locator('#summaryVerify').textContent(),/待核验 1 组 \/ 1 器件/);
  assert(!(await row.isVisible()),'Unverified is not an actual pending difference');
  await page.locator('[data-filter="needs_verification"]').click();
  assert(await row.isVisible());
  assert(await page.evaluate(()=>originalStatusRow===document.querySelector('#rows tr[data-ref="U1"]')));
  // Focus preservation applies to visible rows, not rows hidden by the active filter.
  await page.locator('[data-filter="all"]').click();
  await row.locator('[data-field="value"]').focus();
  item.pcb_unverified_refs='';item.pcb_sync_by_ref={U1:{status:'synced'}};
  await page.evaluate(()=>refreshDesignSyncRows());
  assert.equal(await row.locator('[data-sync-marker]').count(),0);
  assert.match(await page.locator('#summaryPending').textContent(),/待同步 0 组 \/ 0 器件/);
  assert.match(await page.locator('#summaryVerify').textContent(),/待核验 0 组 \/ 0 器件/);
  assert.equal(await page.locator(':focus').getAttribute('data-field'),'value');
  await page.locator('[data-filter="needs_verification"]').click();
  assert(!(await row.isVisible()),'Completed row must leave the pending filter');
  const split=await page.evaluate(()=>rowForRefs({refs:'U1,U2',refs_list:['U1','U2'],
   pcb_changed_refs:'U2',pcb_unverified_refs:'U1',pcb_sync_by_ref:{U1:{status:'unverified'},U2:{status:'pending'}}},['U2']));
  assert.equal(split.pcb_changed_refs,'U2');assert.equal(split.pcb_unverified_refs,'');
  assert.deepEqual(Object.keys(split.pcb_sync_by_ref),['U2']);
  // An older refresh may not restore a pending badge after a newer one clears it.
  await page.unroute('**/api/rows?*');
  let releaseOld, startedOld;
  const oldStarted=new Promise(resolve=>{startedOld=resolve;});
  const oldRelease=new Promise(resolve=>{releaseOld=resolve;});
  let requests=0;
  await page.route('**/api/rows?*',async route=>{
   if(++requests===1){startedOld();await oldRelease;await route.fulfill({json:{rows:[{...item,
    pcb_unverified_refs:'U1',pcb_sync_by_ref:{U1:{status:'unverified',reason:'stale'}}}]}});}
   else await route.fulfill({json:data});
  });
  await page.evaluate(()=>{window.oldStatusRefresh=refreshDesignSyncRows();});
  await oldStarted;
  await page.evaluate(()=>refreshDesignSyncRows());
  releaseOld();await page.evaluate(()=>oldStatusRefresh);
  assert.equal(await row.locator('[data-sync-marker]').count(),0);
  // The table may be temporarily split while the server still returns a group.
  await page.unroute('**/api/rows?*');
  await page.route('**/api/rows?*',route=>route.fulfill({json:{rows:[{...item,
   refs:'U1,U2',refs_list:['U1','U2'],pcb_changed_refs:'U1',
   pcb_sync_by_ref:{U1:{status:'pending',before:'Local:Old',after:'Local:New'},U2:{status:'synced'}}}]}}));
  await page.evaluate(()=>refreshDesignSyncRows());
  await page.locator('[data-filter="needs_sync"]').click();
  assert.match(await row.locator('[data-sync-marker] strong').innerText(),/待同步 PCB/);
  assert.equal(await row.getAttribute('data-refs'),'U1');
  assert.match(await page.locator('#summaryPending').textContent(),/待同步 1 组 \/ 1 器件/);
  assert.deepEqual(errors,[]);
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    try:
        result = subprocess.run(['node', '-e', script], cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, 'STATUS_URL':f'http://127.0.0.1:{server.server_port}/'},
            capture_output=True, text=True, encoding='utf-8', timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown(); server.server_close(); thread.join()

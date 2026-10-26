"""Opt-in WebView2 registry QA with disposable data and no production services."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from http.server import ThreadingHTTPServer

from _scratch import temporary_project
from part_selection_lifecycle import DesktopBridge, DesktopLifecycle, ServiceLifecycle
from part_selection_server import PartSelectionApp, make_handler
from part_selection_tool_windows import ToolWindows
from test_footprint_choices import module
from kicad_plugin.desktop import enable_browser_shortcuts


def wait(window, expression, timeout=25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if window.evaluate_js(expression) is True:
            return
        time.sleep(.1)
    raise AssertionError('Native page did not become ready: ' + expression + '\n' +
                         str(window.evaluate_js('document.body.innerText')).strip()[-6000:])


def capture(window, path):
    from System import Action
    from System.IO import File
    from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
    tasks = []
    stream = File.Create(str(path))
    try:
        window.native.Invoke(Action(lambda: tasks.append(window.native.webview.CoreWebView2.CapturePreviewAsync(
            CoreWebView2CapturePreviewImageFormat.Png, stream))))
        tasks[0].Wait(10000)
        assert tasks[0].IsCompleted and not tasks[0].IsFaulted, 'Native screenshot failed'
    finally:
        stream.Dispose()


def check_native_edit_settings(window):
    from System import Action
    settings = []
    def inspect():
        core = window.native.webview.CoreWebView2
        settings.append((core.Settings.AreDefaultContextMenusEnabled,
                         core.Settings.AreBrowserAcceleratorKeysEnabled,
                         core.Settings.AreDevToolsEnabled))
    window.native.Invoke(Action(inspect))
    assert settings == [(True,True,False)], settings
    assert getattr(window, '_workbench_edit_menu', None), 'Edit menu handler is not installed'


def create_saved_pcb(root):
    python = Path(os.environ.get('PROGRAMFILES','C:/Program Files'))/'KiCad/10.0/bin/python.exe'
    script = '''
import sys
from pathlib import Path
import pcbnew
root=Path(sys.argv[1])
board=pcbnew.BOARD()
specs=[('R1','R'),('R2','R')]+[(ref,'Q8') for ref in ('Q6','Q7','Q9','Q56','Q57','Q62')]
for index,(ref,name) in enumerate(specs):
    footprint=pcbnew.FootprintLoad(str(root/'Local.pretty'),name)
    footprint.SetFPID(pcbnew.LIB_ID('Local',name))
    footprint.SetReference(ref)
    board.Add(footprint)
    footprint.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(20+index*10),pcbnew.FromMM(30)))
    footprint.SetOrientationDegrees(37+index*90)
pcbnew.SaveBoard(str(root/'example.kicad_pcb'),board)
'''
    subprocess.run([str(python),'-B','-c',script,str(root)],check=True,capture_output=True,timeout=30)


def check_native_grouped_preview(app, tools, report):
    refs = 'Q6,Q7,Q9,Q56,Q57,Q62'
    before = {path:path.read_bytes() for path in app.project_root.rglob('*') if path.is_file()}
    response = tools.main_action('open',{'kind':'footprint','request_id':uuid4().hex,
        'payload':{'refs':refs,'bom':'__schematic__'}})
    assert response['ok'],response
    entries = [entry for entry in tools.entries.values() if entry['kind']=='footprint' and not entry['closed']]
    assert len(entries)==1
    child = entries[0]['window']
    page = '[data-fp-page=library] '
    wait(child,"[...document.querySelectorAll('[data-fp-items] option')].some(o=>o.value==='Local:Q10')")
    child.evaluate_js('const s=document.querySelector("[data-fp-items]"); s.value="Local:Q10"; '
        's.dispatchEvent(new Event("change",{bubbles:true})); true')
    wait(child,f'!!document.querySelector("{page}.fp-scene:not([hidden])")')
    child.evaluate_js(f'document.querySelector("{page}.fp-pad-mapping").open=true; '
        f'document.querySelector("{page}[data-fp-mapping-enabled]").click(); true')
    for number in range(1,9):
        assert child.evaluate_js('document.querySelector('+json.dumps(page+f'[aria-label="原端子 {number} 对应目标焊盘"]')+').value')==str(number)
    status = child.evaluate_js(f'document.querySelector("{page}[data-fp-mapping-status]").textContent')
    assert '9, 10' in status and '未分配网络' in status,status
    capture(child,report/'grouped-8-to-10-pcb-baseline.png')
    child.evaluate_js(f'document.querySelector("{page}[data-fp-stage=converted]").scrollIntoView({{block:"center"}}); true')
    capture(child,report/'grouped-8-to-10-overlay.png')
    child.evaluate_js(f'document.querySelector("{page}[data-fp-cancel]").click(); true')
    for _ in range(100):
        if entries[0]['closed']:break
        time.sleep(.1)
    assert entries[0]['closed'],'Grouped preview cancel did not close'
    assert all(path.read_bytes()==data for path,data in before.items()),'Preview changed project files'


def check_native_mapping(app, tools, report):
    from part_selection_overrides import load_overrides
    originals = {path:path.read_bytes() for path in (app.project_root/'Local.pretty').glob('*.kicad_mod')}
    app.save_override({'refs':'R2','stock_c_code':'C101'})

    def convert(importer, code, progress=None):
        folder = importer.root/'Libraries'/'DSMU'/'LCSC'/code
        pretty = folder/f'LCSC_{code}.pretty'
        pretty.mkdir(parents=True)
        text = module('Converted').replace('(pad "1"', '(pad "A1"').replace('(pad "2"', '(pad "A2"')
        (pretty/'Converted.kicad_mod').write_text(text,encoding='utf-8')
        (folder/f'LCSC_{code}.kicad_sym').write_text('(kicad_symbol_lib (version 20241209) '
            f'(generator test) (symbol "Part" (property "Footprint" "LCSC_{code}:Converted")))',encoding='utf-8')
        return {'footprint':f'LCSC_{code}:Converted','symbol':f'LCSC_{code}:Part','c_code':code}

    for source in ('library','convert'):
        with patch('part_selection_footprints.LibraryImporter.convert',autospec=True,side_effect=convert):
            response = tools.main_action('open',{'kind':'footprint','request_id':uuid4().hex,
                'payload':{'refs':'R2','bom':'__schematic__'}})
            assert response['ok'], response
            entries = [e for e in tools.entries.values() if e['kind']=='footprint' and not e['closed']]
            assert len(entries)==1
            child = entries[0]['window']
            wait(child,'!!document.querySelector("[data-fp-tab=convert]")')
            child.evaluate_js(f'document.querySelector("[data-fp-tab={source}]").click(); true')
            page = f'[data-fp-page={source}] '
            if source == 'library':
                wait(child,"[...document.querySelectorAll('[data-fp-items] option')].some(o=>o.value==='Local:Other')")
                child.evaluate_js('const s=document.querySelector("[data-fp-items]"); s.value="Local:Other"; s.dispatchEvent(new Event("change",{bubbles:true})); true')
            wait(child,f'!!document.querySelector("{page}[data-fp-adjust]:not([hidden])")')
            child.evaluate_js(f'const p=document.querySelector("{page}.fp-pad-mapping"); p.open=true; p.querySelector("[data-fp-mapping-enabled]").click(); true')
            targets = ('2','1') if source == 'library' else ('A1','A2')
            for number, target in enumerate(targets,1):
                child.evaluate_js('const s=document.querySelector('+json.dumps(page+f'[aria-label="原端子 {number} 对应目标焊盘"]')+'); '
                    f's.value={json.dumps(target)}; s.dispatchEvent(new Event("change",{{bubbles:true}})); true')
            child.evaluate_js(f'const n=document.querySelector("{page}[data-fp-name]"); n.value="NativeMapped_{source}"; '
                'n.dispatchEvent(new Event("input",{bubbles:true})); true')
            child.evaluate_js(f'const x=document.querySelector("{page}[data-fp-dx]"); x.value="0.1"; '
                'x.dispatchEvent(new Event("input",{bubbles:true})); true')
            child.evaluate_js(f'document.querySelector("{page}[data-fp-ack]").click(); true')
            wait(child,f'document.querySelector("{page}.fp-apply").disabled===false')
            capture(child,report/f'mapping-{source}-native.png')
            assert child.evaluate_js(f'const e=document.querySelector("{page}[data-fp-stage=converted]"); '
                'e.scrollIntoView({block:"center"}); const r=e.getBoundingClientRect(); '
                'e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))'), 'Mapping editor hides native preview'
            child.evaluate_js(f'document.querySelector("{page}.fp-apply").click(); true')
            for _ in range(250):
                if entries[0]['closed']: break
                time.sleep(.1)
            assert entries[0]['closed'], source+' mapping did not save/close'
            record = load_overrides(app.overrides_path)['R2']
            assert record['field_overrides']['footprint']==f'Local:NativeMapped_{source}', record
            assert record['stock_c_code']=='C101'
            assert (app.project_root/'Local.pretty'/f'NativeMapped_{source}.kicad_mod').is_file()
    assert all(path.read_bytes()==data for path,data in originals.items()), 'Original library entries changed'


def check_native_grouped_default_save(app, tools, report):
    from part_selection_overrides import load_overrides
    from part_stock_lookup import save_stock_cache
    refs='Q6,Q7,Q9,Q56,Q57,Q62'
    app.save_override({'refs':refs,'stock_c_code':'C202'})
    save_stock_cache(app.stock_cache_path,{'C202':{'ok':True,'c_code':'C202','package':'SMD','model':'Switch'}})
    originals={path:path.read_bytes() for path in (app.project_root/'Local.pretty').glob('*.kicad_mod')}
    def convert(importer,code,progress=None):
        folder=importer.root/'Libraries/DSMU/LCSC'/code
        pretty=folder/f'LCSC_{code}.pretty';pretty.mkdir(parents=True)
        (pretty/'Q10.kicad_mod').write_bytes((app.project_root/'Local.pretty/Q10.kicad_mod').read_bytes())
        (folder/f'LCSC_{code}.kicad_sym').write_text('(kicad_symbol_lib (symbol "Switch" '
            f'(property "Footprint" "LCSC_{code}:Q10")))',encoding='utf-8')
        return {'footprint':f'LCSC_{code}:Q10','symbol':f'LCSC_{code}:Switch','c_code':code}
    requests=[]
    original_request=app.row_footprint
    def trace(payload,*args,**kwargs):
        if payload.get('footprint_choice'):
            requests.append(payload['footprint_choice'])
            (report/'default-save-choices.json').write_text(json.dumps(requests,ensure_ascii=False,indent=2),encoding='utf-8')
        try:
            return original_request(payload,*args,**kwargs)
        except Exception:
            (report/'default-save-error.log').write_text(traceback.format_exc(),encoding='utf-8')
            raise
    with patch.object(app,'row_footprint',side_effect=trace),patch('part_selection_footprints.LibraryImporter.convert',autospec=True,side_effect=convert):
        for mode,expected in [('library','Q10_mapped'),('convert','SMD')]:
            response=tools.main_action('open',{'kind':'footprint','request_id':uuid4().hex,
                'payload':{'refs':refs,'bom':'__schematic__'}})
            assert response['ok'],response
            entry=next(e for e in tools.entries.values() if e['kind']=='footprint' and not e['closed'])
            child=entry['window'];page=f'[data-fp-page={mode}] '
            wait(child,f'!!document.querySelector("[data-fp-tab={mode}]")')
            child.evaluate_js(f'document.querySelector("[data-fp-tab={mode}]").click(); true')
            if mode=='library':
                wait(child,"[...document.querySelectorAll('[data-fp-items] option')].some(o=>o.value==='Local:Q10')")
                child.evaluate_js('const s=document.querySelector("[data-fp-items]");s.value="Local:Q10";'
                    's.dispatchEvent(new Event("change",{bubbles:true}));true')
            wait(child,f'!!document.querySelector("{page}.fp-scene:not([hidden])")')
            child.evaluate_js(f'document.querySelector("{page}.fp-pad-mapping").open=true;'
                f'document.querySelector("{page}[data-fp-mapping-enabled]").click();true')
            for axis,value in [('dx','0.9756'),('dy','-0.5716')]:
                child.evaluate_js(f'const i=document.querySelector("{page}[data-fp-{axis}]");i.value="{value}";'
                    'i.dispatchEvent(new Event("input",{bubbles:true}));true')
            child.evaluate_js(f'document.querySelector("{page}[data-fp-ack]").click();true')
            wait(child,f'document.querySelector("{page}.fp-apply").disabled===false')
            assert child.evaluate_js(f'document.querySelector("{page}[data-fp-name]").value')==expected
            child.evaluate_js(f'document.querySelector("{page}.fp-apply").click();true')
            for _ in range(250):
                if entry['closed']:break
                if child.evaluate_js('document.body.innerText.includes("封装保存失败")'):
                    raise AssertionError(str(child.evaluate_js('document.body.innerText'))[-4000:])
                time.sleep(.1)
            assert entry['closed'],mode+' grouped default save failed'
            records=load_overrides(app.overrides_path)
            assert all(records[ref]['field_overrides']['footprint']=='Local:'+expected for ref in refs.split(','))
            assert (app.project_root/'Local.pretty'/f'{expected}.kicad_mod').is_file()
            assert 'missing-model.step' in (app.project_root/'Local.pretty'/f'{expected}.kicad_mod').read_text(encoding='utf-8')
    assert all(path.read_bytes()==data for path,data in originals.items())


def run(report):
    import webview
    result = {'passed':False,'checks':{},'timings_ms':{}}
    with temporary_project(prefix='native-tools-', short_paths=True) as directory:
        root = Path(directory)
        pretty = root/'Local.pretty'
        pretty.mkdir()
        for name, width in [('R', .8), ('Other', .9)]:
            (pretty/(name+'.kicad_mod')).write_text(module(name, width), encoding='utf-8')
        for name,count in [('Q8',8),('Q10',10)]:
            pads = ' '.join(f'(pad "{n}" smd rect (at {(n-1)%4-1.5} {((n-1)//4)*2}) '
                           '(size 0.5 1) (layers "F.Cu" "F.Paste" "F.Mask"))' for n in range(1,count+1))
            (pretty/(name+'.kicad_mod')).write_text(f'(footprint "{name}" (version 20241229) '
                f'(generator test) (layer "F.Cu") {pads}'+
                (' (model "${KIPRJMOD}/missing-model.step" (offset (xyz 1 2 3)) '
                 '(scale (xyz 1 1 1)) (rotate (xyz 10 20 30)))' if name=='Q10' else '')+')',encoding='utf-8')
        (root/'fp-lib-table').write_text('(fp_lib_table (version 7) (lib (name "Local") '
            '(type "KiCad") (uri "${KIPRJMOD}/Local.pretty")))', encoding='utf-8')
        (root/'example.kicad_sch').write_text('(kicad_sch (symbol (property "Reference" "R1") '
            '(property "Value" "10k") (property "Comment" "resistor") '
            '(property "Footprint" "Local:R")) (symbol (property "Reference" "R2") '
            '(property "Value" "20k") (property "Comment" "resistor") '
            '(property "Footprint" "Local:R")) '+''.join(f'(symbol (property "Reference" "{ref}") '
                '(property "Value" "Switch") (property "Comment" "Switch") '
                '(property "Footprint" "Local:Q8"))' for ref in ('Q6','Q7','Q9','Q56','Q57','Q62'))+')',encoding='utf-8')
        create_saved_pcb(root)
        pcb_before = (root/'example.kicad_pcb').read_bytes()
        schematic_before = (root/'example.kicad_sch').read_bytes()
        config = root/'settings.json'
        config.write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.','bom_dir':'data'}))
        app = PartSelectionApp(config)
        from part_stock_lookup import save_stock_cache
        save_stock_cache(app.stock_cache_path, {'C101':{'ok':True,'c_code':'C101',
            'param_detail':'阻值: 10k; 精度: ±1%; 最大工作电压: 50V'}})
        app.save_field_override({'refs':'R1','bom':'__schematic__','field_overrides':{'value':'22k'}})
        server = ThreadingHTTPServer(('127.0.0.1',0),make_handler(app))
        def serve():
            try:
                server.serve_forever(poll_interval=.05)
            finally:
                server.server_close()
        thread = threading.Thread(target=serve,daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_port}/'
        lifecycle = DesktopLifecycle(url,uuid4().hex)
        app.lifecycle = ServiceLifecycle(app, server, lifecycle.token)
        lifecycle.project_id = str(root)
        # No network updater or actual install is invoked by this native UI probe.
        lifecycle.updater = SimpleNamespace(status=lambda:{'ok':True,'current_version':'native-probe',
            'mode':'off','installed':False})
        main = webview.create_window('Native tool fixture',url,width=800,height=640,x=50,y=50,text_select=True,
                                     js_api=DesktopBridge(lifecycle))
        lifecycle.window = main
        main.events.closing += lifecycle.on_closing
        main.events.loaded += lambda: enable_browser_shortcuts(main)
        lifecycle.tools = ToolWindows(lifecycle,webview.create_window,enable_browser_shortcuts)
        def worker():
            tools = lifecycle.tools
            try:
                wait(main,'typeof window.openNativeTool === "function" && !!window.pywebview?.api?.tool_window && currentRows.length===3')
                check_native_edit_settings(main)
                result['checks']['native_edit_menu_settings']=True
                for kind in ['settings','inventory','field-mapping','list-view','value-parameters','undo','footprint','plugin-update','design-sync','candidate']:
                    payload = {'refs':'R1','bom':'__schematic__'} if kind in ['value-parameters','undo','footprint','design-sync','candidate'] else {}
                    if kind=='design-sync': payload['destination']='schematic'
                    started = time.perf_counter()
                    response = tools.main_action('open',{'kind':kind,'request_id':uuid4().hex,'payload':payload})
                    assert response['ok'], response
                    child = tools.entries[kind]['window']
                    wait(child,'window.nativeToolMounted === true')
                    result['timings_ms'][kind] = round((time.perf_counter()-started)*1000,1)
                    child.move(main.native.Right + 20,110)
                    for _ in range(30):
                        if child.native.Left > main.native.Right - 100: break
                        time.sleep(.1)
                    assert child.native.Left > main.native.Right - 100, 'Child cannot leave main bounds'
                    assert set(child.evaluate_js('Object.keys(window.pywebview.api)'))=={'tool_window','workbench_edit'}, 'Child has host privileges'
                    if kind=='settings':
                        wait(child,'document.querySelector("#pathSettingsDialog").open')
                        child.evaluate_js('document.querySelector("[name=jump_cursor_behavior]").value="stay"')
                        child.evaluate_js('window.confirm=()=>false; window.hasUnsavedPathSettings=()=>true; true')
                        try: tools.check_exit()
                        except ValueError: pass
                        else: raise AssertionError('Unsaved settings did not block host exit')
                        assert not tools.child_action(kind,'close')['ok']
                        assert tools.child_action(kind,'minimize')['ok']
                        assert tools.main_action('open',{'kind':kind,'request_id':uuid4().hex,'payload':payload})['ok']
                        assert child.evaluate_js('document.querySelector("[name=jump_cursor_behavior]").value')=='stay'
                        capture(child,report/'settings-native.png')
                        child.evaluate_js('document.querySelector("[name=jump_cursor_behavior]").value="return"')
                        child.evaluate_js('window.hasUnsavedPathSettings=()=>false; true')
                        result['checks']['dirty_exit_block_and_state_restore']=True
                    elif kind=='list-view':
                        wait(child,'!!document.querySelector("#columnList [data-column-toggle]")')
                        capture(child,report/'list-native.png')
                    elif kind=='footprint':
                        wait(child,'!!document.querySelector(".fp-native-workflow")')
                        assert child.evaluate_js('getComputedStyle(document.querySelector(".fp-workflow > .fp-title")).display')=='none'
                        assert child.evaluate_js('document.querySelectorAll(".fp-history [data-fp-back]").length')==1
                        assert child.evaluate_js('const h=document.querySelector(".fp-history").getBoundingClientRect(); const t=document.querySelector(".fp-tabs").getBoundingClientRect(); h.y>=t.bottom-1 && h.height<=32')
                        check_native_edit_settings(child)
                        result['checks']['single_native_title_and_child_edit_menu']=True
                        wait(child,"[...document.querySelectorAll('[data-fp-items] option')].some(o=>o.value==='Local:Other')")
                        child.evaluate_js('const s=document.querySelector("[data-fp-items]"); s.value="Local:Other"; s.dispatchEvent(new Event("change",{bubbles:true})); true')
                        wait(child,'!!document.querySelector("[data-fp-page=library] .fp-scene:not([hidden])")')
                        capture(child,report/'footprint-native.png')
                        child.evaluate_js('document.querySelector("[data-fp-page=library] [data-fp-ack]").click(); true')
                        wait(child,'document.querySelector("[data-fp-page=library] .fp-apply").disabled===false')
                        child.evaluate_js('document.querySelector("[data-fp-page=library] .fp-apply").click(); true')
                        for _ in range(200):
                            if kind not in tools.entries: break
                            time.sleep(.1)
                        assert kind not in tools.entries, 'Apply did not close the footprint window'
                        from part_selection_overrides import load_overrides
                        assert load_overrides(app.overrides_path)['R1']['field_overrides']['footprint']=='Local:Other'
                        response = tools.main_action('open',{'kind':kind,'request_id':uuid4().hex,'payload':payload})
                        assert response['ok'], response
                        child = tools.entries[kind]['window']
                        wait(child,'!!document.querySelector("[data-fp-tab=rename]")')
                        child.evaluate_js('document.querySelector("[data-fp-tab=rename]").click(); true')
                        wait(child,'!!document.querySelector("[data-fp-before]")')
                        child.evaluate_js('const n=document.querySelector("[data-fp-page=rename] [data-fp-name]"); n.value="Local:Renamed"; n.dispatchEvent(new Event("input",{bubbles:true})); true')
                        wait(child,'document.querySelector("[data-fp-page=rename] .fp-apply").disabled===false')
                        child.evaluate_js('document.querySelector("[data-fp-back]").click(); document.querySelector("[data-fp-forward]").click(); true')
                        assert child.evaluate_js('document.querySelector("[data-fp-page=rename] [data-fp-name]").value')=='Local:Renamed'
                        capture(child,report/'rename-native.png')
                        child.evaluate_js('document.querySelector("[data-fp-page=rename] .fp-apply").click(); true')
                        for _ in range(200):
                            if kind not in tools.entries: break
                            time.sleep(.1)
                        assert kind not in tools.entries
                        assert load_overrides(app.overrides_path)['R1']['field_overrides']['footprint']=='Local:Renamed'
                        assert (pretty/'Renamed.kicad_mod').exists()
                        result['checks']['native_library_apply_rename_and_navigation']=True
                        assert tools.main_action('open',{'kind':kind,'request_id':uuid4().hex,'payload':payload})['ok']
                        child = tools.entries[kind]['window']
                        wait(child,'!!document.querySelector("[data-fp-close]")')
                        child.evaluate_js('window.rowFootprintSaving=true; true')
                        assert not tools.child_action(kind,'close')['ok'], 'Actual publication must block close'
                        child.evaluate_js('window.rowFootprintSaving=false; true')
                        assert tools.child_action(kind,'close')['ok'], 'Read-only footprint preview must not block close'
                        assert kind not in tools.entries
                        response = tools.main_action('open',{'kind':kind,'request_id':uuid4().hex,'payload':payload})
                        assert response['ok'], response
                        child = tools.entries[kind]['window']
                        wait(child,'!!document.querySelector(".fp-overlay [data-fp-close]")')
                        child.evaluate_js('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true,cancelable:true})); true')
                        for _ in range(100):
                            if kind not in tools.entries: break
                            time.sleep(.1)
                        assert kind not in tools.entries, 'Escape did not close the native footprint utility'
                        result['checks']['footprint_escape_and_native_close']=True
                        result['checks'][kind]=True
                        continue
                    elif kind=='candidate':
                        wait(child,'!!document.querySelector("#candidateKeyword")')
                    wait(child,'window.nativeToolCanReplace() === true')
                    assert tools.child_action(kind,'close')['ok'], kind
                    assert tools.entries[kind]['request'] is None
                    result['checks'][kind]=True
                entered, release = threading.Event(), threading.Event()
                def slow_check(**kwargs):
                    entered.set()
                    if not release.wait(25):
                        raise AssertionError('Native update concurrency probe timed out')
                    return {'ok':True}
                lifecycle.updater.check = slow_check
                lifecycle.updater.pending = None
                main.evaluate_js("window.pywebview.api.plugin_update('check').then(r=>window.nativeUpdateProbe=r); true")
                try:
                    assert entered.wait(10), 'Actual native update bridge did not start'
                    response = tools.main_action('open',{'kind':'settings','request_id':uuid4().hex,'payload':{}})
                    assert response['ok'], response
                    child = tools.entries['settings']['window']
                    wait(child,'document.querySelector("#pathSettingsDialog").open')
                    stopped = lifecycle.finish_exit()
                    assert not stopped['ok'] and '更新' in stopped['error'], stopped
                    assert tools.child_action('settings','close')['ok']
                finally:
                    release.set()
                wait(main,'window.nativeUpdateProbe?.ok === true')
                assert lifecycle.update_busy is False
                result['checks']['native_update_network_does_not_block_tools']=True
                check_native_grouped_preview(app,tools,report)
                result['checks']['grouped_8_to_10_saved_pcb_preview']=True
                check_native_grouped_default_save(app,tools,report)
                result['checks']['grouped_default_save_both_sources']=True
                check_native_mapping(app,tools,report)
                result['checks']['native_mapping_both_sources_and_project_copy']=True
                tools.check_exit(); tools.cancel_exit()
                for ref in ('R1','R2'):
                    assert tools.main_action('open',{'kind':'footprint','request_id':uuid4().hex,
                        'payload':{'refs':ref,'bom':'__schematic__'}})['ok']
                entries=[entry for entry in tools.entries.values() if entry['kind']=='footprint']
                assert len(entries)==2
                for entry in entries:
                    wait(entry['window'],'!!document.querySelector("[data-fp-tab=library]")')
                assert tools.main_action('open',{'kind':'footprint','request_id':uuid4().hex,
                    'payload':{'refs':'R1','bom':'__schematic__'}})['ok']
                assert len([e for e in tools.entries.values() if e['kind']=='footprint'])==2
                tools.check_exit()
                assert (root/'example.kicad_sch').read_bytes()==schematic_before
                assert (root/'example.kicad_pcb').read_bytes()==pcb_before
                result['checks']['saved_pcb_baseline_preview_and_mapping']=True
                result['checks']['multi_row_windows_and_host_cancel']=True
                tools.cancel_exit()
                main.evaluate_js('window.requestDesktopExit(false); true')
                for _ in range(200):
                    if lifecycle.allow_close and all(entry['closed'] for entry in tools.entries.values()): break
                    time.sleep(.1)
                assert lifecycle.allow_close, 'Main window exit did not close its children/backend'
                assert all(entry['closed'] for entry in tools.entries.values())
                thread.join(5)
                assert not thread.is_alive(), 'Owned backend did not exit'
                result['checks']['main_exit_closes_children_and_backend']=True
                result['checks']['native_drag_outside_and_minimal_bridge']=True
                result['passed']=True
            except Exception as error:
                result['error']=repr(error)
                (report/'error.log').write_text(traceback.format_exc(),encoding='utf-8')
            finally:
                if not lifecycle.allow_close:
                    lifecycle.allow_close=True
                    tools.dispose(); main.destroy()
        try:
            webview.start(worker,gui='edgechromium',private_mode=True,
                          storage_path=str(root.parent/'webview'))
        finally:
            if thread.is_alive(): server.shutdown()
            server.server_close();thread.join(timeout=5)
            app.design_sync.close();app.search_jobs.close();app.selection_jobs.close()
            (report/'native-tools.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir',type=Path,required=True)
    args=parser.parse_args()
    report=args.report_dir.resolve()
    if report.is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error('Reports must be outside the repository')
    report.mkdir(parents=True,exist_ok=True)
    import faulthandler
    with (report/'threads.log').open('w',encoding='utf-8') as trace:
        faulthandler.dump_traceback_later(90, file=trace)
        try:
            outcome=run(report)
        finally:
            faulthandler.cancel_dump_traceback_later()
    print(json.dumps(outcome,indent=2))
    raise SystemExit(0 if outcome['passed'] else 1)

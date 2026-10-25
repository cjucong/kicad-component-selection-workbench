"""Opt-in WebView2 registry QA with disposable data and no production services."""
import argparse
import json
import os
from pathlib import Path
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


def run(report):
    import webview
    result = {'passed':False,'checks':{},'timings_ms':{}}
    with temporary_project(prefix='native-tools-', short_paths=True) as directory:
        root = Path(directory)
        pretty = root/'Local.pretty'
        pretty.mkdir()
        for name, width in [('R', .8), ('Other', .9)]:
            (pretty/(name+'.kicad_mod')).write_text(module(name, width), encoding='utf-8')
        (root/'fp-lib-table').write_text('(fp_lib_table (version 7) (lib (name "Local") '
            '(type "KiCad") (uri "${KIPRJMOD}/Local.pretty")))', encoding='utf-8')
        (root/'example.kicad_sch').write_text('(kicad_sch (symbol (property "Reference" "R1") '
            '(property "Value" "10k") (property "Comment" "resistor") '
            '(property "Footprint" "Local:R")) (symbol (property "Reference" "R2") '
            '(property "Value" "20k") (property "Comment" "resistor") '
            '(property "Footprint" "Local:R")))',encoding='utf-8')
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
                wait(main,'typeof window.openNativeTool === "function" && !!window.pywebview?.api?.tool_window && currentRows.length===2')
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

"""Opt-in WebView2 registry QA with disposable data and no production services."""
import argparse
import json
import os
from pathlib import Path
import threading
import time
import traceback
from types import SimpleNamespace
from uuid import uuid4
from http.server import ThreadingHTTPServer

from _scratch import temporary_project
from part_selection_lifecycle import DesktopBridge, DesktopLifecycle
from part_selection_server import PartSelectionApp, make_handler
from part_selection_tool_windows import ToolWindows


def wait(window, expression, timeout=25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if window.evaluate_js(expression) is True:
            return
        time.sleep(.1)
    raise AssertionError('Native page did not become ready: ' + expression)


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


def run(report):
    import webview
    result = {'passed':False,'checks':{},'timings_ms':{}}
    with temporary_project(prefix='native-tools-') as directory:
        root = Path(directory)
        (root/'example.kicad_sch').write_text('(kicad_sch (symbol (property "Reference" "R1") '
            '(property "Value" "10k") (property "Comment" "resistor") '
            '(property "LCSC" "C101") (property "Footprint" "Local:R")))',encoding='utf-8')
        config = root/'settings.json'
        config.write_text(json.dumps({'source_bom':'__schematic__','schematic_dir':'.','bom_dir':'data'}))
        app = PartSelectionApp(config)
        from part_stock_lookup import save_stock_cache
        save_stock_cache(app.stock_cache_path, {'C101':{'ok':True,'c_code':'C101',
            'param_detail':'阻值: 10k; 精度: ±1%; 最大工作电压: 50V'}})
        app.save_field_override({'refs':'R1','bom':'__schematic__','field_overrides':{'value':'22k'}})
        server = ThreadingHTTPServer(('127.0.0.1',0),make_handler(app))
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_port}/'
        lifecycle = DesktopLifecycle(url,uuid4().hex)
        lifecycle.project_id = str(root)
        # No network updater or actual install is invoked by this native UI probe.
        lifecycle.updater = SimpleNamespace(status=lambda:{'ok':True,'current_version':'native-probe',
            'mode':'off','installed':False})
        main = webview.create_window('Native tool fixture',url,width=800,height=640,x=50,y=50,
                                     js_api=DesktopBridge(lifecycle))
        lifecycle.window = main
        lifecycle.tools = ToolWindows(lifecycle,webview.create_window)
        def worker():
            tools = lifecycle.tools
            try:
                wait(main,'typeof window.openNativeTool === "function" && !!window.pywebview?.api?.tool_window && currentRows.length===1')
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
                    assert set(child.evaluate_js('Object.keys(window.pywebview.api)'))=={'tool_window'}, 'Child has host privileges'
                    if kind=='settings':
                        wait(child,'document.querySelector("#pathSettingsDialog").open')
                        child.evaluate_js('document.querySelector("[name=jump_cursor_behavior]").value="stay"')
                        try: tools.check_exit()
                        except ValueError: pass
                        else: raise AssertionError('Unsaved settings did not block host exit')
                        assert not tools.child_action(kind,'close')['ok']
                        assert tools.child_action(kind,'minimize')['ok']
                        assert tools.main_action('open',{'kind':kind,'request_id':uuid4().hex,'payload':payload})['ok']
                        assert child.evaluate_js('document.querySelector("[name=jump_cursor_behavior]").value')=='stay'
                        capture(child,report/'settings-native.png')
                        child.evaluate_js('document.querySelector("[name=jump_cursor_behavior]").value="return"')
                        result['checks']['dirty_exit_block_and_state_restore']=True
                    elif kind=='list-view':
                        wait(child,'!!document.querySelector("#columnList [data-column-toggle]")')
                        capture(child,report/'list-native.png')
                    elif kind=='footprint':
                        wait(child,'!!document.querySelector("[data-fp-close]")')
                        capture(child,report/'footprint-native.png')
                        child.evaluate_js('window.rowFootprintSaving=true; true')
                        assert not tools.child_action(kind,'close')['ok'], 'Actual publication must block close'
                        child.evaluate_js('window.rowFootprintSaving=false; true')
                        assert tools.child_action(kind,'close')['ok'], 'Read-only footprint preview must not block close'
                        assert tools.entries[kind]['request'] is None
                        response = tools.main_action('open',{'kind':kind,'request_id':uuid4().hex,'payload':payload})
                        assert response['ok'], response
                        wait(child,'!!document.querySelector(".fp-overlay [data-fp-close]")')
                        child.evaluate_js('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true,cancelable:true})); true')
                        for _ in range(100):
                            if tools.entries[kind]['request'] is None: break
                            time.sleep(.1)
                        assert tools.entries[kind]['request'] is None, 'Escape did not close the native footprint utility'
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
                tools.check_exit(); tools.cancel_exit()
                result['checks']['native_drag_outside_and_minimal_bridge']=True
                result['passed']=True
            except Exception as error:
                result['error']=repr(error)
                (report/'error.log').write_text(traceback.format_exc(),encoding='utf-8')
            finally:
                tools.dispose(); main.destroy()
        try:
            webview.start(worker,gui='edgechromium',private_mode=True,
                          storage_path=str(root.parent/'webview'))
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
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
    outcome=run(report)
    print(json.dumps(outcome,indent=2))
    raise SystemExit(0 if outcome['passed'] else 1)

"""Opt-in real WebView2 compact progress/close probe, with synthetic sync replies."""
import argparse
import json
from pathlib import Path
import threading
import time
import traceback
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

from _scratch import temporary_project
from part_selection_lifecycle import DesktopBridge, DesktopLifecycle, ServiceLifecycle
from part_selection_server import PartSelectionApp, make_handler
from part_selection_tool_windows import ToolWindows
from kicad_plugin.desktop import enable_browser_shortcuts
from run_native_tool_windows_probe import capture, wait


def run(report):
    import webview
    result = {'passed': False, 'checks': {}}
    replies = {'done': False, 'failure': False}
    with temporary_project(prefix='compact-native-', short_paths=True) as directory:
        root = Path(directory)
        (root/'example.kicad_sch').write_text('(kicad_sch (symbol '
            '(property "Reference" "R1") (property "Value" "10k") '
            '(property "Footprint" "Local:R")))', encoding='utf-8')
        config = root/'settings.json'
        config.write_text(json.dumps({'source_bom': '__schematic__', 'bom_dir': 'data',
                                     'schematic_dir': '.'}), encoding='utf-8')
        app = PartSelectionApp(config)
        base = make_handler(app)
        class Handler(base):
            def synthetic(self, value):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(value, ensure_ascii=False).encode())

            def do_POST(self):
                if self.path in {'/api/start_design_sync_preview', '/api/start_design_sync',
                                 '/api/cancel_design_sync_preview'}:
                    self.rfile.read(int(self.headers.get('Content-Length', '0')))
                    if self.path.endswith('start_design_sync_preview'):
                        self.synthetic({'ok': True, 'done': True, 'job_id': 'preview',
                            'preview': {'ok': True, 'preview_id': 'preview', 'automatic': True,
                                        'schematic_changes': []}})
                    else:
                        self.synthetic({'ok': True, 'job_id': 'synthetic'})
                else:
                    super().do_POST()

            def do_GET(self):
                if self.path.startswith('/api/design_sync_job?'):
                    done, failure = replies['done'], replies['failure']
                    self.synthetic({'ok': not failure, 'job_id': 'synthetic', 'done': done,
                        'stage': ('failed' if failure else 'complete') if done else 'pcb:readback',
                        'message': ('测试：回读失败' if failure else '测试：保存已核验') if done else '测试：正在回读核验',
                        'phase_results': {'schematic': {'ok': True},
                                          'pcb': {'ok': not failure, 'saved': done and not failure}}})
                else:
                    super().do_GET()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        def serve():
            try:
                server.serve_forever()
            finally:
                server.server_close()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/'
        lifecycle = DesktopLifecycle(url, uuid4().hex)
        app.lifecycle = ServiceLifecycle(app, server, lifecycle.token)
        lifecycle.project_id = str(root)
        lifecycle.updater = SimpleNamespace(pending=False, status=lambda: {'ok': True, 'mode': 'off'})
        main = webview.create_window('Compact sync fixture', url, width=1000, height=700,
                                    js_api=DesktopBridge(lifecycle), text_select=True)
        lifecycle.window = main
        main.events.closing += lifecycle.on_closing
        main.events.loaded += lambda: enable_browser_shortcuts(main)
        tools = lifecycle.tools = ToolWindows(lifecycle, webview.create_window, enable_browser_shortcuts)
        completed = threading.Event()

        def worker():
            try:
                wait(main, 'typeof window.openNativeTool === "function" && currentRows.length===1')
                for failure in (False, True):
                    replies.update(done=False, failure=failure)
                    response = tools.main_action('open', {'kind': 'design-sync',
                        'request_id': uuid4().hex,
                        'payload': {'refs': 'R1', 'bom': '__schematic__', 'destination': 'design'}})
                    assert response['ok'], response
                    entry = tools.entries['design-sync']
                    child = entry['window']
                    wait(child, 'document.querySelector("[data-design-status]")?.textContent === "测试：正在回读核验"')
                    assert child.evaluate_js('document.querySelectorAll("[data-design-steps] li").length') == 0
                    assert child.evaluate_js('getComputedStyle(document.querySelector("[data-design-changes]")).display') == 'none'
                    assert not tools.child_action('design-sync', 'close')['ok'], 'Do not close an active write'
                    if not failure:
                        capture(child, report/'sync-native-active.png')
                    replies['done'] = True
                    if failure:
                        wait(child, 'document.querySelector("[data-design-status]")?.textContent === "测试：回读失败"')
                        assert entry['request'] is not None
                        assert child.evaluate_js('document.querySelector("[data-design-refresh]").getClientRects().length > 0')
                        capture(child, report/'sync-native-failed.png')
                        assert tools.child_action('design-sync', 'close')['ok']
                        result['checks']['failure_retained'] = True
                    else:
                        deadline = time.monotonic()+15
                        while entry['request'] is not None and time.monotonic() < deadline:
                            time.sleep(.1)
                        assert entry['request'] is None, 'Verified success did not close through the native bridge'
                        result['checks']['verified_success_auto_closed'] = True
                main.evaluate_js('window.requestDesktopExit(false); true')
                deadline = time.monotonic()+15
                while not lifecycle.allow_close and time.monotonic() < deadline:
                    time.sleep(.1)
                thread.join(5)
                assert lifecycle.allow_close and not thread.is_alive()
                result['checks']['normal_exit_and_backend_cleanup'] = True
                result['passed'] = True
            except Exception as error:
                result['error'] = repr(error)
                (report/'error.log').write_text(traceback.format_exc(), encoding='utf-8')
            finally:
                if not lifecycle.allow_close:
                    lifecycle.allow_close = True
                    tools.dispose()
                    main.destroy()
                completed.set()
        try:
            webview.start(worker, gui='edgechromium', private_mode=True,
                          storage_path=str(root.parent/'webview'))
            assert completed.wait(20), 'Native test worker did not finish'
        finally:
            if thread.is_alive():
                server.shutdown()
            server.server_close()
            thread.join(5)
            app.design_sync.close()
            app.search_jobs.close()
            app.selection_jobs.close()
    result['directory_cleanup'] = not root.exists()
    result['passed'] = result['passed'] and result['directory_cleanup']
    (report/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, required=True)
    args = parser.parse_args()
    report = args.report_dir.resolve()
    assert not report.is_relative_to(Path(__file__).resolve().parents[1])
    report.mkdir(parents=True, exist_ok=True)
    result = run(report)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['passed'] else 1)

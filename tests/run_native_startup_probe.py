"""Real WebView2 loading/handoff/close test; no CAD or production backend."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def run_case(case):
    sys.path.insert(0, str(ROOT / 'tools/kicad_plugin'))
    import webview
    from startup_progress import StartupProgress
    progress = StartupProgress('启动测试')
    release, loaded = threading.Event(), threading.Event()
    result = {'case':case, 'passed':False}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<html><body><h1 id="workbench">Workbench ready</h1></body></html>')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    lifecycle = SimpleNamespace(url='http://127.0.0.1:0/', allow_close=False, on_closing=lambda:True)
    def loader():
        assert release.wait(15)
        if case == 'failure':
            raise RuntimeError('synthetic backend failure')
        return f'http://127.0.0.1:{server.server_port}/'
    window = progress.open(webview, 'KPS isolated startup test', lifecycle, loader, None, loaded.set)
    finished = threading.Event()
    def exercise():
        try:
            deadline = time.monotonic()+15
            while not window.evaluate_js('typeof window.setStartupStage === "function"'):
                assert time.monotonic() < deadline
                time.sleep(.1)
            handle = int(window.native.Handle.ToInt64())
            progress.stage('Testing same-window progress')
            assert window.evaluate_js('document.querySelector("#stage").textContent') == 'Testing same-window progress'
            assert window.evaluate_js('!!document.querySelector("progress")')
            report = Path(os.environ['KPS_STARTUP_REPORT']) if os.environ.get('KPS_STARTUP_REPORT') else None
            if report:
                from run_native_tool_windows_probe import capture
                capture(window, report/(case+'-loading.png'))
            if case == 'cancel':
                window.destroy()
                release.set()
                progress.close()
                assert progress.cancelled and not progress.ready
            else:
                release.set()
                if case == 'success':
                    assert loaded.wait(15)
                    assert window.evaluate_js('!!document.querySelector("#workbench")')
                else:
                    while window.evaluate_js('document.querySelector("#stage")?.dataset.error') != 'true':
                        assert time.monotonic() < deadline
                        time.sleep(.1)
                    assert 'synthetic backend failure' in window.evaluate_js('document.body.innerText')
                    assert not progress.ready
                assert int(window.native.Handle.ToInt64()) == handle
                assert len(webview.windows) == 1
                window.destroy()
            result.update(passed=True, same_window=True)
        except Exception as exc:
            result['error'] = repr(exc)
            result['traceback'] = traceback.format_exc()
            release.set()
            window.destroy()
        finally:
            finished.set()
    try:
        webview.start(exercise, gui='edgechromium', private_mode=True)
        assert finished.wait(20)
        progress.close()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(5)
    print(json.dumps(result), flush=True)
    if os.environ.get('KPS_STARTUP_REPORT'):
        Path(os.environ['KPS_STARTUP_REPORT'], case+'.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=['success','cancel','failure'])
    args = parser.parse_args()
    if args.case:
        raise SystemExit(run_case(args.case))
    for case in ('success','cancel','failure'):
        completed = subprocess.run([sys.executable,'-B',__file__,'--case',case], timeout=60)
        if completed.returncode:
            raise SystemExit(completed.returncode)

"""Loading page and guarded handoff inside the main desktop window."""
from __future__ import annotations

from html import escape
import json
import logging
import threading
import time


class StartupProgress:
    def __init__(self, title):
        self.title = title
        self.message = '正在启动…'
        self.window = None
        self.cancelled = False
        self.error = None
        self.ready = False
        self._thread = None
        self._lock = threading.RLock()
        self._started = time.perf_counter()

    def start(self):
        self.stage('正在核验启动工程…')

    def html(self):
        return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>启动工作台</title><style>
body{margin:0;background:#f5f7f9;color:#22302b;font:15px "Segoe UI","Microsoft YaHei",sans-serif;
display:grid;place-items:center;min-height:100vh}main{width:min(440px,calc(100vw - 64px))}
h1{font-size:24px;font-weight:600;letter-spacing:0;margin:0 0 28px}
p{line-height:1.6;overflow-wrap:anywhere}progress{width:100%;height:8px;accent-color:#258459}
small{color:#65716c}p[data-error="true"]{color:#b13131}</style>
<main><h1>''' + escape(self.title) + '''</h1><p id="stage" role="status">''' + escape(self.message) + '''</p>
<progress aria-label="启动进度"></progress><p><small id="elapsed"></small></p></main>
<script>const started=performance.now();setInterval(()=>{
document.querySelector('#elapsed').textContent='已用 '+((performance.now()-started)/1000).toFixed(1)+' s';},200);
window.setStartupStage=(message,error=false)=>{const p=document.querySelector('#stage');
p.textContent=message;p.dataset.error=String(error);document.querySelector('progress').hidden=error;};
</script></html>'''

    def stage(self, message, *, error=False):
        self.message = message
        logging.info('Desktop startup stage=%s elapsed_ms=%.1f', message,
                     (time.perf_counter() - self._started) * 1000)
        if self.window and not self.cancelled and not self.ready:
            try:
                self.window.evaluate_js('window.setStartupStage?.(' + json.dumps(message) +
                                        ',' + json.dumps(error) + ')')
            except Exception:
                logging.info('Startup page not available for stage update', exc_info=True)

    def open(self, webview, title, lifecycle, loader, bridge, on_ready):
        window = webview.create_window(title, html=self.html(), width=1380, height=900,
            min_size=(800, 500), text_select=True, js_api=bridge)
        self.window = lifecycle.window = window

        def initialize():
            try:
                self.stage('正在启动工程后台…')
                url = loader()
                with self._lock:
                    lifecycle.url = url
                    if self.cancelled:
                        return
                self.stage('正在加载工作台…')
                window.load_url(url)
            except Exception as exc:
                self.error = str(exc)
                logging.exception('Desktop initialization failed')
                self.stage('启动未完成：' + str(exc), error=True)

        def loaded():
            ready = False
            with self._lock:
                if self.cancelled:
                    return
                if self._thread is None:
                    self._thread = threading.Thread(target=initialize, name='desktop-startup')
                    self._thread.start()
                elif window.get_current_url() == lifecycle.url:
                    self.ready = True
                    logging.info('Desktop workbench ready elapsed_ms=%.1f',
                                 (time.perf_counter() - self._started) * 1000)
                    ready = True
            if ready:
                on_ready()

        def closing():
            with self._lock:
                if not self.ready:
                    self.cancelled = True
                    lifecycle.allow_close = True
                    return True
            return lifecycle.on_closing()

        window.events.loaded += loaded
        window.events.closing += closing
        return window

    def close(self):
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

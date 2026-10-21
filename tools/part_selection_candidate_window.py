"""One native candidate window, owned by the current desktop workbench."""
import json
import logging
import threading


class CandidateBridge:
    def __init__(self, owner):
        self._owner = owner

    def candidate_window(self, action, value=None):
        return self._owner.child_action(action, value)


class CandidateWindows:
    def __init__(self, lifecycle, create_window, on_loaded=None):
        self._lifecycle = lifecycle
        self._create_window = create_window
        self._on_loaded = on_loaded
        self._window = None
        self._ready = threading.Event()
        self._destroying = False
        self._request = None
        self._minimized = False
        self._url = lifecycle.url + '?view=candidate'

    def _main_message(self, name, value=None):
        main = self._lifecycle.window
        if main.get_current_url() != self._lifecycle.url or self._lifecycle.allow_close:
            raise ValueError('工作台已离开当前工程，不能操作子窗口')
        return main.evaluate_js('window.' + name + '?.(' + json.dumps(value, ensure_ascii=True) + '); true')

    @staticmethod
    def _validate_request(value):
        fields = ('refs', 'ref', 'bom', 'comment', 'value', 'footprint', 'code')
        if not isinstance(value, dict) or set(value) - set(fields):
            raise ValueError('更换型号窗口参数无效')
        if any(not isinstance(item, str) or len(item) > 16000 for item in value.values()):
            raise ValueError('更换型号窗口参数过长或类型无效')
        if not value.get('refs', '').strip():
            raise ValueError('请选择需要更换型号的器件')
        return {key: value.get(key, '') for key in fields}

    def _ensure_window(self):
        if self._window is not None:
            return
        self._ready.clear()
        self._window = self._create_window(
            '更换型号', self._url, width=1180, height=720, min_size=(760, 480),
            text_select=True, hidden=True, js_api=CandidateBridge(self))

        def loaded():
            if self._on_loaded:
                self._on_loaded(self._window)
            self._ready.set()

        self._window.events.loaded += loaded
        self._window.events.closing += self._on_closing
        self._window.events.minimized += lambda: self._set_minimized(True)
        self._window.events.restored += lambda: self._set_minimized(False)

    def _set_minimized(self, value):
        self._minimized = value

    def _show(self):
        self._window.show()
        if self._minimized:
            self._window.restore()

    def _on_closing(self):
        if self._destroying:
            return True

        def close():
            try:
                self._window.evaluate_js('window.closeCandidates?.(); true')
            except Exception:
                logging.exception('Candidate window close deferred')

        threading.Thread(target=close, daemon=True, name='candidate-close').start()
        return False

    def main_action(self, action, value=None):
        lifecycle = self._lifecycle
        if not lifecycle.lock.acquire(blocking=False):
            return {'ok': False, 'error': '工作台正在打开窗口或退出，请稍候'}
        try:
            if lifecycle.allow_close or lifecycle.window.get_current_url() != lifecycle.url:
                raise ValueError('仅允许当前工作台操作更换型号窗口')
            if action == 'open':
                request = self._validate_request(value)
                self._ensure_window()
                if not self._ready.wait(15):
                    raise ValueError('更换型号窗口加载超时，请稍后重试')
                if self._window.get_current_url() != self._url:
                    raise ValueError('更换型号窗口地址已改变，请重启工作台')
                result = self._window.evaluate_js('window.receiveNativeCandidateRequest(' + json.dumps(request, ensure_ascii=True) + ')')
                if not isinstance(result, dict) or not result.get('ok'):
                    raise ValueError((result or {}).get('error', '更换型号窗口尚未就绪') if isinstance(result, dict) else '更换型号窗口尚未就绪')
                self._request = request
                self._window.set_title('更换型号：' + request['refs'][:100])
                self._show()
                return {'ok': True}
            if action == 'minimize':
                if self._window:
                    self._window.hide()
                return {'ok': True}
            if action == 'restore':
                if not self._window or not self._request:
                    raise ValueError('没有可恢复的更换型号窗口，请点击器件的更换型号')
                self._show()
                return {'ok': True}
            raise ValueError('不支持的更换型号窗口操作')
        except Exception as exc:
            logging.exception('Candidate window action failed')
            return {'ok': False, 'error': str(exc)}
        finally:
            lifecycle.lock.release()

    def child_action(self, action, value=None):
        try:
            if not self._window or self._window.get_current_url() != self._url or self._destroying:
                raise ValueError('仅允许当前更换型号子窗口调用')
            if self._lifecycle.allow_close:
                raise ValueError('工作台正在退出')
            if value is not None:
                raise ValueError('子窗口操作不接受额外参数')
            if action in ('close', 'minimize'):
                if action == 'close':
                    self._request = None
                self._window.hide()
                self._main_message('nativeCandidateVisibility', {'open': action != 'close', 'minimized': True})
                return {'ok': True}
            if action == 'changed':
                self._main_message('nativeCandidateSaved', self._request)
                return {'ok': True}
            raise ValueError('不支持的子窗口操作')
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    def check_exit(self):
        if self._window and self._window.get_current_url() == self._url:
            if self._window.evaluate_js('window.prepareNativeCandidateExit?.() === true') is not True:
                raise ValueError('更换型号窗口正在保存或转换，请完成后再退出工作台')

    def cancel_exit(self):
        if self._window and not self._destroying:
            self._window.evaluate_js('window.cancelNativeCandidateExit?.(); true')

    def dispose(self):
        self._destroying = True
        if self._window:
            self._window.destroy()

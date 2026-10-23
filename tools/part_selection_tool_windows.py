"""Project-owned native utility windows; renderer bridges have no host privileges."""
from copy import deepcopy
import json
import logging
import threading
from urllib.parse import urlencode


TOOL_TITLES = {
    'candidate':'更换型号', 'footprint':'封装管理', 'design-sync':'同步设计',
    'settings':'工程设置', 'inventory':'本地库存', 'value-parameters':'补充 Value',
    'undo':'撤回工作台修改', 'list-view':'列表视图', 'field-mapping':'字段映射',
    'plugin-update':'插件更新',
}
ROW_FIELDS = {'ref', 'refs', 'bom'}
TOOL_FIELDS = {
    kind: (ROW_FIELDS | {'comment','value','footprint','code'} if kind == 'candidate'
           else ROW_FIELDS | {'destination','alignment'} if kind == 'design-sync'
           else ROW_FIELDS | {'mode'} if kind == 'footprint'
           else ROW_FIELDS if kind in ('value-parameters','undo') else set())
    for kind in TOOL_TITLES
}


class ToolBridge:
    def __init__(self, owner, kind):
        self._owner, self._kind = owner, kind

    def tool_window(self, action, value=None):
        return self._owner.child_action(self._kind, action, value)


class ToolWindows:
    def __init__(self, lifecycle, create_window, on_loaded=None):
        self.lifecycle = lifecycle
        self.create_window, self.on_loaded = create_window, on_loaded
        self.entries = {}
        self.lock = threading.RLock()
        self.destroying = False

    @staticmethod
    def validate_request(value):
        if not isinstance(value, dict) or set(value) != {'kind','request_id','payload'}:
            raise ValueError('工具窗口请求无效')
        kind, rid, payload = value['kind'], value['request_id'], value['payload']
        if not isinstance(kind, str) or kind not in TOOL_FIELDS:
            raise ValueError('不支持的工具窗口')
        if not isinstance(rid, str) or not rid or len(rid) > 128:
            raise ValueError('工具请求编号无效')
        if not isinstance(payload, dict) or set(payload) - TOOL_FIELDS[kind]:
            raise ValueError('工具窗口参数无效')
        if any(not isinstance(v, str) or len(v) > 16000 for v in payload.values()):
            raise ValueError('工具窗口参数过长或类型无效')
        if ROW_FIELDS & TOOL_FIELDS[kind] and not (payload.get('refs','').strip() or payload.get('ref','').strip()):
            raise ValueError('请选择需要处理的器件')
        return deepcopy(value)

    def _verify_host(self):
        if self.destroying or self.lifecycle.allow_close or self.lifecycle.window.get_current_url() != self.lifecycle.url:
            raise ValueError('工作台已退出或离开当前工程')

    def _entry(self, kind):
        self._verify_host()
        entry = self.entries.get(kind)
        if not entry or entry['window'].get_current_url() != entry['url']:
            raise ValueError('工具窗口地址或所属工程已改变')
        return entry

    def _main_message(self, callback, value):
        self._verify_host()
        return self.lifecycle.window.evaluate_js('window.' + callback + '?.(' + json.dumps(value, ensure_ascii=True) + '); true')

    def _envelope(self, entry):
        from part_selection_lifecycle import owner_id
        return {**entry['request'], 'host':owner_id(self.lifecycle.token),
                'project':getattr(self.lifecycle, 'project_id', self.lifecycle.url)}

    def _deliver(self, kind):
        entry = self._entry(kind)
        if not entry['ready'] or entry['request'] is None:
            return
        result = entry['window'].evaluate_js('window.receiveNativeToolRequest?.(' +
            json.dumps(self._envelope(entry), ensure_ascii=True) + ')')
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise ValueError((result or {}).get('error', '工具页面尚未就绪') if isinstance(result, dict) else '工具页面尚未就绪')

    def _create(self, kind, request):
        query = {'view':'candidate'} if kind == 'candidate' else {'view':'tool','tool':kind}
        entry = {'url':self.lifecycle.url + '?' + urlencode(query), 'ready':False,
                 'request':request, 'minimized':False}
        window = self.create_window(TOOL_TITLES[kind], entry['url'],
            width=1180 if kind in ('candidate','footprint','design-sync') else 900,
            height=720, min_size=(540,400), text_select=True, hidden=False,
            frameless=False, js_api=ToolBridge(self, kind))
        entry['window'] = window
        self.entries[kind] = entry

        def loaded():
            try:
                if self.on_loaded:
                    self.on_loaded(window)
                entry['ready'] = True
                self._deliver(kind)
            except Exception:
                logging.exception('Tool window delivery deferred: %s', kind)

        def closing():
            if self.destroying:
                return True
            threading.Thread(target=lambda:self.child_action(kind,'close'), daemon=True,
                             name='tool-close-' + kind).start()
            return False

        window.events.loaded += loaded
        window.events.closing += closing
        window.events.minimized += lambda:entry.update(minimized=True)
        window.events.restored += lambda:entry.update(minimized=False)
        return entry

    def main_action(self, action, value=None):
        if not self.lifecycle.lock.acquire(blocking=False):
            return {'ok':False,'error':'工作台正在退出或处理另一个窗口请求'}
        try:
            self._verify_host()
            with self.lock:
                if action == 'open':
                    request = self.validate_request(value)
                    kind = request['kind']
                    entry = self.entries.get(kind)
                    if entry is None:
                        entry = self._create(kind, request)
                    else:
                        self._entry(kind)
                        previous = entry['request']
                        if previous is None or previous['payload'] != request['payload']:
                            if entry['ready'] and entry['window'].evaluate_js('window.nativeToolCanReplace?.() === true') is not True:
                                raise ValueError('工具窗口有未保存编辑或正在处理，请先完成当前操作')
                            entry['request'] = request
                            try:
                                self._deliver(kind)
                            except Exception:
                                entry['request'] = previous
                                raise
                    entry['window'].show()
                    if entry['minimized']:
                        entry['window'].restore()
                    return {'ok':True, 'request_id':entry['request']['request_id']}
                if action not in ('restore','minimize') or not isinstance(value,dict) or set(value) != {'kind'}:
                    raise ValueError('不支持的工具窗口操作')
                entry = self._entry(value['kind'])
                if entry['request'] is None:
                    raise ValueError('没有可恢复的工具窗口')
                if action == 'minimize':
                    entry['window'].hide()
                else:
                    entry['window'].show()
                    if entry['minimized']:
                        entry['window'].restore()
                return {'ok':True}
        except Exception as exc:
            logging.exception('Tool window action failed')
            return {'ok':False,'error':str(exc)}
        finally:
            self.lifecycle.lock.release()

    def child_action(self, kind, action, value=None):
        try:
            with self.lock:
                entry = self._entry(kind)
                if value is not None:
                    raise ValueError('子窗口操作不接受额外参数')
                if action == 'ready':
                    entry['ready'] = True
                    self._deliver(kind)
                elif action == 'changed':
                    if entry['request'] is None:
                        raise ValueError('工具请求已关闭')
                    self._main_message('nativeToolChanged', self._envelope(entry))
                elif action in ('close','minimize'):
                    if action == 'close' and entry['ready']:
                        if entry['window'].evaluate_js('window.prepareNativeToolExit?.() === true') is not True:
                            raise ValueError('工具窗口有未保存编辑或正在处理，窗口已保留')
                    entry['window'].hide()
                    self._main_message('nativeToolVisibility', {'kind':kind,'open':action!='close','minimized':True})
                    if action == 'close':
                        entry['request'] = None
                        if entry['ready']:
                            entry['window'].evaluate_js('window.cancelNativeToolExit?.(); true')
                else:
                    raise ValueError('不支持的子窗口操作')
            return {'ok':True}
        except Exception as exc:
            return {'ok':False,'error':str(exc)}

    def check_exit(self):
        for kind in list(self.entries):
            entry = self._entry(kind)
            if entry['ready'] and entry['request'] is not None:
                if entry['window'].evaluate_js('window.prepareNativeToolExit?.() === true') is not True:
                    raise ValueError(TOOL_TITLES[kind] + '尚有未保存编辑或任务，请先完成')

    def cancel_exit(self):
        for entry in self.entries.values():
            if entry['ready'] and entry['window'].get_current_url() == entry['url']:
                entry['window'].evaluate_js('window.cancelNativeToolExit?.(); true')

    def dispose(self):
        self.destroying = True
        for entry in self.entries.values():
            entry['window'].destroy()

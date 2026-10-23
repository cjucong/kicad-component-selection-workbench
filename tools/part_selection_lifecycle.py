"""Authenticated desktop ownership and graceful, idle-only service shutdown."""
import ctypes
import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def owner_id(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest() if token else ''


class ServiceLifecycle:
    def __init__(self, app, server, token):
        self.app, self.server, self.token = app, server, token

    @property
    def identity(self):
        return owner_id(self.token)

    def shutdown(self, token):
        if not self.token or not hmac.compare_digest(self.token, str(token)):
            raise ValueError('后台身份不匹配，不能关闭其他窗口或工程的后台')
        with self.app.settings_gate.editing():
            design_sync = getattr(self.app, 'design_sync', None)
            if self.app.search_jobs.busy() or self.app.selection_jobs.busy() or (design_sync and design_sync.busy()):
                raise ValueError('搜索或转换尚未完成，请稍后关闭或重启工作台')
            if not self.app.settings_gate.stopping:
                self.app.settings_gate.stopping = True
                threading.Thread(target=self.server.shutdown, daemon=True, name='desktop-shutdown').start()
        return {'ok':True}

    def owner_exited(self):
        # A crashed host must not leave a service behind or interrupt a write.
        while not self.app.settings_gate.stopping:
            try:
                with self.app.settings_gate.lock:
                    if self.app.settings_gate.active:
                        raise ValueError('Request still active')
                    self.shutdown(self.token)
                return
            except ValueError:
                time.sleep(0.5)

    def watch_owner(self, pid):
        if os.name == 'nt':
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.WaitForSingleObject.restype = ctypes.c_uint32
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x00100000, False, pid)
            if handle:
                try:
                    if kernel.WaitForSingleObject(handle, 0xffffffff) != 0:
                        logging.error('Could not monitor desktop process %s', pid)
                        return
                finally:
                    kernel.CloseHandle(handle)
            elif ctypes.get_last_error() != 87:  # ERROR_INVALID_PARAMETER: process already exited.
                logging.error('Could not open desktop process %s', pid)
                return
        else:
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    return
                time.sleep(0.5)
        self.owner_exited()


class DesktopBridge:
    def __init__(self, lifecycle):
        self._lifecycle = lifecycle

    def finish_exit(self, restart=False, update=False):
        return self._lifecycle.finish_exit(restart, update)

    def candidate_window(self, action, value=None):
        if self._lifecycle.tools is not None:
            from uuid import uuid4
            request = ({'kind':'candidate','request_id':uuid4().hex,'payload':value}
                       if action == 'open' else {'kind':'candidate'})
            return self._lifecycle.tools.main_action(action, request)
        if self._lifecycle.candidates is None:
            return {'ok': False, 'error': '当前宿主不支持独立选型窗口，请重新安装插件'}
        return self._lifecycle.candidates.main_action(action, value)

    def tool_window(self, action, value=None):
        if self._lifecycle.tools is None:
            return {'ok':False,'error':'当前宿主不支持独立工具窗口，请重新安装并重启工作台'}
        return self._lifecycle.tools.main_action(action, value)

    def plugin_update(self, action, value=None):
        lifecycle = self._lifecycle
        if not lifecycle.lock.acquire(blocking=False):
            return {'ok':False,'error':'工作台正在退出或处理更新，请稍候'}
        reserved = False
        try:
            if lifecycle.window.get_current_url()!=lifecycle.url or lifecycle.allow_close:
                raise ValueError('仅允许当前工作台管理插件更新')
            updater = lifecycle.updater
            if updater is None:
                raise ValueError('当前宿主不支持更新，请重新安装插件')
            if action=='status':
                return updater.status()
            if lifecycle.update_busy:
                raise ValueError('正在检查或下载插件更新，请稍候；其他工具仍可使用')
            if action not in ('check','preferences','prepare'):
                raise ValueError('不支持的更新操作')
            lifecycle.update_busy = reserved = True
        except Exception as exc:
            return {'ok':False,'error':str(exc)}
        finally:
            lifecycle.lock.release()
        # Network I/O must not own the native-window lifecycle lock.
        try:
            if action=='check':
                if value is not None and type(value) is not bool:
                    raise ValueError('检查更新参数无效')
                return updater.check(force=value is not False)
            if action=='preferences':
                return updater.preferences(value)
            if action=='prepare':
                return updater.prepare()
        except Exception as exc:
            return {'ok':False,'error':str(exc)}
        finally:
            if reserved:
                with lifecycle.lock:
                    lifecycle.update_busy = False


class DesktopLifecycle:
    """WebView bridge; never terminates an arbitrary PID or a KiCad process."""
    def __init__(self, url, token):
        self.url, self.token = url, token
        self.window = None
        self.allow_close = False
        self.restart = False
        self.shutdown_requested = False
        self.lock = threading.Lock()
        self.updater = None
        self.update_busy = False
        self.install_update = False
        self.candidates = None
        self.tools = None

    def on_closing(self):
        if self.allow_close:
            return True
        def prepare():
            try:
                prepared = self.window.evaluate_js(
                    'typeof window.requestDesktopExit === "function" '
                    '? (window.requestDesktopExit(false), true) : false')
                if prepared is False:
                    # A script parse error can leave static chrome but no editable rows.
                    empty = self.window.evaluate_js(
                        '!!document.querySelector("#rows") '
                        '&& document.querySelector("#rows").children.length === 0 '
                        '&& !document.querySelector("[data-field], [data-composing=\\"1\\"]")')
                    if empty is True:
                        logging.warning('Closing uninitialized workbench without editable rows')
                        self.finish_exit(False)
                    else:
                        logging.error('Exit handler unavailable; editable workbench retained')
            except Exception:
                logging.exception('Could not prepare workbench close; window retained')
        # WinForms invokes Closing on its UI thread; waiting for JS here deadlocks.
        threading.Thread(target=prepare, daemon=True, name='desktop-close').start()
        return False

    def _stop_backend(self):
        if not self.shutdown_requested:
            request = Request(self.url+'api/desktop_shutdown',data=b'{}',headers={
                'Content-Type':'application/json','X-Desktop-Token':self.token})
            try:
                with urlopen(request,timeout=5) as response:
                    result = json.load(response)
            except OSError as exc:
                if hasattr(exc,'read'):
                    try:
                        raise ValueError(json.loads(exc.read())['error']) from exc
                    except (KeyError, json.JSONDecodeError):
                        pass
                raise ValueError('后台未能确认退出，请稍后重试；窗口与编辑内容已保留') from exc
            if not result.get('ok'):
                raise ValueError(result.get('error') or '后台拒绝退出')
            self.shutdown_requested = True
        parsed = urlsplit(self.url)
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            try:
                with socket.create_connection((parsed.hostname,parsed.port),timeout=0.3):
                    pass
            except OSError:
                return
            time.sleep(0.1)
        raise ValueError('后台仍在完成退出，请稍后再次关闭；不会强制终止同步')

    def finish_exit(self, restart=False, update=False):
        if not self.lock.acquire(blocking=False):
            return {'ok':False,'error':'正在退出工作台，请稍候'}
        try:
            if type(restart) is not bool or type(update) is not bool or self.window.get_current_url()!=self.url:
                raise ValueError('仅允许当前工作台请求退出')
            if self.update_busy:
                raise ValueError('正在检查或下载插件更新，请完成后再关闭或重启；窗口与编辑内容已保留')
            if update and (not self.updater or not self.updater.pending):
                raise ValueError('请先下载并校验更新')
            install_update = bool(self.updater and self.updater.pending and (
                update or self.updater.status()['mode']=='auto'))
            if install_update:
                try:
                    self.updater.check_installation_sessions()
                except Exception:
                    if update:
                        raise
                    logging.exception('Automatic plugin installation deferred; other workbenches may still be active')
                    install_update = False
            if self.candidates:
                self.candidates.check_exit()
            if self.tools:
                self.tools.check_exit()
            self._stop_backend()
            self.install_update = install_update
            self.restart = restart
            self.allow_close = True
            if self.candidates:
                self.candidates.dispose()
            if self.tools:
                self.tools.dispose()
            self.window.destroy()
            return {'ok':True}
        except Exception as exc:
            logging.exception('Desktop close deferred')
            if self.tools and not self.allow_close:
                try:
                    self.tools.cancel_exit()
                except Exception:
                    logging.exception('Could not resume tool windows')
            if self.candidates and not self.allow_close:
                try:
                    self.candidates.cancel_exit()
                except Exception:
                    logging.exception('Could not resume candidate window')
            return {'ok':False,'error':str(exc)}
        finally:
            self.lock.release()

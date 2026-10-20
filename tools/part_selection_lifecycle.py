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
            if self.app.search_jobs.busy() or self.app.selection_jobs.busy():
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

    def finish_exit(self, restart=False):
        return self._lifecycle.finish_exit(restart)


class DesktopLifecycle:
    """WebView bridge; never terminates an arbitrary PID or a KiCad process."""
    def __init__(self, url, token):
        self.url, self.token = url, token
        self.window = None
        self.allow_close = False
        self.restart = False
        self.shutdown_requested = False
        self.lock = threading.Lock()

    def on_closing(self):
        if self.allow_close:
            return True
        def prepare():
            try:
                self.window.evaluate_js('window.requestDesktopExit(false)')
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

    def finish_exit(self, restart=False):
        if not self.lock.acquire(blocking=False):
            return {'ok':False,'error':'正在退出工作台，请稍候'}
        try:
            if type(restart) is not bool or self.window.get_current_url()!=self.url:
                raise ValueError('仅允许当前工作台请求退出')
            self._stop_backend()
            self.restart = restart
            self.allow_close = True
            self.window.destroy()
            return {'ok':True}
        except Exception as exc:
            logging.exception('Desktop close deferred')
            return {'ok':False,'error':str(exc)}
        finally:
            self.lock.release()

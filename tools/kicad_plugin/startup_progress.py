"""Dependency-free Windows startup indicator with its own UI message pump."""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path


class StartupProgress:
    def __init__(self, title):
        self.title = title
        self.message = '正在启动…'
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._started = time.perf_counter()

    def start(self):
        if os.name == 'nt' and self._thread is None:
            self._thread = threading.Thread(target=self._run, name='startup-progress', daemon=True)
            self._thread.start()
            # Bound the initial paint wait; native UI failure must not block launch.
            self._ready.wait(0.3)

    def stage(self, message):
        self.message = message
        logging.info('Desktop startup stage=%s elapsed_ms=%.1f', message,
                     (time.perf_counter() - self._started) * 1000)

    def close(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    def _run(self):
        hwnd = None
        activation = None
        window_class = None
        cookie = ctypes.c_size_t()
        try:
            # Marquee requires Common Controls v6 even when the host Python
            # executable has no such manifest. Activation is UI-thread-local.
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            class Activation(ctypes.Structure):
                _fields_ = [('size', wintypes.ULONG), ('flags', wintypes.DWORD),
                    ('source', wintypes.LPCWSTR), ('architecture', wintypes.USHORT),
                    ('language', wintypes.USHORT), ('directory', wintypes.LPCWSTR),
                    ('resource', wintypes.LPCWSTR), ('application', wintypes.LPCWSTR),
                    ('module', wintypes.HMODULE)]
            kernel.CreateActCtxW.argtypes = [ctypes.POINTER(Activation)]
            kernel.CreateActCtxW.restype = wintypes.HANDLE
            kernel.ActivateActCtx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t)]
            kernel.DeactivateActCtx.argtypes = [wintypes.DWORD, ctypes.c_size_t]
            kernel.ReleaseActCtx.argtypes = [wintypes.HANDLE]
            context = Activation()
            context.size = ctypes.sizeof(context)
            context.source = str(Path(__file__).with_suffix('.manifest'))
            activation = kernel.CreateActCtxW(ctypes.byref(context))
            if activation == ctypes.c_void_p(-1).value:
                activation = None
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.ActivateActCtx(activation, ctypes.byref(cookie)):
                raise ctypes.WinError(ctypes.get_last_error())
            user = ctypes.WinDLL('user32', use_last_error=True)
            user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
            user.CreateWindowExW.restype = wintypes.HWND
            user.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user.DefWindowProcW.restype = wintypes.LPARAM
            procedure_type = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND,
                wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            class WindowClass(ctypes.Structure):
                _fields_ = [('style', wintypes.UINT), ('procedure', procedure_type),
                    ('class_extra', ctypes.c_int), ('window_extra', ctypes.c_int),
                    ('instance', wintypes.HINSTANCE), ('icon', wintypes.HICON),
                    ('cursor', wintypes.HANDLE), ('background', wintypes.HBRUSH),
                    ('menu', wintypes.LPCWSTR), ('name', wintypes.LPCWSTR)]
            user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
            user.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
            user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user.SendMessageW.restype = wintypes.LPARAM
            user.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
            user.DestroyWindow.argtypes = [wintypes.HWND]
            user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user.UpdateWindow.argtypes = [wintypes.HWND]
            user.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                          wintypes.UINT, wintypes.UINT, wintypes.UINT]
            user.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user.DispatchMessageW.restype = wintypes.LPARAM
            class Controls(ctypes.Structure):
                _fields_ = [('size', wintypes.DWORD), ('classes', wintypes.DWORD)]
            common = ctypes.WinDLL('comctl32', use_last_error=True)
            common.InitCommonControlsEx.argtypes = [ctypes.POINTER(Controls)]
            controls = Controls(ctypes.sizeof(Controls), 0x20)  # ICC_PROGRESS_CLASS
            if not common.InitCommonControlsEx(ctypes.byref(controls)):
                raise ctypes.WinError(ctypes.get_last_error())
            width, height = 440, 145
            x = max(0, (user.GetSystemMetrics(0) - width) // 2)
            y = max(0, (user.GetSystemMetrics(1) - height) // 2)
            # No close button: startup cannot abandon newly owned work halfway.
            procedure = procedure_type(user.DefWindowProcW)
            definition = WindowClass()
            definition.procedure = procedure
            definition.background = 16  # COLOR_BTNFACE + 1
            definition.name = f'KiCadPartSelectionStartup_{threading.get_native_id()}'
            if not user.RegisterClassW(ctypes.byref(definition)):
                raise ctypes.WinError(ctypes.get_last_error())
            window_class = definition.name
            # Stay visible above the initializing WebView without taking focus.
            hwnd = user.CreateWindowExW(0x88, window_class, self.title,
                0x00C00000 | 0x00800000, x, y, width, height, None, None, None, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            label = user.CreateWindowExW(0, 'STATIC', self.message, 0x50000000,
                20, 20, 395, 28, hwnd, None, None, None)
            bar = user.CreateWindowExW(0, 'msctls_progress32', '', 0x50000008,
                20, 63, 395, 18, hwnd, None, None, None)
            if not label or not bar:
                raise ctypes.WinError(ctypes.get_last_error())
            gdi = ctypes.WinDLL('gdi32')
            gdi.GetStockObject.argtypes = [ctypes.c_int]
            gdi.GetStockObject.restype = wintypes.HANDLE
            user.SendMessageW(label, 0x30, gdi.GetStockObject(17), 1)  # DEFAULT_GUI_FONT
            user.SendMessageW(bar, 0x40A, 1, 30)  # PBM_SETMARQUEE, no percentage
            if not self._stop.is_set():
                user.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
                user.UpdateWindow(hwnd)
            self._ready.set()
            logging.info('Desktop startup indicator ready elapsed_ms=%.1f',
                         (time.perf_counter() - self._started) * 1000)
            message = wintypes.MSG()
            previous = None
            while not self._stop.is_set():
                if previous != self.message:
                    previous = self.message
                    user.SetWindowTextW(label, previous)
                while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    user.TranslateMessage(ctypes.byref(message))
                    user.DispatchMessageW(ctypes.byref(message))
                self._stop.wait(0.03)
        except Exception:
            logging.exception('Could not display startup indicator; continuing GUI launch')
        finally:
            if hwnd:
                user.DestroyWindow(hwnd)
            if window_class:
                user.UnregisterClassW(window_class, None)
            if cookie.value:
                kernel.DeactivateActCtx(0, cookie)
            if activation:
                kernel.ReleaseActCtx(activation)
            self._ready.set()

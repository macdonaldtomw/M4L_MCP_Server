"""Find open Max editor windows (Windows only).

Overwriting a device file while its Max editor window is open makes Max's
reload fail, and closing that window afterwards has crashed Live. device_save
refuses in that case.
"""
from __future__ import annotations

import os
import sys


def top_windows():
    """[(title, exe_basename)] of visible top-level windows."""
    if sys.platform != 'win32':
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    out = []
    names = {}

    def exe_of(pid):
        if pid in names:
            return names[pid]
        name = ''
        h = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                n = wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                    name = os.path.basename(buf.value)
            finally:
                kernel32.CloseHandle(h)
        names[pid] = name
        return name

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append((buf.value, exe_of(pid.value)))
        return True

    user32.EnumWindows(cb, 0)
    return out


def open_max_editors(device_path: str):
    """Titles of Max-owned windows that show this device."""
    base = os.path.basename(device_path)
    stem = os.path.splitext(base)[0].lower()
    hits = []
    for title, exe in top_windows():
        if not exe.lower().startswith('max'):        # Max.exe / MaxRT.exe; not Live, not Bome
            continue
        t = title.lower()
        if stem and (stem in t or base.lower() in t):
            hits.append(title)
    return hits

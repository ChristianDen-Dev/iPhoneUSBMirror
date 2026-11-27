"""
winutil.py - small Windows helpers used by the receiver window (ctypes only,
no extra installs). Every function fails quietly on other systems or errors,
so the mirror keeps working even if one of these tricks is unavailable.
"""

import ctypes
import os
import sys
import uuid
from ctypes import wintypes

IS_WIN = sys.platform == "win32"

if IS_WIN:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:
        dwmapi = None

    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]

HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0010

# Known folder IDs (Windows "Videos" and "Pictures", follows OneDrive moves)
FOLDER_VIDEOS = "18989B1D-99B5-455B-841C-AB7C74E4DDFC"
FOLDER_PICTURES = "33E28130-4E1E-4676-835A-98395C3BC3BB"


def get_hwnd():
    """Window handle of the pygame window (0 if unknown)."""
    try:
        import pygame
        return int(pygame.display.get_wm_info().get("window", 0))
    except Exception:
        return 0


def set_dpi_aware():
    """Real pixels on high-DPI laptop screens (no blurry Windows upscaling)."""
    if not IS_WIN:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def ui_scale():
    """Windows display scale (1.0 = 100 %, 1.5 = 150 %)."""
    if IS_WIN:
        try:
            return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
        except Exception:
            pass
    return 1.0


def dark_title_bar(hwnd):
    """Windows 10/11 dark title bar to match the app."""
    if not (IS_WIN and hwnd and dwmapi):
        return
    value = ctypes.c_int(1)
    for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (new id, then old id)
        try:
            if dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr, ctypes.byref(value), 4) == 0:
                return
        except Exception:
            return


def set_topmost(hwnd, on):
    if IS_WIN and hwnd:
        try:
            user32.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        except Exception:
            pass


def window_rect(hwnd):
    """(left, top, right, bottom) of the whole window incl. title bar, or None."""
    if not (IS_WIN and hwnd):
        return None
    r = wintypes.RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return (r.left, r.top, r.right, r.bottom)
    return None


def move_window(hwnd, x, y):
    if IS_WIN and hwnd:
        try:
            user32.SetWindowPos(hwnd, 0, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        except Exception:
            pass


def work_area():
    """Desktop area without the taskbar: (left, top, right, bottom)."""
    if IS_WIN:
        r = wintypes.RECT()
        try:
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):   # SPI_GETWORKAREA
                return (r.left, r.top, r.right, r.bottom)
        except Exception:
            pass
    return None


def visible_rect(hwnd):
    """Window rect WITHOUT the invisible Windows 10/11 resize borders."""
    if IS_WIN and hwnd and dwmapi:
        r = wintypes.RECT()
        try:
            if dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r)) == 0:
                return (r.left, r.top, r.right, r.bottom)   # 9 = DWMWA_EXTENDED_FRAME_BOUNDS
        except Exception:
            pass
    return window_rect(hwnd)


def keep_on_screen(hwnd):
    """Slide the window back inside the work area if part of it is off-screen
    (measured by its visible edges, so a full-width window fits exactly)."""
    wr, vr, wa = window_rect(hwnd), visible_rect(hwnd), work_area()
    if not (wr and vr and wa):
        return
    w, h = vr[2] - vr[0], vr[3] - vr[1]
    x = min(max(vr[0], wa[0]), max(wa[0], wa[2] - w))
    y = min(max(vr[1], wa[1]), max(wa[1], wa[3] - h))
    if (x, y) != (vr[0], vr[1]):
        move_window(hwnd, wr[0] + x - vr[0], wr[1] + y - vr[1])


def frame_extra(hwnd=0):
    """(width, height) the visible title bar + borders add around the picture."""
    vr = visible_rect(hwnd) if hwnd else None
    if IS_WIN and hwnd and vr:
        try:
            c = wintypes.RECT()
            user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(c))
            ew, eh = (vr[2] - vr[0]) - c.right, (vr[3] - vr[1]) - c.bottom
            if 0 <= ew < 100 and 20 <= eh < 150:      # (a borderless window gives eh < 20)
                return ew, eh
        except Exception:
            pass
    try:
        return 2, ctypes.windll.user32.GetSystemMetrics(4) + 2  # caption height + border
    except Exception:
        return 2, 32


def popup_menu(hwnd, items):
    """Native right-click menu at the mouse. It is its own window, so OBS
    Window Capture of the mirror never shows it.

    items: list of (key, text, checked, enabled) or None for a separator.
    Returns the chosen key or None."""
    if not (IS_WIN and hwnd):
        return None
    MF_STRING, MF_SEPARATOR, MF_CHECKED, MF_GRAYED = 0x0, 0x800, 0x8, 0x1
    TPM_RETURNCMD, TPM_RIGHTBUTTON, TPM_NONOTIFY = 0x0100, 0x0002, 0x0080
    menu = user32.CreatePopupMenu()
    if not menu:
        return None
    keys = {}
    try:
        for i, item in enumerate(items, start=1):
            if item is None:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                continue
            key, text, checked, enabled = item
            flags = MF_STRING | (MF_CHECKED if checked else 0) | (0 if enabled else MF_GRAYED)
            user32.AppendMenuW(menu, flags, i, text)
            keys[i] = key
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)
        chosen = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
                                       pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, 0, 0, 0)   # WM_NULL: documented fix so the menu closes properly
        return keys.get(chosen)
    finally:
        user32.DestroyMenu(menu)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def known_folder(folder_id, fallback_name):
    """Path of a Windows library folder (Videos / Pictures)."""
    if IS_WIN:
        try:
            u = uuid.UUID(folder_id)
            g = _GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
            p = ctypes.c_wchar_p()
            if shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
                path = p.value
                ole32.CoTaskMemFree(p)
                if path:
                    return path
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), fallback_name)


def open_folder(path):
    try:
        os.makedirs(path, exist_ok=True)
        os.startfile(path)
    except Exception:
        pass

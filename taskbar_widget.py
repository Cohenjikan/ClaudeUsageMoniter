"""Native taskbar widget window — the Soda Music technique, in ctypes.

A WS_POPUP layered window, created on its own thread with per-monitor DPI
awareness (so it renders crisp at 125%/150% while the tkinter app stays
DPI-unaware), and OWNED by the taskbar (Shell_TrayWnd). Owned windows always
sit above their owner, so the widget stays on the taskbar band without any
topmost bump loop.

Content is a premultiplied BGRA bitmap pushed with UpdateLayeredWindow: real
per-pixel alpha (anti-aliased shapes, no colour-key halos). Pixels drawn with
alpha 0 are click-through, so the renderer paints an alpha-1 backdrop.

Thread model: every Win32 call on the widget window happens on the widget
thread. The tk thread talks to it only through `update()/show()/hide()/
set_side()/close()` (store state + PostMessage) and reads input back from the
thread-safe `events` queue: ("click",), ("menu",), ("hover", bool),
("rerender",).
"""
from __future__ import annotations

import ctypes
import logging
import queue
import threading
import winreg
from ctypes import wintypes

from PIL import Image, ImageChops

log = logging.getLogger(__name__)

u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]


def _sig(fn, res, *args):
    fn.restype = res
    fn.argtypes = list(args)


H, U, W, L = wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
_sig(u32.DefWindowProcW, LRESULT, H, U, W, L)
_sig(u32.RegisterClassExW, wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
_sig(u32.CreateWindowExW, H, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
     wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     H, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
_sig(u32.DestroyWindow, wintypes.BOOL, H)
_sig(u32.ShowWindow, wintypes.BOOL, H, ctypes.c_int)
_sig(u32.PostMessageW, wintypes.BOOL, H, U, W, L)
_sig(u32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), H, U, U)
_sig(u32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
_sig(u32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
_sig(u32.PostQuitMessage, None, ctypes.c_int)
_sig(u32.FindWindowW, H, wintypes.LPCWSTR, wintypes.LPCWSTR)
_sig(u32.FindWindowExW, H, H, H, wintypes.LPCWSTR, wintypes.LPCWSTR)
_sig(u32.GetWindowRect, wintypes.BOOL, H, ctypes.POINTER(wintypes.RECT))
_sig(u32.IsWindow, wintypes.BOOL, H)
_sig(u32.IsWindowVisible, wintypes.BOOL, H)
_sig(u32.GetWindowLongPtrW, ctypes.c_ssize_t, H, ctypes.c_int)
_sig(u32.SetWindowLongPtrW, ctypes.c_ssize_t, H, ctypes.c_int, ctypes.c_ssize_t)
_sig(u32.SetWindowPos, wintypes.BOOL, H, H, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, ctypes.c_int, U)
_sig(u32.SetTimer, ctypes.c_size_t, H, ctypes.c_size_t, U, ctypes.c_void_p)
_sig(u32.RegisterWindowMessageW, U, wintypes.LPCWSTR)
_sig(u32.TrackMouseEvent, wintypes.BOOL, ctypes.POINTER(TRACKMOUSEEVENT))
_sig(u32.GetDC, wintypes.HDC, H)
_sig(u32.ReleaseDC, ctypes.c_int, H, wintypes.HDC)
_sig(u32.UpdateLayeredWindow, wintypes.BOOL, H, wintypes.HDC,
     ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
     ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
     ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD)
_sig(u32.LoadCursorW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPVOID)
_sig(u32.GetDpiForWindow, U, H)
_sig(u32.SetThreadDpiAwarenessContext, ctypes.c_void_p, ctypes.c_void_p)
_sig(g32.CreateCompatibleDC, wintypes.HDC, wintypes.HDC)
_sig(g32.DeleteDC, wintypes.BOOL, wintypes.HDC)
_sig(g32.CreateDIBSection, wintypes.HBITMAP, wintypes.HDC, ctypes.c_void_p, U,
     ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)
_sig(g32.SelectObject, wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
_sig(g32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
_sig(k32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW = 0x8, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
GWL_EXSTYLE, GWLP_HWNDPARENT = -20, -8
HWND_TOPMOST, HWND_NOTOPMOST = H(-1), H(-2)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
WM_DESTROY, WM_TIMER, WM_MOUSEMOVE = 0x2, 0x113, 0x200
WM_LBUTTONUP, WM_RBUTTONUP, WM_MOUSELEAVE = 0x202, 0x205, 0x2A3
WM_MOUSEACTIVATE, WM_SETTINGCHANGE, WM_DPICHANGED = 0x21, 0x1A, 0x2E0
WM_DISPLAYCHANGE, WM_CLOSE = 0x7E, 0x10
MA_NOACTIVATE = 3
TME_LEAVE = 0x2
WM_APP_UPDATE, WM_APP_SHOW, WM_APP_HIDE = 0x8001, 0x8002, 0x8003
DPI_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
IDC_HAND = 32649

_EXPLORER_ADV = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def _reg_dword(path: str, name: str, default: int) -> int:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return int(winreg.QueryValueEx(k, name)[0])
    except OSError:
        return default


def taskbar_is_light() -> bool:
    """Taskbar follows the *system* theme (not the app theme)."""
    return _reg_dword(_PERSONALIZE, "SystemUsesLightTheme", 0) == 1


def taskbar_left_aligned() -> bool:
    """Win11 TaskbarAl: 0 = left-aligned (Start button sits at the far left)."""
    return _reg_dword(_EXPLORER_ADV, "TaskbarAl", 1) == 0


def _premultiplied_bgra(img: Image.Image) -> bytes:
    r, g, b, a = img.convert("RGBA").split()
    r, g, b = (ImageChops.multiply(c, a) for c in (r, g, b))
    return Image.merge("RGBA", (r, g, b, a)).tobytes("raw", "BGRA")


class NativeTaskbarWidget:
    MARGIN = 12  # logical px from the taskbar edge / tray area

    def __init__(self, side: str = "left") -> None:
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.side = side
        self.scale = 1.0
        self.hwnd = 0
        self._lock = threading.Lock()
        self._image: Image.Image | None = None
        self._visible = True
        self._hover = False
        self._taskbar = 0
        self._msg_taskbar_created = -1  # set once the window exists
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="taskbar-widget",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(5)

    # ------------------------------------------------ tk-thread API
    def update(self, img: Image.Image) -> None:
        with self._lock:
            self._image = img
        self._post(WM_APP_UPDATE)

    def show(self) -> None:
        self._visible = True
        self._post(WM_APP_SHOW)

    def hide(self) -> None:
        self._visible = False
        self._post(WM_APP_HIDE)

    def set_side(self, side: str) -> None:
        self.side = side
        self._post(WM_APP_UPDATE)

    def close(self) -> None:
        self._post(WM_CLOSE)

    def _post(self, msg: int) -> None:
        if self.hwnd:
            u32.PostMessageW(self.hwnd, msg, 0, 0)

    # ------------------------------------------------ widget thread
    def _run(self) -> None:
        try:
            u32.SetThreadDpiAwarenessContext(DPI_PER_MONITOR_AWARE_V2)
        except (AttributeError, OSError):
            pass  # pre-1703 Windows: falls back to unaware (blurrier, works)
        hinst = k32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._proc)  # keep a reference!
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.hCursor = u32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND))
        wc.lpszClassName = "ClaudeUsageTaskbarWidget"
        u32.RegisterClassExW(ctypes.byref(wc))
        self._taskbar = int(u32.FindWindowW("Shell_TrayWnd", None) or 0)
        self.hwnd = int(u32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
            wc.lpszClassName, "Claude Usage", WS_POPUP, 0, 0, 1, 1,
            H(self._taskbar or 0), None, hinst, None) or 0)
        if not self.hwnd:
            log.error("taskbar widget: CreateWindowEx failed (%d)",
                      ctypes.get_last_error())
            self._ready.set()
            return
        self._msg_taskbar_created = u32.RegisterWindowMessageW("TaskbarCreated")
        self._refresh_scale()
        u32.SetTimer(self.hwnd, 1, 1000, None)
        log.info("taskbar widget: hwnd=%#x owner=%#x scale=%.2f",
                 self.hwnd, self._taskbar, self.scale)
        self._ready.set()
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

    def _refresh_scale(self) -> None:
        tb = self._taskbar or self.hwnd
        dpi = u32.GetDpiForWindow(tb) if tb else 96
        new = (dpi or 96) / 96
        if abs(new - self.scale) > 1e-3:
            self.scale = new
            self.events.put(("rerender",))

    def _ensure_owner(self) -> None:
        tb = int(u32.FindWindowW("Shell_TrayWnd", None) or 0)
        if tb and (tb != self._taskbar
                   or int(u32.GetWindowLongPtrW(self.hwnd, GWLP_HWNDPARENT)) != tb):
            self._taskbar = tb
            u32.SetWindowLongPtrW(self.hwnd, GWLP_HWNDPARENT, tb)
            log.info("taskbar widget: re-attached to taskbar %#x", tb)
            self._refresh_scale()
        # Mirror the taskbar's topmost state: when a fullscreen app demotes
        # the taskbar we must not float above that app either.
        if self._taskbar:
            tb_top = bool(u32.GetWindowLongPtrW(self._taskbar, GWL_EXSTYLE) & WS_EX_TOPMOST)
            u32.SetWindowPos(self.hwnd, HWND_TOPMOST if tb_top else HWND_NOTOPMOST,
                             0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _target_pos(self, w: int, h: int) -> tuple[int, int] | None:
        if not self._taskbar:
            return None
        r = wintypes.RECT()
        if not u32.GetWindowRect(self._taskbar, ctypes.byref(r)):
            return None
        tw, th = r.right - r.left, r.bottom - r.top
        if th >= tw:  # vertical taskbar (via mods) — not supported, park off
            return None
        m = round(self.MARGIN * self.scale)
        side = self.side
        # A left-aligned taskbar puts Start at the far left; don't cover it.
        if side == "left" and taskbar_left_aligned():
            side = "right"
        if side == "left":
            x = r.left + m
        else:
            tray = u32.FindWindowExW(self._taskbar, None, "TrayNotifyWnd", None)
            tr = wintypes.RECT()
            if tray and u32.GetWindowRect(tray, ctypes.byref(tr)):
                x = tr.left - w - m
            else:
                x = r.right - w - m
        y = r.top + (th - h) // 2
        return x, y

    def _apply(self) -> None:
        with self._lock:
            img = self._image
        if img is None:
            return
        w, h = img.size
        pos = self._target_pos(w, h)
        if pos is None:
            u32.ShowWindow(self.hwnd, SW_HIDE)
            return
        bits = _premultiplied_bgra(img)
        screen = u32.GetDC(None)
        mem = g32.CreateCompatibleDC(screen)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0,
                               0, 0, 0, 0, 0)
        ptr = ctypes.c_void_p()
        dib = g32.CreateDIBSection(screen, ctypes.byref(bmi), 0,
                                   ctypes.byref(ptr), None, 0)
        try:
            if not dib or not ptr.value:
                return
            ctypes.memmove(ptr.value, bits, len(bits))
            old = g32.SelectObject(mem, dib)
            blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
            ok = u32.UpdateLayeredWindow(
                self.hwnd, screen, ctypes.byref(wintypes.POINT(*pos)),
                ctypes.byref(wintypes.SIZE(w, h)), mem,
                ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), 2)
            g32.SelectObject(mem, old)
            if not ok:
                log.warning("UpdateLayeredWindow failed (%d)", ctypes.get_last_error())
        finally:
            if dib:
                g32.DeleteObject(dib)
            g32.DeleteDC(mem)
            u32.ReleaseDC(None, screen)
        if self._visible and not u32.IsWindowVisible(self.hwnd):
            u32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

    def _proc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_APP_UPDATE:
                self._apply()
                return 0
            if msg == WM_APP_SHOW:
                self._apply()
                u32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                return 0
            if msg == WM_APP_HIDE:
                u32.ShowWindow(hwnd, SW_HIDE)
                return 0
            if msg == WM_TIMER:
                self._ensure_owner()
                self._refresh_scale()
                self._apply()  # follows taskbar moves / auto-hide / tray width
                return 0
            if msg == self._msg_taskbar_created:  # explorer restarted
                self._ensure_owner()
                self._apply()
                return 0
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_MOUSEMOVE:
                if not self._hover:
                    self._hover = True
                    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT),
                                          TME_LEAVE, hwnd, 0)
                    u32.TrackMouseEvent(ctypes.byref(tme))
                    self.events.put(("hover", True))
                return 0
            if msg == WM_MOUSELEAVE:
                self._hover = False
                self.events.put(("hover", False))
                return 0
            if msg == WM_LBUTTONUP:
                self.events.put(("click",))
                return 0
            if msg == WM_RBUTTONUP:
                self.events.put(("menu",))
                return 0
            if msg in (WM_SETTINGCHANGE, WM_DPICHANGED, WM_DISPLAYCHANGE):
                self._refresh_scale()
                self.events.put(("rerender",))  # theme may have flipped
            if msg == WM_CLOSE:
                u32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self.hwnd = 0
                u32.PostQuitMessage(0)
                return 0
        except Exception:  # never let an exception unwind into user32
            log.exception("taskbar widget wndproc failed (msg=%#x)", msg)
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

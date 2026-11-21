import colorsys
import ctypes
import ctypes.wintypes
import io
import math
import os
import subprocess
import sys
import threading
import time
import traceback
import winreg
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from PIL import Image
from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from config import APP_DIR, load_config, save_config
from smtc_listener import SpotifySMTCListener, TrackState
from spotify_api import REDIRECT_URI, SpotifyExtraState, SpotifyWebAPI, TrackItem

# --- Crash Logger (Helpful when running silently under pythonw.exe) ---

ERROR_LOG_PATH = APP_DIR / "now_bar_error.log"


def _install_error_hook() -> None:
    """Logs any uncaught exceptions to now_bar_error.log when running without a console."""
    def _hook(exc_type, exc_value, exc_tb):
        try:
            with ERROR_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


# --- Win32, DWM, Process & Startup Registry Helpers ---

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_BORDER = 0x00800000
WS_THICKFRAME = 0x00040000
WS_DLGFRAME = 0x00400000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
MONITOR_DEFAULTTONEAREST = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

DWMWA_NCRENDERING_POLICY = 2
DWMNCRP_DISABLED = 1
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DONOTROUND = 1
DWMWA_BORDER_COLOR = 34
DWMWA_COLOR_NONE = 0xFFFFFFFE

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "SpotifyNowBar"


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork", ctypes.wintypes.RECT),
        ("dwFlags", ctypes.wintypes.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("cntUsage", ctypes.wintypes.DWORD),
        ("th32ProcessID", ctypes.wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.wintypes.DWORD),
        ("cntThreads", ctypes.wintypes.DWORD),
        ("th32ParentProcessID", ctypes.wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.DWORD]
kernel32.Process32FirstW.restype = ctypes.wintypes.BOOL
kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = ctypes.wintypes.BOOL
kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def is_spotify_running() -> bool:
    """Fast native Win32 snapshot check for Spotify.exe (zero subprocesses, <1ms)."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return False
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                if entry.szExeFile.lower() == "spotify.exe":
                    return True
                if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    break
    except Exception:
        pass
    finally:
        kernel32.CloseHandle(snap)
    return False


def disable_dwm_frame(hwnd: int) -> None:
    """Removes the Windows 11 DWM rounded box outline & drop shadow on transparent windows."""
    try:
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        style &= ~(WS_BORDER | WS_THICKFRAME | WS_DLGFRAME)
        user32.SetWindowLongW(hwnd, GWL_STYLE, style)

        val_nc = ctypes.c_int(DWMNCRP_DISABLED)
        dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_NCRENDERING_POLICY, ctypes.byref(val_nc), ctypes.sizeof(val_nc)
        )

        val_corner = ctypes.c_int(DWMWCP_DONOTROUND)
        dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(val_corner),
            ctypes.sizeof(val_corner),
        )

        val_border = ctypes.c_uint32(DWMWA_COLOR_NONE)
        dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_BORDER_COLOR, ctypes.byref(val_border), ctypes.sizeof(val_border)
        )

        user32.SetWindowPos(
            hwnd,
            0,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
    except Exception:
        pass


def set_click_through(hwnd: int, enable: bool) -> None:
    """Toggles Win32 WS_EX_TRANSPARENT so the hidden bar never blocks browser tabs."""
    try:
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enable:
            style |= WS_EX_TRANSPARENT | WS_EX_LAYERED
        else:
            style &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception:
        pass


def is_foreground_fullscreen(own_hwnd: int) -> bool:
    """Detects whether the currently active window is covering the entire monitor."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd or hwnd == own_hwnd:
            return False
        cls = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, cls, 128)
        if cls.value in (
            "Progman",
            "WorkerW",
            "Shell_TrayWnd",
            "Shell_SecondaryTrayWnd",
        ):
            return False
        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return False
        mr = mi.rcMonitor
        return (
            rect.left <= mr.left
            and rect.top <= mr.top
            and rect.right >= mr.right
            and rect.bottom >= mr.bottom
        )
    except Exception:
        return False


def _get_exe_name(hwnd: int) -> str:
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not hproc:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = ctypes.wintypes.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(hproc)
    return ""


def open_spotify_app() -> None:
    """Restores Spotify from minimized/tray or launches it if closed."""
    found_hwnd = [0]
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )

    def _cb(hwnd, _lparam):
        if _get_exe_name(hwnd) == "spotify.exe":
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, cls, 128)
            if cls.value.startswith("Chrome_WidgetWin"):
                title = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title, 256)
                if title.value or user32.IsWindowVisible(hwnd):
                    found_hwnd[0] = hwnd
                    return False
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(_cb), 0)
    except Exception:
        pass

    hwnd = found_hwnd[0]
    if hwnd and user32.IsWindowVisible(hwnd):
        try:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            else:
                user32.ShowWindow(hwnd, 5)  # SW_SHOW
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(hwnd)
            return
        except Exception:
            pass

    try:
        os.startfile("spotify:")
    except Exception:
        pass


def _get_pythonw_path() -> str:
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pyw if os.path.exists(pyw) else sys.executable


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return bool(val)
    except OSError:
        return False


def set_autostart_enabled(enable: bool) -> bool:
    """Adds or removes silent pythonw.exe startup entry in HKCU\\...\\Run."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enable:
                cmd = f'"{_get_pythonw_path()}" "{os.path.abspath(__file__)}" --detached'
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


# --- Dynamic Album-Art Color Extraction & Utilities ---

def extract_accent_colors(img_bytes: Optional[bytes]) -> Tuple[QColor, QColor]:
    """Samples the cover art at 24x24 to pick vibrant dark-glass accent & glow colors."""
    fallback = (QColor(29, 185, 84), QColor(20, 120, 68))
    if not img_bytes:
        return fallback
    try:
        im = (
            Image.open(io.BytesIO(img_bytes))
            .convert("RGB")
            .resize((24, 24), Image.Resampling.BILINEAR)
        )
        pixels = list(im.getdata())
        best_score = -1.0
        best_rgb = (29, 185, 84)
        for r, g, b in pixels:
            h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            if v < 0.18:
                continue
            score = (s ** 1.35) * (0.4 + 0.6 * (1.0 - abs(v - 0.68)))
            if score > best_score:
                best_score = score
                best_rgb = (r, g, b)

        r, g, b = best_rgb
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        s1 = max(0.40, min(0.86, s))
        v1 = max(0.64, min(0.94, v))
        pr, pg, pb = [int(x * 255) for x in colorsys.hsv_to_rgb(h, s1, v1)]
        sr, sg, sb = [
            int(x * 255) for x in colorsys.hsv_to_rgb((h + 0.06) % 1.0, s1 * 0.8, v1 * 0.55)
        ]
        return QColor(pr, pg, pb), QColor(sr, sg, sb)
    except Exception:
        return fallback


def fmt_time(ms: int) -> str:
    sec = max(0, int(ms // 1000))
    return f"{sec // 60}:{sec % 60:02d}"


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    return QColor(
        int(lerp(c1.red(), c2.red(), t)),
        int(lerp(c1.green(), c2.green(), t)),
        int(lerp(c1.blue(), c2.blue(), t)),
        int(lerp(c1.alpha(), c2.alpha(), t)),
    )


# --- Focusable Search Box for Frameless Overlay ---

class SearchInput(QLineEdit):
    def mousePressEvent(self, event):
        win = self.window()
        if win:
            win.activateWindow()
        self.setFocus()
        super().mousePressEvent(event)


# --- Spotify PKCE Connection Dialog ---

class SpotifyConnectDialog(QDialog):
    auth_finished = Signal(bool, str)

    def __init__(self, api: SpotifyWebAPI, current_client_id: str, parent=None):
        super().__init__(parent)
        self._api = api
        self.setWindowTitle("Connect Spotify Web API (PKCE)")
        self.setFixedWidth(460)
        self.setStyleSheet("""
            QDialog { background: #14161A; color: #F1F5F9; font-family: 'Segoe UI'; }
            QLabel { color: #CBD5E1; font-size: 13px; }
            QLineEdit {
                background: #1E2229; color: #FFFFFF; border: 1px solid #334155;
                border-radius: 8px; padding: 8px 10px; font-size: 13px;
            }
            QPushButton {
                background: #1DB954; color: #08120B; border: none; border-radius: 8px;
                padding: 9px 16px; font-weight: 600; font-size: 13px;
            }
            QPushButton:hover { background: #22C55E; }
            QPushButton:disabled { background: #334155; color: #94A3B8; }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        info = QLabel(
            "<b>Connect / Re-link Spotify (Heart • Queue • Liked • Playlists • Search • Volume):</b><br><br>"
            "1. Open <b>developer.spotify.com/dashboard</b> → your App.<br>"
            f"2. Redirect URI must be: <code>{REDIRECT_URI}</code><br>"
            "3. Paste your <b>Client ID</b> below and authorize in your browser."
        )
        info.setWordWrap(True)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(info)

        self.input_id = QLineEdit(current_client_id)
        self.input_id.setPlaceholderText("Paste your Spotify Client ID here...")
        layout.addWidget(self.input_id)

        self.status_lbl = QLabel("")
        layout.addWidget(self.status_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_connect = QPushButton("Authorize in Browser")
        self.btn_connect.clicked.connect(self._start_auth)
        btn_row.addWidget(self.btn_connect)
        layout.addLayout(btn_row)

        self.auth_finished.connect(self._on_auth_done)

    def _start_auth(self):
        self.btn_connect.setEnabled(False)
        self.status_lbl.setText("Waiting for Spotify login in your browser...")
        self._api.start_pkce_auth(
            self.input_id.text(),
            lambda ok, msg: self.auth_finished.emit(ok, msg),
        )

    @Slot(bool, str)
    def _on_auth_done(self, ok: bool, msg: str):
        self.btn_connect.setEnabled(True)
        self.status_lbl.setText(msg)
        if ok:
            QMessageBox.information(
                self, "Connected", "Spotify Web API is linked to Now Bar!"
            )
            self.accept()


# --- Main Floating One UI Now Bar (27" 1440p Tuned & iGPU Optimized) ---

class SpotifyNowBar(QWidget):
    smtc_updated = Signal(object)
    api_updated = Signal(object)
    drawer_loaded = Signal(str, object, str)
    toast_signal = Signal(str)

    CANVAS_W = 504
    CANVAS_H = 568
    PILL_W, PILL_H = 386, 58
    CARD_W, CARD_H = 468, 224
    DRAWER_H = 304
    TOP_MARGIN = 14
    ROW_H = 50

    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        if "only_when_spotify_running" not in self.cfg:
            self.cfg["only_when_spotify_running"] = True
            save_config(self.cfg)
        self.api = SpotifyWebAPI(self.cfg)

        # Frameless, always on top, no taskbar icon, no Windows 11 DWM shadow box
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.resize(self.CANVAS_W, self.CANVAS_H)

        # Pre-created fonts to avoid per-frame allocations on iGPU
        self._f_badge = QFont("Segoe UI Semibold", 7)
        self._f_title_c = QFont("Segoe UI Semibold", 10)
        self._f_artist_c = QFont("Segoe UI", 8)
        self._f_title_e = QFont("Segoe UI Semibold", 12)
        self._f_artist_e = QFont("Segoe UI", 9)
        self._f_small = QFont("Segoe UI", 8)
        self._f_small_b = QFont("Segoe UI Semibold", 8)
        self._f_row_t = QFont("Segoe UI Semibold", 9)
        self._f_row_a = QFont("Segoe UI", 8)
        self._f_arrow = QFont("Segoe UI", 13)
        self._f_rep1 = QFont("Segoe UI Bold", 6)

        # Embedded lightweight search input (visible only on Search tab)
        self._search_input = SearchInput(self)
        self._search_input.setPlaceholderText("Search songs or artists on Spotify... (Enter)")
        self._search_input.setStyleSheet("""
            QLineEdit {
                background: rgba(255, 255, 255, 18);
                color: #F8FAFC;
                border: 1px solid rgba(255, 255, 255, 36);
                border-radius: 10px;
                padding: 5px 12px;
                font-family: 'Segoe UI';
                font-size: 12px;
            }
            QLineEdit:focus {
                border: 1px solid rgba(29, 185, 84, 190);
                background: rgba(255, 255, 255, 24);
            }
        """)
        self._search_input.hide()
        self._search_input.returnPressed.connect(self._run_search_now)

        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(420)
        self._search_debounce.timeout.connect(self._run_search_now)
        self._search_input.textChanged.connect(lambda _t: self._search_debounce.start())

        # Playback, Spotify process & API state
        self.track = TrackState()
        self.extra = SpotifyExtraState(connected=self.api.is_connected)
        self._spotify_running = is_spotify_running()
        self._last_track_key = ""
        self._cover_bytes_hash = 0
        self._cover_compact_pm: Optional[QPixmap] = None
        self._cover_expanded_pm: Optional[QPixmap] = None

        # Auto-skip filter for "Slide Left to Remove from Queue"
        self._removed_queue_keys: Set[str] = set()

        # Drawer state: None | "queue" | "liked" | "library" | "search"
        self._drawer_tab: Optional[str] = None
        self._drawer_p = 0.0
        self._drawer_scroll = 0.0
        self._drawer_loading = False
        self._drawer_status = ""
        self._active_playlist: Optional[TrackItem] = None

        self._liked_items: List[TrackItem] = []
        self._playlist_items: List[TrackItem] = []
        self._playlist_tracks: List[TrackItem] = []
        self._search_items: List[TrackItem] = []

        # Row swipe/slide state
        self._drag_row_idx = -1
        self._drag_start_x = 0.0
        self._drag_start_y = 0.0
        self._drag_dx = 0.0
        self._dragging_row = False
        self._hover_row_idx = -1
        self._list_viewport = QRectF()

        # Dynamic accent colors
        self._accent_from = QColor(29, 185, 84)
        self._accent_to = QColor(29, 185, 84)
        self._glow_from = QColor(20, 120, 68)
        self._glow_to = QColor(20, 120, 68)
        self._accent_cur = QColor(29, 185, 84)
        self._glow_cur = QColor(20, 120, 68)

        # Visual state
        self._state = "hidden"
        self._is_click_through = False
        self._drop_p = 0.0
        self._expand_p = 0.0
        self._hover_btn = ""
        self._hitboxes: Dict[str, QRectF] = {}
        self._seeking = False
        self._seek_preview_ratio = 0.0
        self._dragging_vol = False

        # Hover-expandable volume popover state
        self._vol_open = False
        self._vol_open_p = 0.0
        self._vol_pill_rect = QRectF()
        self._vol_close_timer = QTimer(self)
        self._vol_close_timer.setSingleShot(True)
        self._vol_close_timer.setInterval(260)
        self._vol_close_timer.timeout.connect(self._close_vol_slider)

        self._vol_anim = QVariantAnimation(self)
        self._vol_anim.valueChanged.connect(self._on_vol_anim_val)

        # Animated wavy progress slider + marquee + 7-bar visualizer
        self._phase = 0.0
        self._wave_amp = 0.0
        self._vis_energy = 0.0
        self._marquee_x = 0.0

        # Volume & Action Toast
        self._toast_text = ""
        self._toast_until = 0.0
        self._vol_hud_value = 50
        self._vol_prev_nonzero = 50
        self._vol_debounce = QTimer(self)
        self._vol_debounce.setSingleShot(True)
        self._vol_debounce.setInterval(120)
        self._vol_debounce.timeout.connect(self._flush_volume_api)

        # Dwell & auto-hide timers
        self._edge_hover_since: Optional[float] = None
        self._leave_timer = QTimer(self)
        self._leave_timer.setSingleShot(True)
        self._leave_timer.setInterval(450)
        self._leave_timer.timeout.connect(self._on_leave_timeout)

        self._peek_timer = QTimer(self)
        self._peek_timer.setSingleShot(True)
        self._peek_timer.setInterval(3500)
        self._peek_timer.timeout.connect(self._on_peek_timeout)

        # Spring & smooth animations
        self._drop_anim = QVariantAnimation(self)
        self._drop_anim.valueChanged.connect(self._on_drop_val)
        self._drop_anim.finished.connect(self._on_drop_finished)

        self._expand_anim = QVariantAnimation(self)
        self._expand_anim.valueChanged.connect(self._on_expand_val)

        self._drawer_anim = QVariantAnimation(self)
        self._drawer_anim.setDuration(260)
        self._drawer_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._drawer_anim.valueChanged.connect(self._on_drawer_val)

        self._color_anim = QVariantAnimation(self)
        self._color_anim.setDuration(460)
        self._color_anim.setStartValue(0.0)
        self._color_anim.setEndValue(1.0)
        self._color_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._color_anim.valueChanged.connect(self._on_color_val)

        # 30fps visual loop (stopped completely at 0% CPU when hidden)
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(33)
        self._anim_timer.timeout.connect(self._on_visual_tick)

        # 60ms top-edge hover sensor
        self._sensor_timer = QTimer(self)
        self._sensor_timer.setInterval(60)
        self._sensor_timer.timeout.connect(self._poll_top_edge)
        self._sensor_timer.start()

        # Lightweight 1.5s Spotify process watcher
        self._proc_timer = QTimer(self)
        self._proc_timer.setInterval(1500)
        self._proc_timer.timeout.connect(self._check_spotify_process)
        self._proc_timer.start()

        # Signals & SMTC listener
        self.smtc_updated.connect(self._handle_smtc)
        self.api_updated.connect(self._handle_api)
        self.drawer_loaded.connect(self._handle_drawer_loaded)
        self.toast_signal.connect(self._show_toast)

        self.smtc = SpotifySMTCListener(lambda st: self.smtc_updated.emit(st))
        self.smtc.start()

        self._reposition()
        self._setup_tray()
        self.show()
        disable_dwm_frame(int(self.winId()))

        # Apply saved pin mode only if allowed (Spotify running or setting off)
        pin = self.cfg.get("pin_mode", "auto")
        if self._is_bar_allowed() and pin == "compact":
            self._transition_to("compact")
        elif self._is_bar_allowed() and pin == "expanded":
            self._transition_to("expanded")
        else:
            self._apply_click_through(True)

    # --- Spotify Running Guard ---

    def _is_bar_allowed(self) -> bool:
        if not self.cfg.get("only_when_spotify_running", True):
            return True
        return self._spotify_running or self.track.active

    def _check_spotify_process(self) -> None:
        running = self.track.active or is_spotify_running()
        if running == self._spotify_running:
            return
        self._spotify_running = running

        if not self._is_bar_allowed():
            self._edge_hover_since = None
            self._peek_timer.stop()
            self._leave_timer.stop()
            self._close_drawer()
            self._set_vol_open(False)
            self._transition_to("hidden")
            self.update()
        else:
            pin = self.cfg.get("pin_mode", "auto")
            if pin == "compact":
                self._transition_to("compact")
            elif pin == "expanded":
                self._transition_to("expanded")
            self.update()

    # --- Positioning & System Tray ---

    def _reposition(self) -> None:
        screen = QApplication.primaryScreen()
        if not screen:
            return
        geo = screen.geometry()
        x = geo.x() + (geo.width() - self.CANVAS_W) // 2
        y = geo.y()
        self.move(x, y)

    def _create_tray_icon(self) -> QIcon:
        pm = QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(29, 185, 84))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(4, 14, 56, 36, 18, 18)
        p.setBrush(QColor(10, 14, 12))
        p.drawRoundedRect(18, 25, 6, 14, 3, 3)
        p.drawRoundedRect(29, 20, 6, 24, 3, 3)
        p.drawRoundedRect(40, 27, 6, 10, 3, 3)
        p.end()
        return QIcon(pm)

    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(self._create_tray_icon(), self)
        self.tray.setToolTip("Spotify Now Bar")
        menu = QMenu()

        pin_menu = menu.addMenu("Display Mode")
        pin_group = QActionGroup(self)
        self._pin_actions: Dict[str, QAction] = {}
        for mode, label in [
            ("auto", "Auto-Hide (Hover Top-Center)"),
            ("compact", "Pin Compact Pill"),
            ("expanded", "Pin Expanded Card"),
        ]:
            act = QAction(label, self, checkable=True)
            act.setChecked(self.cfg.get("pin_mode") == mode)
            act.triggered.connect(lambda _=False, m=mode: self._set_pin_mode(m))
            pin_group.addAction(act)
            pin_menu.addAction(act)
            self._pin_actions[mode] = act

        act_peek = QAction("Auto-Peek on Song Change", self, checkable=True)
        act_peek.setChecked(bool(self.cfg.get("track_peek", True)))
        act_peek.triggered.connect(self._toggle_track_peek)
        menu.addAction(act_peek)

        act_only_sp = QAction(
            "Hide Completely When Spotify Is Closed", self, checkable=True
        )
        act_only_sp.setChecked(bool(self.cfg.get("only_when_spotify_running", True)))
        act_only_sp.triggered.connect(self._toggle_only_when_running)
        menu.addAction(act_only_sp)

        act_fs = QAction("Pause Hover in Fullscreen Apps", self, checkable=True)
        act_fs.setChecked(bool(self.cfg.get("hide_in_fullscreen", True)))
        act_fs.triggered.connect(self._toggle_fullscreen_guard)
        menu.addAction(act_fs)

        act_startup = QAction("Start with Windows", self, checkable=True)
        act_startup.setChecked(is_autostart_enabled())
        act_startup.triggered.connect(self._toggle_autostart)
        menu.addAction(act_startup)

        menu.addSeparator()

        act_connect = QAction("Connect / Re-link Spotify Web API...", self)
        act_connect.triggered.connect(self._open_connect_dialog)
        menu.addAction(act_connect)

        act_show = QAction("Drop Down Now", self)
        act_show.triggered.connect(self._manual_drop_down)
        menu.addAction(act_show)

        menu.addSeparator()

        act_quit = QAction("Quit Spotify Now Bar", self)
        act_quit.triggered.connect(self._quit_app)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._manual_drop_down()
            if reason == QSystemTrayIcon.ActivationReason.Trigger
            else None
        )
        self.tray.show()

    def _manual_drop_down(self) -> None:
        if self._is_bar_allowed():
            self._transition_to("expanded")
        else:
            open_spotify_app()

    def _set_pin_mode(self, mode: str) -> None:
        self.cfg["pin_mode"] = mode
        save_config(self.cfg)
        if mode in self._pin_actions:
            self._pin_actions[mode].setChecked(True)
        if not self._is_bar_allowed():
            self._transition_to("hidden")
            return
        if mode == "auto":
            self._close_drawer()
            self._transition_to("hidden")
        elif mode == "compact":
            self._close_drawer()
            self._transition_to("compact")
        else:
            self._transition_to("expanded")

    def _cycle_pin_mode(self) -> None:
        order = ["auto", "compact", "expanded"]
        cur = self.cfg.get("pin_mode", "auto")
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "auto"
        self._set_pin_mode(nxt)
        labels = {
            "auto": "Mode: Auto-Hide",
            "compact": "Mode: Pinned Pill",
            "expanded": "Mode: Pinned Card",
        }
        self._show_toast(labels.get(nxt, ""))

    def _toggle_track_peek(self, checked: bool) -> None:
        self.cfg["track_peek"] = checked
        save_config(self.cfg)

    def _toggle_only_when_running(self, checked: bool) -> None:
        self.cfg["only_when_spotify_running"] = checked
        save_config(self.cfg)
        if not self._is_bar_allowed():
            self._transition_to("hidden")
        else:
            pin = self.cfg.get("pin_mode", "auto")
            if pin in ("compact", "expanded"):
                self._transition_to(pin)
        self.update()

    def _toggle_fullscreen_guard(self, checked: bool) -> None:
        self.cfg["hide_in_fullscreen"] = checked
        save_config(self.cfg)

    def _toggle_autostart(self, checked: bool) -> None:
        ok = set_autostart_enabled(checked)
        if ok:
            self._show_toast(
                "Start with Windows: On" if checked else "Start with Windows: Off"
            )

    def _open_connect_dialog(self) -> None:
        dlg = SpotifyConnectDialog(self.api, self.cfg.get("client_id", ""), self)
        if dlg.exec():
            self._sync_web_api_async(include_queue=True)
            if self._drawer_tab:
                self._load_drawer_data(self._drawer_tab)

    def _quit_app(self) -> None:
        self.smtc.stop()
        self.tray.hide()
        QApplication.quit()

    @Slot(str)
    def _show_toast(self, text: str) -> None:
        self._toast_text = text
        self._toast_until = time.monotonic() + 2.2
        self.update()

    # --- Drawer Management (Queue / Liked / Playlists / Search) ---

    def _toggle_drawer(self, tab: str) -> None:
        if not self.api.is_connected:
            self._open_connect_dialog()
            return
        if self._drawer_tab == tab:
            self._close_drawer()
            return

        self._drawer_tab = tab
        self._drawer_scroll = 0.0
        self._active_playlist = None
        self._drawer_status = ""

        self._drawer_anim.stop()
        self._drawer_anim.setStartValue(self._drawer_p)
        self._drawer_anim.setEndValue(1.0)
        self._drawer_anim.start()

        if tab == "search":
            self._layout_search_input()
            self._search_input.show()
            self.activateWindow()
            self._search_input.setFocus()
        else:
            self._search_input.hide()
            self._search_input.clearFocus()
            self._load_drawer_data(tab)

        self.update()

    def _close_drawer(self) -> None:
        if self._drawer_tab is None and self._drawer_p <= 0.01:
            return
        self._drawer_tab = None
        self._active_playlist = None
        self._search_input.hide()
        self._search_input.clearFocus()
        self._drawer_anim.stop()
        self._drawer_anim.setStartValue(self._drawer_p)
        self._drawer_anim.setEndValue(0.0)
        self._drawer_anim.start()
        self.update()

    def _layout_search_input(self) -> None:
        if self._drawer_tab == "search" and self._drop_p > 0.7 and self._drawer_p > 0.45:
            rect = self._card_rect()
            sx = int(rect.left() + 18)
            sy = int(rect.top() + self.CARD_H + 10)
            sw = int(rect.width() - 36)
            self._search_input.setGeometry(sx, sy, sw, 32)
            if not self._search_input.isVisible():
                self._search_input.show()
        else:
            if self._search_input.isVisible():
                self._search_input.hide()

    def _load_drawer_data(self, tab: str) -> None:
        self._drawer_loading = True
        self._drawer_status = "Loading..."
        self.update()

        def _worker():
            if tab == "queue":
                items = self.api.fetch_queue()
                self.drawer_loaded.emit("queue", items, "" if items else "Queue is empty")
            elif tab == "liked":
                items = self.api.fetch_liked_songs(limit=30)
                self.drawer_loaded.emit(
                    "liked", items, "" if items else "No liked songs found"
                )
            elif tab == "library":
                items, err = self.api.fetch_playlists(limit=25)
                self.drawer_loaded.emit(
                    "library", items, err or ("" if items else "No playlists found")
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _load_playlist_tracks(self, playlist: TrackItem) -> None:
        self._active_playlist = playlist
        self._drawer_scroll = 0.0
        self._drawer_loading = True
        self._drawer_status = "Loading tracks..."
        self._playlist_tracks = []
        self.update()

        def _worker():
            items, err = self.api.fetch_playlist_items(playlist.playlist_id, limit=30)
            self.drawer_loaded.emit("playlist_tracks", items, err)

        threading.Thread(target=_worker, daemon=True).start()

    def _run_search_now(self) -> None:
        q = self._search_input.text().strip()
        if not q:
            self._search_items = []
            self._drawer_status = "Type a song or artist to search Spotify"
            self.update()
            return
        self._drawer_loading = True
        self._drawer_status = "Searching..."
        self._drawer_scroll = 0.0
        self.update()

        def _worker():
            items = self.api.search_tracks(q)
            self.drawer_loaded.emit(
                "search", items, "" if items else "No matching tracks found"
            )

        threading.Thread(target=_worker, daemon=True).start()

    @Slot(str, object, str)
    def _handle_drawer_loaded(self, kind: str, items: List[TrackItem], err: str) -> None:
        self._drawer_loading = False
        self._drawer_status = err
        if kind == "queue":
            self.extra.queue_items = items
        elif kind == "liked":
            self._liked_items = items
        elif kind == "library":
            self._playlist_items = items
        elif kind == "playlist_tracks":
            self._playlist_tracks = items
        elif kind == "search":
            self._search_items = items
        self.update()

    def _current_drawer_items(self) -> List[TrackItem]:
        if self._drawer_tab == "queue":
            return [
                t
                for t in self.extra.queue_items
                if f"{t.title}::{t.artist}".lower() not in self._removed_queue_keys
            ]
        if self._drawer_tab == "liked":
            return self._liked_items
        if self._drawer_tab == "library":
            return self._playlist_tracks if self._active_playlist else self._playlist_items
        if self._drawer_tab == "search":
            return self._search_items
        return []

    # --- State & Spring Transitions ---

    def _apply_click_through(self, enable: bool) -> None:
        if self._is_click_through != enable:
            self._is_click_through = enable
            set_click_through(int(self.winId()), enable)

    def _transition_to(self, target: str) -> None:
        if target != "hidden" and not self._is_bar_allowed():
            target = "hidden"
        if target == self._state:
            return
        prev = self._state
        self._state = target

        if target != "hidden":
            self._reposition()
            self._apply_click_through(False)
            if not self._anim_timer.isActive():
                self._anim_timer.start()
        else:
            self._close_drawer()

        if target != "expanded":
            self._set_vol_open(False)

        self._drop_anim.stop()
        if target == "hidden":
            self._drop_anim.setDuration(240)
            self._drop_anim.setEasingCurve(QEasingCurve.Type.InCubic)
            self._drop_anim.setStartValue(self._drop_p)
            self._drop_anim.setEndValue(0.0)
            self._drop_anim.start()
        else:
            self._drop_anim.setDuration(360)
            curve = QEasingCurve(QEasingCurve.Type.OutBack)
            curve.setOvershoot(0.95)
            self._drop_anim.setEasingCurve(curve)
            self._drop_anim.setStartValue(self._drop_p)
            self._drop_anim.setEndValue(1.0)
            self._drop_anim.start()

        self._expand_anim.stop()
        target_exp = 1.0 if target == "expanded" else 0.0
        if prev == "hidden":
            self._expand_p = target_exp
        else:
            self._expand_anim.setDuration(260)
            self._expand_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._expand_anim.setStartValue(self._expand_p)
            self._expand_anim.setEndValue(target_exp)
            self._expand_anim.start()

        if target == "expanded" and self.api.is_connected:
            self._sync_web_api_async(include_queue=True)

        self.update()

    def _on_drop_val(self, val: float) -> None:
        self._drop_p = float(val)
        self._layout_search_input()
        self.update()

    def _on_drop_finished(self) -> None:
        if self._state == "hidden" and self._drop_p <= 0.01:
            self._drop_p = 0.0
            self._apply_click_through(True)
            self._anim_timer.stop()
            self.update()

    def _on_expand_val(self, val: float) -> None:
        self._expand_p = float(val)
        self._layout_search_input()
        self.update()

    def _on_drawer_val(self, val: float) -> None:
        self._drawer_p = float(val)
        self._layout_search_input()
        self.update()

    def _set_vol_open(self, open_now: bool) -> None:
        """Smoothly slides open or closes the floating hover volume slider capsule."""
        if open_now and not self.api.is_connected:
            return
        if open_now:
            self._vol_close_timer.stop()
        if self._vol_open == open_now:
            return
        self._vol_open = open_now
        self._vol_anim.stop()
        self._vol_anim.setDuration(210 if open_now else 185)
        self._vol_anim.setEasingCurve(
            QEasingCurve.Type.OutCubic
            if open_now
            else QEasingCurve.Type.InOutCubic
        )
        self._vol_anim.setStartValue(self._vol_open_p)
        self._vol_anim.setEndValue(1.0 if open_now else 0.0)
        self._vol_anim.start()

    def _close_vol_slider(self) -> None:
        if not self._dragging_vol:
            self._set_vol_open(False)

    def _on_vol_anim_val(self, val: float) -> None:
        self._vol_open_p = float(val)
        self.update()

    def _on_color_val(self, t: float) -> None:
        self._accent_cur = lerp_color(self._accent_from, self._accent_to, float(t))
        self._glow_cur = lerp_color(self._glow_from, self._glow_to, float(t))
        self.update()

    def _on_visual_tick(self) -> None:
        if self.track.is_playing:
            self._phase += 0.19
        target_wave = (
            1.0
            if (self.track.is_playing and not self._seeking and self._hover_btn != "seek")
            else 0.0
        )
        self._wave_amp += (target_wave - self._wave_amp) * 0.16
        target_vis = 1.0 if self.track.is_playing else 0.0
        self._vis_energy += (target_vis - self._vis_energy) * 0.14
        self._marquee_x += 0.72
        self.update()

    # --- Top-Center Edge Hover Sensor ---

    def _card_rect(self) -> QRectF:
        w = lerp(self.PILL_W, self.CARD_W, self._expand_p)
        base_h = lerp(self.PILL_H, self.CARD_H, self._expand_p)
        h = base_h + (self.DRAWER_H * self._drawer_p * self._expand_p)
        x = (self.CANVAS_W - w) / 2.0
        y = lerp(-base_h - 8.0, float(self.TOP_MARGIN), self._drop_p)
        return QRectF(x, y, w, h)

    def _poll_top_edge(self) -> None:
        if not self._is_bar_allowed():
            self._edge_hover_since = None
            return

        screen = QApplication.primaryScreen()
        if not screen:
            return
        geo = screen.geometry()
        pos = QCursor.pos()
        cx = geo.x() + geo.width() // 2
        in_top_zone = (geo.y() <= pos.y() <= geo.y() + 6) and (abs(pos.x() - cx) <= 125)

        if self._state == "hidden":
            if in_top_zone:
                if self.cfg.get("hide_in_fullscreen", True) and is_foreground_fullscreen(
                    int(self.winId())
                ):
                    self._edge_hover_since = None
                    return
                now = time.monotonic()
                if self._edge_hover_since is None:
                    self._edge_hover_since = now
                elif (now - self._edge_hover_since) * 1000 >= int(
                    self.cfg.get("hover_dwell_ms", 150)
                ):
                    self._edge_hover_since = None
                    self._peek_timer.stop()
                    self._transition_to("expanded")
            else:
                self._edge_hover_since = None
        else:
            if (
                self._search_input.hasFocus()
                or self._seeking
                or self._dragging_vol
                or self._dragging_row
            ):
                self._leave_timer.stop()
                return

            local_pt = self.mapFromGlobal(pos)
            active_rect = self._card_rect().adjusted(-20, -22, 20, 20)
            inside = active_rect.contains(QPointF(local_pt)) or in_top_zone

            # Auto-collapse volume popover when cursor moves off its zone
            if self._vol_open and not self._dragging_vol:
                lp = QPointF(local_pt)
                in_vol_zone = (
                    not self._vol_pill_rect.isNull()
                    and self._vol_pill_rect.adjusted(-10, -10, 10, 10).contains(lp)
                ) or (
                    "vol_mute" in self._hitboxes
                    and self._hitboxes["vol_mute"].adjusted(-6, -6, 6, 6).contains(lp)
                )
                if not in_vol_zone and not self._vol_close_timer.isActive():
                    self._vol_close_timer.start(260)

            if inside:
                self._leave_timer.stop()
                if self._state == "compact":
                    self._peek_timer.stop()
                    self._transition_to("expanded")
            else:
                if not self._peek_timer.isActive() and not self._leave_timer.isActive():
                    if self.cfg.get("pin_mode", "auto") != "expanded":
                        self._leave_timer.start()

    def _on_leave_timeout(self) -> None:
        if self._search_input.hasFocus():
            return
        pin = self.cfg.get("pin_mode", "auto")
        if pin == "compact" and self._is_bar_allowed():
            self._close_drawer()
            self._transition_to("compact")
        elif pin == "auto" or not self._is_bar_allowed():
            self._transition_to("hidden")

    def _on_peek_timeout(self) -> None:
        if self.cfg.get("pin_mode", "auto") == "auto" and self._state == "compact":
            self._transition_to("hidden")

    # --- Data & Pre-Scaled Artwork Handlers ---

    def _update_artwork(self, img_bytes: Optional[bytes]) -> None:
        if not img_bytes:
            return
        h = hash(img_bytes[:512]) ^ len(img_bytes)
        if h == self._cover_bytes_hash:
            return
        self._cover_bytes_hash = h

        pm = QPixmap()
        if pm.loadFromData(img_bytes):
            self._cover_compact_pm = pm.scaled(
                80,
                80,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._cover_expanded_pm = pm.scaled(
                144,
                144,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )

        new_accent, new_glow = extract_accent_colors(img_bytes)
        self._accent_from = QColor(self._accent_cur)
        self._glow_from = QColor(self._glow_cur)
        self._accent_to = new_accent
        self._glow_to = new_glow
        self._color_anim.stop()
        self._color_anim.start()

    @Slot(object)
    def _handle_smtc(self, state: TrackState) -> None:
        track_changed = state.active and (state.track_key != self._last_track_key)
        self.track = state

        if state.active and not self._spotify_running:
            self._spotify_running = True

        if state.thumbnail_bytes:
            self._update_artwork(state.thumbnail_bytes)

        if track_changed:
            self._last_track_key = state.track_key
            self._marquee_x = 0.0

            # Auto-skip if user swiped left to remove this song from the Queue
            key_lower = state.track_key.lower()
            if key_lower in self._removed_queue_keys:
                self._removed_queue_keys.discard(key_lower)
                self._show_toast(f"Auto-skipped removed track: {state.title}")
                self.smtc.next_track()
                return

            if self.api.is_connected:
                self._sync_web_api_async(include_queue=True)

            if (
                self._is_bar_allowed()
                and self.cfg.get("track_peek", True)
                and self._state == "hidden"
                and not (
                    self.cfg.get("hide_in_fullscreen", True)
                    and is_foreground_fullscreen(int(self.winId()))
                )
            ):
                self._transition_to("compact")
                self._peek_timer.start()

        self.update()

    def _sync_web_api_async(self, include_queue: bool = True) -> None:
        def _worker():
            st = self.api.fetch_extra_state(include_queue=include_queue)
            self.api_updated.emit(st)

        threading.Thread(target=_worker, daemon=True).start()

    @Slot(object)
    def _handle_api(self, extra: SpotifyExtraState) -> None:
        self.extra = extra
        if extra.cover_bytes:
            self._update_artwork(extra.cover_bytes)
        if extra.progress_ms >= 0:
            self.track.position_ms = extra.progress_ms
            self.track.updated_at = time.monotonic()
        if extra.duration_ms > 0:
            self.track.duration_ms = extra.duration_ms
        if extra.volume >= 0 and not self._dragging_vol:
            self._vol_hud_value = extra.volume
            if extra.volume > 0:
                self._vol_prev_nonzero = extra.volume
        self.update()

    def _live_position_ms(self) -> int:
        if not self.track.active:
            return 0
        pos = self.track.position_ms
        if self.track.is_playing:
            pos += int((time.monotonic() - self.track.updated_at) * 1000)
        if self.track.duration_ms > 0:
            return min(pos, self.track.duration_ms)
        return max(0, pos)

    # --- Mouse, Slide Gestures, Hover Volume, Double-Click & Scroll ---

    def _row_index_at(self, pt: QPointF) -> int:
        if self._drawer_p < 0.85 or not self._list_viewport.contains(pt):
            return -1
        items = self._current_drawer_items()
        y_rel = pt.y() - self._list_viewport.top() + self._drawer_scroll
        idx = int(y_rel // self.ROW_H)
        if 0 <= idx < len(items):
            return idx
        return -1

    def _set_volume_from_pos(self, x: float) -> None:
        if "vol_slider" not in self._hitboxes:
            return
        r = self._hitboxes["vol_slider"]
        ratio = max(0.0, min(1.0, (x - r.left()) / max(1.0, r.width())))
        val = int(round(ratio * 100))
        if val != self._vol_hud_value:
            self._vol_hud_value = val
            if val > 0:
                self._vol_prev_nonzero = val
            self._vol_debounce.start()
            self.update()

    def mouseMoveEvent(self, event):
        pt = event.position()

        if self._seeking and "seek" in self._hitboxes:
            r = self._hitboxes["seek"]
            self._seek_preview_ratio = max(
                0.0, min(1.0, (pt.x() - r.left()) / max(1.0, r.width()))
            )
            self.update()
            return

        if self._dragging_vol:
            self._set_volume_from_pos(pt.x())
            return

        if self._drag_row_idx >= 0:
            dx = pt.x() - self._drag_start_x
            if abs(dx) > 8 or self._dragging_row:
                self._dragging_row = True
                self._drag_dx = max(-130.0, min(130.0, dx))
                self.update()
            return

        hovered = ""
        for name, rect in self._hitboxes.items():
            if rect.contains(pt):
                hovered = name
                break

        # Open/hold hover volume popover over speaker icon or expanded capsule
        if self._expand_p > 0.8 and self.api.is_connected:
            in_vol = (
                "vol_mute" in self._hitboxes
                and self._hitboxes["vol_mute"].adjusted(-4, -4, 4, 4).contains(pt)
            ) or (
                self._vol_open_p > 0.05
                and not self._vol_pill_rect.isNull()
                and self._vol_pill_rect.adjusted(-8, -8, 8, 8).contains(pt)
            )
            if in_vol:
                self._set_vol_open(True)
            elif self._vol_open and not self._vol_close_timer.isActive():
                self._vol_close_timer.start(260)

        row_h = self._row_index_at(pt)
        if hovered != self._hover_btn or row_h != self._hover_row_idx:
            self._hover_btn = hovered
            self._hover_row_idx = row_h
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if (hovered or row_h >= 0)
                else Qt.CursorShape.ArrowCursor
            )
            self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pt = event.position()

        if self._search_input.hasFocus():
            self._search_input.clearFocus()

        if (
            "seek" in self._hitboxes
            and self._hitboxes["seek"].adjusted(0, -7, 0, 7).contains(pt)
        ):
            r = self._hitboxes["seek"]
            self._seeking = True
            self._seek_preview_ratio = max(
                0.0, min(1.0, (pt.x() - r.left()) / max(1.0, r.width()))
            )
            self.update()
            return

        if (
            self.api.is_connected
            and "vol_slider" in self._hitboxes
            and self._hitboxes["vol_slider"].adjusted(0, -6, 0, 6).contains(pt)
        ):
            self._dragging_vol = True
            self._set_volume_from_pos(pt.x())
            return

        idx = self._row_index_at(pt)
        if idx >= 0:
            self._drag_row_idx = idx
            self._drag_start_x = pt.x()
            self._drag_start_y = pt.y()
            self._drag_dx = 0.0
            self._dragging_row = False
            return

        for name, rect in self._hitboxes.items():
            if rect.contains(pt):
                self._trigger_action(name)
                return

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return

        if self._dragging_vol:
            self._dragging_vol = False
            self._vol_debounce.stop()
            self._flush_volume_api()
            pt = event.position()
            if (
                self._vol_pill_rect.isNull()
                or not self._vol_pill_rect.adjusted(-8, -8, 8, 8).contains(pt)
            ):
                self._vol_close_timer.start(260)
            self.update()
            return

        if self._seeking:
            self._seeking = False
            if self.track.duration_ms > 0:
                target_ms = int(self._seek_preview_ratio * self.track.duration_ms)
                self.track.position_ms = target_ms
                self.track.updated_at = time.monotonic()
                if self.api.is_connected:
                    threading.Thread(
                        target=self.api.seek_ms, args=(target_ms,), daemon=True
                    ).start()
                else:
                    self.smtc.seek_ms(target_ms)
            self.update()
            return

        if self._drag_row_idx >= 0:
            idx = self._drag_row_idx
            dx = self._drag_dx
            was_drag = self._dragging_row
            self._drag_row_idx = -1
            self._drag_dx = 0.0
            self._dragging_row = False

            items = self._current_drawer_items()
            if 0 <= idx < len(items):
                item = items[idx]
                if was_drag:
                    if dx >= 72:
                        self._on_row_slide_right(item)
                    elif dx <= -72:
                        self._on_row_slide_left(item)
                else:
                    self._on_row_click(item)
            self.update()

    def mouseDoubleClickEvent(self, event):
        if "art" in self._hitboxes and self._hitboxes["art"].contains(event.position()):
            open_spotify_app()

    def wheelEvent(self, event):
        if self._state == "hidden":
            return
        pt = event.position()

        if self._drawer_p > 0.7 and self._list_viewport.contains(pt):
            items = self._current_drawer_items()
            max_scroll = max(0.0, len(items) * self.ROW_H - self._list_viewport.height())
            step = -38.0 if event.angleDelta().y() > 0 else 38.0
            self._drawer_scroll = max(0.0, min(max_scroll, self._drawer_scroll + step))
            self.update()
            return

        # Scroll adjusts Spotify app volume and briefly opens the hover slider
        if not self.api.is_connected:
            return
        delta = 5 if event.angleDelta().y() > 0 else -5
        base = self._vol_hud_value if self._vol_hud_value >= 0 else 50
        self._vol_hud_value = max(0, min(100, base + delta))
        if self._vol_hud_value > 0:
            self._vol_prev_nonzero = self._vol_hud_value
        if self._state == "expanded":
            self._set_vol_open(True)
            self._vol_close_timer.start(1300)
        self._show_toast(f"Spotify Volume: {self._vol_hud_value}%")
        self._vol_debounce.start()

    def _flush_volume_api(self) -> None:
        val = self._vol_hud_value
        threading.Thread(target=self.api.set_volume, args=(val,), daemon=True).start()

    # --- Row Click & Slide Actions ---

    def _on_row_click(self, item: TrackItem) -> None:
        if item.is_playlist:
            self._load_playlist_tracks(item)
            return
        self._show_toast(f"Playing: {item.title}")

        def _do():
            if self.api.play_track(item.uri):
                time.sleep(0.4)
                self._sync_web_api_async(include_queue=True)
            else:
                self.toast_signal.emit("Open Spotify on an active device first")

        threading.Thread(target=_do, daemon=True).start()

    def _on_row_slide_right(self, item: TrackItem) -> None:
        if item.is_playlist:
            self._show_toast(f"Playing playlist: {item.title}")
            threading.Thread(
                target=self.api.play_context, args=(item.uri,), daemon=True
            ).start()
            return

        if self._drawer_tab == "queue":
            self._show_toast(f"Jumping to: {item.title}")

            def _play():
                if self.api.play_track(item.uri):
                    time.sleep(0.45)
                    self._sync_web_api_async(include_queue=True)

            threading.Thread(target=_play, daemon=True).start()
        else:
            self._show_toast(f"Added to queue: {item.title}")

            def _q():
                if self.api.add_to_queue(item.uri):
                    time.sleep(0.35)
                    self._sync_web_api_async(include_queue=True)
                else:
                    self.toast_signal.emit("Could not add to queue")

            threading.Thread(target=_q, daemon=True).start()

    def _on_row_slide_left(self, item: TrackItem) -> None:
        if item.is_playlist:
            self._load_playlist_tracks(item)
            return

        if self._drawer_tab == "queue":
            self._removed_queue_keys.add(f"{item.title}::{item.artist}".lower())
            self._show_toast(f"Removed '{item.title}' (auto-skips if reached)")
        elif self._drawer_tab == "liked":
            self._liked_items = [t for t in self._liked_items if t.uri != item.uri]
            self._show_toast(f"Removed from Liked: {item.title}")
            threading.Thread(
                target=self.api.remove_from_library, args=(item.uri,), daemon=True
            ).start()
        else:
            self._show_toast(f"Saved to Liked Songs: {item.title}")
            threading.Thread(
                target=self.api.save_to_library, args=(item.uri,), daemon=True
            ).start()

    # --- Button Actions ---

    def _trigger_action(self, name: str) -> None:
        if name == "play":
            self.smtc.toggle_play_pause()
        elif name == "next":
            self.smtc.next_track()
        elif name == "prev":
            self.smtc.previous_track()
        elif name == "pin":
            self._cycle_pin_mode()
        elif name == "connect":
            self._open_connect_dialog()
        elif name == "vol_mute" and self.api.is_connected:
            if self._vol_hud_value > 0:
                self._vol_prev_nonzero = self._vol_hud_value
                self._vol_hud_value = 0
                self._show_toast("Spotify Muted")
            else:
                self._vol_hud_value = max(15, self._vol_prev_nonzero)
                self._show_toast(f"Spotify Volume: {self._vol_hud_value}%")
            self._flush_volume_api()
            self.update()
        elif name == "drawer_retry":
            if self._drawer_tab == "search":
                self._run_search_now()
            elif self._drawer_tab == "library" and self._active_playlist:
                self._load_playlist_tracks(self._active_playlist)
            elif self._drawer_tab:
                self._load_drawer_data(self._drawer_tab)
        elif name in ("tab_queue", "tab_liked", "tab_library", "tab_search"):
            self._toggle_drawer(name.replace("tab_", ""))
        elif name == "pl_back":
            self._active_playlist = None
            self._drawer_scroll = 0.0
            self._drawer_status = ""
            self.update()
        elif name == "pl_play_all" and self._active_playlist:
            pl = self._active_playlist
            self._show_toast(f"Playing playlist: {pl.title}")
            threading.Thread(
                target=self.api.play_context, args=(pl.uri,), daemon=True
            ).start()
        elif name == "like":
            if not self.api.is_connected:
                self._open_connect_dialog()
                return
            if self.extra.track_uri:
                prev_liked = self.extra.is_liked
                self.extra.is_liked = not prev_liked
                self._show_toast(
                    "Added to Liked Songs"
                    if not prev_liked
                    else "Removed from Liked Songs"
                )
                uri = self.extra.track_uri

                def _do():
                    self.extra.is_liked = self.api.toggle_like(uri, prev_liked)

                threading.Thread(target=_do, daemon=True).start()
        elif name == "shuffle" and self.api.is_connected:
            new_s = not self.extra.shuffle
            self.extra.shuffle = new_s
            self._show_toast("Shuffle: On" if new_s else "Shuffle: Off")
            threading.Thread(
                target=self.api.set_shuffle, args=(new_s,), daemon=True
            ).start()
        elif name == "repeat" and self.api.is_connected:
            cycle = {"off": "context", "context": "track", "track": "off"}
            new_r = cycle.get(self.extra.repeat, "off")
            self.extra.repeat = new_r
            names = {
                "off": "Repeat: Off",
                "context": "Repeat: All",
                "track": "Repeat: Track",
            }
            self._show_toast(names.get(new_r, ""))
            threading.Thread(
                target=self.api.set_repeat, args=(new_r,), daemon=True
            ).start()

    # --- Custom One UI 7 Rendering ---

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._hitboxes.clear()
        self._list_viewport = QRectF()
        self._vol_pill_rect = QRectF()

        # Stay completely invisible when Spotify is closed
        if not self._is_bar_allowed() and self._drop_p <= 0.01:
            return

        # 1) Retracted 3.5px top-center dynamic glow bar (click-through)
        if self._drop_p < 0.25 and self._is_bar_allowed():
            edge_alpha = (1.0 - self._drop_p / 0.25) * (
                0.88 if self.track.active else 0.34
            )
            bar_w = 88.0 if self.track.is_playing else 58.0
            bar_x = (self.CANVAS_W - bar_w) / 2.0
            c = QColor(self._accent_cur)
            c.setAlphaF(edge_alpha)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(bar_x, 1.0, bar_w, 3.5), 1.75, 1.75)

        if self._drop_p <= 0.01:
            return

        rect = self._card_rect()
        radius = 28.0
        opacity = max(0.0, min(1.0, self._drop_p * 1.45))
        p.setOpacity(opacity)

        # 2) Soft layered drop shadow (painted inside transparent canvas)
        for i, spread in enumerate((11, 6, 2)):
            sh = QColor(0, 0, 0, 24 + i * 20)
            p.setBrush(sh)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(
                rect.adjusted(-spread, -spread + 5, spread, spread + 8),
                radius + spread * 0.5,
                radius + spread * 0.5,
            )

        # 3) Frosted dark glass body with dynamic album-art ambient glow
        clip = QPainterPath()
        clip.addRoundedRect(rect, radius, radius)
        p.save()
        p.setClipPath(clip)

        base_grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        base_grad.setColorAt(0.0, QColor(17, 19, 25, 242))
        base_grad.setColorAt(1.0, QColor(11, 12, 17, 248))
        p.fillRect(rect, base_grad)

        rad = QRadialGradient(
            QPointF(rect.left() + 74, rect.top() + 44), rect.width() * 0.88
        )
        g1 = QColor(self._accent_cur)
        g1.setAlpha(72)
        g2 = QColor(self._glow_cur)
        g2.setAlpha(28)
        rad.setColorAt(0.0, g1)
        rad.setColorAt(0.55, g2)
        rad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(rect, rad)

        # Top specular highlight
        rim = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + 26))
        rim.setColorAt(0.0, QColor(255, 255, 255, 28))
        rim.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 26), rim)

        if self._expand_p < 0.5:
            p.setOpacity(opacity * (1.0 - self._expand_p * 2.0))
            self._paint_compact(p, rect)
        else:
            p.setOpacity(opacity * ((self._expand_p - 0.5) * 2.0))
            self._paint_expanded(p, rect)
            if self._drawer_p > 0.08:
                p.setOpacity(opacity * min(1.0, self._drawer_p * 1.3))
                self._paint_drawer(p, rect)

        p.restore()

        # Crisp 1px glass border
        p.setOpacity(opacity)
        p.setPen(QPen(QColor(255, 255, 255, 36), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

    def _draw_rounded_art(
        self, p: QPainter, r: QRectF, radius: float, compact: bool = False
    ) -> None:
        p.save()
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        p.setClipPath(path)
        pm = self._cover_compact_pm if compact else self._cover_expanded_pm
        if pm and not pm.isNull():
            p.drawPixmap(r, pm, QRectF(pm.rect()))
        else:
            p.fillRect(r, QColor(34, 38, 48))
            p.setPen(QPen(QColor(148, 163, 184), 2.0))
            cx, cy = r.center().x(), r.center().y()
            p.drawEllipse(QPointF(cx - 5, cy + 5), 4, 3)
            p.drawLine(QPointF(cx - 1, cy + 5), QPointF(cx - 1, cy - 8))
            p.drawLine(QPointF(cx - 1, cy - 8), QPointF(cx + 8, cy - 10))
        p.restore()

    def _draw_marquee_text(
        self, p: QPainter, rect: QRectF, text: str, font: QFont, color: QColor
    ) -> None:
        p.save()
        p.setClipRect(rect)
        p.setFont(font)
        p.setPen(color)
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(text)
        if tw <= rect.width():
            p.drawText(
                rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text
            )
        else:
            gap = 46.0
            span = tw + gap
            offset = self._marquee_x % span
            p.drawText(
                QRectF(rect.left() - offset, rect.top(), tw + 12, rect.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text,
            )
            p.drawText(
                QRectF(rect.left() - offset + span, rect.top(), tw + 12, rect.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text,
            )
        p.restore()

    def _draw_mini_eq(self, p: QPainter, x: float, cy: float) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._accent_cur)
        for i in range(3):
            if self.track.is_playing:
                h = 5.0 + 10.0 * (0.5 + 0.5 * math.sin(self._phase + i * 1.45))
            else:
                h = 4.0
            p.drawRoundedRect(QRectF(x + i * 5.2, cy - h / 2.0, 3.2, h), 1.6, 1.6)

    def _draw_pill_visualizer(self, p: QPainter, x: float, cy: float) -> None:
        """Spacious 7-bar glowing harmonic waveform for the compact pill."""
        num_bars = 7
        bar_w = 3.6
        gap = 4.6
        envelopes = (0.48, 0.74, 0.94, 1.0, 0.90, 0.70, 0.46)
        freqs = (1.15, 1.45, 1.05, 1.35, 1.60, 1.25, 0.95)
        offsets = (0.0, 1.1, 2.2, 0.7, 3.1, 1.8, 2.7)

        bright = QColor(
            min(255, self._accent_cur.red() + 65),
            min(255, self._accent_cur.green() + 65),
            min(255, self._accent_cur.blue() + 65),
        )
        glow_c = QColor(self._accent_cur)
        glow_c.setAlpha(int(48 * self._vis_energy))

        p.setPen(Qt.PenStyle.NoPen)
        for i in range(num_bars):
            bx = x + i * (bar_w + gap)
            wave = (
                0.5
                + 0.34 * math.sin(self._phase * freqs[i] + offsets[i])
                + 0.16 * math.sin(self._phase * 2.1 - offsets[i] * 1.4)
            )
            active_h = 5.0 + 23.0 * envelopes[i] * wave
            idle_h = 4.2 + 2.2 * envelopes[i]
            h = lerp(idle_h, active_h, self._vis_energy)

            if self._vis_energy > 0.08:
                p.setBrush(glow_c)
                p.drawRoundedRect(
                    QRectF(bx - 1.5, cy - h / 2.0 - 1.5, bar_w + 3.0, h + 3.0),
                    (bar_w + 3.0) / 2.0,
                    (bar_w + 3.0) / 2.0,
                )

            grad = QLinearGradient(bx, cy - h / 2.0, bx, cy + h / 2.0)
            grad.setColorAt(0.0, bright)
            grad.setColorAt(0.55, self._accent_cur)
            grad.setColorAt(1.0, self._glow_cur)
            p.setBrush(grad)
            p.drawRoundedRect(
                QRectF(bx, cy - h / 2.0, bar_w, h), bar_w / 2.0, bar_w / 2.0
            )

    def _paint_compact(self, p: QPainter, rect: QRectF) -> None:
        art_r = QRectF(rect.left() + 9, rect.top() + 9, 40, 40)
        self._draw_rounded_art(p, art_r, 20.0, compact=True)
        self._hitboxes["art"] = art_r

        title = self.track.title if self.track.active else "Spotify Idle"
        artist = (
            self.track.artist
            if self.track.active
            else "Play a track to light up Now Bar"
        )

        # Clean 18px breathing room between scrolling text and the 7-bar visualizer
        vis_w = 7 * 3.6 + 6 * 4.6
        vis_x = rect.right() - vis_w - 20.0
        text_x = art_r.right() + 13.0
        text_w = max(80.0, vis_x - text_x - 18.0)

        self._draw_marquee_text(
            p,
            QRectF(text_x, rect.top() + 8, text_w, 22),
            title,
            self._f_title_c,
            QColor(248, 250, 252),
        )
        self._draw_marquee_text(
            p,
            QRectF(text_x, rect.top() + 30, text_w, 18),
            artist,
            self._f_artist_c,
            QColor(186, 198, 214),
        )

        self._draw_pill_visualizer(p, vis_x, rect.center().y())

    def _paint_animated_slider(self, p: QPainter, bar_r: QRectF, ratio: float) -> None:
        cy = bar_r.center().y()
        fill_w = bar_r.width() * ratio
        knob_x = bar_r.left() + fill_w

        # Unfilled track
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 38))
        p.drawRoundedRect(
            QRectF(knob_x, cy - 2.0, max(0.0, bar_r.right() - knob_x), 4.0), 2.0, 2.0
        )

        # Animated wavy progress track
        if fill_w > 2.0:
            if self._wave_amp > 0.05 and fill_w > 8.0:
                path = QPainterPath()
                amp = 2.6 * self._wave_amp
                freq = 0.28
                speed = self._phase * 1.45
                steps = max(12, int(fill_w / 3.0))
                for i in range(steps + 1):
                    t = i / steps
                    x = bar_r.left() + t * fill_w
                    env = min(1.0, (x - bar_r.left()) / 10.0) * min(
                        1.0, (knob_x - x) / 10.0
                    )
                    y = cy + math.sin((x - bar_r.left()) * freq - speed) * amp * env
                    if i == 0:
                        path.moveTo(x, y)
                    else:
                        path.lineTo(x, y)

                glow_c = QColor(self._accent_cur)
                glow_c.setAlpha(70)
                p.setPen(
                    QPen(glow_c, 6.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                )
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)

                p.setPen(
                    QPen(
                        self._accent_cur,
                        2.8,
                        Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap,
                    )
                )
                p.drawPath(path)
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(self._accent_cur)
                p.drawRoundedRect(QRectF(bar_r.left(), cy - 2.2, fill_w, 4.4), 2.2, 2.2)

        # One UI pill thumb
        active_thumb = self._hover_btn == "seek" or self._seeking
        tw = 6.0 if active_thumb else 4.2
        th = 16.0 if active_thumb else 12.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 250))
        p.drawRoundedRect(
            QRectF(knob_x - tw / 2.0, cy - th / 2.0, tw, th), tw / 2.0, tw / 2.0
        )

    def _paint_expanded(self, p: QPainter, rect: QRectF) -> None:
        # Top section: Album Art (double-click opens Spotify) + Track Meta + Pin & Heart
        art_r = QRectF(rect.left() + 18, rect.top() + 18, 72, 72)
        self._draw_rounded_art(p, art_r, 15.0, compact=False)
        self._hitboxes["art"] = art_r

        meta_x = art_r.right() + 15
        meta_w = rect.width() - 180

        badge_text = "SPOTIFY HYBRID" if self.api.is_connected else "SPOTIFY LOCAL"
        p.setFont(self._f_badge)
        bw = QFontMetrics(self._f_badge).horizontalAdvance(badge_text) + 14
        badge_r = QRectF(meta_x, rect.top() + 19, bw, 17)
        bc = QColor(self._accent_cur)
        bc.setAlpha(44)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bc)
        p.drawRoundedRect(badge_r, 8.5, 8.5)
        p.setPen(self._accent_cur)
        p.drawText(badge_r, Qt.AlignmentFlag.AlignCenter, badge_text)

        if self.track.active:
            self._draw_mini_eq(p, badge_r.right() + 9, badge_r.center().y())

        title = self.track.title if self.track.active else "Spotify Not Playing"
        artist = (
            self.track.artist
            if self.track.active
            else "Double-click cover to open Spotify"
        )
        self._draw_marquee_text(
            p,
            QRectF(meta_x, rect.top() + 40, meta_w, 25),
            title,
            self._f_title_e,
            QColor(255, 255, 255),
        )
        self._draw_marquee_text(
            p,
            QRectF(meta_x, rect.top() + 65, meta_w, 20),
            artist,
            self._f_artist_e,
            QColor(192, 204, 220),
        )

        # Pin & Heart buttons
        pin_r = QRectF(rect.right() - 46, rect.top() + 18, 30, 30)
        like_r = QRectF(rect.right() - 46, rect.top() + 56, 30, 30)
        self._hitboxes["pin"] = pin_r
        self._hitboxes["like"] = like_r

        pin_active = self.cfg.get("pin_mode", "auto") != "auto"
        self._draw_icon_btn(
            p, pin_r, "pin", active=pin_active, hovered=(self._hover_btn == "pin")
        )
        self._draw_icon_btn(
            p,
            like_r,
            "heart_filled" if self.extra.is_liked else "heart",
            active=self.extra.is_liked,
            hovered=(self._hover_btn == "like"),
            accent_override=QColor(29, 215, 96) if self.extra.is_liked else None,
        )

        # Animated Wavy Seek Bar + Timestamps
        seek_y = rect.top() + 111
        dur = self.track.duration_ms
        pos = self._live_position_ms()
        ratio = (
            self._seek_preview_ratio
            if self._seeking
            else ((pos / dur) if dur > 0 else 0.0)
        )
        ratio = max(0.0, min(1.0, ratio))

        bar_r = QRectF(rect.left() + 58, seek_y - 3.0, rect.width() - 116, 6.0)
        self._hitboxes["seek"] = bar_r.adjusted(0, -8, 0, 8)
        self._paint_animated_slider(p, bar_r, ratio)

        p.setFont(self._f_small)
        p.setPen(QColor(178, 192, 210))
        disp_pos = int(ratio * dur) if self._seeking else pos
        p.drawText(
            QRectF(rect.left() + 15, seek_y - 9, 38, 18),
            Qt.AlignmentFlag.AlignCenter,
            fmt_time(disp_pos),
        )
        p.drawText(
            QRectF(rect.right() - 53, seek_y - 9, 38, 18),
            Qt.AlignmentFlag.AlignCenter,
            fmt_time(dur),
        )

        # Transport Row: Shuffle | Prev | Play/Pause | Next | Repeat | Hover Volume
        ctrl_y = rect.top() + 149
        cx = rect.center().x()

        play_r = QRectF(cx - 21, ctrl_y - 21, 42, 42)
        prev_r = QRectF(cx - 80, ctrl_y - 17, 34, 34)
        next_r = QRectF(cx + 46, ctrl_y - 17, 34, 34)
        shuf_r = QRectF(cx - 138, ctrl_y - 16, 32, 32)
        rep_r = QRectF(cx + 106, ctrl_y - 16, 32, 32)

        vol_covering = self._vol_open_p > 0.25
        self._hitboxes["play"] = play_r
        self._hitboxes["prev"] = prev_r
        if not vol_covering:
            self._hitboxes["next"] = next_r
        if self.api.is_connected:
            self._hitboxes["shuffle"] = shuf_r
            if not vol_covering:
                self._hitboxes["repeat"] = rep_r

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 248 if self._hover_btn == "play" else 232))
        p.drawEllipse(play_r)
        self._draw_vector_icon(
            p,
            play_r,
            "pause" if self.track.is_playing else "play",
            QColor(14, 16, 20),
        )

        self._draw_icon_btn(p, prev_r, "prev", hovered=(self._hover_btn == "prev"))
        self._draw_icon_btn(
            p, next_r, "next", hovered=(self._hover_btn == "next" and not vol_covering)
        )
        self._draw_icon_btn(
            p,
            shuf_r,
            "shuffle",
            active=self.extra.shuffle,
            hovered=(self._hover_btn == "shuffle"),
            dimmed=not self.api.is_connected,
        )
        self._draw_icon_btn(
            p,
            rep_r,
            "repeat1" if self.extra.repeat == "track" else "repeat",
            active=(self.extra.repeat != "off"),
            hovered=(self._hover_btn == "repeat" and not vol_covering),
            dimmed=not self.api.is_connected,
        )

        # Hover-Expandable Spotify App Volume Popover (slides left from speaker icon)
        vol_val = max(0, min(100, self._vol_hud_value))
        vol_ratio = vol_val / 100.0
        mute_r = QRectF(rect.right() - 46, ctrl_y - 15, 30, 30)

        if vol_val == 0:
            vol_icon = "vol_mute"
        elif vol_val < 50:
            vol_icon = "vol_low"
        else:
            vol_icon = "vol_high"

        if self._vol_open_p > 0.01 and self.api.is_connected:
            pill_w = lerp(34.0, 198.0, self._vol_open_p)
            pill_h = 36.0
            pill_r = QRectF(
                rect.right() - 14.0 - pill_w,
                ctrl_y - pill_h / 2.0,
                pill_w,
                pill_h,
            )
            self._vol_pill_rect = pill_r

            # Frosted capsule backdrop over the right half of the transport row
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, int(90 * self._vol_open_p)))
            p.drawRoundedRect(pill_r.adjusted(-2, 2, 2, 4), 18.0, 18.0)

            p.setBrush(
                QColor(18, 21, 28, int(248 * min(1.0, self._vol_open_p * 1.4)))
            )
            p.setPen(QPen(QColor(255, 255, 255, int(46 * self._vol_open_p)), 1.0))
            p.drawRoundedRect(pill_r, 18.0, 18.0)

            inner_alpha = max(0.0, min(1.0, (self._vol_open_p - 0.18) / 0.82))
            if inner_alpha > 0.01:
                p.save()
                pill_clip = QPainterPath()
                pill_clip.addRoundedRect(pill_r, 18.0, 18.0)
                p.setClipPath(pill_clip)
                p.setOpacity(p.opacity() * inner_alpha)

                pct_r = QRectF(pill_r.left() + 8.0, ctrl_y - 9.0, 32.0, 18.0)
                p.setFont(self._f_small_b)
                p.setPen(QColor(216, 226, 238))
                p.drawText(pct_r, Qt.AlignmentFlag.AlignCenter, f"{vol_val}%")

                bar_left = pill_r.left() + 44.0
                bar_right = mute_r.left() - 8.0
                bar_w = max(20.0, bar_right - bar_left)
                vol_bar_r = QRectF(bar_left, ctrl_y - 2.5, bar_w, 5.0)
                if self._vol_open_p > 0.45:
                    self._hitboxes["vol_slider"] = vol_bar_r.adjusted(-4, -10, 4, 10)

                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 36))
                p.drawRoundedRect(vol_bar_r, 2.5, 2.5)

                v_fill = bar_w * vol_ratio
                if v_fill > 0:
                    vgrad = QLinearGradient(bar_left, ctrl_y, bar_right, ctrl_y)
                    vgrad.setColorAt(0.0, self._glow_cur)
                    vgrad.setColorAt(1.0, self._accent_cur)
                    p.setBrush(vgrad)
                    p.drawRoundedRect(
                        QRectF(bar_left, vol_bar_r.top(), v_fill, vol_bar_r.height()),
                        2.5,
                        2.5,
                    )

                v_active = self._hover_btn == "vol_slider" or self._dragging_vol
                vr = 6.2 if v_active else 4.8
                p.setBrush(QColor(255, 255, 255, 248))
                p.drawEllipse(QPointF(bar_left + v_fill, ctrl_y), vr, vr)
                p.restore()

        if self.api.is_connected:
            self._hitboxes["vol_mute"] = mute_r

        self._draw_icon_btn(
            p,
            mute_r,
            vol_icon,
            active=(vol_val == 0 and self.api.is_connected),
            hovered=(self._hover_btn == "vol_mute"),
            dimmed=not self.api.is_connected,
            accent_override=QColor(248, 113, 113),
        )

        # Drawer Tabs (Queue • Liked • Playlists • Search) OR Toast Banner
        tab_y = rect.top() + 184
        if time.monotonic() < self._toast_until and self._toast_text:
            toast_r = QRectF(rect.left() + 20, tab_y + 2, rect.width() - 40, 26)
            tc = QColor(self._accent_cur)
            tc.setAlpha(42)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tc)
            p.drawRoundedRect(toast_r, 13, 13)
            p.setFont(self._f_small_b)
            p.setPen(QColor(245, 252, 248))
            p.drawText(toast_r, Qt.AlignmentFlag.AlignCenter, self._toast_text)
        elif not self.api.is_connected:
            conn_r = QRectF(rect.left() + 20, tab_y + 2, rect.width() - 40, 26)
            self._hitboxes["connect"] = conn_r
            p.setFont(self._f_small)
            p.setPen(
                self._accent_cur
                if self._hover_btn == "connect"
                else QColor(166, 180, 198)
            )
            p.drawText(
                conn_r,
                Qt.AlignmentFlag.AlignCenter,
                "Click to link Spotify Platinum (Queue • Liked • Search • Volume)",
            )
        else:
            tabs = [
                ("tab_queue", "Queue"),
                ("tab_liked", "Liked"),
                ("tab_library", "Playlists"),
                ("tab_search", "Search"),
            ]
            gap = 8.0
            total_w = rect.width() - 36.0
            tw = (total_w - gap * (len(tabs) - 1)) / len(tabs)
            for i, (key, label) in enumerate(tabs):
                tr = QRectF(rect.left() + 18 + i * (tw + gap), tab_y + 2, tw, 26)
                self._hitboxes[key] = tr
                is_active = self._drawer_tab == key.replace("tab_", "")
                is_hov = self._hover_btn == key
                if is_active:
                    bg = QColor(self._accent_cur)
                    bg.setAlpha(68)
                    fg = QColor(255, 255, 255)
                elif is_hov:
                    bg = QColor(255, 255, 255, 28)
                    fg = QColor(240, 246, 252)
                else:
                    bg = QColor(255, 255, 255, 13)
                    fg = QColor(178, 192, 210)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(bg)
                p.drawRoundedRect(tr, 13, 13)
                p.setFont(self._f_small_b if is_active else self._f_small)
                p.setPen(fg)
                p.drawText(tr, Qt.AlignmentFlag.AlignCenter, label)

    def _paint_drawer(self, p: QPainter, rect: QRectF) -> None:
        top_y = rect.top() + self.CARD_H
        p.setPen(QPen(QColor(255, 255, 255, 24), 1.0))
        p.drawLine(QPointF(rect.left() + 18, top_y), QPointF(rect.right() - 18, top_y))

        header_y = top_y + 8
        list_top = top_y + 38

        if self._drawer_tab == "search":
            list_top = top_y + 50
        elif self._drawer_tab == "library" and self._active_playlist:
            back_r = QRectF(rect.left() + 18, header_y, 58, 24)
            play_r = QRectF(rect.right() - 110, header_y, 92, 24)
            self._hitboxes["pl_back"] = back_r
            self._hitboxes["pl_play_all"] = play_r

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(
                QColor(255, 255, 255, 26 if self._hover_btn == "pl_back" else 16)
            )
            p.drawRoundedRect(back_r, 12, 12)
            p.setFont(self._f_small_b)
            p.setPen(QColor(236, 242, 248))
            p.drawText(back_r, Qt.AlignmentFlag.AlignCenter, "← Back")

            pc = QColor(self._accent_cur)
            pc.setAlpha(210 if self._hover_btn == "pl_play_all" else 170)
            p.setBrush(pc)
            p.drawRoundedRect(play_r, 12, 12)
            p.setPen(QColor(8, 12, 10))
            p.drawText(play_r, Qt.AlignmentFlag.AlignCenter, "▶ Play All")

            title_r = QRectF(
                back_r.right() + 10,
                header_y,
                play_r.left() - back_r.right() - 20,
                24,
            )
            self._draw_marquee_text(
                p,
                title_r,
                self._active_playlist.title,
                self._f_row_t,
                QColor(248, 250, 252),
            )
        else:
            hints = {
                "queue": "Up Next  •  Slide → Play Now   |   Slide ← Remove & Skip",
                "liked": "Liked Songs  •  Click Play   |   Slide → Queue   |   Slide ← Unlike",
                "library": "Your Playlists  •  Click Open   |   Slide → Play Playlist",
            }
            p.setFont(self._f_small)
            p.setPen(QColor(156, 170, 190))
            p.drawText(
                QRectF(rect.left() + 20, header_y, rect.width() - 40, 22),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                hints.get(self._drawer_tab or "", ""),
            )

        vp = QRectF(
            rect.left() + 14,
            list_top,
            rect.width() - 28,
            max(20.0, rect.bottom() - list_top - 14),
        )
        self._list_viewport = vp

        items = self._current_drawer_items()
        if self._drawer_loading or not items:
            msg = self._drawer_status or (
                "Loading..." if self._drawer_loading else "Nothing here yet"
            )
            p.setFont(self._f_artist_e)
            p.setPen(QColor(162, 176, 194))
            text_r = vp.adjusted(
                16,
                0,
                -16,
                -42 if (not self._drawer_loading and self._drawer_status) else 0,
            )
            p.drawText(
                text_r,
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                msg,
            )

            # Actionable Retry or Re-link button if an API error occurs
            if not self._drawer_loading and self._drawer_status:
                low = self._drawer_status.lower()
                is_relink = (
                    "re-link" in low or "permission" in low or "expired" in low
                )
                btn_key = "connect" if is_relink else "drawer_retry"
                btn_lbl = "Re-link Spotify" if is_relink else "↻ Retry"
                btn_w = 132.0 if is_relink else 96.0
                btn_r = QRectF(
                    vp.center().x() - btn_w / 2.0,
                    vp.center().y() + 18.0,
                    btn_w,
                    30.0,
                )
                self._hitboxes[btn_key] = btn_r
                bg = QColor(self._accent_cur)
                bg.setAlpha(215 if self._hover_btn == btn_key else 170)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(bg)
                p.drawRoundedRect(btn_r, 15.0, 15.0)
                p.setFont(self._f_small_b)
                p.setPen(QColor(8, 12, 10))
                p.drawText(btn_r, Qt.AlignmentFlag.AlignCenter, btn_lbl)
            return

        p.save()
        p.setClipRect(vp)
        fm_t = QFontMetrics(self._f_row_t)
        fm_a = QFontMetrics(self._f_row_a)

        for idx, item in enumerate(items):
            ry = vp.top() + idx * self.ROW_H - self._drawer_scroll
            if ry + self.ROW_H < vp.top() or ry > vp.bottom():
                continue

            row_r = QRectF(vp.left() + 2, ry + 3, vp.width() - 4, self.ROW_H - 6)
            dx = (
                self._drag_dx
                if (self._dragging_row and idx == self._drag_row_idx)
                else 0.0
            )

            if abs(dx) > 4:
                if dx > 0:
                    bg_c = QColor(29, 185, 84, int(min(220, abs(dx) * 2.1)))
                    lbl = (
                        "▶ Play Now"
                        if (self._drawer_tab == "queue" or item.is_playlist)
                        else "+ Add to Queue"
                    )
                    align = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
                else:
                    bg_c = (
                        QColor(239, 68, 68, int(min(220, abs(dx) * 2.1)))
                        if self._drawer_tab in ("queue", "liked")
                        else QColor(236, 72, 153, int(min(220, abs(dx) * 2.1)))
                    )
                    if self._drawer_tab == "queue":
                        lbl = "✕ Remove (Skip)"
                    elif self._drawer_tab == "liked":
                        lbl = "✕ Unlike"
                    elif item.is_playlist:
                        lbl = "Open Playlist"
                    else:
                        lbl = "♥ Save to Liked"
                    align = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight

                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(bg_c)
                p.drawRoundedRect(row_r, 12, 12)
                p.setFont(self._f_small_b)
                p.setPen(QColor(255, 255, 255))
                p.drawText(row_r.adjusted(14, 0, -14, 0), align, lbl)

            fg_r = row_r.translated(dx, 0)
            is_hov = (idx == self._hover_row_idx) and not self._dragging_row
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 24 if is_hov else 12))
            p.drawRoundedRect(fg_r, 12, 12)

            num_r = QRectF(fg_r.left() + 10, fg_r.top() + 8, 24, fg_r.height() - 16)
            p.setFont(self._f_small_b)
            p.setPen(self._accent_cur if is_hov else QColor(148, 163, 184))
            p.drawText(
                num_r,
                Qt.AlignmentFlag.AlignCenter,
                "♫" if item.is_playlist else str(idx + 1),
            )

            tx = num_r.right() + 6
            tw = fg_r.width() - 94

            p.setFont(self._f_row_t)
            p.setPen(QColor(246, 250, 255))
            p.drawText(
                QRectF(tx, fg_r.top() + 5, tw, 18),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                fm_t.elidedText(item.title, Qt.TextElideMode.ElideRight, int(tw)),
            )

            p.setFont(self._f_row_a)
            p.setPen(QColor(168, 182, 200))
            p.drawText(
                QRectF(tx, fg_r.top() + 23, tw, 16),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                fm_a.elidedText(item.artist, Qt.TextElideMode.ElideRight, int(tw)),
            )

            right_txt = "›" if item.is_playlist else fmt_time(item.duration_ms)
            p.setFont(self._f_arrow if item.is_playlist else self._f_small)
            p.setPen(QColor(148, 163, 184))
            p.drawText(
                QRectF(fg_r.right() - 48, fg_r.top(), 40, fg_r.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                right_txt,
            )

        p.restore()

    def _draw_icon_btn(
        self,
        p: QPainter,
        r: QRectF,
        icon: str,
        active: bool = False,
        hovered: bool = False,
        dimmed: bool = False,
        accent_override: Optional[QColor] = None,
    ) -> None:
        if hovered and not dimmed:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 28))
            p.drawEllipse(r)

        if dimmed:
            color = QColor(148, 163, 184, 75)
        elif active:
            color = accent_override or self._accent_cur
        else:
            color = QColor(240, 245, 252, 240 if hovered else 208)

        self._draw_vector_icon(p, r, icon, color)

    def _draw_vector_icon(
        self, p: QPainter, r: QRectF, name: str, color: QColor
    ) -> None:
        cx, cy = r.center().x(), r.center().y()
        p.save()

        if name == "play":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            path = QPainterPath()
            path.moveTo(cx - 4.8, cy - 8.0)
            path.lineTo(cx + 8.5, cy)
            path.lineTo(cx - 4.8, cy + 8.0)
            path.closeSubpath()
            p.drawPath(path)

        elif name == "pause":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(cx - 5.8, cy - 7.5, 4.0, 15.0), 1.6, 1.6)
            p.drawRoundedRect(QRectF(cx + 1.8, cy - 7.5, 4.0, 15.0), 1.6, 1.6)

        elif name == "next":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            path = QPainterPath()
            path.moveTo(cx - 6.2, cy - 6.5)
            path.lineTo(cx + 3.2, cy)
            path.lineTo(cx - 6.2, cy + 6.5)
            path.closeSubpath()
            p.drawPath(path)
            p.drawRoundedRect(QRectF(cx + 4.0, cy - 6.5, 2.5, 13.0), 1.0, 1.0)

        elif name == "prev":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            path = QPainterPath()
            path.moveTo(cx + 6.2, cy - 6.5)
            path.lineTo(cx - 3.2, cy)
            path.lineTo(cx + 6.2, cy + 6.5)
            path.closeSubpath()
            p.drawPath(path)
            p.drawRoundedRect(QRectF(cx - 6.5, cy - 6.5, 2.5, 13.0), 1.0, 1.0)

        elif name in ("heart", "heart_filled"):
            path = QPainterPath()
            path.moveTo(cx, cy + 6.5)
            path.cubicTo(cx - 9.4, cy + 0.6, cx - 8.8, cy - 7.5, cx - 3.8, cy - 6.8)
            path.cubicTo(cx - 1.2, cy - 6.4, cx, cy - 3.8, cx, cy - 3.8)
            path.cubicTo(cx, cy - 3.8, cx + 1.2, cy - 6.4, cx + 3.8, cy - 6.8)
            path.cubicTo(cx + 8.8, cy - 7.5, cx + 9.4, cy + 0.6, cx, cy + 6.5)
            if name == "heart_filled":
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color)
            else:
                p.setPen(
                    QPen(
                        color,
                        1.8,
                        Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin,
                    )
                )
                p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

        elif name == "pin":
            p.setPen(
                QPen(
                    color,
                    1.7,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawLine(QPointF(cx - 4.2, cy - 5.8), QPointF(cx + 4.2, cy - 5.8))
            p.drawLine(QPointF(cx - 2.8, cy - 5.8), QPointF(cx - 4.4, cy + 1.2))
            p.drawLine(QPointF(cx + 2.8, cy - 5.8), QPointF(cx + 4.4, cy + 1.2))
            p.drawLine(QPointF(cx - 5.4, cy + 1.2), QPointF(cx + 5.4, cy + 1.2))
            p.drawLine(QPointF(cx, cy + 1.2), QPointF(cx, cy + 6.8))

        elif name == "shuffle":
            pen = QPen(
                color,
                1.8,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p1 = QPainterPath()
            p1.moveTo(cx - 7.0, cy - 4.2)
            p1.cubicTo(cx - 1.5, cy - 4.2, cx + 1.0, cy + 4.2, cx + 7.0, cy + 4.2)
            p2 = QPainterPath()
            p2.moveTo(cx - 7.0, cy + 4.2)
            p2.cubicTo(cx - 1.5, cy + 4.2, cx + 1.0, cy - 4.2, cx + 7.0, cy - 4.2)
            p.drawPath(p1)
            p.drawPath(p2)
            p.drawLine(QPointF(cx + 4.4, cy - 6.4), QPointF(cx + 7.2, cy - 4.2))
            p.drawLine(QPointF(cx + 4.4, cy - 2.0), QPointF(cx + 7.2, cy - 4.2))
            p.drawLine(QPointF(cx + 4.4, cy + 2.0), QPointF(cx + 7.2, cy + 4.2))
            p.drawLine(QPointF(cx + 4.4, cy + 6.4), QPointF(cx + 7.2, cy + 4.2))

        elif name in ("repeat", "repeat1"):
            pen = QPen(
                color,
                1.8,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(cx - 7.0, cy - 4.8, 14.0, 9.6), 3.2, 3.2)
            p.drawLine(QPointF(cx + 3.8, cy - 6.8), QPointF(cx + 6.6, cy - 4.8))
            p.drawLine(QPointF(cx - 3.8, cy + 6.8), QPointF(cx - 6.6, cy + 4.8))
            if name == "repeat1":
                p.setFont(self._f_rep1)
                p.drawText(
                    QRectF(cx - 4, cy - 4.8, 8, 9.6),
                    Qt.AlignmentFlag.AlignCenter,
                    "1",
                )

        elif name in ("vol_mute", "vol_low", "vol_high"):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            sp = QPainterPath()
            sp.moveTo(cx - 6.8, cy - 2.4)
            sp.lineTo(cx - 3.6, cy - 2.4)
            sp.lineTo(cx + 0.4, cy - 5.8)
            sp.lineTo(cx + 0.4, cy + 5.8)
            sp.lineTo(cx - 3.6, cy + 2.4)
            sp.lineTo(cx - 6.8, cy + 2.4)
            sp.closeSubpath()
            p.drawPath(sp)

            pen = QPen(
                color,
                1.6,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            if name == "vol_mute":
                p.drawLine(QPointF(cx + 2.8, cy - 2.6), QPointF(cx + 7.2, cy + 2.6))
                p.drawLine(QPointF(cx + 7.2, cy - 2.6), QPointF(cx + 2.8, cy + 2.6))
            else:
                p.drawArc(QRectF(cx - 0.5, cy - 3.6, 5.2, 7.2), -55 * 16, 110 * 16)
                if name == "vol_high":
                    p.drawArc(
                        QRectF(cx - 1.2, cy - 6.0, 9.2, 12.0), -55 * 16, 110 * 16
                    )

        p.restore()


def _relaunch_detached_if_console() -> None:
    """Re-launches silently via pythonw.exe so double-clicking or closing CMD keeps Now Bar alive."""
    if sys.platform != "win32":
        return
    if "--console" in sys.argv or "--detached" in sys.argv:
        return
    if os.path.basename(sys.executable).lower() != "python.exe":
        return
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        return
    try:
        flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        subprocess.Popen(
            [pyw, os.path.abspath(__file__), "--detached", *sys.argv[1:]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
        sys.exit(0)
    except Exception:
        pass


def main():
    _relaunch_detached_if_console()
    _install_error_hook()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    bar = SpotifyNowBar()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
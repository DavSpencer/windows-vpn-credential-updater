# vpn_cred_updater.py
# Bulk-update saved credentials of built-in Windows VPN profiles via RasSetCredentialsW.
# Requires: Python 3.11+, customtkinter. Windows only.
# Author: Micro  |  Telegram: https://t.me/MicroLab_SecZero

import base64
import ctypes
import json
import math
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import tkinter.messagebox as mb
import webbrowser
import weakref
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache

import customtkinter as ctk

TELEGRAM_URL = "https://t.me/MicroLab_SecZero"

# ----------------------------------------------------------------------------
# DPI awareness (must happen before any window is created)
# ----------------------------------------------------------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ----------------------------------------------------------------------------
# Win32 RAS bindings
# ----------------------------------------------------------------------------
UNLEN = 256
PWLEN = 256
DNLEN = 15
RASCM_UserName = 0x00000001
RASCM_Password = 0x00000002


class RASCREDENTIALSW(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("dwMask", wintypes.DWORD),
        ("szUserName", ctypes.c_wchar * (UNLEN + 1)),
        ("szPassword", ctypes.c_wchar * (PWLEN + 1)),
        ("szDomain", ctypes.c_wchar * (DNLEN + 1)),
    ]


_rasapi = ctypes.WinDLL("rasapi32.dll")
_RasSetCredentialsW = _rasapi.RasSetCredentialsW
_RasSetCredentialsW.argtypes = [
    wintypes.LPCWSTR,                 # lpszPhonebook (NULL = current user's default)
    wintypes.LPCWSTR,                 # lpszEntry
    ctypes.POINTER(RASCREDENTIALSW),  # lpCredentials
    wintypes.BOOL,                    # fClearCredentials
]
_RasSetCredentialsW.restype = wintypes.DWORD

_RasGetErrorStringW = _rasapi.RasGetErrorStringW
_RasGetErrorStringW.argtypes = [wintypes.UINT, wintypes.LPWSTR, wintypes.DWORD]
_RasGetErrorStringW.restype = wintypes.DWORD


def ras_error_message(code: int) -> str:
    size = 512
    buf = ctypes.create_unicode_buffer(size)
    rc = _RasGetErrorStringW(code, buf, size)
    if rc == 0 and buf.value:
        return buf.value.strip()
    return "Unknown RAS error."


ALLUSER_PBK = os.path.join(
    os.environ.get("ProgramData", r"C:\ProgramData"),
    r"Microsoft\Network\Connections\Pbk\rasphone.pbk",
)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
    else:
        exe = sys.executable
        params = " ".join(f'"{a}"' for a in [os.path.abspath(sys.argv[0])] + sys.argv[1:])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    return rc > 32


# ----------------------------------------------------------------------------
# Profile discovery
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Profile:
    name: str
    all_user: bool
    status: str
    server: str
    tunnel: str = ""

    @property
    def key(self) -> tuple:
        return (self.all_user, self.name)

    @property
    def connected(self) -> bool:
        return self.status.lower() == "connected"


_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
function Get-P($all) {
  if ($all) { $p = Get-VpnConnection -AllUserConnection } else { $p = Get-VpnConnection }
  @($p | ForEach-Object {
    [pscustomobject]@{
      Name = [string]$_.Name
      Status = [string]$_.ConnectionStatus
      Server = [string]$_.ServerAddress
      Tunnel = [string]$_.TunnelType
    }
  })
}
$o = [pscustomobject]@{ User = @(Get-P $false); All = @(Get-P $true) }
ConvertTo-Json -InputObject $o -Depth 4 -Compress
"""


class ProfileError(Exception):
    pass


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, dict):
        return [v]
    return list(v)


def fetch_profiles() -> list[Profile]:
    encoded = base64.b64encode(_PS_SCRIPT.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True, timeout=45,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    except FileNotFoundError:
        raise ProfileError("powershell.exe was not found. Cannot list VPN profiles.")
    except subprocess.TimeoutExpired:
        raise ProfileError("PowerShell timed out while listing VPN profiles.")
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0 and not out:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ProfileError(f"PowerShell failed (exit {proc.returncode}). {err[:300]}")
    if not out:
        return []
    try:
        data = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        raise ProfileError("Could not parse PowerShell output.")
    profiles: list[Profile] = []
    for scope_key, all_user in (("User", False), ("All", True)):
        for item in _as_list(data.get(scope_key)):
            profiles.append(Profile(item.get("Name", ""), all_user, item.get("Status", ""),
                                    item.get("Server", ""), item.get("Tunnel", "")))
    profiles = [p for p in profiles if p.name]
    profiles.sort(key=lambda p: (p.name.lower(), p.all_user))
    return profiles


def tunnel_label(t: str) -> str:
    names = {"ikev2": "IKEv2", "l2tp": "L2TP", "sstp": "SSTP", "pptp": "PPTP", "automatic": "AUTO"}
    t = (t or "").strip()
    return names.get(t.lower(), t.upper() or "VPN")


# ----------------------------------------------------------------------------
# Theme: every colour is a (light, dark) pair
# ----------------------------------------------------------------------------
BG = ("#eef1f5", "#1d2127")
CARD = ("#ffffff", "#262b32")
BAR = ("#e4e8ee", "#181b20")
BORDER = ("#d6dce5", "#363d47")
ROW = ("#f3f5f9", "#2d333c")
ROW_HOVER = ("#e6ebf2", "#38404b")
ROW_SEL = ("#dbe8fb", "#27405f")
ROW_SEL_BORDER = ("#74a6ee", "#3c6fb8")
FIELD = ("#f3f5f9", "#1d2127")
TEXT = ("#1b1f24", "#e9ecf1")
MUTED = ("#5d6877", "#9aa4b1")
ACCENT = ("#1a6fe0", "#1f58b8")
ACCENT_HOVER = ("#1559b8", "#2b6ad3")
ACCENT_EDGE = ("#4c8ee8", "#4a86dc")
DISABLED_BG = ("#cdd5e1", "#323a45")
CHECK_BORDER = ("#9aa6b6", "#5d6773")
OK_TEXT = ("#1c8a42", "#4fd07a")
ERR_TEXT = ("#c53b36", "#f2736e")
OK_FILL = ("#2e9e4f", "#2e9e4f")
ERR_FILL = ("#d9534f", "#d9534f")
BADGE_BG = ("#dde3ec", "#3a424d")
BADGE_TEXT = ("#46505e", "#b5bdc9")
SCOPE_BG = ("#d3e4fb", "#244262")
SCOPE_TEXT = ("#195cb8", "#8fbcf7")
LIVE_BG = ("#d5efdd", "#1f4030")
LINK = ("#0b66c3", "#5aa9ff")
BANNER_BG = ("#fff1cf", "#4a3a12")
BANNER_TEXT = ("#6b4a00", "#ffd98a")

FONT = "Segoe UI"
ICON_FONT = "Segoe MDL2 Assets"
HAS_ICON_FONT = False  # decided at start-up (needs a Tk root)

GLYPH_SEARCH = "\uE721"
GLYPH_REFRESH = "\uE72C"
GLYPH_EYE = "\uE7B3"
GLYPH_EYE_OFF = "\uED1A"


def col(pair):
    if isinstance(pair, str):
        return pair
    return pair[1] if ctk.get_appearance_mode() == "Dark" else pair[0]


@lru_cache(maxsize=None)
def F(size: int, weight: str = "normal", underline: bool = False):
    return ctk.CTkFont(family=FONT, size=size, weight=weight, underline=underline)


@lru_cache(maxsize=None)
def IF(size: int):
    return ctk.CTkFont(family=ICON_FONT, size=size)


def dpi_scale(widget) -> float:
    try:
        return max(1.0, widget.winfo_fpixels("1i") / 96.0)
    except Exception:
        return 1.0


# ----------------------------------------------------------------------------
# Small vector icons drawn on canvases (no image files, no Pillow)
# ----------------------------------------------------------------------------
def _bezier(p0, p1, p2, n=10):
    pts = []
    for i in range(n + 1):
        t = i / n
        pts.append(((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
                    (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]))
    return pts


SHIELD = ([(50, 5), (88, 19), (88, 48)]
          + _bezier((88, 48), (86, 78), (50, 95))[1:]
          + _bezier((50, 95), (14, 78), (12, 48))[1:]
          + [(12, 19)])

SHIELD_STATES = {
    "brand":   dict(fill=("#2f80ed", "#2f80ed"), edge=("#8dbcfa", "#8dbcfa"), glyph="#ffffff"),
    "idle":    dict(fill=("#c3d0e6", "#45536c"), edge=("#8ea5ca", "#7d90b0"), glyph=("#ffffff", "#e6edf8")),
    "working": dict(fill=("#4f8fe8", "#2f66c4"), edge=("#2f6fd0", "#6da2f2"), glyph="#ffffff"),
    "ok":      dict(fill=("#3dae5d", "#2e9e4f"), edge=("#2b8c47", "#6fd08b"), glyph="#ffffff"),
    "warn":    dict(fill=("#e0883a", "#c77d00"), edge=("#c0701f", "#f0b24d"), glyph="#ffffff"),
    "error":   dict(fill=("#e06762", "#d9534f"), edge=("#c2433f", "#f08b87"), glyph="#ffffff"),
}


class VectorIcon(tk.Canvas):
    _instances: "weakref.WeakSet" = weakref.WeakSet()

    def __init__(self, master, size, bg_pair):
        px = max(10, round(size * dpi_scale(master)))
        super().__init__(master, width=px, height=px, highlightthickness=0, bd=0, bg=col(bg_pair))
        self.px = px
        self.bg_pair = bg_pair
        VectorIcon._instances.add(self)

    def redraw(self):
        self.delete("all")
        self.configure(bg=col(self.bg_pair))
        self._draw(self.px)

    def _draw(self, px):  # pragma: no cover - overridden
        pass

    @classmethod
    def refresh_all(cls):
        for icon in list(cls._instances):
            try:
                icon.redraw()
            except tk.TclError:
                pass


class ShieldIcon(VectorIcon):
    def __init__(self, master, size, bg_pair, state="idle"):
        super().__init__(master, size, bg_pair)
        self.state = state
        self.angle = 0
        self.redraw()

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self.redraw()

    def tick(self):
        self.angle = (self.angle - 20) % 360
        self.redraw()

    def _draw(self, px):
        k = px / 100.0
        st = SHIELD_STATES[self.state]
        pts = [c * k for xy in SHIELD for c in xy]
        self.create_polygon(pts, fill=col(st["fill"]), outline=col(st["edge"]),
                            width=max(1, round(2.5 * k)), joinstyle="round")
        g = col(st["glyph"])
        w = max(2, round(5 * k))
        if self.state in ("idle", "working"):
            cx, cy, r = 50, 53, 19
            box = ((cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k)
            for off in (30, 210):
                start = self.angle + off
                self.create_arc(*box, start=start, extent=130, style="arc", outline=g, width=w)
                # Canvas arcs have no arrowheads, so draw one by hand at the arc's end.
                a = math.radians(start + 130)
                ex, ey = cx + r * math.cos(a), cy - r * math.sin(a)
                tx, ty = -math.sin(a), -math.cos(a)      # direction of travel
                nx, ny = math.cos(a), -math.sin(a)       # radial direction
                head = [(ex + tx * 12, ey + ty * 12),
                        (ex + nx * 9, ey + ny * 9),
                        (ex - nx * 9, ey - ny * 9)]
                self.create_polygon([c * k for xy in head for c in xy], fill=g, outline=g)
        elif self.state == "ok":
            self.create_line([c * k for xy in [(30, 54), (45, 69), (71, 38)] for c in xy],
                             fill=g, width=w + 1, capstyle="round", joinstyle="round")
        elif self.state in ("warn", "error"):
            self.create_line(50 * k, 30 * k, 50 * k, 58 * k, fill=g, width=w + 1, capstyle="round")
            self.create_oval(45.5 * k, 65.5 * k, 54.5 * k, 74.5 * k, fill=g, outline=g)
        elif self.state == "brand":
            self.create_oval(41 * k, 33 * k, 59 * k, 51 * k, fill=g, outline=g)
            self.create_polygon([c * k for xy in [(45, 48), (55, 48), (58, 71), (42, 71)] for c in xy],
                                fill=g, outline=g)


class CircleIcon(VectorIcon):
    def __init__(self, master, size, bg_pair, ok: bool):
        super().__init__(master, size, bg_pair)
        self.ok = ok
        self.redraw()

    def _draw(self, px):
        self.create_oval(1, 1, px - 1, px - 1, fill=col(OK_FILL if self.ok else ERR_FILL), outline="")
        w = max(2, round(px * 0.09))
        if self.ok:
            pts = [(0.28, 0.52), (0.43, 0.67), (0.72, 0.36)]
            self.create_line([c * px for xy in pts for c in xy], fill="#ffffff", width=w,
                             capstyle="round", joinstyle="round")
        else:
            for (a, b), (c, d) in [((0.33, 0.33), (0.67, 0.67)), ((0.67, 0.33), (0.33, 0.67))]:
                self.create_line(a * px, b * px, c * px, d * px, fill="#ffffff", width=w, capstyle="round")


class TelegramIcon(VectorIcon):
    def __init__(self, master, size, bg_pair):
        super().__init__(master, size, bg_pair)
        self.redraw()

    def _draw(self, px):
        k = px / 100.0
        self.create_oval(0, 0, px - 1, px - 1, fill="#2aabee", outline="")
        plane = [(22, 50), (77, 28), (67, 76), (49, 62), (38, 71), (36, 57)]
        self.create_polygon([c * k for xy in plane for c in xy], fill="#ffffff", outline="")


# ----------------------------------------------------------------------------
# Reusable widgets
# ----------------------------------------------------------------------------
class IconEntry(ctk.CTkFrame):
    """Rounded field with an optional leading icon and trailing widget."""

    def __init__(self, master, icon=None, placeholder=None, show=None):
        super().__init__(master, corner_radius=10, fg_color=FIELD, border_width=1, border_color=BORDER)
        self.grid_columnconfigure(1, weight=1)
        if icon:
            ctk.CTkLabel(self, text=icon, font=IF(15), text_color=MUTED, width=30).grid(
                row=0, column=0, padx=(8, 0))
        self.entry = ctk.CTkEntry(self, placeholder_text=placeholder, show=show, fg_color="transparent",
                                  border_width=0, height=36, font=F(14), text_color=TEXT)
        self.entry.grid(row=0, column=1, sticky="ew", padx=(0 if icon else 8, 4), pady=2)
        self.entry.bind("<FocusIn>", lambda _e: self.configure(border_color=ACCENT_EDGE), add="+")
        self.entry.bind("<FocusOut>", lambda _e: self.configure(border_color=BORDER), add="+")

    def add_trailing(self, widget):
        widget.grid(row=0, column=2, padx=(0, 5))


class ProfileRow(ctk.CTkFrame):
    def __init__(self, master, profile: Profile, selected: bool, on_toggle):
        super().__init__(master, corner_radius=10, border_width=1, fg_color=ROW, border_color=BORDER)
        self.profile = profile
        self._on_toggle = on_toggle
        self.var = ctk.BooleanVar(value=selected)
        self.grid_columnconfigure(1, weight=1)

        self.cb = ctk.CTkCheckBox(self, text="", variable=self.var, width=24, checkbox_width=22,
                                  checkbox_height=22, corner_radius=6, border_width=2,
                                  fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=CHECK_BORDER,
                                  command=self._changed)
        self.cb.grid(row=0, column=0, padx=(12, 4), pady=11)
        self.lbl = ctk.CTkLabel(self, text=profile.name, anchor="w", font=F(14), text_color=TEXT)
        self.lbl.grid(row=0, column=1, sticky="ew", padx=(4, 6))

        clickable = [self, self.lbl]
        column = 2
        badges = []
        if profile.connected:
            badges.append(("CONNECTED", LIVE_BG, OK_TEXT))
        if profile.all_user:
            badges.append(("ALL USERS", SCOPE_BG, SCOPE_TEXT))
        badges.append((tunnel_label(profile.tunnel), BADGE_BG, BADGE_TEXT))
        for text, bg, fg in badges:
            b = ctk.CTkLabel(self, text=f" {text} ", font=F(10, "bold"), fg_color=bg, text_color=fg,
                             corner_radius=6, height=20)
            b.grid(row=0, column=column, padx=(0, 8))
            clickable.append(b)
            column += 1
        for w in clickable:
            w.bind("<Button-1>", self._click)
        self._paint()

    def selected(self) -> bool:
        return bool(self.var.get())

    def set(self, value: bool):
        self.var.set(value)
        self._paint()

    def _click(self, _event=None):
        self.var.set(not self.var.get())
        self._changed()

    def _changed(self):
        self._paint()
        self._on_toggle()

    def _paint(self):
        sel = self.selected()
        self.configure(fg_color=ROW_SEL if sel else ROW, border_color=ROW_SEL_BORDER if sel else BORDER)


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        global HAS_ICON_FONT
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")
        self.title("Windows VPN Credential Updater")
        self.geometry("1020x700")
        self.minsize(940, 640)
        self.configure(fg_color=BG)
        try:
            HAS_ICON_FONT = ICON_FONT in set(tkfont.families(self))
        except Exception:
            HAS_ICON_FONT = False

        self.admin = is_admin()
        self.rows: dict[tuple, ProfileRow] = {}
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.spinning = False
        self.show_pw = False
        self._rest_state = "idle"
        self._last_term = ""
        self._mode = ctk.get_appearance_mode()

        self._build()
        self.after(80, self._poll)
        self.refresh()

    # ---- layout ----------------------------------------------------------
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 12))
        self.logo = ShieldIcon(hdr, 42, BG, state="brand")
        self.logo.grid(row=0, column=0, rowspan=2, padx=(0, 14))
        ctk.CTkLabel(hdr, text="Windows VPN Credential Updater", font=F(26), text_color=TEXT).grid(
            row=0, column=1, sticky="w")
        ctk.CTkLabel(hdr, text="Update Saved Username & Password", font=F(15), text_color=MUTED).grid(
            row=1, column=1, sticky="w")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=19, pady=(0, 12))
        body.grid_rowconfigure(0, weight=1)
        for i, w in enumerate((5, 4, 5)):
            body.grid_columnconfigure(i, weight=w, uniform="cols")
        self._build_profiles(body)
        self._build_credentials(body)
        self._build_status(body)
        self._build_statusbar()

    def _card(self, parent, column):
        card = ctk.CTkFrame(parent, corner_radius=14, fg_color=CARD, border_width=1, border_color=BORDER)
        card.grid(row=0, column=column, sticky="nsew", padx=5)
        return card

    def _header(self, card, text):
        ctk.CTkLabel(card, text=text, font=F(15, "bold"), text_color=TEXT).grid(
            row=0, column=0, sticky="w", padx=18, pady=(16, 10))

    def _ghost_button(self, parent, text, command, width=0, font=None):
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=30, corner_radius=8,
                             fg_color=ROW, hover_color=ROW_HOVER, text_color=TEXT, font=font or F(12))

    def _build_profiles(self, parent):
        card = self._card(parent, 0)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(3, weight=1)
        self._header(card, "VPN PROFILES")

        self.search = IconEntry(card, icon=GLYPH_SEARCH if HAS_ICON_FONT else None, placeholder="Search…")
        self.search.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        self.ent_search = self.search.entry
        self._bind_edit(self.ent_search)

        tools = ctk.CTkFrame(card, fg_color="transparent")
        tools.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 6))
        tools.grid_columnconfigure(2, weight=1)
        self._ghost_button(tools, "Select all", self.select_all, 82).grid(row=0, column=0, padx=(0, 6))
        self._ghost_button(tools, "Select none", self.select_none, 90).grid(row=0, column=1)
        if HAS_ICON_FONT:
            refresh = self._ghost_button(tools, GLYPH_REFRESH, self.refresh, 34, IF(14))
        else:
            refresh = self._ghost_button(tools, "Refresh", self.refresh, 70)
        refresh.grid(row=0, column=3)

        self.list_frame = ctk.CTkScrollableFrame(card, fg_color=CARD, corner_radius=0)
        self.list_frame.grid(row=3, column=0, sticky="nsew", padx=(10, 6), pady=(0, 12))
        self.lbl_empty = ctk.CTkLabel(self.list_frame, text="", font=F(13), text_color=MUTED)

    def _build_credentials(self, parent):
        card = self._card(parent, 1)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(7, weight=1)
        self._header(card, "NEW CREDENTIALS")

        ctk.CTkLabel(card, text="New Username:", font=F(14), text_color=TEXT).grid(
            row=1, column=0, sticky="w", padx=18, pady=(2, 6))
        self.user_box = IconEntry(card, placeholder="(Enter Username)")
        self.user_box.grid(row=2, column=0, sticky="ew", padx=16)
        self.ent_user = self.user_box.entry
        self._bind_edit(self.ent_user)

        ctk.CTkLabel(card, text="New Password:", font=F(14), text_color=TEXT).grid(
            row=3, column=0, sticky="w", padx=18, pady=(14, 6))
        self.pass_box = IconEntry(card, show="•")
        self.pass_box.grid(row=4, column=0, sticky="ew", padx=16)
        self.ent_pass = self.pass_box.entry
        self._bind_edit(self.ent_pass)
        self.btn_eye = ctk.CTkButton(
            self.pass_box, text=GLYPH_EYE if HAS_ICON_FONT else "Show", command=self._toggle_show,
            width=34 if HAS_ICON_FONT else 50, height=30, corner_radius=8, fg_color="transparent",
            hover_color=ROW_HOVER, text_color=MUTED, font=IF(15) if HAS_ICON_FONT else F(12))
        self.pass_box.add_trailing(self.btn_eye)

        self.btn_apply = ctk.CTkButton(
            card, text="Apply", height=42, corner_radius=10, font=F(15, "bold"), state="disabled",
            fg_color=DISABLED_BG, hover_color=ACCENT_HOVER, text_color="#ffffff",
            text_color_disabled=("#8b95a3", "#7d8794"), command=self.apply)
        self.btn_apply.grid(row=5, column=0, sticky="ew", padx=16, pady=(18, 0))

        # admin banner (hidden until needed)
        self.banner = ctk.CTkFrame(card, corner_radius=10, fg_color=BANNER_BG)
        self.banner.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.banner, font=F(12), text_color=BANNER_TEXT, justify="left", anchor="w",
                     wraplength=215,
                     text="All-user profiles are ticked, but this app is not running as administrator. "
                          "Those profiles will be skipped.").grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 6))
        ctk.CTkButton(self.banner, text="Restart as administrator", height=32, corner_radius=8,
                      font=F(12, "bold"), fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=self.restart_admin).grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        self.banner.grid(row=6, column=0, sticky="ew", padx=16, pady=(14, 0))
        self.banner.grid_remove()

        ctk.CTkLabel(
            card, font=F(11), text_color=MUTED, justify="left", anchor="w", wraplength=235,
            text="Only the saved username and password change. Server, VPN type and sign-in method "
                 "stay as they are.\n\nThe password is never stored and is cleared after Apply."
        ).grid(row=8, column=0, sticky="sw", padx=18, pady=(8, 16))

    def _build_status(self, parent):
        card = self._card(parent, 2)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(4, weight=1)
        self._header(card, "UPDATE STATUS")

        self.shield = ShieldIcon(card, 112, CARD, state="idle")
        self.shield.grid(row=1, column=0, pady=(0, 8))
        self.progress = ctk.CTkProgressBar(card, height=8, corner_radius=4, fg_color=ROW,
                                           progress_color=ACCENT)
        self.progress.set(0)
        self.progress.grid(row=2, column=0, sticky="ew", padx=20, pady=(2, 4))
        self.lbl_progress = ctk.CTkLabel(card, text="", font=F(12), text_color=MUTED)
        self.lbl_progress.grid(row=3, column=0, pady=(0, 6))

        self.results = ctk.CTkScrollableFrame(card, fg_color=CARD, corner_radius=0)
        self.results.grid(row=4, column=0, sticky="nsew", padx=(10, 6), pady=(0, 12))
        self.lbl_results_empty = ctk.CTkLabel(
            self.results, text="Results will appear here\nafter you click Apply.",
            font=F(13), text_color=MUTED, justify="center")
        self.lbl_results_empty.pack(pady=18)

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=BAR, height=36)
        bar.grid(row=2, column=0, sticky="ew")

        def divider():
            ctk.CTkFrame(bar, width=1, height=14, fg_color=BORDER).pack(side="left", pady=11)

        self.lbl_state = ctk.CTkLabel(bar, text="Ready", font=F(12), text_color=TEXT)
        self.lbl_state.pack(side="left", padx=(20, 12), pady=7)
        divider()
        self.lbl_sel = ctk.CTkLabel(bar, text="Selected: 0 Profiles", font=F(12), text_color=MUTED)
        self.lbl_sel.pack(side="left", padx=12, pady=7)
        divider()
        ctk.CTkLabel(bar, text="Administrator" if self.admin else "Standard user", font=F(12),
                     text_color=MUTED).pack(side="left", padx=12, pady=7)

        link = ctk.CTkLabel(bar, text="Telegram channel", font=F(12, underline=True), text_color=LINK)
        link.pack(side="right", padx=(6, 20), pady=7)
        try:
            link.configure(cursor="hand2")
        except Exception:
            pass
        link.bind("<Button-1>", lambda _e: webbrowser.open(TELEGRAM_URL))
        TelegramIcon(bar, 16, BAR).pack(side="right", pady=10)
        ctk.CTkLabel(bar, text="Made by Micro  |  ©2026 MicroLab", font=F(12), text_color=MUTED).pack(
            side="right", padx=(0, 8), pady=7)

    # ---- helpers ---------------------------------------------------------
    def _bind_edit(self, entry):
        for seq in ("<KeyRelease>", "<<Paste>>", "<<Cut>>"):
            entry.bind(seq, lambda _e: self.after(10, self._edited), add="+")

    def _edited(self):
        term = self.ent_search.get().strip().lower()
        if term != self._last_term:
            self._last_term = term
            self._apply_filter()
        self._update_state()

    def set_status(self, text: str):
        self.lbl_state.configure(text=text)

    def _toggle_show(self):
        self.show_pw = not self.show_pw
        self.ent_pass.configure(show="" if self.show_pw else "•")
        if HAS_ICON_FONT:
            self.btn_eye.configure(text=GLYPH_EYE_OFF if self.show_pw else GLYPH_EYE)
        else:
            self.btn_eye.configure(text="Hide" if self.show_pw else "Show")

    def ticked(self) -> list[Profile]:
        return [r.profile for r in self.rows.values() if r.selected()]

    def _update_state(self):
        sel = self.ticked()
        if (not self.admin) and any(p.all_user for p in sel):
            self.banner.grid()
        else:
            self.banner.grid_remove()
        ready = (bool(sel) and bool(self.ent_user.get().strip()) and bool(self.ent_pass.get())
                 and not self.busy)
        self.btn_apply.configure(state="normal" if ready else "disabled",
                                 fg_color=ACCENT if ready else DISABLED_BG)
        n = len(sel)
        self.lbl_sel.configure(text=f"Selected: {n} Profile{'' if n == 1 else 's'}")

    def _set_busy(self, busy: bool):
        self.busy = busy
        self.spinning = busy
        if not busy:
            self.shield.angle = 0
            self.shield.set_state(self._rest_state)
            self.shield.redraw()

    # ---- list handling ---------------------------------------------------
    def refresh(self):
        if self.busy:
            return
        self._set_busy(True)
        self.shield.set_state("working")
        self.btn_apply.configure(state="disabled", fg_color=DISABLED_BG)
        self.set_status("Loading VPN profiles…")
        keep = {p.key for p in self.ticked()}

        def work():
            try:
                self.q.put(("profiles", fetch_profiles(), keep))
            except ProfileError as e:
                self.q.put(("profiles_error", str(e)))
            except Exception as e:
                self.q.put(("profiles_error", f"Unexpected error: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _populate(self, profiles: list[Profile], keep: set):
        for r in self.rows.values():
            r.destroy()
        self.rows.clear()
        for p in profiles:
            self.rows[p.key] = ProfileRow(self.list_frame, p, p.key in keep, self._update_state)
        self._apply_filter()

    def _visible_rows(self):
        term = self.ent_search.get().strip().lower()
        return [r for r in self.rows.values()
                if not term or term in r.profile.name.lower() or term in r.profile.server.lower()]

    def _apply_filter(self):
        for r in self.rows.values():
            r.pack_forget()
        self.lbl_empty.pack_forget()
        visible = self._visible_rows()
        for r in visible:
            r.pack(fill="x", padx=2, pady=3)
        if not visible:
            self.lbl_empty.configure(
                text="No VPN profiles found." if not self.rows else "No profiles match your search.")
            self.lbl_empty.pack(pady=24)

    def select_all(self):
        for r in self._visible_rows():
            r.set(True)
        self._update_state()

    def select_none(self):
        for r in self._visible_rows():
            r.set(False)
        self._update_state()

    # ---- results ---------------------------------------------------------
    def _clear_results(self):
        for w in self.results.winfo_children():
            if w is not self.lbl_results_empty:
                w.destroy()
        self.lbl_results_empty.pack_forget()

    def _add_result(self, title: str, ok: bool, badge: str | None, detail: str | None):
        self.lbl_results_empty.pack_forget()
        card = ctk.CTkFrame(self.results, corner_radius=10, fg_color=ROW, border_width=1,
                            border_color=BORDER)
        card.pack(fill="x", padx=2, pady=3)
        card.grid_columnconfigure(3, weight=1)
        CircleIcon(card, 28, ROW, ok).grid(row=0, column=0, rowspan=3, padx=(12, 10), pady=10)
        ctk.CTkLabel(card, text="SUCCESS" if ok else "ERROR", font=F(13, "bold"),
                     text_color=OK_TEXT if ok else ERR_TEXT).grid(row=0, column=1, sticky="w", pady=(10, 0))
        if badge:
            ctk.CTkLabel(card, text=f" {badge} ", font=F(10, "bold"), fg_color=ERR_FILL, text_color="#ffffff",
                         corner_radius=6, height=18).grid(row=0, column=2, sticky="w", padx=(8, 0), pady=(10, 0))
        ctk.CTkLabel(card, text=title, font=F(14), text_color=TEXT, anchor="w", justify="left",
                     wraplength=200).grid(row=1, column=1, columnspan=3, sticky="w", padx=(0, 10),
                                          pady=(0, 0 if detail else 10))
        if detail:
            ctk.CTkLabel(card, text=detail, font=F(12), text_color=MUTED, anchor="w", justify="left",
                         wraplength=200).grid(row=2, column=1, columnspan=3, sticky="w", padx=(0, 10),
                                              pady=(0, 10))
        self.after(30, self._scroll_results_end)

    def _scroll_results_end(self):
        try:
            self.results._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    # ---- apply -----------------------------------------------------------
    def restart_admin(self):
        if relaunch_as_admin():
            self.destroy()
        else:
            mb.showerror("Elevation failed", "Elevation was cancelled or failed. Staying in the current session.")

    def apply(self):
        sel = self.ticked()
        user = self.ent_user.get().strip()
        pwd = self.ent_pass.get()
        if not sel or not user or not pwd:
            return
        if len(user) > UNLEN or len(pwd) > PWLEN:
            mb.showerror("Input too long", f"Username and password must each be at most {UNLEN} characters.")
            return
        connected = [p.name for p in sel if p.connected]
        if connected:
            ok = mb.askokcancel(
                "Connected profiles",
                "These profiles are currently connected:\n\n  " + "\n  ".join(connected) +
                "\n\nNew credentials apply on the next connection, not to the active session. Continue?")
            if not ok:
                return

        self._set_busy(True)
        self.shield.set_state("working")
        self._clear_results()
        self.progress.set(0)
        self.lbl_progress.configure(text=f"Updating 0 of {len(sel)}…")
        self.btn_apply.configure(state="disabled", fg_color=DISABLED_BG)
        self.set_status("Applying…")
        admin = self.admin
        threading.Thread(target=self._worker, args=(sel, user, pwd, admin), daemon=True).start()

        # Clear the password everywhere on the GUI side.
        self.ent_pass.delete(0, "end")
        if self.show_pw:
            self._toggle_show()
        pwd = None  # noqa: F841  (drop this reference)

    def _worker(self, sel: list[Profile], user: str, pwd: str, admin: bool):
        cred = RASCREDENTIALSW()
        good = bad = 0
        try:
            cred.dwSize = ctypes.sizeof(RASCREDENTIALSW)
            cred.dwMask = RASCM_UserName | RASCM_Password
            cred.szUserName = user
            cred.szPassword = pwd
            pwd = None
            total = len(sel)
            for i, p in enumerate(sel, 1):
                scope = "All-user" if p.all_user else "Current-user"
                if p.all_user and not admin:
                    bad += 1
                    self.q.put(("result", p, False, "UAC",
                                "All-user profile: restart the app as administrator."))
                else:
                    try:
                        pbk = ALLUSER_PBK if p.all_user else None
                        rc = _RasSetCredentialsW(pbk, p.name, ctypes.byref(cred), False)
                        if rc == 0:
                            good += 1
                            note = " Applies on next connection." if p.connected else ""
                            self.q.put(("result", p, True, None, f"{scope} profile updated.{note}"))
                        else:
                            bad += 1
                            self.q.put(("result", p, False, f"RAS {rc}", ras_error_message(rc)))
                    except Exception as e:
                        bad += 1
                        self.q.put(("result", p, False, "EXC", str(e)))
                self.q.put(("progress", i, total))
        finally:
            ctypes.memset(ctypes.addressof(cred), 0, ctypes.sizeof(cred))
            user = None
            self.q.put(("done", good, bad))

    # ---- queue pump ------------------------------------------------------
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "profiles":
                    self._set_busy(False)
                    self._populate(msg[1], msg[2])
                    self._update_state()
                    self.set_status("Ready")
                elif kind == "profiles_error":
                    self._set_busy(False)
                    self._populate([], set())
                    self._clear_results()
                    self._add_result("Could not load VPN profiles", False, None, msg[1])
                    self._update_state()
                    self.set_status("Ready")
                elif kind == "progress":
                    _, i, total = msg
                    self.progress.set(i / total)
                    self.lbl_progress.configure(text=f"Updating {i} of {total}…")
                elif kind == "result":
                    _, p, ok, badge, detail = msg
                    self._add_result(p.name, ok, badge, detail)
                elif kind == "done":
                    _, good, bad = msg
                    self._rest_state = "ok" if bad == 0 else ("warn" if good else "error")
                    self._set_busy(False)
                    if bad == 0:
                        self.lbl_progress.configure(text=f"All {good} profile(s) updated.")
                    else:
                        self.lbl_progress.configure(text=f"{good} succeeded, {bad} failed.")
                    self._update_state()
                    self.set_status("Ready")
        except queue.Empty:
            pass
        if self.spinning:
            self.shield.tick()
        mode = ctk.get_appearance_mode()
        if mode != self._mode:
            self._mode = mode
            VectorIcon.refresh_all()
        self.after(80, self._poll)


if __name__ == "__main__":
    if os.name != "nt":
        print("This tool only runs on Windows.")
        sys.exit(1)
    App().mainloop()

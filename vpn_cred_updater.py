# vpn_cred_updater.py
# Bulk-update saved credentials of built-in Windows VPN profiles via RasSetCredentialsW.
# Requires: Python 3.11+, customtkinter. Windows only.
# Author: Micro  |  Telegram: https://t.me/MicroLab_SecZero

import base64
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter.messagebox as mb
import webbrowser
from ctypes import wintypes
from dataclasses import dataclass

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


def ras_error_text(code: int) -> str:
    size = 512
    buf = ctypes.create_unicode_buffer(size)
    rc = _RasGetErrorStringW(code, buf, size)
    if rc == 0 and buf.value:
        return f"{buf.value.strip()} (RAS error {code})"
    return f"Unknown RAS error {code}"


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

    @property
    def key(self) -> tuple:
        return (self.all_user, self.name)

    @property
    def label(self) -> str:
        scope = "All users" if self.all_user else "Current user"
        extra = "  •  CONNECTED" if self.status.lower() == "connected" else ""
        return f"{self.name}   [{scope}]{extra}"


_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
function Get-P($all) {
  if ($all) { $p = Get-VpnConnection -AllUserConnection } else { $p = Get-VpnConnection }
  @($p | ForEach-Object {
    [pscustomobject]@{ Name = [string]$_.Name; Status = [string]$_.ConnectionStatus; Server = [string]$_.ServerAddress }
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
    for item in _as_list(data.get("User")):
        profiles.append(Profile(item.get("Name", ""), False, item.get("Status", ""), item.get("Server", "")))
    for item in _as_list(data.get("All")):
        profiles.append(Profile(item.get("Name", ""), True, item.get("Status", ""), item.get("Server", "")))
    profiles = [p for p in profiles if p.name]
    profiles.sort(key=lambda p: (p.name.lower(), p.all_user))
    return profiles


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
OK_COLOR = "#2e9e4f"
ERR_COLOR = "#d9534f"
WARN_COLOR = "#c77d00"


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")
        self.title("VPN Credential Updater")
        self.geometry("820x760")
        self.minsize(720, 640)

        self.admin = is_admin()
        self.profiles: list[Profile] = []
        self.rows: dict[tuple, tuple[ctk.BooleanVar, ctk.CTkCheckBox, Profile]] = {}
        self.q: queue.Queue = queue.Queue()
        self.busy = False

        self.var_user = ctk.StringVar()
        self.var_pass = ctk.StringVar()
        self.var_filter = ctk.StringVar()
        self.var_show = ctk.BooleanVar(value=False)

        self._build()
        self.var_user.trace_add("write", lambda *_: self._update_state())
        self.var_pass.trace_add("write", lambda *_: self._update_state())
        self.var_filter.trace_add("write", lambda *_: self._apply_filter())
        self.after(100, self._poll)
        self.refresh()

    # ---- layout ----------------------------------------------------------
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=3)
        self.grid_rowconfigure(6, weight=2)

        ctk.CTkLabel(self, text="VPN Credential Updater",
                     font=ctk.CTkFont(size=24, weight="bold")).grid(
            row=0, column=0, padx=20, pady=(16, 0), sticky="w")
        ctk.CTkLabel(self, text="Set the same saved username and password on many Windows VPN profiles.",
                     text_color=("gray35", "gray65")).grid(row=1, column=0, padx=20, pady=(0, 8), sticky="w")

        # credentials
        cred = ctk.CTkFrame(self, corner_radius=12)
        cred.grid(row=2, column=0, padx=20, pady=6, sticky="ew")
        cred.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(cred, text="Username").grid(row=0, column=0, padx=(14, 8), pady=(12, 6), sticky="w")
        ctk.CTkEntry(cred, textvariable=self.var_user, corner_radius=8).grid(
            row=0, column=1, padx=(0, 14), pady=(12, 6), sticky="ew")
        ctk.CTkLabel(cred, text="Password").grid(row=1, column=0, padx=(14, 8), pady=(6, 12), sticky="w")
        self.ent_pass = ctk.CTkEntry(cred, textvariable=self.var_pass, show="•", corner_radius=8)
        self.ent_pass.grid(row=1, column=1, padx=(0, 8), pady=(6, 12), sticky="ew")
        ctk.CTkCheckBox(cred, text="Show", variable=self.var_show, width=60,
                        command=self._toggle_show).grid(row=1, column=2, padx=(0, 14), pady=(6, 12))

        # profile list
        box = ctk.CTkFrame(self, corner_radius=12)
        box.grid(row=3, column=0, padx=20, pady=6, sticky="nsew")
        box.grid_columnconfigure(0, weight=1)
        box.grid_rowconfigure(1, weight=1)
        bar = ctk.CTkFrame(box, fg_color="transparent")
        bar.grid(row=0, column=0, padx=10, pady=(10, 4), sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(bar, textvariable=self.var_filter, placeholder_text="Filter profiles…",
                     corner_radius=8).grid(row=0, column=0, padx=(0, 8), sticky="ew")
        ctk.CTkButton(bar, text="Select all", width=90, command=self.select_all).grid(row=0, column=1, padx=3)
        ctk.CTkButton(bar, text="Select none", width=90, command=self.select_none).grid(row=0, column=2, padx=3)
        ctk.CTkButton(bar, text="Refresh", width=80, command=self.refresh).grid(row=0, column=3, padx=(3, 0))
        self.list_frame = ctk.CTkScrollableFrame(box, corner_radius=8)
        self.list_frame.grid(row=1, column=0, padx=10, pady=(4, 10), sticky="nsew")
        self.lbl_empty = ctk.CTkLabel(self.list_frame, text="")

        # admin banner (hidden until needed)
        self.banner = ctk.CTkFrame(self, corner_radius=10, fg_color=("#fff4d6", "#4a3a10"))
        self.banner.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.banner, text="All-user profiles are selected, but this app is not running as administrator. "
                                       "Those profiles will be skipped.",
                     wraplength=520, justify="left", text_color=("#6b4a00", "#ffd98a")).grid(
            row=0, column=0, padx=12, pady=8, sticky="w")
        ctk.CTkButton(self.banner, text="Restart as administrator", command=self.restart_admin).grid(
            row=0, column=1, padx=12, pady=8)
        self.banner.grid(row=4, column=0, padx=20, pady=4, sticky="ew")
        self.banner.grid_remove()

        # apply + progress
        act = ctk.CTkFrame(self, fg_color="transparent")
        act.grid(row=5, column=0, padx=20, pady=(6, 2), sticky="ew")
        act.grid_columnconfigure(1, weight=1)
        self.btn_apply = ctk.CTkButton(act, text="Apply", width=120, height=36, state="disabled",
                                       command=self.apply)
        self.btn_apply.grid(row=0, column=0, padx=(0, 12))
        self.progress = ctk.CTkProgressBar(act)
        self.progress.set(0)
        self.progress.grid(row=0, column=1, sticky="ew")

        # results
        self.results = ctk.CTkTextbox(self, corner_radius=10, height=120, state="disabled", wrap="word")
        self.results.grid(row=6, column=0, padx=20, pady=6, sticky="nsew")
        self.results.tag_config("ok", foreground=OK_COLOR)
        self.results.tag_config("err", foreground=ERR_COLOR)
        self.results.tag_config("warn", foreground=WARN_COLOR)

        # status bar + channel link
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=7, column=0, padx=20, pady=(0, 8), sticky="ew")
        foot.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(foot, text="", anchor="w", height=26,
                                   text_color=("gray30", "gray70"))
        self.status.grid(row=0, column=0, sticky="ew")
        link = ctk.CTkLabel(foot, text="✈ Join @MicroLab_SecZero for more tools",
                            text_color=("#0b66c3", "#5aa9ff"), cursor="hand2",
                            font=ctk.CTkFont(underline=True))
        link.grid(row=0, column=1, padx=(12, 0), sticky="e")
        link.bind("<Button-1>", lambda _e: webbrowser.open(TELEGRAM_URL))

    # ---- helpers ---------------------------------------------------------
    def set_status(self, text: str):
        who = "administrator" if self.admin else "standard user"
        self.status.configure(text=f"{text}    |    Running as {who}")

    def _toggle_show(self):
        self.ent_pass.configure(show="" if self.var_show.get() else "•")

    def _log(self, text: str, tag: str | None = None):
        self.results.configure(state="normal")
        if tag:
            self.results.insert("end", text + "\n", tag)
        else:
            self.results.insert("end", text + "\n")
        self.results.see("end")
        self.results.configure(state="disabled")

    def _clear_log(self):
        self.results.configure(state="normal")
        self.results.delete("1.0", "end")
        self.results.configure(state="disabled")

    def ticked(self) -> list[Profile]:
        return [p for (v, _, p) in self.rows.values() if v.get()]

    def _update_state(self):
        sel = self.ticked()
        need_admin = (not self.admin) and any(p.all_user for p in sel)
        if need_admin:
            self.banner.grid()
        else:
            self.banner.grid_remove()
        ready = bool(sel) and bool(self.var_user.get().strip()) and bool(self.var_pass.get()) and not self.busy
        self.btn_apply.configure(state="normal" if ready else "disabled")
        if not self.busy:
            self.set_status(f"{len(sel)} of {len(self.rows)} profile(s) selected")

    # ---- list handling ---------------------------------------------------
    def refresh(self):
        if self.busy:
            return
        self.busy = True
        self.btn_apply.configure(state="disabled")
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
        for (_, w, _) in self.rows.values():
            w.destroy()
        self.rows.clear()
        self.lbl_empty.pack_forget()
        self.profiles = profiles
        for p in profiles:
            var = ctk.BooleanVar(value=p.key in keep)
            cb = ctk.CTkCheckBox(self.list_frame, text=p.label, variable=var,
                                 command=self._update_state)
            self.rows[p.key] = (var, cb, p)
        self._apply_filter()

    def _apply_filter(self):
        term = self.var_filter.get().strip().lower()
        for (_, w, _) in self.rows.values():
            w.pack_forget()
        self.lbl_empty.pack_forget()
        shown = 0
        for (_, w, p) in self.rows.values():
            if not term or term in p.name.lower():
                w.pack(anchor="w", padx=8, pady=4, fill="x")
                shown += 1
        if shown == 0:
            self.lbl_empty.configure(
                text="No VPN profiles found." if not self.rows else "No profiles match the filter.")
            self.lbl_empty.pack(pady=20)

    def _visible(self):
        term = self.var_filter.get().strip().lower()
        return [v for (v, _, p) in self.rows.values() if not term or term in p.name.lower()]

    def select_all(self):
        for v in self._visible():
            v.set(True)
        self._update_state()

    def select_none(self):
        for v in self._visible():
            v.set(False)
        self._update_state()

    # ---- apply -----------------------------------------------------------
    def restart_admin(self):
        if relaunch_as_admin():
            self.destroy()
        else:
            mb.showerror("Elevation failed", "Elevation was cancelled or failed. Staying in the current session.")

    def apply(self):
        sel = self.ticked()
        user = self.var_user.get().strip()
        pwd = self.var_pass.get()
        if not sel or not user or not pwd:
            return
        if len(user) > UNLEN or len(pwd) > PWLEN:
            mb.showerror("Input too long", f"Username and password must each be at most {UNLEN} characters.")
            return
        connected = [p.name for p in sel if p.status.lower() == "connected"]
        if connected:
            ok = mb.askokcancel(
                "Connected profiles",
                "These profiles are currently connected:\n\n  " + "\n  ".join(connected) +
                "\n\nNew credentials apply on the next connection, not to the active session. Continue?")
            if not ok:
                return

        self.busy = True
        self._clear_log()
        self.progress.set(0)
        self.btn_apply.configure(state="disabled")
        self.set_status("Applying…")
        admin = self.admin
        threading.Thread(target=self._worker, args=(sel, user, pwd, admin), daemon=True).start()

        # Clear the password everywhere on the GUI side.
        self.var_pass.set("")
        self.ent_pass.delete(0, "end")
        pwd = None  # noqa: F841  (drop this reference)

    def _worker(self, sel: list[Profile], user: str, pwd: str, admin: bool):
        cred = RASCREDENTIALSW()
        try:
            cred.dwSize = ctypes.sizeof(RASCREDENTIALSW)
            cred.dwMask = RASCM_UserName | RASCM_Password
            cred.szUserName = user
            cred.szPassword = pwd
            pwd = None
            total = len(sel)
            for i, p in enumerate(sel, 1):
                scope = "all-user" if p.all_user else "per-user"
                if p.all_user and not admin:
                    self.q.put(("result", p, False, "Skipped: all-user profile needs administrator rights."))
                else:
                    try:
                        pbk = ALLUSER_PBK if p.all_user else None
                        rc = _RasSetCredentialsW(pbk, p.name, ctypes.byref(cred), False)
                        if rc == 0:
                            note = " (applies on next connection)" if p.status.lower() == "connected" else ""
                            self.q.put(("result", p, True, f"Updated {scope} profile{note}."))
                        else:
                            self.q.put(("result", p, False, ras_error_text(rc)))
                    except Exception as e:
                        self.q.put(("result", p, False, f"Exception: {e}"))
                self.q.put(("progress", i / total))
        finally:
            ctypes.memset(ctypes.addressof(cred), 0, ctypes.sizeof(cred))
            user = None
            self.q.put(("done",))

    # ---- queue pump ------------------------------------------------------
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "profiles":
                    self.busy = False
                    self._populate(msg[1], msg[2])
                    self._update_state()
                    self.set_status(f"{len(msg[1])} profile(s) found")
                elif kind == "profiles_error":
                    self.busy = False
                    self._populate([], set())
                    self._log(msg[1], "err")
                    self._update_state()
                    self.set_status("Could not load profiles")
                elif kind == "progress":
                    self.progress.set(msg[1])
                elif kind == "result":
                    _, p, ok, text = msg
                    scope = "All users" if p.all_user else "Current user"
                    self._log(f"{'✔' if ok else '✘'}  {p.name} [{scope}]: {text}", "ok" if ok else "err")
                elif kind == "done":
                    self.busy = False
                    self._update_state()
                    self.set_status("Finished. Password cleared from the form.")
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    if os.name != "nt":
        print("This tool only runs on Windows.")
        sys.exit(1)
    App().mainloop()

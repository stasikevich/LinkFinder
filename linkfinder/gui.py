"""The window: a presentation layer over the same engine the CLI uses.

Nothing here talks to a device. Every network call goes through the existing
discovery functions on a worker thread, and results reach Tk only through a
queue drained on the main thread.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Dict, List, Optional

from . import __author__, __title__, __version__, store
from .cli import _local_label, render_csv
from .engine import Options, discover_many, expand_targets, load_config
from .model import TargetResult, results_to_json
from .sshx import Creds

MODES = ["auto", "ssh", "bmc", "esxi", "windows", "windows-local", "switch", "switch-cli"]

PALETTES = {
    "light": {
        "bg": "#f4f6f8", "surface": "#ffffff", "border": "#d7dde3",
        "text": "#16202a", "muted": "#64748b", "accent": "#1f6feb",
        "field": "#ffffff", "sel": "#dbeafe",
        "ok": "#1a7f37", "warn": "#bf6a02", "bad": "#c5221f", "off": "#6b7280",
    },
    "dark": {
        "bg": "#1b1f24", "surface": "#22272e", "border": "#363b42",
        "text": "#e6edf3", "muted": "#9aa4af", "accent": "#4493f8",
        "field": "#2a3139", "sel": "#2d4a73",
        "ok": "#3fb950", "warn": "#d29922", "bad": "#f85149", "off": "#8b949e",
    },
}

EVIDENCE = {
    "direct": "most likely a direct cable - each end hears only the other",
    "segment": "same layer-2 segment; a multicast frame does not prove a cable",
    "heard-by": "the far end hears this port's clock, so they share a segment",
    "heard": "frames were heard on this port",
}

STATUS_COLOUR = {
    "UP": "ok", "USED": "ok", "scanned": "ok", "high": "ok",
    "DEGRADED": "warn", "medium": "warn", "FREE_CANDIDATE": "warn",
    "DOWN": "bad", "unreachable": "bad", "error": "bad",
    "DISABLED": "off", "UNKNOWN": "off", "unscanned": "off", "low": "off",
    "out-of-scope": "off", "limit-reached": "off",
}

FRIENDLY = (
    ("rejected the credentials", "SSH authentication failed"),
    ("did not get us in", "SSH could not log in"),
    ("no snmp response", "SNMP timeout"),
    ("did not answer snmp", "SNMP timeout"),
    ("no redfish service", "Redfish unavailable or wrong credentials"),
    ("exposes no lldp", "LLDP data unavailable on this device"),
    ("no lldp/cdp received", "LLDP data unavailable on these ports"),
    ("nothing to talk to", "No reachable management service"),
    ("could not resolve a mac", "MAC address unknown for this target"),
    ("paramiko is not installed", "paramiko is missing - pip install paramiko"),
)


def friendly_error(text: str) -> str:
    low = (text or "").lower()
    for needle, message in FRIENDLY:
        if needle in low:
            return message
    return (text or "").split("\n")[0][:120]


def enable_dpi_awareness() -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def detect_system_theme() -> str:
    if sys.platform.startswith("win"):
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if value else "dark"
        except Exception:
            return "light"
    return "light"


class Theme:
    """clam restyled by hand: the stock Windows theme ignores colours."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.style = ttk.Style(root)
        self.name = "light"
        self.colours = PALETTES["light"]

    def apply(self, requested: str) -> None:
        self.name = detect_system_theme() if requested == "system" else requested
        self.colours = PALETTES.get(self.name, PALETTES["light"])
        c = self.colours
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.configure(background=c["bg"])
        self.style.configure(".", font=("Segoe UI", 9), background=c["bg"],
                             foreground=c["text"])
        self.style.configure("TFrame", background=c["bg"])
        self.style.configure("Surface.TFrame", background=c["surface"], relief="flat",
                             borderwidth=1, bordercolor=c["border"],
                             lightcolor=c["surface"], darkcolor=c["surface"])
        self.style.configure("TLabel", background=c["bg"], foreground=c["text"])
        self.style.configure("Surface.TLabel", background=c["surface"], foreground=c["text"])
        self.style.configure("Muted.TLabel", background=c["surface"], foreground=c["muted"])
        self.style.configure("MutedBg.TLabel", background=c["bg"], foreground=c["muted"])
        self.style.configure("Title.TLabel", background=c["surface"], foreground=c["muted"],
                             font=("Segoe UI", 8, "bold"))
        for key in ("ok", "warn", "bad", "off", "accent"):
            self.style.configure("%s.Surface.TLabel" % key, background=c["surface"],
                                 foreground=c[key])
            self.style.configure("%s.TLabel" % key, background=c["bg"], foreground=c[key])
        self.style.configure("TButton", background=c["surface"], foreground=c["text"],
                             bordercolor=c["border"], focuscolor=c["accent"],
                             padding=(12, 6), relief="flat")
        self.style.map("TButton", background=[("active", c["sel"]), ("disabled", c["bg"])],
                       foreground=[("disabled", c["muted"])])
        self.style.configure("Accent.TButton", background=c["accent"], foreground="#ffffff",
                             padding=(18, 7), relief="flat")
        self.style.map("Accent.TButton",
                       background=[("active", c["accent"]), ("disabled", c["border"])],
                       foreground=[("disabled", c["muted"])])
        for widget in ("TEntry", "TSpinbox", "TCombobox"):
            self.style.configure(widget, fieldbackground=c["field"], background=c["field"],
                                 foreground=c["text"], bordercolor=c["border"],
                                 arrowcolor=c["text"], insertcolor=c["text"], padding=4)
        self.style.map("TCombobox", fieldbackground=[("readonly", c["field"])])
        self.style.configure("TCheckbutton", background=c["bg"], foreground=c["text"],
                             focuscolor=c["bg"])
        self.style.map("TCheckbutton", background=[("active", c["bg"])])
        self.style.configure("Surface.TCheckbutton", background=c["surface"],
                             foreground=c["text"], focuscolor=c["surface"])
        self.style.configure("TNotebook", background=c["bg"], borderwidth=0,
                             tabmargins=0, bordercolor=c["border"],
                             lightcolor=c["bg"], darkcolor=c["bg"])
        self.style.configure("TNotebook.Tab", background=c["bg"], foreground=c["muted"],
                             padding=(16, 8), borderwidth=0)
        self.style.map("TNotebook.Tab", background=[("selected", c["surface"])],
                       foreground=[("selected", c["text"])])
        self.style.configure("Treeview", background=c["surface"], fieldbackground=c["surface"],
                             foreground=c["text"], bordercolor=c["border"], rowheight=24,
                             borderwidth=0)
        self.style.configure("Treeview.Heading", background=c["bg"], foreground=c["muted"],
                             relief="flat", font=("Segoe UI", 8, "bold"))
        self.style.map("Treeview", background=[("selected", c["sel"])],
                       foreground=[("selected", c["text"])])
        self.style.configure("TProgressbar", background=c["accent"], troughcolor=c["border"],
                             bordercolor=c["border"], lightcolor=c["accent"],
                             darkcolor=c["accent"])
        self.style.configure("TSeparator", background=c["border"])

    def style_for(self, token: str, on_surface: bool = True) -> str:
        key = STATUS_COLOUR.get(token, "off")
        return "%s.Surface.TLabel" % key if on_surface else "%s.TLabel" % key


def add_placeholder(entry: ttk.Entry, variable: tk.StringVar, text: str,
                    theme: "Theme") -> None:
    """Grey hint text in an empty field, gone the moment it is used.

    An empty box labelled Target does not say whether it wants an address, a
    name, or several of them. The example answers that before the first
    mistake rather than after it.
    """
    showing = {"on": False}

    def show() -> None:
        if not variable.get():
            showing["on"] = True
            variable.set(text)
            entry.configure(foreground=theme.colours["muted"])

    def hide(_event=None) -> None:
        if showing["on"] and variable.get() == text:
            variable.set("")
        showing["on"] = False
        entry.configure(foreground=theme.colours["text"])

    def restore(_event=None) -> None:
        if not variable.get():
            show()

    entry.bind("<FocusIn>", hide)
    entry.bind("<FocusOut>", restore)
    entry.placeholder = text
    entry.is_placeholder = lambda: showing["on"]
    show()


class Card(ttk.Frame):
    """A titled block of key/value rows - the readable alternative to a textbox."""

    def __init__(self, master, title: str, theme: Theme, width: int = 30):
        super().__init__(master, style="Surface.TFrame", padding=(14, 10, 14, 12))
        self.theme = theme
        self.value_width = width
        ttk.Label(self, text=title.upper(), style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 6))
        self.rows: Dict[str, ttk.Label] = {}
        self._next = 1

    def set(self, key: str, value: str, token: str = "") -> None:
        text = value if value not in (None, "") else "-"
        style = self.theme.style_for(token) if token else "Surface.TLabel"
        if key in self.rows:
            self.rows[key].configure(text=text, style=style)
            return
        ttk.Label(self, text=key, style="Muted.TLabel").grid(
            row=self._next, column=0, sticky=tk.W, padx=(0, 14), pady=1)
        label = ttk.Label(self, text=text, style=style, width=self.value_width, anchor=tk.W)
        label.grid(row=self._next, column=1, sticky=tk.W, pady=1)
        self.rows[key] = label
        self._next += 1

    def clear(self) -> None:
        for label in self.rows.values():
            label.configure(text="-", style="Surface.TLabel")


class StatusBar(ttk.Frame):
    def __init__(self, master, theme: Theme):
        super().__init__(master, padding=(14, 7))
        self.theme = theme
        self.dot = ttk.Label(self, text="\u25cf", style="off.TLabel")
        self.dot.pack(side=tk.LEFT, padx=(0, 7))
        self.phase = ttk.Label(self, text="Ready", style="TLabel")
        self.phase.pack(side=tk.LEFT)
        self.elapsed = ttk.Label(self, text="", style="MutedBg.TLabel")
        self.elapsed.pack(side=tk.RIGHT)
        self.counter = ttk.Label(self, text="", style="MutedBg.TLabel")
        self.counter.pack(side=tk.RIGHT, padx=(0, 18))
        self.bar = ttk.Progressbar(self, mode="indeterminate", length=170)
        self._running = False
        self._started: Optional[float] = None
        self._tick()

    def busy(self, phase: str) -> None:
        self._started = time.time()
        self.dot.configure(style="accent.TLabel")
        self.phase.configure(text=phase)
        self.counter.configure(text="")
        if not self._running:
            self.bar.pack(side=tk.RIGHT, padx=(0, 18))
            self.bar.start(12)
            self._running = True

    def set_phase(self, phase: str) -> None:
        self.phase.configure(text=phase)

    def set_counter(self, text: str) -> None:
        self.counter.configure(text=text)

    def done(self, phase: str, token: str = "UP") -> None:
        self._started = None
        if self._running:
            self.bar.stop()
            self.bar.pack_forget()
            self._running = False
        self.dot.configure(style=self.theme.style_for(token, on_surface=False))
        self.phase.configure(text=phase or "Done")

    def _tick(self) -> None:
        if self._started:
            seconds = int(time.time() - self._started)
            self.elapsed.configure(text="%02d:%02d" % (seconds // 60, seconds % 60))
        self.after(500, self._tick)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.results: List[TargetResult] = []
        self.health_report = None
        self.port_rows: Dict[str, object] = {}
        self.crawler = None
        self.last_topology_html = ""
        self.last_error = ""
        self.messages: "queue.Queue[str]" = queue.Queue()
        self.var: Dict[str, tk.Variable] = {}

        root.title("%s %s - by %s" % (__title__, __version__, __author__))
        root.geometry("1180x780")
        root.minsize(980, 620)
        self.theme = Theme(root)
        self.var["theme"] = tk.StringVar(value="system")
        self.theme.apply("system")

        self._build_topbar()
        self._build_advanced()
        self._build_modes()
        ttk.Separator(root).pack(side=tk.BOTTOM, fill=tk.X)
        self.status = StatusBar(root, self.theme)
        self.status.pack(side=tk.BOTTOM, fill=tk.X)
        self.root.after(120, self._drain)

    def _build_topbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(14, 12, 14, 8))
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="Target", style="MutedBg.TLabel").pack(side=tk.LEFT)
        self.var["targets"] = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.var["targets"], width=32)
        entry.pack(side=tk.LEFT, padx=(8, 16))
        entry.bind("<Return>", lambda _e: self.start())
        self.target_entry = entry
        add_placeholder(entry, self.var["targets"],
                        "10.0.0.5, 10.0.0.6  or  server1", self.theme)
        ttk.Label(bar, text="Login", style="MutedBg.TLabel").pack(side=tk.LEFT)
        self.var["user"] = tk.StringVar(value=str(self.cfg.get("user", "")))
        ttk.Entry(bar, textvariable=self.var["user"], width=14).pack(side=tk.LEFT, padx=(8, 12))
        ttk.Label(bar, text="Password", style="MutedBg.TLabel").pack(side=tk.LEFT)
        self.var["password"] = tk.StringVar(value=str(self.cfg.get("password", "")))
        password = ttk.Entry(bar, textvariable=self.var["password"], width=16,
                             show="\u2022")
        password.pack(side=tk.LEFT, padx=(8, 6))
        ttk.Label(bar, text="(SSH, or the BMC)", style="MutedBg.TLabel").pack(
            side=tk.LEFT, padx=(0, 16))
        self.btn_run = ttk.Button(bar, text="Scan", style="Accent.TButton", command=self.start)
        self.btn_run.pack(side=tk.LEFT)
        ttk.Button(bar, text="Advanced", command=self.toggle_advanced).pack(
            side=tk.LEFT, padx=8)
        ttk.Combobox(bar, textvariable=self.var["theme"], width=8, state="readonly",
                     values=["system", "light", "dark"]).pack(side=tk.RIGHT)
        ttk.Label(bar, text="Theme", style="MutedBg.TLabel").pack(side=tk.RIGHT, padx=(0, 8))
        self.var["theme"].trace_add("write", lambda *_a: self.retheme())

    def _build_advanced(self) -> None:
        self.advanced = ttk.Frame(self.root, style="Surface.TFrame", padding=(14, 10))
        self.advanced_open = False
        grid = self.advanced

        def field(label, key, row, col, default="", show=None, width=20):
            ttk.Label(grid, text=label, style="Muted.TLabel").grid(
                row=row, column=col, sticky=tk.W, padx=(0, 8), pady=4)
            self.var[key] = tk.StringVar(value=str(self.cfg.get(default, "")) if default else "")
            ttk.Entry(grid, textvariable=self.var[key], width=width, show=show).grid(
                row=row, column=col + 1, sticky=tk.W, padx=(0, 20), pady=4)

        field("SSH key file", "key", 0, 0, default="key")
        field("BMC user", "bmc_user", 0, 2, default="bmc_user")
        field("BMC password", "bmc_password", 0, 4, default="bmc_password", show="\u2022")
        field("Switches (SNMP)", "switches", 1, 0, width=24)
        self.var["switches"].set(", ".join(self.cfg.get("switches", []) or []))
        field("SNMP community", "community", 1, 2)
        self.var["community"].set(self.cfg.get("community", "public"))
        field("Interfaces", "ifaces", 1, 4)
        field("MAC (switch-side)", "mac", 2, 0)
        ttk.Label(grid, text="Mode", style="Muted.TLabel").grid(
            row=2, column=2, sticky=tk.W, padx=(0, 8))
        self.var["mode"] = tk.StringVar(value="auto")
        ttk.Combobox(grid, textvariable=self.var["mode"], values=MODES, width=16,
                     state="readonly").grid(row=2, column=3, sticky=tk.W, padx=(0, 20))
        ttk.Label(grid, text="Capture seconds", style="Muted.TLabel").grid(
            row=2, column=4, sticky=tk.W, padx=(0, 8))
        self.var["seconds"] = tk.StringVar(value="65")
        ttk.Spinbox(grid, from_=10, to=600, increment=5, width=6,
                    textvariable=self.var["seconds"]).grid(row=2, column=5, sticky=tk.W)
        ticks = ttk.Frame(grid, style="Surface.TFrame")
        ticks.grid(row=3, column=0, columnspan=6, sticky=tk.W, pady=(10, 0))
        self.mgmt_only = tk.BooleanVar(value=False)
        self.enable_lldp = tk.BooleanVar(value=True)
        self.learn = tk.BooleanVar(value=False)
        for text, var in (("management interface only", self.mgmt_only),
                          ("enable LLDP on a BMC if it is off", self.enable_lldp),
                          ("remember switches it finds (writes to disk)", self.learn)):
            ttk.Checkbutton(ticks, text=text, variable=var,
                            style="Surface.TCheckbutton").pack(side=tk.LEFT, padx=(0, 18))
        ttk.Button(ticks, text="Known switches", command=self.show_switches).pack(
            side=tk.LEFT, padx=(10, 6))
        ttk.Button(ticks, text="Forget", command=self.forget_switches).pack(side=tk.LEFT)

    def toggle_advanced(self) -> None:
        if self.advanced_open:
            self.advanced.pack_forget()
        else:
            self.advanced.pack(fill=tk.X, padx=14, pady=(0, 6), before=self.pages)
        self.advanced_open = not self.advanced_open

    def _build_modes(self) -> None:
        """Two places to be, not eight.

        Tabs were splitting one question across several of them: a port's
        neighbour on one, its health on another, its timing on a third. A
        device is one page now, the site is the other, and everything known
        about a port is on the row and in the panel under it.
        """
        bar = ttk.Frame(self.root, padding=(14, 0, 14, 6))
        bar.pack(fill=tk.X)
        self.mode = tk.StringVar(value="device")
        for value, label in (("device", "Device"), ("map", "Map"), ("log", "Log")):
            ttk.Radiobutton(bar, text=label, value=value, variable=self.mode,
                            style="Toolbutton", width=9,
                            command=self._show_mode).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(bar, text="Export...", command=self.export_csv).pack(side=tk.RIGHT)
        ttk.Button(bar, text="Compare...", command=self.compare_topologies).pack(
            side=tk.RIGHT, padx=6)

        self.pages = ttk.Frame(self.root)
        self.pages.pack(fill=tk.BOTH, expand=True, padx=14, pady=(2, 8))
        self.page = {}
        self._build_device_page()
        self._build_map_page()
        self._build_log_page()
        self._show_mode()

    def _show_mode(self) -> None:
        for name, frame in self.page.items():
            frame.pack_forget()
        self.page[self.mode.get()].pack(fill=tk.BOTH, expand=True)

    def _build_device_page(self) -> None:
        page = ttk.Frame(self.pages)
        self.page["device"] = page

        self.device_line = ttk.Label(page, text="Enter an address and press Scan.",
                                     style="MutedBg.TLabel")
        self.device_line.pack(anchor=tk.W, pady=(0, 2))
        self.scan_note = ttk.Label(page, text="", style="MutedBg.TLabel")
        self.scan_note.pack(anchor=tk.W, pady=(0, 8))

        picker = ttk.Frame(page)
        picker.pack(fill=tk.X, pady=(0, 6))
        self.device_label = ttk.Label(picker, text="Device", style="MutedBg.TLabel")
        self.device_pick = ttk.Combobox(picker, state="readonly", width=38, values=[])
        self.device_pick.bind("<<ComboboxSelected>>", self._show_device)

        controls = ttk.Frame(page)
        controls.pack(fill=tk.X, pady=(0, 8))
        self.port_health = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="Also read switch port counters",
                        variable=self.port_health,
                        command=self._sync_health_controls).pack(side=tk.LEFT)
        ttk.Label(controls, text="sample", style="MutedBg.TLabel").pack(
            side=tk.LEFT, padx=(18, 6))
        self.var["health_interval"] = tk.StringVar(value="3")
        spin = ttk.Spinbox(controls, from_=1, to=60, width=4,
                           textvariable=self.var["health_interval"])
        spin.pack(side=tk.LEFT)
        ttk.Label(controls, text="s", style="MutedBg.TLabel").pack(side=tk.LEFT, padx=(4, 18))
        ttk.Label(controls, text="expect", style="MutedBg.TLabel").pack(
            side=tk.LEFT, padx=(0, 6))
        self.var["expect_speed"] = tk.StringVar(value="")
        speed = ttk.Entry(controls, textvariable=self.var["expect_speed"], width=7)
        speed.pack(side=tk.LEFT)
        ttk.Label(controls, text="Mbps", style="MutedBg.TLabel").pack(side=tk.LEFT, padx=(4, 0))
        self.health_widgets = [spin, speed]

        self.tree = self._table(page, [
            ("port", "Port", 150), ("status", "Status", 110),
            ("to", "Connected to", 330), ("issues", "Issues", 260)], height=12)
        self.tree.bind("<<TreeviewSelect>>", self._show_neighbor_detail)
        self.neighbor_rows = {}

        self.explain = scrolledtext.ScrolledText(page, height=10, relief="flat",
                                                 wrap=tk.WORD)
        self.explain.pack(fill=tk.BOTH, expand=False, pady=(10, 0))
        self._explain_hint()
        self._sync_health_controls()

    def _explain_hint(self) -> None:
        """An empty panel should still say what it is for."""
        self.explain.delete(1.0, tk.END)
        self.explain.insert(
            tk.END, "Select a port above to see how its far end was established, "
                    "what the two sides were compared on, and why the verdict is "
                    "what it is.")

    def _build_map_page(self) -> None:
        page = ttk.Frame(self.pages)
        self.page["map"] = page
        row = ttk.Frame(page)
        row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row, text="max depth", style="MutedBg.TLabel").pack(side=tk.LEFT)
        self.max_depth = tk.StringVar(value="2")
        ttk.Spinbox(row, from_=1, to=8, width=4, textvariable=self.max_depth).pack(
            side=tk.LEFT, padx=(8, 18))
        ttk.Label(row, text="max devices", style="MutedBg.TLabel").pack(side=tk.LEFT)
        self.max_devices = tk.StringVar(value="25")
        ttk.Spinbox(row, from_=1, to=500, width=5,
                    textvariable=self.max_devices).pack(side=tk.LEFT, padx=(8, 18))
        ttk.Label(row, text="stay inside (CIDR)", style="MutedBg.TLabel").pack(side=tk.LEFT)
        self.scope = tk.StringVar(value="")
        scope = ttk.Entry(row, textvariable=self.scope, width=22)
        scope.pack(side=tk.LEFT, padx=(8, 18))
        self.auto_discover = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="keep going without asking",
                        variable=self.auto_discover).pack(side=tk.LEFT)

        buttons = ttk.Frame(page)
        buttons.pack(fill=tk.X, pady=(0, 8))
        self.btn_topo = ttk.Button(buttons, text="Generate", style="Accent.TButton",
                                   command=self.start_topology)
        self.btn_topo.pack(side=tk.LEFT)
        ttk.Button(buttons, text="Open", command=self.view_topology).pack(
            side=tk.LEFT, padx=8)
        ttk.Button(buttons, text="Merge...", command=self.merge_topologies).pack(
            side=tk.LEFT)
        self.btn_stop = ttk.Button(buttons, text="Stop", command=self.stop_topology,
                                   state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=8)
        self.map_hint = ttk.Label(
            page, style="MutedBg.TLabel",
            text="Walks outward from the address above and writes one HTML file "
                 "you can open from disk.")
        self.map_hint.pack(anchor=tk.W, pady=(0, 8))

        self.topo_tree = self._table(page, [
            ("depth", "Depth", 60), ("device", "Device", 170), ("kind", "Kind", 80),
            ("state", "State", 110), ("ip", "Management IP", 170),
            ("why", "Why", 320)], height=16)

    def _build_log_page(self) -> None:
        page = ttk.Frame(self.pages)
        self.page["log"] = page
        row = ttk.Frame(page)
        row.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(row, text="Save CSV", command=self.export_csv).pack(side=tk.LEFT)
        ttk.Button(row, text="Save JSON", command=self.export_json).pack(
            side=tk.LEFT, padx=8)
        ttk.Button(row, text="Error details", command=self.show_details).pack(
            side=tk.LEFT)
        ttk.Button(row, text="Remembered switches",
                   command=self.show_switches).pack(side=tk.LEFT, padx=8)
        self.log = scrolledtext.ScrolledText(page, height=24, relief="flat",
                                             wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True)

    def _table(self, parent, columns, height=16, pady=(0, 0)) -> ttk.Treeview:
        holder = ttk.Frame(parent)
        holder.pack(fill=tk.BOTH, expand=True, pady=pady)
        tree = ttk.Treeview(holder, columns=[c[0] for c in columns], show="headings",
                            height=height)
        for key, title, width in columns:
            tree.heading(key, text=title, anchor=tk.W)
            tree.column(key, width=width, anchor=tk.W)
        bar = ttk.Scrollbar(holder, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        for token in ("ok", "warn", "bad", "off"):
            tree.tag_configure(token, foreground=self.theme.colours[token])
        return tree

    def retheme(self) -> None:
        self.theme.apply(self.var["theme"].get())
        colours = self.theme.colours
        for tree in (self.tree, self.topo_tree):
            for token in ("ok", "warn", "bad", "off"):
                tree.tag_configure(token, foreground=colours[token])
        for widget in (self.log, self.explain):
            widget.configure(background=colours["surface"], foreground=colours["text"],
                             insertbackground=colours["text"])

    def _sync_health_controls(self) -> None:
        """The boxes stay dead until the read is asked for, so nothing is read."""
        state = tk.NORMAL if self.port_health.get() else tk.DISABLED
        for widget in self.health_widgets:
            widget.configure(state=state)

    def say(self, message: str) -> None:
        self.messages.put(message)

    def _drain(self) -> None:
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            self.log.insert(tk.END, message + "\n")
            self.log.see(tk.END)
            phase = self._phase_of(message)
            if phase:
                self.status.set_phase(phase)
        self.root.after(120, self._drain)

    @staticmethod
    def _phase_of(message: str) -> str:
        text = message.strip()
        low = text.lower()
        if low.startswith("[depth"):
            return "Scanning %s..." % text.split("]")[-1].strip()
        for needle, phase in (
                ("auto-selected", "Choosing an access path..."),
                ("lldpctl", "Reading LLDP..."),
                ("networkctl", "Reading LLDP..."),
                ("lldptool", "Reading LLDP..."),
                ("capturing with", "Capturing LLDP/CDP..."),
                ("entering toolbox", "Capturing LLDP/CDP..."),
                ("raw-socket sniffer", "Capturing LLDP/CDP..."),
                ("snmp on", "Reading SNMP..."),
                ("switch side", "Asking the switch..."),
                ("looks like", "Reading the switch CLI..."),
                ("redfish", "Reading the BMC..."),
                ("sampling counters", "Sampling port counters..."),
                ("merged", "Merging...")):
            if needle in low:
                return phase
        return ""

    def build_options(self) -> Options:
        def get(key):
            return self.var[key].get().strip() if key in self.var else ""

        ifaces = [x for x in get("ifaces").replace(",", " ").split() if x] or None
        switches = [x for x in get("switches").replace(",", " ").split() if x]
        try:
            speed = int(get("expect_speed") or 0)
        except ValueError:
            speed = 0
        try:
            interval = float(get("health_interval") or 3)
        except ValueError:
            interval = 3.0
        return Options(
            mode=get("mode") or "auto",
            ssh=Creds(user=get("user"), password=get("password"), key_file=get("key")),
            bmc_user=get("bmc_user"), bmc_password=get("bmc_password"),
            enable_lldp=self.enable_lldp.get(),
            community=get("community") or "public", switches=switches,
            seconds=int(get("seconds") or 65), ifaces=ifaces,
            mgmt_only=self.mgmt_only.get(), mac=get("mac"), learn=self.learn.get(),
            port_health=self.port_health.get(), port_health_interval=interval,
            expect_speed=speed)

    def targets_text(self) -> str:
        """What the user typed, which is nothing when the hint is still showing."""
        entry = getattr(self, "target_entry", None)
        if entry is not None and getattr(entry, "is_placeholder", lambda: False)():
            return ""
        return self.var["targets"].get()

    def start(self) -> None:
        targets = expand_targets([self.targets_text()])
        if not targets:
            messagebox.showwarning("Nothing to do", "Enter a target address first.")
            return
        self.last_error = ""
        self.btn_run.configure(state=tk.DISABLED, text="Scanning")
        self.status.busy("Scanning %s..." % targets[0])
        opts = self.build_options()
        thread = threading.Thread(target=self._work, args=(targets, opts))
        thread.daemon = True
        thread.start()

    def _work(self, targets, opts) -> None:
        results, report = [], None
        try:
            results = discover_many(targets, opts, log=self.say)
            if opts.ptp:
                self._switch_ptp(targets, opts, results)
            if opts.port_health:
                report = self._collect_health(targets, opts, results)
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self.say(self.last_error)
        self.root.after(0, lambda: self._finish(results, report))

    def _switch_ptp(self, targets, opts, results=None) -> None:
        """What each switch says about timing, into the log where it belongs."""
        from .port_health import switches_worth_asking
        from .probe_snmp import build_switches
        from .ptp_snmp import read_switch_ptp_safely

        specs = switches_worth_asking(results=results, explicit=opts.switches,
                                      targets=[])
        for switch in build_switches(specs, opts.community):
            if not switch.identify():
                continue
            self.say("  " + read_switch_ptp_safely(switch).summary())

    def _collect_health(self, targets, opts, results=None):
        """Ask every switch the scan found, not the server we were pointed at.

        Reporting only the first one that answered hid the rest: a handful of
        servers usually face two or three switches, and the one that answers
        first is not necessarily the one being asked about.
        """
        from .port_health import collect_safely, switches_worth_asking
        from .probe_snmp import build_switches

        hosts = switches_worth_asking(results=results, explicit=opts.switches,
                                      targets=targets)
        if not hosts:
            self.say("  port health: nothing we found publishes a usable management "
                     "address, so there is no switch to ask")
            return []
        reports = []
        for switch in build_switches(hosts, opts.community):
            if not switch.identify():
                self.say("  port health: %s did not answer SNMP" % switch.host)
                continue
            self.say("  reading port health on %s (%s)" % (switch.name, switch.host))
            reports.append(collect_safely(switch,
                                          interval=opts.port_health_interval,
                                          expect_speed=opts.expect_speed,
                                          log=self.say))
        if not reports:
            self.say("  port health: none of %s answered SNMP - check the community "
                     "under Advanced" % ", ".join(hosts))
        return reports

    def _finish(self, results: List[TargetResult], report=None) -> None:
        self.results = results
        self.health_report = report
        self.port_rows = {}
        stamp = time.strftime("%H:%M:%S")
        self.scan_note.configure(
            text="Scanned %d device(s) at %s: %s"
            % (len(results), stamp, ", ".join(r.target for r in results)))
        labels = ["%s  (%s)" % (r.target, r.device_name or r.kind or "?")
                  for r in results]
        self.device_pick.configure(values=labels)
        if len(results) > 1:
            self.device_label.pack(side=tk.LEFT, padx=(0, 8))
            self.device_pick.pack(side=tk.LEFT)
            self.device_pick.current(0)
        else:
            self.device_label.pack_forget()
            self.device_pick.pack_forget()
        self._show_device()
        self.btn_run.configure(state=tk.NORMAL, text="Scan")
        resolved = sum(1 for r in results for n in r.neighbors if n.resolved)
        if resolved:
            self.status.done("Done - %d link(s) resolved" % resolved, "UP")
        else:
            first = next((r for r in results if r.error), None)
            if first is not None:
                self.last_error = first.error
            self.status.done(friendly_error(self.last_error) or "Nothing resolved",
                             "DOWN")
        self.mode.set("device")
        self._show_mode()

    def _health_for(self, nb):
        """The switch port this cable lands on, if port health was read."""
        for report in self._health_reports():
            if report.host and nb.mgmt_ip and report.host != nb.mgmt_ip:
                continue
            for port in report.ports:
                if nb.best_port and nb.best_port in (port.name, port.descr):
                    return port
        return None

    def _health_reports(self):
        report = self.health_report
        if report is None:
            return []
        return list(report) if isinstance(report, (list, tuple)) else [report]

    @staticmethod
    def _connected_to(nb) -> str:
        if nb.resolved:
            return "%s  %s" % (nb.switch or "?", nb.best_port or "?")
        peer = nb.extra.get("ptp_peer")
        if peer:
            how = {"direct": "direct cable, PTP",
                   "segment": "same segment, PTP"}.get(
                       nb.extra.get("ptp_peer_evidence", ""), "heard over PTP")
            return "%s  (%s)" % (peer, how)
        return "unknown"

    def _issues_for(self, nb, port) -> str:
        from .linkcheck import problems

        bits = []
        for layer in problems(nb.extra.get("checks") or {}):
            verdict = (nb.extra["checks"][layer] or {}).get("verdict", "")
            bits.append("%s %s" % (layer.upper(), verdict))
        if port is not None and port.status in ("DEGRADED", "DOWN"):
            bits.append("port %s" % port.status)
        if not nb.resolved and not nb.extra.get("ptp_peer") and nb.local_speed:
            bits.append("no LLDP")
        return ", ".join(bits)

    @staticmethod
    def _status_of(nb, port) -> str:
        if port is not None and port.oper:
            state = port.oper.upper()
        else:
            state = "UP" if nb.local_speed else "-"
        speed = (nb.local_speed or "").replace("Mb/s", "")
        if speed.isdigit():
            value = int(speed)
            speed = "%dG" % (value // 1000) if value >= 1000 else "%dM" % value
        return ("%s  %s" % (state, speed)).strip()

    def _present(self, results: List[TargetResult]) -> None:
        self._show_device()

    def _show_device(self, _event=None) -> None:
        """One device, one table, one row per cable."""
        self.tree.delete(*self.tree.get_children())
        self.neighbor_rows = {}
        self.explain.delete(1.0, tk.END)
        results = self.results or []
        if not results:
            self.device_line.configure(text="Enter an address and press Scan.")
            self._explain_hint()
            return
        index = self.device_pick.current() if len(results) > 1 else 0
        result = results[max(0, min(index, len(results) - 1))]
        parts = [result.device_name or result.target, result.kind or "unknown"]
        if result.device_descr:
            parts.append(result.device_descr[:46])
        self.device_line.configure(text="  -  ".join(parts))
        if result.error and not result.neighbors:
            self.explain.insert(tk.END, friendly_error(result.error) or result.error)

        for nb in result.neighbors:
            port = self._health_for(nb)
            issues = self._issues_for(nb, port)
            tag = "bad" if issues else ("ok" if nb.resolved else "off")
            if issues and not nb.resolved:
                tag = "warn"
            item = self.tree.insert("", tk.END, tags=(tag,), values=(
                _local_label(nb), self._status_of(nb, port),
                self._connected_to(nb), issues or "-"))
            self.neighbor_rows[item] = (nb, port)

    def _show_neighbor_detail(self, _event=None) -> None:
        """Everything known about one cable, the answer first."""
        from .linkcheck import describe

        selection = self.tree.selection()
        self.explain.delete(1.0, tk.END)
        if not selection:
            return
        found = self.neighbor_rows.get(selection[0])
        if not found:
            self._explain_hint()
            return
        nb, port = found
        about = [bit for bit in (nb.local_mac, nb.local_speed,
                                 "MTU %s" % nb.local_mtu if nb.local_mtu else "")
                 if bit]
        lines = ["%s on %s%s" % (_local_label(nb), nb.host or "-",
                                 "   (%s)" % ", ".join(about) if about else "")]
        if nb.resolved:
            lines.append("Switch: %s - %s   [%s/%s]"
                         % (nb.switch or "-", nb.best_port or "-",
                            nb.proto or "-", nb.source or "-"))
        if nb.extra.get("ptp_peer"):
            lines.append("Far end: %s" % nb.extra["ptp_peer"])
            lines.append("Evidence: %s" % EVIDENCE.get(
                nb.extra.get("ptp_peer_evidence", ""), "heard on this port"))
        if nb.extra.get("ptp_grandmaster"):
            lines.append("Grandmaster: %s" % nb.extra["ptp_grandmaster"])
        if nb.note:
            lines.append(nb.note)

        checks = nb.extra.get("checks") or {}
        if checks:
            lines.append("")
            lines.extend(describe(checks))

        ptp = nb.extra.get("ptp") or {}
        if ptp:
            bits = [str(ptp[k]) for k in ("role", "state", "transport") if ptp.get(k)]
            if ptp.get("domain", -1) >= 0:
                bits.append("domain %s" % ptp["domain"])
            if ptp.get("offset_ns") is not None:
                bits.append("offset %+d ns" % ptp["offset_ns"])
            lines.append("")
            lines.append("PTP: %s" % ", ".join(bits))

        if port is not None:
            lines.append("")
            lines.append("Switch port %s: %s / %s, %s"
                         % (port.label, port.status, port.usage,
                            port.speed or "no speed"))
            for key in ("rx_errors", "tx_errors", "rx_discards", "tx_discards"):
                if key in port.counters:
                    lines.append("  %-12s %d (+%d during the sample)"
                                 % (key, port.counters[key],
                                    port.deltas.get(key, 0)))
            for reason in port.reasons:
                lines.append("  %s" % reason)
        self.explain.insert(tk.END, (chr(10)).join(lines))

    def _present_health(self, reports) -> None:
        """Port health is a layer on the device table, not a page of its own."""
        self._show_device()

    def _show_port_detail(self, _event=None) -> None:
        self._show_neighbor_detail()

    def _confirm_on_main_thread(self, depth, candidates) -> bool:
        answer = {"yes": False}
        done = threading.Event()

        def ask():
            shown = "\n".join("   %-16s %s" % (ip, label) for ip, label in candidates[:15])
            if len(candidates) > 15:
                shown += "\n   ... and %d more" % (len(candidates) - 15)
            answer["yes"] = messagebox.askyesno(
                "Topology discovery",
                "Found %d new network device(s) at depth %d:\n\n%s\n\n"
                "Add and scan discovered neighbours?" % (len(candidates), depth, shown))
            done.set()

        self.root.after(0, ask)
        done.wait()
        return answer["yes"]

    def _topology_progress(self, done: int, queued: int) -> None:
        self.root.after(0, lambda: self.status.set_counter(
            "device %d, queued %d" % (done, queued)))

    def start_topology(self) -> None:
        seeds = expand_targets([self.targets_text()])
        if not seeds:
            messagebox.showwarning("No seed", "Put the seed address in Target.")
            return
        path = filedialog.asksaveasfilename(
            title="Save the topology page as", defaultextension=".html",
            initialfile="site.topology.html", filetypes=[("HTML", "*.html")])
        if not path:
            return
        self.mode.set("map")
        self._show_mode()
        self.topo_tree.delete(*self.topo_tree.get_children())
        self.btn_topo.configure(state=tk.DISABLED, text="Discovering")
        self.btn_run.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.status.busy("Discovering topology from %s..." % seeds[0])
        opts = self.build_options()
        thread = threading.Thread(target=self._topology_work, args=(seeds[0], opts, path))
        thread.daemon = True
        thread.start()

    def _topology_work(self, seed, opts, html_path) -> None:
        from .crawl import Crawler, Limits
        from .render_topology import write_html
        topology = None
        try:
            limits = Limits(max_depth=int(self.max_depth.get() or 2),
                            max_devices=int(self.max_devices.get() or 25),
                            scope=[s for s in self.scope.get().replace(",", " ").split() if s])
            confirm = ((lambda _d, _c: True) if self.auto_discover.get()
                       else self._confirm_on_main_thread)
            self.crawler = Crawler(opts, limits=limits, confirm=confirm, log=self.say,
                                   progress=self._topology_progress, workers=4)
            topology = self.crawler.run(seed)
            json_path = os.path.splitext(html_path)[0] + ".json"
            with open(json_path, "w", encoding="utf-8") as handle:
                handle.write(topology.to_json())
            write_html(topology.to_dict(), html_path)
            self.say(topology.summary())
            if topology.stopped_because:
                self.say("stopped because: %s" % topology.stopped_because)
            self.say("written to %s and %s" % (json_path, html_path))
            self.last_topology_html = html_path
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self.say(self.last_error)
            html_path = ""
        self.root.after(0, lambda: self._topology_done(topology, html_path))

    def _topology_done(self, topology, html_path) -> None:
        self.btn_topo.configure(state=tk.NORMAL, text="Generate")
        self.btn_run.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        self.crawler = None
        if topology is not None:
            for node in sorted(topology.nodes.values(),
                               key=lambda n: (99 if n.depth is None else n.depth, n.label)):
                self.topo_tree.insert(
                    "", tk.END, tags=(STATUS_COLOUR.get(node.state, "off"),),
                    values=("-" if node.depth is None else node.depth, node.label,
                            node.kind, node.state, ", ".join(node.mgmt_ips) or "-",
                            node.chassis_id or "-",
                            friendly_error(node.error) or node.error or "-"))
            self.status.done(topology.summary(), "UP" if topology.nodes else "DOWN")
        else:
            self.status.done(friendly_error(self.last_error) or "Topology failed", "DOWN")
        if html_path and messagebox.askyesno("Topology ready", "Open it in the browser?"):
            webbrowser.open("file:///" + html_path.replace("\\", "/"))

    def stop_topology(self) -> None:
        if self.crawler is not None:
            self.crawler.stop()
            self.say("stop requested - finishing the devices already in flight")
            self.btn_stop.configure(state=tk.DISABLED)
            self.status.set_phase("Stopping...")

    def merge_topologies(self) -> None:
        from .render_topology import write_html
        from .topology import merge_files

        paths = filedialog.askopenfilenames(title="Topology JSON files to merge",
                                            filetypes=[("JSON", "*.json")])
        if not paths or len(paths) < 2:
            if paths:
                messagebox.showinfo("Merge", "Pick at least two files.")
            return
        out = filedialog.asksaveasfilename(title="Save the merged map as",
                                           defaultextension=".html",
                                           initialfile="site.topology.html",
                                           filetypes=[("HTML", "*.html")])
        if not out:
            return
        try:
            combined = merge_files(list(paths))
            json_path = os.path.splitext(out)[0] + ".json"
            with open(json_path, "w", encoding="utf-8") as handle:
                handle.write(combined.to_json())
            write_html(combined.to_dict(), out)
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            messagebox.showerror("Merge failed", friendly_error(self.last_error))
            return
        self.last_topology_html = out
        self.say("merged %d file(s): %s" % (len(paths), combined.summary()))
        self.status.done("Merged %d file(s)" % len(paths), "UP")
        if messagebox.askyesno("Merged", "Open the combined map?"):
            webbrowser.open("file:///" + out.replace("\\", "/"))

    def compare_topologies(self) -> None:
        """Two saved scans, and what moved between them."""
        from .diff import compare_files, render as render_diff

        before = filedialog.askopenfilename(title="The earlier topology JSON",
                                            filetypes=[("JSON", "*.json")])
        if not before:
            return
        after = filedialog.askopenfilename(title="The later topology JSON",
                                           filetypes=[("JSON", "*.json")])
        if not after:
            return
        try:
            report = compare_files(before, after)
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            messagebox.showerror("Compare failed", friendly_error(self.last_error)
                                 or self.last_error)
            return
        self.mode.set("log")
        self._show_mode()
        self.say("")
        for line in render_diff(report).splitlines():
            self.say(line)
        self.status.done(report.summary(), "UP" if report.quiet else "DEGRADED")

    def view_topology(self) -> None:
        path = self.last_topology_html
        if not path or not os.path.exists(path):
            path = filedialog.askopenfilename(title="Open a topology page",
                                              filetypes=[("HTML", "*.html")])
        if path:
            self.last_topology_html = path
            webbrowser.open("file:///" + path.replace("\\", "/"))

    def show_switches(self) -> None:
        self.mode.set("log")
        self._show_mode()
        for line in store.describe().splitlines():
            self.say(line)

    def forget_switches(self) -> None:
        count = len(store.load())
        if not count:
            messagebox.showinfo("Nothing to forget", "No switches are remembered yet.")
            return
        if messagebox.askyesno("Forget switches",
                               "Forget all %d remembered switch(es)?" % count):
            self.say("forgot %d remembered switch(es)" % store.forget())

    def show_details(self) -> None:
        if not self.last_error:
            messagebox.showinfo("No errors", "Nothing has failed in this session.")
            return
        messagebox.showerror("Technical details", self.last_error)

    def export_csv(self) -> None:
        if not self.results:
            messagebox.showinfo("Nothing yet", "Run a scan first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")])
        if path:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(render_csv(self.results))
            self.say("CSV written to %s" % path)

    def export_json(self) -> None:
        if not self.results:
            messagebox.showinfo("Nothing yet", "Run a scan first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                            filetypes=[("JSON", "*.json")])
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(results_to_json(self.results))
            self.say("JSON written to %s" % path)


def run_gui() -> None:
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except tk.TclError:
        pass
    app = App(root)
    app.retheme()
    root.mainloop()

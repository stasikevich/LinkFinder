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
import tempfile
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Dict, List, Optional

from . import store
from .branding import icon_path, signature


def wear_the_icon(root) -> None:
    """Put the packaged icon on the window, not only on the file.

    PyInstaller's --icon dresses the executable in Explorer and stops there.
    The window and the taskbar ask the toolkit, which hands over Tk's own
    default - so a branded build looked right on disk and wrong once opened.
    An open build carries no icon file and keeps the default, and a toolkit
    that will not take an .ico must not bring the window down over it.
    """
    path = icon_path()
    if not path:
        return
    try:
        root.iconbitmap(path)
    except Exception:
        pass
from .cli import _local_label, render_csv
from .diagnose import (_short, failed_hosts, friendly_error,
                       run_verdict, shared_login_refused)
from .engine import (Options, config_path, discover_many, load_config,
                     per_host_from_config, per_host_to_config, save_config,
                     targets_with_logins)
from .model import TargetResult, results_to_json
from .sshx import Creds
from .userinput import (check_walk, cost_note, decimal_number, settled,
                        whole_number)

MODES = ["auto", "ssh", "bmc", "esxi", "windows", "windows-local",
         "switch", "switch-cli", "pdu"]

# How much of the Advanced panel is on screen at once. The settings add up
# to about 1500 pixels, and a laptop screen is 768: without a ceiling the
# bottom half of them is unreachable. Enough to work in, and the rest is a
# scroll away.
ADVANCED_HEIGHT = 300

PALETTES = {
    "light": {
        "bg": "#f4f6f8", "surface": "#ffffff", "border": "#d7dde3",
        "text": "#16202a", "muted": "#64748b", "accent": "#1f6feb",
        "field": "#ffffff", "sel": "#dbeafe", "btn": "#eef1f5",
        "ok": "#1a7f37", "warn": "#bf6a02", "bad": "#c5221f", "off": "#6b7280",
    },
    "dark": {
        "bg": "#1b1f24", "surface": "#22272e", "border": "#363b42",
        "text": "#e6edf3", "muted": "#9aa4af", "accent": "#4493f8",
        "field": "#2a3139", "sel": "#2d4a73", "btn": "#2f363d",
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

class Tooltip:
    """A one-line explanation that appears where the pointer already is.

    Buttons carry verbs, not reasons, and a second copy of the documentation
    inside the window would drift away from the real one. A tooltip says the
    reason at the moment it is wanted and keeps no copy of anything.
    """

    def __init__(self, widget, text: str, delay: int = 450):
        self.widget, self.text, self.delay = widget, text, delay
        self.window = None
        self.timer = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self.timer = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self.timer is not None:
            try:
                self.widget.after_cancel(self.timer)
            except Exception:
                pass
            self.timer = None

    def _show(self):
        if self.window is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except Exception:
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry("+%d+%d" % (x, y))
        tk.Label(self.window, text=self.text, justify=tk.LEFT,
                 background="#1a202c", foreground="#ffffff",
                 relief=tk.SOLID, borderwidth=0, padx=8, pady=5,
                 font=("Segoe UI", 9), wraplength=360).pack()

    def _hide(self, _event=None):
        self._cancel()
        if self.window is not None:
            self.window.destroy()
            self.window = None


def tip(widget, text: str):
    """Attach an explanation and hand the widget back, so it can be packed."""
    Tooltip(widget, text)
    return widget


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
        # a flat button the same colour as the panel behind it reads as a
        # label, so every button gets its own fill and a one pixel edge
        self.style.configure("TButton", background=c["btn"], foreground=c["text"],
                             bordercolor=c["border"], focuscolor=c["accent"],
                             lightcolor=c["btn"], darkcolor=c["btn"],
                             padding=(12, 6), relief="solid", borderwidth=1)
        self.style.map("TButton", background=[("active", c["sel"]), ("disabled", c["bg"])],
                       foreground=[("disabled", c["muted"])])
        self.style.configure("Small.TButton", background=c["btn"], foreground=c["muted"],
                             bordercolor=c["border"], lightcolor=c["btn"],
                             darkcolor=c["btn"], padding=(6, 2), relief="solid",
                             borderwidth=1)
        self.style.map("Small.TButton",
                       background=[("active", c["sel"]), ("disabled", c["bg"])],
                       foreground=[("active", c["bad"]), ("disabled", c["muted"])])
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
    # the hint counts as showing only while the field still holds it, so a
    # value put there by anything other than typing is still read back
    entry.is_placeholder = lambda: showing["on"] and variable.get() == text
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
        self.results_at = 0.0
        self.health_report = None
        self.port_rows: Dict[str, object] = {}
        self.crawler = None
        self.last_topology_html = ""
        # The map object itself, so the browser can be answered about it.
        self.last_topology = None
        self.map_server = None
        self.last_error = ""
        self.messages: "queue.Queue[str]" = queue.Queue()
        self.var: Dict[str, tk.Variable] = {}

        root.title(signature())
        wear_the_icon(root)
        # Fit the screen rather than assume one. Asking for 1180x780 on a
        # 1366x768 laptop already loses the bottom row, and opening the
        # settings used to ask for 1550 - half of them off the screen with
        # no way to reach them. The ceiling keeps that impossible however
        # much the panel grows later.
        screen_w = max(640, root.winfo_screenwidth())
        screen_h = max(480, root.winfo_screenheight())
        roomy_w, roomy_h = min(1180, screen_w - 40), min(780, screen_h - 90)
        root.geometry("%dx%d" % (roomy_w, roomy_h))
        root.minsize(min(980, roomy_w), min(620, roomy_h))
        root.maxsize(screen_w, screen_h - 60)
        self.theme = Theme(root)
        self.var["theme"] = tk.StringVar(value="system")
        self.theme.apply("system")

        self._build_menu()
        self._build_topbar()
        self._build_advanced()
        self._build_modes()
        ttk.Separator(root).pack(side=tk.BOTTOM, fill=tk.X)
        self.status = StatusBar(root, self.theme)
        self.status.pack(side=tk.BOTTOM, fill=tk.X)
        self.root.after(120, self._drain)
        self._seal_old_passwords()
        self._say_where()
        self._show_netbox_state()
        for key in ("netbox_url", "netbox_token"):
            if key in self.var:
                self.var[key].trace_add(
                    "write", lambda *_a: self._show_netbox_state())

    def _later(self, call) -> None:
        """Hand work back to the window from a worker thread.

        Closing the window while a scan or a NetBox comparison is still
        running leaves the thread holding a window that is already gone.
        There is nothing left to show by then, so it stops quietly rather
        than dying with a traceback into a console a GUI build has not got.
        """
        try:
            self.root.after(0, call)
        except (RuntimeError, tk.TclError):
            pass

    def _say_where(self) -> None:
        """Say whether these marks are this machine's or the team's."""
        from . import shared

        from . import setups as setup_store

        try:
            self.say(shared.say_where())
            moved = setup_store.move_to_shared()
            if moved:
                self.say("moved %d mark(s) made on this machine into the "
                         "shared folder" % len(moved))
        except Exception:
            pass

    def _seal_old_passwords(self) -> None:
        """Move a password left in the defaults file into the system store."""
        from . import vault
        from .engine import seal_stored_passwords, seal_stored_token

        try:
            sealed, why = seal_stored_passwords()
        except Exception:
            return
        if seal_stored_token():
            self.say("moved the saved NetBox token into %s" % vault.describe())
        if sealed:
            self.say("moved the saved password for %s into %s"
                     % (", ".join(sealed), vault.describe()))
        elif why:
            self.say("warning: %s" % why)

    def _build_menu(self) -> None:
        """Where a Windows program keeps its file actions and its help.

        A "?" button in a corner is an afterthought made visible. A menu is
        where people already look, and it takes the file actions out of the
        toolbars, which are about the work in front of you.
        """
        menu = tk.Menu(self.root)

        files = tk.Menu(menu, tearoff=0)
        files.add_command(label="Open a saved map...",
                          command=self.open_another_map)
        files.add_command(label="Save...", command=self.save_something)
        files.add_command(label="Open the folder with maps && reports",
                          command=self.open_files_folder)
        files.add_separator()
        files.add_command(label="Save a host-list template...",
                          command=self.write_list_example)
        files.add_command(label="Save a NetBox report example...",
                          command=self.write_netbox_example)
        # The comparison is written beside every map already. This is for
        # handing it to somebody: it is the one table people are sent to find.
        files.add_command(label="Save the NetBox-vs-network report...",
                          command=self.save_netbox_comparison)
        files.add_separator()
        files.add_command(label="Merge maps...", command=self.merge_topologies)
        files.add_command(label="Compare two maps...",
                          command=self.compare_topologies)
        files.add_separator()
        files.add_command(label="Exit", command=self.root.destroy)
        menu.add_cascade(label="File", menu=files)

        view = tk.Menu(menu, tearoff=0)
        view.add_command(label="Advanced settings",
                         command=self.toggle_advanced)
        look = tk.Menu(view, tearoff=0)
        for name in ("system", "light", "dark"):
            look.add_radiobutton(label=name, value=name,
                                 variable=self.var["theme"])
        view.add_cascade(label="Appearance", menu=look)
        menu.add_cascade(label="View", menu=view)

        help_menu = tk.Menu(menu, tearoff=0)
        help_menu.add_command(label="Guide", accelerator="F1",
                              command=self.open_help)
        help_menu.add_separator()
        help_menu.add_command(label="About", command=self.about)
        menu.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menu)
        self.root.bind("<F1>", lambda _e: self.open_help())

    def about(self) -> None:
        from . import __copyright__

        messagebox.showinfo(
            "About",
            signature() + chr(10) * 2
            + "Reads switches, servers, BMCs and handsets to answer which "
              "port every link is in." + chr(10) * 2
            + "It only reads. Nothing is written to NetBox, ever."
            + chr(10) * 2 + __copyright__)

    def _build_topbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(14, 12, 14, 8))
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="Target", style="MutedBg.TLabel").pack(side=tk.LEFT)
        self.var["targets"] = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.var["targets"], width=32)
        entry.pack(side=tk.LEFT, padx=(8, 4))
        entry.bind("<Return>", lambda _e: self.start())
        self.target_entry = entry
        add_placeholder(entry, self.var["targets"],
                        "10.0.0.5, 10.0.0.0/24  or  root@10.0.0.5", self.theme)
        tip(ttk.Button(bar, text="List...", width=7, command=self.load_per_host),
            "A file with one address per line. A line like root@10.0.0.5 carries "
            "that host's own login, and a [name] line puts everything under it in "
            "a setup.").pack(side=tk.LEFT, padx=(0, 16))
        ttk.Label(bar, text="Login", style="MutedBg.TLabel").pack(side=tk.LEFT)
        self.var["user"] = tk.StringVar(value=str(self.cfg.get("user", "")))
        ttk.Entry(bar, textvariable=self.var["user"], width=14).pack(side=tk.LEFT, padx=(8, 12))
        ttk.Label(bar, text="Password", style="MutedBg.TLabel").pack(side=tk.LEFT)
        self.var["password"] = tk.StringVar(value=str(self.cfg.get("password", "")))
        password = ttk.Entry(bar, textvariable=self.var["password"], width=16,
                             show="\u2022")
        password.pack(side=tk.LEFT, padx=(8, 6))
        self._secret(password)
        # A password typed blind costs a login attempt, and network gear
        # drops the session after two or three of them. Seeing what you
        # typed is cheaper than finding out from the far end.
        self.show_secrets = tk.BooleanVar(value=False)
        tip(ttk.Checkbutton(bar, text="show", variable=self.show_secrets,
                            style="Toolbutton", command=self.reveal_secrets),
            "Show every password in this window as plain text - here, under "
            "Advanced, and in the per-host table.").pack(side=tk.LEFT,
                                                         padx=(0, 10))
        ttk.Label(bar, text="(SSH, or the BMC)", style="MutedBg.TLabel").pack(
            side=tk.LEFT, padx=(0, 16))
        # Advanced is reached from View, and from nowhere else. It was a
        # button here as well, which is one idea wearing two hats in a bar
        # that is meant to be the three fields you fill in.
        self.var["theme"].trace_add("write", lambda *_a: self.retheme())

    def _build_advanced(self) -> None:
        """Four named groups, not one heap.

        Everything settable used to sit in a single unlabelled block: how to
        log in, where else to look, how hard to look, and NetBox. A setting
        is findable when the panel says what it is for.
        """
        self.advanced = ttk.Frame(self.root, style="Surface.TFrame",
                                  padding=(14, 10))
        self.advanced_open = False

        # Measured at 1550 pixels tall with everything open, on a laptop
        # screen 768 high: the lower half of the settings was simply off
        # the bottom, with no way to reach it and nothing saying so. They
        # scroll now, so the panel can keep growing and the window cannot.
        holder = ttk.Frame(self.advanced)
        holder.pack(fill=tk.BOTH, expand=True)
        self.advanced_canvas = tk.Canvas(holder, height=ADVANCED_HEIGHT,
                                         highlightthickness=0, bd=0)
        rail = ttk.Scrollbar(holder, orient=tk.VERTICAL,
                             command=self.advanced_canvas.yview)
        inside = ttk.Frame(self.advanced_canvas, style="Surface.TFrame")
        window = self.advanced_canvas.create_window((0, 0), window=inside,
                                                    anchor=tk.NW)
        self.advanced_canvas.configure(yscrollcommand=rail.set)
        self.advanced_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rail.pack(side=tk.RIGHT, fill=tk.Y)

        def resized(_event=None):
            self.advanced_canvas.configure(
                scrollregion=self.advanced_canvas.bbox("all"))

        def widened(event):
            self.advanced_canvas.itemconfigure(window, width=event.width)

        inside.bind("<Configure>", resized)
        self.advanced_canvas.bind("<Configure>", widened)
        # The wheel only scrolls what the pointer is over, so it does not
        # steal the wheel from the device list behind it.
        for widget in (self.advanced_canvas, inside):
            widget.bind("<Enter>", lambda _e: self.advanced_canvas.bind_all(
                "<MouseWheel>", self._wheel_advanced))
            widget.bind("<Leave>", lambda _e: self.advanced_canvas.unbind_all(
                "<MouseWheel>"))

        def group(title):
            box = ttk.LabelFrame(inside, text=title, padding=(10, 6))
            box.pack(fill=tk.X, pady=(0, 8))
            return box

        def field(parent, label, key, row, col, default="", show=None,
                  width=20):
            ttk.Label(parent, text=label, style="Muted.TLabel").grid(
                row=row, column=col, sticky=tk.W, padx=(0, 8), pady=4)
            self.var[key] = tk.StringVar(
                value=str(self.cfg.get(default, "")) if default else "")
            box = ttk.Entry(parent, textvariable=self.var[key], width=width,
                            show=show)
            box.grid(row=row, column=col + 1, sticky=tk.W, padx=(0, 20), pady=4)
            if show:
                self._secret(box)

        # --- 1. how to get in -------------------------------------------
        inside = group("Getting in")
        creds = ttk.Frame(inside)
        creds.pack(fill=tk.X)
        field(creds, "SSH key file", "key", 0, 0, default="key")
        field(creds, "SSH port", "ssh_port", 0, 2, width=8)
        self.var["ssh_port"].set(str(self.cfg.get("ssh_port", "") or 22))
        field(creds, "BMC user", "bmc_user", 1, 0, default="bmc_user")
        field(creds, "BMC password", "bmc_password", 1, 2,
              default="bmc_password", show="\u2022")
        self._build_per_host(inside, row=None)

        # --- 2. the inventory -------------------------------------------
        inventory = group("NetBox")
        netbox_box = ttk.Frame(inventory)
        netbox_box.pack(fill=tk.X)
        saved = (self.cfg.get("netbox") or {}) if isinstance(
            self.cfg.get("netbox"), dict) else {}
        ttk.Label(netbox_box, text="address", style="Muted.TLabel").pack(
            side=tk.LEFT, padx=(0, 8))
        self.var["netbox_url"] = tk.StringVar(value="")
        ttk.Entry(netbox_box, textvariable=self.var["netbox_url"],
                  width=34).pack(side=tk.LEFT, padx=(0, 18))
        self.var["netbox_url"].set(saved.get("url", ""))
        ttk.Label(netbox_box, text="token", style="Muted.TLabel").pack(
            side=tk.LEFT, padx=(0, 8))
        self.var["netbox_token"] = tk.StringVar(value="")
        token_box = ttk.Entry(netbox_box, textvariable=self.var["netbox_token"],
                              width=26, show="\u2022")
        token_box.pack(side=tk.LEFT, padx=(0, 12))
        # A NetBox token is nbt_<key>.<secret> - long, pasted, and easy to
        # cut short by one character. It hides with the rest.
        self._secret(token_box)
        from . import vault as _vault

        self.var["netbox_token"].set(
            _vault.unprotect(str(saved.get("token", "") or "")))
        tip(ttk.Button(netbox_box, text="Test", command=self.test_netbox),
            "Ask NetBox for one device, say plainly what came back, and keep "
            "the address and token if it worked.").pack(side=tk.LEFT,
                                                        padx=(0, 6))
        tip(ttk.Button(netbox_box, text="Forget token",
                       command=self.forget_netbox),
            "Remove the saved NetBox address and token from this machine."
            ).pack(side=tk.LEFT)
        # This used to live on the map, inside a panel that has since gone.
        # Without it there is no way at all to say which NetBox tags are
        # benches, and every comparison reports "no setup tags named yet".
        tip(ttk.Button(netbox_box, text="Setup tags...",
                       command=self.choose_setup_tags),
            "This estate has hundreds of tags and most of them are teams, "
            "not benches. Say which ones name a setup.").pack(
            side=tk.LEFT, padx=(6, 0))
        ttk.Label(inventory, style="Muted.TLabel", wraplength=860,
                  justify=tk.LEFT,
                  text="A NetBox 4.5 token is nbt_<key>.<secret> - the key is "
                       "on the token's page, the secret is shown once when it "
                       "is made. An older NetBox issues a 40-character "
                       "hexadecimal token, which is also accepted. A working "
                       "Test saves both, sealed by this machine's own store."
                  ).pack(anchor=tk.W, pady=(6, 0))

        # --- 3. where else to look --------------------------------------
        elsewhere = group("Where else to look")
        wide = ttk.Frame(elsewhere)
        wide.pack(fill=tk.X)
        field(wide, "Switches (SNMP)", "switches", 0, 0, width=24)
        self.var["switches"].set(", ".join(self.cfg.get("switches", []) or []))
        field(wide, "SNMP community", "community", 0, 2)
        self.var["community"].set(self.cfg.get("community", "public"))
        field(wide, "Interfaces", "ifaces", 1, 0)
        field(wide, "MAC (switch-side)", "mac", 1, 2)
        kept = ttk.Frame(elsewhere)
        kept.pack(fill=tk.X, pady=(6, 0))
        self.learn = tk.BooleanVar(value=False)
        tip(ttk.Checkbutton(kept, text="remember switches it finds",
                            variable=self.learn),
            "Keep found switches so later runs ask them without you typing "
            "them.").pack(side=tk.LEFT, padx=(0, 18))
        tip(ttk.Button(kept, text="Known switches", command=self.show_switches),
            "Switches remembered from earlier runs, asked again "
            "automatically.").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(kept, text="Forget switches",
                   command=self.forget_switches).pack(
            side=tk.LEFT)

        # --- 4. how hard to look ----------------------------------------
        effort = group("How hard to look")
        how = ttk.Frame(effort)
        how.pack(fill=tk.X)
        ttk.Label(how, text="Mode", style="Muted.TLabel").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 8))
        self.var["mode"] = tk.StringVar(value="auto")
        ttk.Combobox(how, textvariable=self.var["mode"], values=MODES,
                     width=16, state="readonly").grid(row=0, column=1,
                                                      sticky=tk.W,
                                                      padx=(0, 20))
        ttk.Label(how, text="Capture seconds", style="Muted.TLabel").grid(
            row=0, column=2, sticky=tk.W, padx=(0, 8))
        self.var["seconds"] = tk.StringVar(value="90")
        ttk.Spinbox(how, from_=10, to=600, increment=5, width=6,
                    textvariable=self.var["seconds"]).grid(row=0, column=3,
                                                           sticky=tk.W)
        ticks = ttk.Frame(effort)
        ticks.pack(fill=tk.X, pady=(8, 0))
        self.mgmt_only = tk.BooleanVar(value=False)
        self.enable_lldp = tk.BooleanVar(value=True)
        self.ask_adb = tk.BooleanVar(value=False)
        self.read_serial = tk.BooleanVar(value=True)
        for text, var, hint in (
                ("management interface only", self.mgmt_only,
                 "Look only at the interface the address you typed lives on."),
                ("enable LLDP on a BMC if it is off", self.enable_lldp,
                 "The one setting this tool will change, and only on a BMC. "
                 "Without it a BMC reports no neighbour at all."),
                ("name handsets over adb", self.ask_adb,
                 "USB devices are always read. Tick this to also ask adb on "
                 "the host for each handset's model - it may start an adb "
                 "server there, which is why it is a choice."),
                ("ask the radio console ports who they are", self.read_serial,
                 "On by default. Each port that looks like a radio console is "
                 "opened at 115200 and asked its name; it logs in with the "
                 "console logins the build carries if the port asks. A port "
                 "that answers is drawn as a radio cabled to this host. A rack "
                 "server is skipped. Untick to only list the ports and never "
                 "open one.")):
            tip(ttk.Checkbutton(ticks, text=text, variable=var,
                                style="Surface.TCheckbutton"), hint).pack(
                side=tk.LEFT, padx=(0, 18))

        # What stops a walk. These are guard rails somebody sets once, not a
        # choice to make on every run, so they sit here rather than beside
        # the button - and the page says which ones are in force.
        walk = ttk.Frame(effort)
        walk.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(walk, text="Map Network stops after",
                  style="Muted.TLabel").pack(side=tk.LEFT, padx=(0, 8))
        self.max_depth = tk.StringVar(value="0")
        ttk.Spinbox(walk, from_=0, to=8, width=4,
                    textvariable=self.max_depth).pack(side=tk.LEFT)
        ttk.Label(walk, text="hops or", style="Muted.TLabel").pack(
            side=tk.LEFT, padx=(6, 8))
        self.max_devices = tk.StringVar(value="25")
        ttk.Spinbox(walk, from_=1, to=500, width=5,
                    textvariable=self.max_devices).pack(side=tk.LEFT)
        ttk.Label(walk, text="devices", style="Muted.TLabel").pack(
            side=tk.LEFT, padx=(6, 18))
        ttk.Label(walk, text="stay inside (CIDR)", style="Muted.TLabel").pack(
            side=tk.LEFT, padx=(0, 8))
        self.scope = tk.StringVar(value="")
        ttk.Entry(walk, textvariable=self.scope, width=20).pack(
            side=tk.LEFT, padx=(0, 18))
        self.auto_discover = tk.BooleanVar(value=False)
        tip(ttk.Checkbutton(walk, text="keep going without asking",
                            variable=self.auto_discover,
                            style="Surface.TCheckbutton"),
            "Carry on past the device limit instead of stopping to ask."
            ).pack(side=tk.LEFT)

        counters = ttk.Frame(effort)
        counters.pack(fill=tk.X, pady=(6, 0))
        self.port_health = tk.BooleanVar(value=False)
        ttk.Checkbutton(counters, text="read switch port counters",
                        variable=self.port_health,
                        command=self._sync_health_controls).pack(side=tk.LEFT)
        ttk.Label(counters, text="sample", style="MutedBg.TLabel").pack(
            side=tk.LEFT, padx=(18, 6))
        self.var["health_interval"] = tk.StringVar(value="3")
        spin = ttk.Spinbox(counters, from_=1, to=60, width=4,
                           textvariable=self.var["health_interval"])
        spin.pack(side=tk.LEFT)
        ttk.Label(counters, text="s", style="MutedBg.TLabel").pack(
            side=tk.LEFT, padx=(4, 18))
        ttk.Label(counters, text="expect", style="MutedBg.TLabel").pack(
            side=tk.LEFT, padx=(0, 6))
        self.var["expect_speed"] = tk.StringVar(value="")
        speed = ttk.Entry(counters, textvariable=self.var["expect_speed"],
                          width=7)
        speed.pack(side=tk.LEFT)
        ttk.Label(counters, text="Mbps", style="MutedBg.TLabel").pack(
            side=tk.LEFT, padx=(4, 0))
        self.health_widgets = [spin, speed]
        self._sync_health_controls()

    def _build_per_host(self, grid, row=None) -> None:
        """Hosts that do not share the login in the top bar.

        The engine has always accepted a login per host; until now the only
        way to give it one was to type root@10.0.0.5 into Target, which the
        window never said. This is that same facility, visible.
        """
        box = ttk.Frame(grid, style="Surface.TFrame")
        # A group that lays its children out with pack cannot take a child
        # placed with grid: Tk refuses to mix the two in one parent.
        if row is None:
            box.pack(fill=tk.X, pady=(12, 0))
        else:
            box.grid(row=row, column=0, columnspan=6, sticky=tk.W + tk.E,
                     pady=(12, 0))

        head = ttk.Frame(box, style="Surface.TFrame")
        head.pack(fill=tk.X)
        ttk.Label(head, text="Per-host logins", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Button(head, text="Clear", command=self.clear_per_host).pack(
            side=tk.LEFT, padx=(12, 0))

        self.cred_rows_frame = ttk.Frame(box, style="Surface.TFrame")
        self.cred_rows_frame.pack(fill=tk.X, pady=(6, 0))
        self.cred_rows = []
        for column, text in enumerate(("Host", "User", "Password", "Port")):
            ttk.Label(self.cred_rows_frame, text=text, style="Muted.TLabel").grid(
                row=0, column=column, sticky=tk.W, padx=(0, 8), pady=(0, 2))

        foot = ttk.Frame(box, style="Surface.TFrame")
        foot.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(foot, text="+ add host", style="Small.TButton",
                   command=lambda: self.add_cred_row()).pack(side=tk.LEFT)
        self.remember_logins = tk.BooleanVar(value=False)
        ttk.Checkbutton(foot, text="remember these logins",
                        variable=self.remember_logins,
                        style="Surface.TCheckbutton").pack(side=tk.LEFT, padx=(16, 0))
        ttk.Button(foot, text="Forget logins",
                   command=self.forget_per_host).pack(side=tk.LEFT, padx=(10, 0))

        ttk.Label(box, style="Muted.TLabel",
                  text="A host listed here is asked as well, with its own login - "
                       "it does not have to be in Target too. Everything else uses "
                       "the Login and Password in the top bar, and a login typed "
                       "straight into Target as root@10.0.0.5 wins over both.").pack(
            anchor=tk.W, pady=(6, 0))

        saved = per_host_from_config(self.cfg)
        for host in sorted(saved):
            self.add_cred_row(host, saved[host].user, saved[host].password,
                              getattr(saved[host], "port", 22) or 22)
        if saved:
            self.remember_logins.set(True)
        else:
            self.add_cred_row()

        self._build_extra_logins(box)

    def _build_extra_logins(self, parent) -> None:
        """Logins to fall back on when you do not know which host wants which.

        These are not tied to a host: they are tried in order, and only after a
        device has actually rejected the login before them. Network gear drops
        the session after two or three failures, which is why the list is meant
        to stay short and why nothing here is tried speculatively.
        """
        ttk.Separator(parent, orient="horizontal").pack(fill=tk.X, pady=(12, 10))

        head = ttk.Frame(parent, style="Surface.TFrame")
        head.pack(fill=tk.X)
        ttk.Label(head, text="Extra logins to try",
                  style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Label(head, text="in order, and only after a login was rejected",
                  style="Muted.TLabel").pack(side=tk.LEFT, padx=(10, 0))

        self.extra_rows_frame = ttk.Frame(parent, style="Surface.TFrame")
        self.extra_rows_frame.pack(fill=tk.X, pady=(6, 0))
        self.extra_rows = []
        for column, text in enumerate(("User", "Password")):
            ttk.Label(self.extra_rows_frame, text=text, style="Muted.TLabel").grid(
                row=0, column=column, sticky=tk.W, padx=(0, 8), pady=(0, 2))

        foot = ttk.Frame(parent, style="Surface.TFrame")
        foot.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(foot, text="+ add login", style="Small.TButton",
                   command=lambda: self.add_extra_row()).pack(side=tk.LEFT)
        ttk.Label(foot, style="Muted.TLabel",
                  text="each one is another failed attempt on a device that "
                       "refuses it - keep the list short").pack(side=tk.LEFT,
                                                                padx=(14, 0))

        for entry in (self.cfg.get("creds") or []):
            if isinstance(entry, dict) and entry.get("user"):
                self.add_extra_row(entry.get("user", ""), entry.get("password", ""))
        if not self.extra_rows:
            self.add_extra_row()

    def add_extra_row(self, user: str = "", password: str = "") -> None:
        held = {"user": tk.StringVar(value=user),
                "password": tk.StringVar(value=password)}
        held["widgets"] = [
            ttk.Entry(self.extra_rows_frame, textvariable=held["user"], width=22),
            ttk.Entry(self.extra_rows_frame, textvariable=held["password"], width=18,
                      show="\u2022"),
            ttk.Button(self.extra_rows_frame, text="\u2715", width=3,
                       style="Small.TButton",
                       command=lambda: self.remove_extra_row(held))]
        self._secret(held["widgets"][1])
        self.extra_rows.append(held)
        self._place_rows(self.extra_rows)

    def remove_extra_row(self, held) -> None:
        self._drop_row(self.extra_rows, held)
        if not self.extra_rows:
            self.add_extra_row()
        else:
            self._place_rows(self.extra_rows)

    def extra_creds(self):
        """The fallback logins, in the order they are written on screen."""
        out = []
        for held in self.extra_rows:
            user = held["user"].get().strip()
            if user:
                out.append(Creds(user=user, password=held["password"].get()))
        return out

    def add_cred_row(self, host: str = "", user: str = "", password: str = "",
                     port: str = "") -> None:
        held = {"host": tk.StringVar(value=host), "user": tk.StringVar(value=user),
                "password": tk.StringVar(value=password),
                "port": tk.StringVar(value=str(port or ""))}
        held["widgets"] = [
            ttk.Entry(self.cred_rows_frame, textvariable=held["host"], width=22),
            ttk.Entry(self.cred_rows_frame, textvariable=held["user"], width=16),
            ttk.Entry(self.cred_rows_frame, textvariable=held["password"], width=18,
                      show="\u2022"),
            # Blank means "whatever SSH port is set above", so a table full of
            # ordinary hosts needs nothing typed in it.
            ttk.Entry(self.cred_rows_frame, textvariable=held["port"], width=6),
            ttk.Button(self.cred_rows_frame, text="\u2715", width=3,
                       style="Small.TButton",
                       command=lambda: self.remove_cred_row(held))]
        self._secret(held["widgets"][2])
        self.cred_rows.append(held)
        self._place_rows(self.cred_rows)

    def _place_rows(self, rows) -> None:
        """Re-grid after an add or a remove; row 0 belongs to the headers."""
        for index, held in enumerate(rows):
            for column, widget in enumerate(held["widgets"]):
                widget.grid(row=index + 1, column=column, sticky=tk.W,
                            padx=(0, 8), pady=2)

    @staticmethod
    def _drop_row(rows, held) -> None:
        for widget in held["widgets"]:
            widget.destroy()
        if held in rows:
            rows.remove(held)

    def remove_cred_row(self, held) -> None:
        self._drop_row(self.cred_rows, held)
        if not self.cred_rows:
            self.add_cred_row()
        else:
            self._place_rows(self.cred_rows)

    def clear_per_host(self) -> None:
        for held in list(self.cred_rows):
            self._drop_row(self.cred_rows, held)
        self.add_cred_row()

    def ssh_port(self) -> int:
        """The port everything uses unless a row says otherwise."""
        try:
            port = int((self.var["ssh_port"].get() or "22").strip())
        except (KeyError, ValueError):
            return 22
        return port if 0 < port < 65536 else 22

    def per_host_table(self):
        """What the table says, as the engine wants it: host -> Creds.

        A host listed twice used to be settled silently by whichever row
        came last, so a password that was being looked straight at could
        have no effect at all. The last row still wins - that is the only
        sane rule when a table is edited top to bottom - but it is said.
        """
        out = {}
        shared = self.ssh_port()
        twice = []
        for held in self.cred_rows:
            host = held["host"].get().strip()
            user = held["user"].get().strip()
            password = held["password"].get()
            try:
                port = int((held["port"].get() or "").strip() or shared)
            except ValueError:
                port = shared
            if host and (user or password or port != 22):
                if host in out:
                    twice.append(host)
                out[host] = Creds(user=user, password=password, port=port)
        for host in dict.fromkeys(twice):
            self.say("%s is in the per-host logins more than once - the "
                     "lowest row is the one being used" % host)
        return out

    # What a host list looks like, as a file somebody can start from. The
    # format was only ever described in prose, so everybody wrote their
    # first one wrong and found out from a list of complaints.
    LIST_EXAMPLE = """# ----------------------------------------------------------------------
# A host list for LinkFinder.  Load it with the  List...  button.
#
# Rules:
#   - one host per line (an IP address or a name)
#   - a blank line is fine; anything after a #  is a comment, ignored
# ----------------------------------------------------------------------

# 1) Just an address.
#    It is scanned with the Login and Password you typed in the top bar.
198.51.100.11
server-05.lab.example

# 2) An address that needs its OWN login.
#    Write it as   user:password@host   and it uses that instead.
root:MyPassw0rd@198.51.100.10

# 3) A whole subnet in one line.
#    Written as address/mask, it is opened out into every host inside it -
#    you do not have to list them one by one.
198.51.100.16/30

# 4) Group hosts into a "setup" (a bench, a rack, a project).
#    A  [name]  heading puts every host below it into that setup,
#    until the next heading.  The setup shows up as a filter on the map.
[bench-a]
198.51.100.20
root:MyPassw0rd@198.51.100.21

[bench-b]
198.51.100.32/29
"""

    def write_list_example(self) -> None:
        """Save a host list with the format written into it as comments."""
        path = filedialog.asksaveasfilename(
            title="Save a host-list template", defaultextension=".txt",
            initialfile="host-list-template.txt",
            filetypes=[("Text", "*.txt")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as out:
                out.write(self.LIST_EXAMPLE)
        except OSError as exc:
            messagebox.showerror("Could not write it", str(exc))
            return
        self.say("a host-list template is in %s - edit it and load it back "
                 "with List..." % path)

    def write_netbox_example(self) -> None:
        """Save a sample of the NetBox-vs-network report, so its shape is clear.

        The real report needs NetBox set up; this one is invented, so somebody
        can see what the comparison looks like before they wire NetBox in - a
        row per disagreement, what is on the wire beside what NetBox holds.
        """
        import csv as _csv
        import io as _io

        from . import findings as findings_csv

        sample = [
            {"status": "mismatch", "label": "server-12", "field": "rack",
             "actual": "seen on sw-core-1 Gi1/14", "documented": "Rack B07",
             "detail": "the cable is not where NetBox says the device is",
             "evidence": "LLDP from both ends", "confidence": "high",
             "confirm": "", "netbox": "dcim/devices/341"},
            {"status": "only here", "label": "nuc-bench-3", "field": "device",
             "actual": "scanned, up", "documented": "",
             "detail": "on the network, no record in NetBox",
             "evidence": "SSH + serial G2VCHB4", "confidence": "high",
             "confirm": "IT to create the record", "netbox": ""},
            {"status": "mismatch", "label": "sw-access-2", "field": "IPMI",
             "actual": "10.0.5.9 (read from the host)", "documented": "10.0.5.90",
             "detail": "NetBox holds a different controller address",
             "evidence": "ipmitool lan print", "confidence": "high",
             "confirm": "", "netbox": "ipam/ip-addresses/88"},
        ]
        text = findings_csv.as_csv(sample, findings_csv.NETBOX_COLUMNS) \
            if hasattr(findings_csv, "as_csv") else ""
        if not text:
            out = _io.StringIO()
            writer = _csv.writer(out)
            writer.writerow([h for h, _k in findings_csv.NETBOX_COLUMNS])
            for row in sample:
                writer.writerow([row.get(k, "") for _h, k in
                                 findings_csv.NETBOX_COLUMNS])
            text = out.getvalue()
        path = filedialog.asksaveasfilename(
            title="Save a NetBox report example", defaultextension=".csv",
            initialfile="netbox-report-example.csv",
            filetypes=[("A table for a spreadsheet", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        except OSError as exc:
            messagebox.showerror("Could not write it", str(exc))
            return
        self.say("a NetBox report example is in %s - this is invented, to show "
                 "the shape; the real one needs NetBox set up under Advanced"
                 % path)

    def load_per_host(self) -> None:
        """Read a host list file, the same format the command line takes."""
        path = filedialog.askopenfilename(
            title="Host list", filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        from .targets import complain, setups as setups_in_file, split

        try:
            hosts, logins, bad = split([], path)
            self.target_setups = setups_in_file([], path)
        except OSError as exc:
            messagebox.showerror("Could not read it", str(exc))
            return
        for line in complain(bad):
            self.say(line)
        self.clear_per_host()
        first = True
        for host in hosts:
            user, password = logins.get(host, ("", ""))
            if user or password:
                if first:
                    self.clear_per_host()
                    self.cred_rows[0]["host"].set(host)
                    self.cred_rows[0]["user"].set(user)
                    self.cred_rows[0]["password"].set(password)
                    first = False
                else:
                    self.add_cred_row(host, user, password)
        carried = len(self.per_host_table())
        self.var["targets"].set(", ".join(hosts))
        self.say("loaded %d host(s) from %s, %d of them with their own login"
                 % (len(hosts), path, carried))
        if self.target_setups:
            named = sorted(set(self.target_setups.values()))
            self.say("the file names %d setup(s): %s" % (len(named), ", ".join(named)))
        if not carried:
            messagebox.showinfo(
                "Loaded", "%d host(s) went into Target. None of them carried a login "
                "of its own - write them as root@10.0.0.5 in the file to fill this "
                "table." % len(hosts))

    def forget_per_host(self) -> None:
        if not messagebox.askyesno(
                "Forget saved logins",
                "Remove the saved per-host logins from %s?\n\nThe table stays on "
                "screen; only the file is cleared." % config_path()):
            return
        try:
            save_config({"per_host": {}, "creds": []})
        except OSError as exc:
            messagebox.showerror("Could not write it", str(exc))
            return
        self.remember_logins.set(False)
        self.say("per-host logins removed from %s" % config_path())

    def _remember_per_host(self, table) -> None:
        """Write the table to the defaults file, but only when asked to."""
        if not getattr(self, "remember_logins", None) or not self.remember_logins.get():
            return
        extras = [{"user": c.user, "password": c.password}
                  for c in self.extra_creds()]
        try:
            lost = []
            keep = {"per_host": per_host_to_config(table, lost=lost),
                    "creds": extras, "ssh_port": self.ssh_port()}
            url, token = "", ""
            if "netbox_url" in self.var:
                url = self.var["netbox_url"].get().strip()
                token = self.var["netbox_token"].get().strip()
            if url or token:
                from . import vault

                # Sealed like a password: it opens the whole inventory.
                sealed = vault.protect(token) if token else ""
                if token and not sealed:
                    self.say("warning: the NetBox token was NOT saved - %s"
                             % vault.why_not())
                keep["netbox"] = {"url": url, "token": sealed or ""}
            path = save_config(keep)
        except OSError as exc:
            self.say("could not save the logins: %s" % exc)
            return
        if lost:
            from . import vault

            self.say("warning: the password for %s was NOT saved - %s"
                     % (", ".join(lost), vault.why_not()))
        self.say("remembered %d per-host login(s) and %d extra(s) in %s"
                 % (len(table), len(extras), path))
        if keep.get("netbox", {}).get("token"):
            from .engine import exposed_by_git
            risk = exposed_by_git(path)
            if risk:
                self.say("warning: " + risk)
                messagebox.showwarning(
                    "Saved where git can see it",
                    risk + ".\n\nThat file now holds a NetBox token. Either "
                    "add it to .gitignore, or clear the token box and use the "
                    "LINKFINDER_NETBOX_TOKEN environment variable instead.")

    def toggle_advanced(self) -> None:
        if self.advanced_open:
            self.advanced.pack_forget()
        else:
            self.advanced.pack(fill=tk.X, padx=14, pady=(0, 6), before=self.pages)
        self.advanced_open = not self.advanced_open

    def _build_modes(self) -> None:
        """One place to work, and the log.

        Device and Map were two pages for one question. They took the same
        targets and the same logins and differed only in how far to walk,
        yet each had its own button to start it - and the Device page had to
        explain the other one ("or open Map and press Generate"), which is a
        layout telling on itself. Both buttons live together now, and the
        answer lands on one page: the devices, and under them the ports of
        whichever device is selected.
        """
        bar = ttk.Frame(self.root, padding=(14, 0, 14, 6))
        bar.pack(fill=tk.X)
        self.mode = tk.StringVar(value="work")
        for value, label in (("work", "Devices"), ("log", "Log")):
            ttk.Radiobutton(bar, text=label, value=value, variable=self.mode,
                            style="Toolbutton", width=9,
                            command=self._show_mode).pack(side=tk.LEFT, padx=(0, 4))

        self.pages = ttk.Frame(self.root)
        self.pages.pack(fill=tk.BOTH, expand=True, padx=14, pady=(2, 8))
        self.page = {}
        self._build_work_page()
        self._build_log_page()
        self._show_mode()

    def _show_mode(self) -> None:
        for name, frame in self.page.items():
            frame.pack_forget()
        self.page[self.mode.get()].pack(fill=tk.BOTH, expand=True)
        if self.mode.get() != "log":
            self._refresh_map_hint()

    def _refresh_map_hint(self) -> None:
        """Say what each of the two buttons will actually do.

        The limits that stop a walk live in Advanced, where they belong, so
        this names the ones in force rather than leaving somebody to find
        out how far Map goes by pressing it.
        """
        base = ("Scan asks the address(es) in Target and nothing else.  "
                "Map Network walks outward from them and draws it - up to "
                "%s hops or %s devices, changeable under Advanced."
                % (self._walk_depth(), self._walk_devices()))
        try:
            seeds, _logins = targets_with_logins([self.targets_text()])
            known = self._reusable(seeds)
        except Exception:
            known = []
        if known:
            base += ("  Starting from the scan of %d device(s) taken %s - "
                     "only what is new gets asked."
                     % (len(known), self._age_phrase()))
        self.map_hint.configure(text=base)

    def _walk_depth(self) -> str:
        return (self.max_depth.get().strip()
                if getattr(self, "max_depth", None) is not None else "0") or "0"

    def _walk_devices(self) -> str:
        return (self.max_devices.get().strip()
                if getattr(self, "max_devices", None) is not None else "25") or "25"

    def _build_work_page(self) -> None:
        """Two actions, one list of devices, and the ports of the one selected.

        Scanning and drawing the map are the two things this tool is for, so
        they are two buttons. They were briefly one button with a "how far"
        choice beside it; that made the map look like a setting rather than
        the thing you came to produce, and it is not what anybody reached
        for. The pages behind them stayed merged, which was the part worth
        keeping.
        """
        page = ttk.Frame(self.pages)
        self.page["work"] = page
        self.page["device"] = page      # old names, same page
        self.page["map"] = page

        doing = ttk.Frame(page)
        doing.pack(fill=tk.X, pady=(0, 6))
        self.btn_run = tip(ttk.Button(doing, text="Scan", style="Accent.TButton",
                                      command=self.start),
                           "Ask the addresses in Target which switch port every "
                           "link is in. Read-only: no configuration is changed.")
        self.btn_run.pack(side=tk.LEFT)
        self.btn_topo = tip(ttk.Button(doing, text="Map Network",
                                       command=self.start_topology),
                            "Walk outward from the addresses in Target, draw "
                            "the map and open it. The limits that stop the "
                            "walk are under Advanced.")
        self.btn_topo.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_stop = tip(ttk.Button(doing, text="Stop",
                                       command=self.stop_topology,
                                       state=tk.DISABLED),
                            "Finish the devices already being asked, then stop.")
        self.btn_stop.pack(side=tk.LEFT, padx=(12, 0))

        tip(ttk.Button(doing, text="Open Map", command=self.open_live_map),
            "Open the map already drawn, without walking anything again. "
            "Setups, what NetBox does not know and what moved since last "
            "time are all done there, on the picture.").pack(side=tk.RIGHT)
        tip(ttk.Button(doing, text="SSH", command=self.open_ssh),
            "Open a shell on the selected device, using the login already "
            "entered. The tool still only reads - what you type in that "
            "window is you.").pack(side=tk.RIGHT)
        self.netbox_state = ttk.Label(doing, text="", style="MutedBg.TLabel")
        self.netbox_state.pack(side=tk.RIGHT, padx=8)
        # Neither of these was ever a decision worth asking for. If NetBox is
        # set up, use it; if a map has been drawn before, say what moved. An
        # option whose right answer is always yes is not an option.
        self.ask_netbox = tk.BooleanVar(value=True)
        self.watching = tk.BooleanVar(value=True)

        self.map_hint = ttk.Label(page, style="MutedBg.TLabel", wraplength=900,
                                  justify=tk.LEFT, text="")
        self.map_hint.pack(anchor=tk.W, pady=(0, 2))
        self.device_line = ttk.Label(page, text="", style="MutedBg.TLabel")
        self.scan_note = ttk.Label(page, text="", style="MutedBg.TLabel")
        self.scan_note.pack(anchor=tk.W, pady=(0, 2))
        self.site_note = ttk.Label(page, text="", style="MutedBg.TLabel")
        self.site_note.pack(anchor=tk.W, pady=(0, 6))

        self.topo_tree = self._table(page, [
            ("depth", "Depth", 60), ("device", "Device", 180), ("kind", "Kind", 80),
            ("setup", "Setup", 110), ("state", "State", 100),
            ("ip", "Management IP", 150), ("why", "Why", 230)], height=10)
        self.topo_tree.configure(selectmode="extended")
        self.topo_tree.bind("<<TreeviewSelect>>", self._show_device)
        self.topo_rows = {}
        self.target_setups = {}

        # The picker is how one scanned device was chosen before. The table
        # above does that job now, so it only appears when there is no table
        # row to click - a plain scan of a host nothing else knows about.
        picker = ttk.Frame(page)
        picker.pack(fill=tk.X, pady=(6, 2))
        self.device_label = ttk.Label(picker, text="Device", style="MutedBg.TLabel")
        self.device_pick = ttk.Combobox(picker, state="readonly", width=38, values=[])
        self.device_pick.bind("<<ComboboxSelected>>", self._show_device)

        self.ports_title = ttk.Label(page, text="", style="MutedBg.TLabel")
        self.tree = self._table(page, [
            ("port", "Port", 150), ("status", "Status", 110),
            ("to", "Connected to", 330), ("issues", "Issues", 260)], height=9)
        self.tree.bind("<<TreeviewSelect>>", self._show_neighbor_detail)
        self.neighbor_rows = {}
        self._ports_shown = False
        self._hide_ports_table()

        self.explain = scrolledtext.ScrolledText(page, height=7, relief="flat",
                                                 wrap=tk.WORD)
        self.explain.pack(fill=tk.BOTH, expand=False, pady=(8, 0))
        self._explain_hint()
        self._sync_health_controls()
        self._refresh_map_hint()

    def _explain_hint(self) -> None:
        """An empty panel should still say what it is for."""
        self.explain.delete(1.0, tk.END)
        self.explain.insert(
            tk.END, "Select a port above to see how its far end was established, "
                    "what the two sides were compared on, and why the verdict is "
                    "what it is.")

    def _hide_ports_table(self) -> None:
        """An empty grid of twelve rows is chrome, not information."""
        try:
            self.tree.pack_forget()
            if getattr(self, "tree_scroll", None) is not None:
                self.tree_scroll.pack_forget()
        except Exception:
            pass
        self._ports_shown = False

    def _show_ports_table(self) -> None:
        if self._ports_shown:
            return
        try:
            self.tree.pack(fill=tk.BOTH, expand=True)
            self._ports_shown = True
        except Exception:
            pass

    def _build_log_page(self) -> None:
        page = ttk.Frame(self.pages)
        self.page["log"] = page
        row = ttk.Frame(page)
        row.pack(fill=tk.X, pady=(0, 8))
        # Save lives in one place now. These two said "this device" and
        # saved the whole scan, which is worse than not being here.
        tip(ttk.Button(row, text="Error details", command=self.show_details),
            "The unshortened error for every address that did not answer.").pack(
            side=tk.LEFT)
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
        speed = settled(whole_number(get("expect_speed"), "expected speed"), 0)
        interval = settled(decimal_number(get("health_interval"),
                                           "reading interval"), 3.0)
        watch = settled(whole_number(get("seconds"), "seconds to watch"), 90)
        return Options(
            mode=get("mode") or "auto",
            ssh=Creds(user=get("user"), password=get("password"),
                      key_file=get("key"), port=self.ssh_port()),
            bmc_user=get("bmc_user"), bmc_password=get("bmc_password"),
            enable_lldp=self.enable_lldp.get(),
            community=get("community") or "public", switches=switches,
            seconds=watch, ifaces=ifaces,
            mgmt_only=self.mgmt_only.get(), mac=get("mac"), learn=self.learn.get(),
            adb=self.ask_adb.get(), read_serial=self.read_serial.get(),
            port_health=self.port_health.get(), port_health_interval=interval,
            expect_speed=speed, cred_list=self.extra_creds())

    def _unreadable(self, walk: bool = False) -> List[str]:
        """Every field that cannot be read, so all of them are fixed at once."""
        def get(key):
            return self.var[key].get().strip() if key in self.var else ""

        out = []
        for key, field in (("expect_speed", "expected speed"),
                           ("seconds", "seconds to watch")):
            out.append(whole_number(get(key), field)[1])
        out.append(decimal_number(get("health_interval"), "reading interval")[1])
        if walk:
            _settings, bad = check_walk(self.max_depth.get(),
                                        self.max_devices.get(),
                                        self.scope.get())
            out.extend(bad)
        return [why for why in out if why]

    def _refuse(self, problems: List[str]) -> bool:
        """Say which field and what was in it, and do not start."""
        if not problems:
            return False
        messagebox.showerror(
            "Check these settings",
            "Nothing has been contacted. Fix and press again.\n\n   "
            + "\n   ".join(problems))
        return True

    def targets_text(self) -> str:
        """What the user typed, which is nothing when the hint is still showing."""
        entry = getattr(self, "target_entry", None)
        if entry is not None and getattr(entry, "is_placeholder", lambda: False)():
            return ""
        return self.var["targets"].get()

    def _typed_per_host(self):
        """The hosts somebody typed into the per-host table, in order."""
        return [held["host"].get().strip() for held in self.cred_rows
                if held["host"].get().strip()]

    def _with_per_host_targets(self, targets, opts):
        """A host given its own login is a host somebody means to reach.

        The table only ever decided which password to use once a walk
        happened to arrive somewhere. So four hosts typed in with their
        users and passwords were never visited at all, and nothing said
        so - the map simply came back without them.
        """
        from .engine import host_key

        known = {host_key(t)[0] for t in targets or []}
        extra = [host for host in (getattr(opts, "per_host", None) or {})
                 if host_key(host)[0] not in known]
        if extra:
            self.say("also asking %d host(s) that have their own login here: %s"
                     % (len(extra), ", ".join(extra)))
        return list(targets or []) + extra

    def _merged_per_host(self, inline):
        """The Advanced table, with anything typed into Target winning over it."""
        table = self.per_host_table()
        table.update(inline or {})
        self._remember_per_host(table)
        return table

    def start(self) -> None:
        self.results = []
        targets, per_host = targets_with_logins([self.targets_text()],
                                                log=self.say)
        # A table of hosts, each with its own login, and an empty Target is
        # somebody who has already said what to scan. Refusing them because
        # one field is blank is the tool reading its own form rather than
        # the person filling it in.
        if not targets and self._typed_per_host():
            targets = self._typed_per_host()
            self.say("Target is empty, so the %d host(s) in the per-host "
                     "table are what gets asked" % len(targets))
        if not targets:
            messagebox.showwarning(
                "Nothing to do",
                "Enter a target address first, or put hosts with their own "
                "logins in the table under Advanced. A host that needs its "
                "own login can also be written as root@10.0.0.5.")
            return
        if self._refuse(self._unreadable()):
            return
        self.last_error = ""
        self.btn_run.configure(state=tk.DISABLED, text="Scanning")
        self.btn_topo.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.status.busy("Scanning %s..." % targets[0])
        opts = self.build_options()
        opts.per_host = self._merged_per_host(per_host)
        targets = self._with_per_host_targets(targets, opts)
        thread = threading.Thread(target=self._work, args=(targets, opts))
        thread.daemon = True
        thread.start()

    def _arrived(self, result, total: int) -> None:
        """One host is done: put it on screen now, not when the last one is."""
        if any(r.target == result.target for r in self.results):
            return
        self.results.append(result)
        labels = ["%s  (%s)" % (r.target, r.device_name or r.kind or "?")
                  for r in self.results]
        self.device_pick.configure(values=labels)
        if len(self.results) == 1:
            self.device_pick.current(0)
            self._show_device()
        else:
            self.device_label.pack(side=tk.LEFT, padx=(0, 8))
            self.device_pick.pack(side=tk.LEFT)
        self.scan_note.configure(
            text="%d of %d device(s) done" % (len(self.results), total))

    def _work(self, targets, opts) -> None:
        results, report = [], None
        total = len(targets)

        def landed(result):
            self._later(lambda r=result: self._arrived(r, total))

        try:
            results = discover_many(targets, opts, log=self.say,
                                    on_result=landed)
            if opts.ptp:
                self._switch_ptp(targets, opts, results)
            if opts.port_health:
                report = self._collect_health(targets, opts, results)
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self.say(self.last_error)
        self._later(lambda: self._finish(results, report))

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
        # A scan that worked is the moment somebody is most likely to stop,
        # having never seen the map. Point at it once, here.
        if results and any(not getattr(r, "error", "") for r in results):
            self.say("to see how these connect to everything else, "
                     "press Map Network")
        # The devices table is the one list now, so a plain look fills it too
        # rather than leaving it empty beside a picker that has the answer.
        self._fill_devices_from_scan(results)
        pattern = shared_login_refused(results, self.per_host_table())
        if pattern:
            self.say(pattern)
        self.results_at = time.time()
        self.health_report = report
        self.port_rows = {}
        stamp = time.strftime("%H:%M:%S")
        self.scan_note.configure(
            text="Scan of %d device(s) at %s" % (len(results), stamp))
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
        self._show_site_findings(results)
        self._show_device()
        self.btn_run.configure(state=tk.NORMAL, text="Scan")
        self.btn_topo.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        resolved = sum(1 for r in results for n in r.neighbors if n.resolved)
        bad = failed_hosts(results)
        for host, why in bad:
            self.say("%s: %s" % (host, why))
        if bad:
            self.last_error = "%s: %s" % bad[0]
        self.status.done(*run_verdict(resolved, bad))
        self.mode.set("device")
        self._show_mode()

    def _show_site_findings(self, results) -> None:
        """Things that belong to the site rather than to any one device.

        A split PTP domain or a duplicated address spans machines, so it has
        no row to sit on. It goes above the table instead of being repeated
        onto every port it touches.
        """
        lines = []
        try:
            from .ptp_tree import build as build_ptp, domain_problems
            for problem in domain_problems(build_ptp(results)):
                lines.append(problem["detail"])
        except Exception as exc:
            self.say("  ptp tree: %s" % exc)
        for result in results or []:
            found = (getattr(result, "extra", {}) or {}).get("duplicates") or {}
            for item in (found.get("ips") or []) + (found.get("macs") or []):
                if item["detail"] not in lines:
                    lines.append(item["detail"])
            break
        self.site_note.configure(text="  |  ".join(_short(line) for line in lines))
        for line in lines:
            self.say("  site: %s" % line)

    @staticmethod
    def _shorten(line: str) -> str:
        return _short(line)

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
        # The column is called "Connected to". A far end found by asking over
        # IPv6 is still a far end, and leaving it as "unknown" here while the
        # command line prints it is the tool knowing and not saying.
        peer = nb.extra.get("neigh6_peer")
        if peer:
            how = {"direct": "direct cable, IPv6",
                   "loopback": "cabled back into this host",
                   "segment": "same segment, IPv6"}.get(
                       nb.extra.get("neigh6_peer_evidence", ""), "heard over IPv6")
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
            self.device_line.configure(text="Type an address and press Scan to look at one device, or open Map and press Generate to draw how everything connects.")
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
        # Only now is there anything to look at.
        if self.neighbor_rows:
            self._show_ports_table()
        else:
            self._hide_ports_table()

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

        card = nb.extra.get("nic_card")
        if card:
            from .sensors import Card

            lines.append("NIC: %s" % Card(**card).describe())
        module = nb.extra.get("optics")
        if module:
            from .optics import diagnostics, label as optic_label

            lines.append("Optic: %s   (%s)" % (optic_label(module) or "unknown",
                                               diagnostics(module)))
            if module.get("vendor") or module.get("part"):
                lines.append("       %s %s %s" % (module.get("vendor", ""),
                                                  module.get("part", ""),
                                                  module.get("serial", "")))

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

        self._later(ask)
        done.wait()
        return answer["yes"]

    def _topology_progress(self, done: int, queued: int) -> None:
        self._later(lambda: self.status.set_counter(
            "device %d, queued %d" % (done, queued)))
        # Show what has been found so far. Waiting until the end means a
        # person watching a long crawl has nothing to look at and presses
        # Stop blind. The crawler calls this between levels, when the
        # workers for the level just done have finished, so reading the
        # graph here is safe.
        self._later(self._show_so_far)

    def _show_so_far(self) -> None:
        """Fill the device table from the crawl as it stands."""
        crawler = getattr(self, "crawler", None)
        if crawler is None or not hasattr(self, "topo_tree"):
            return
        try:
            nodes = sorted(crawler.topology.nodes.values(),
                           key=lambda n: (99 if n.depth is None else n.depth,
                                          n.label))
        except Exception:
            return               # a view of progress never stops the scan
        self.topo_tree.delete(*self.topo_tree.get_children())
        self.topo_rows = {}
        for node in nodes:
            row = self.topo_tree.insert(
                "", tk.END, tags=(STATUS_COLOUR.get(node.state, "off"),),
                values=("-" if node.depth is None else node.depth, node.label,
                        node.kind, ", ".join(node.setups) or "-", node.state,
                        ", ".join(node.mgmt_ips) or "-",
                        friendly_error(node.error) or node.error or "-"))
            self.topo_rows[row] = node

    def _fill_devices_from_scan(self, results) -> None:
        """Put the hosts a plain look reached into the device table.

        The table used to be filled only by a walk, so after a plain look it
        sat empty next to a dropdown that knew the answer - two lists of the
        same thing, one of them blank.
        """
        if not hasattr(self, "topo_tree"):
            return
        try:
            from .cli import scanned_as_topology

            topology = scanned_as_topology(list(results or []))
        except Exception as exc:                 # a view never stops a scan
            self.say("could not list the devices: %s" % exc)
            return
        self.last_topology = topology
        self.topo_tree.delete(*self.topo_tree.get_children())
        self.topo_rows = {}
        for node in sorted(topology.nodes.values(),
                           key=lambda n: (99 if n.depth is None else n.depth,
                                          n.label)):
            row = self.topo_tree.insert(
                "", tk.END, tags=(STATUS_COLOUR.get(node.state, "off"),),
                values=("-" if node.depth is None else node.depth, node.label,
                        node.kind, ", ".join(node.setups) or "-", node.state,
                        ", ".join(node.mgmt_ips) or "-",
                        friendly_error(node.error) or node.error or "-"))
            self.topo_rows[row] = node

    def _own_map_path(self) -> str:
        """Where a map goes when nobody asked to put it anywhere.

        Being made to name a file before the work has produced one is a
        question the tool can answer for itself. Save... still puts a copy
        wherever you want it.
        """
        from . import history

        where = history.folder()
        try:
            os.makedirs(where, exist_ok=True)
        except OSError:
            where = tempfile.gettempdir()
        return os.path.join(where, "last.topology.html")

    def _reusable(self, seeds):
        """Scan results for the addresses being mapped, if they are still fresh."""
        if not self.results:
            return []
        wanted = set(seeds)
        return [r for r in self.results if r.target in wanted]

    def _age_phrase(self) -> str:
        age = int(time.time() - (self.results_at or time.time()))
        if age < 90:
            return "just now"
        return "%d min ago" % (age // 60)

    def start_topology(self, depth=None) -> None:
        """Walk outward from Target, draw the map, and show it.

        `depth` is how far to go; left alone it uses the limit under
        Advanced, which is what the button does.
        """
        # read the field exactly as a plain scan does: a seed may carry its
        # own login, and a mistyped address is rejected here, not probed
        seeds, per_host = targets_with_logins([self.targets_text()], log=self.say)
        # The same as a plain scan: a filled-in table and an empty Target is
        # somebody who has already said where to start.
        if not seeds and self._typed_per_host():
            seeds = self._typed_per_host()
            self.say("Target is empty, so the walk starts from the %d host(s) "
                     "in the per-host table" % len(seeds))
        if not seeds:
            messagebox.showwarning(
                "No seed", "Put the seed address in Target, or put hosts with "
                "their own logins in the table under Advanced. A host that "
                "needs its own login can also be written as root@10.0.0.5.")
            return
        if self._refuse(self._unreadable(walk=True)):
            return
        # Nobody is asked where to put a file before the work has produced
        # one. It goes where this tool keeps its own, and Save... puts a copy
        # wherever you want it afterwards.
        path = self._own_map_path()
        self.walk_depth_override = depth
        self.mode.set("work")
        self._show_mode()
        self.topo_tree.delete(*self.topo_tree.get_children())
        self.btn_topo.configure(state=tk.DISABLED, text="Drawing")
        self.btn_run.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        opts = self.build_options()
        opts.per_host = self._merged_per_host(per_host)
        # A host given its own login is a host somebody means to reach, so
        # it is a seed as much as anything typed into Target. Without this
        # the walk simply never went there and the map came back short.
        seeds = self._with_per_host_targets(seeds, opts)
        known = self._reusable(seeds)
        if known:
            self.say("starting from the scan of %d device(s) taken %s - "
                     "they are not asked again"
                     % (len(known), self._age_phrase()))
        self.status.busy("Discovering topology from %s..."
                         % (", ".join(seeds) if len(seeds) < 4
                            else "%d addresses" % len(seeds)))
        thread = threading.Thread(target=self._topology_work,
                                  args=(seeds, opts, path, known))
        thread.daemon = True
        thread.start()

    def _crosscheck(self, topology) -> None:
        """Hold the finished map against the last scan and say what it lost.

        Both buttons read the same device, so a link the scan resolved and the
        map does not carry is a defect in the conversion. Nobody can see that
        by eye across two tabs, so the tool checks itself and says so.
        """
        if not self.results:
            return
        from .crosscheck import compare

        reused = getattr(self.crawler, "adopted", None) or set()
        try:
            lost = compare(self.results, topology.to_dict(), reused)
        except Exception as exc:
            self.say("could not compare the map with the scan: %s" % exc)
            return
        if not lost:
            self.say("checked against the scan: every link it found is on the map")
            return
        self.say("the map and the scan do not agree on %d point(s):" % len(lost))
        for line in lost:
            self.say("  " + line)

    def _topology_work(self, seed, opts, html_path, known=None) -> None:
        from .crawl import Crawler, Limits
        from .render_topology import write_html
        topology = None
        try:
            # How far to walk is the limit set under Advanced, unless a
            # caller asked for a particular depth.
            wanted = getattr(self, "walk_depth_override", None)
            settings, _checked = check_walk(
                str(wanted) if wanted else self.max_depth.get(),
                self.max_devices.get(), self.scope.get())
            limits = Limits(**settings)
            self.say(cost_note(settings["max_depth"], settings["max_devices"],
                               settings["scope"]))
            confirm = ((lambda _d, _c: True) if self.auto_discover.get()
                       else self._confirm_on_main_thread)
            self.crawler = Crawler(opts, limits=limits, confirm=confirm, log=self.say,
                                   progress=self._topology_progress, workers=4)
            topology = self.crawler.run(seed, known=known)
            self._stamp_setups(topology)
            # Only when there is something to ask. Trying without an
            # address just writes a complaint into every scan's log.
            netbox_rows = None
            if self.ask_netbox.get() and self.netbox_ready()[0]:
                try:
                    netbox_rows = self._ask_netbox(topology)
                except Exception as problem:      # never lose a finished crawl
                    self.say("netbox: %s" % problem)
            moved_rows = self._what_changed(topology)
            # The page is for looking at. The other two are for working
            # with, and asking for them through a dialog after every walk
            # is a step nobody should have to take, so all three are
            # written together.
            stem = os.path.splitext(html_path)[0]
            json_path = stem + ".json"
            with open(json_path, "w", encoding="utf-8") as handle:
                handle.write(topology.to_json())
            csv_path = stem + ".csv"
            try:
                from .render_topology import topology_csv

                with open(csv_path, "w", encoding="utf-8", newline="") as out:
                    out.write(topology_csv(topology.to_dict()))
            except Exception as problem:   # never lose a finished crawl
                self.say("table not written: %s" % problem)
                csv_path = ""
            write_html(topology.to_dict(), html_path)
            # The three questions asked of a finished map. They used to be
            # buttons on the picture, which put a panel between the reader
            # and the map and left the answers nowhere once the tab closed.
            from . import findings as findings_csv
            from . import suspicious

            odd_rows = None
            try:
                odd_rows = suspicious.gather(topology.to_dict())
                self.say(suspicious.summary(odd_rows))
            except Exception as problem:
                self.say("  what does not add up, skipped: %s" % problem)
            extra = findings_csv.beside_map(stem, netbox=netbox_rows,
                                            moved=moved_rows, odd=odd_rows,
                                            log=self.say)
            self.say(topology.summary())
            self._crosscheck(topology)
            if topology.stopped_because:
                self.say("stopped because: %s" % topology.stopped_because)
            self.say("written to %s"
                     % ", ".join([p for p in (csv_path, json_path, html_path)
                                  if p] + extra))
            self.last_topology_html = html_path
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self.say(self.last_error)
            html_path = ""
        self._later(lambda: self._topology_done(topology, html_path))

    def _topology_done(self, topology, html_path) -> None:
        self.last_topology = topology
        if topology is not None:
            from . import history

            try:
                history.keep(topology.to_dict())
            except Exception:
                pass                      # history is a convenience, not a step
        self.btn_topo.configure(state=tk.NORMAL, text="Map Network")
        self.btn_run.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        self.crawler = None
        if topology is not None:
            self.topo_rows = {}
            for node in sorted(topology.nodes.values(),
                               key=lambda n: (99 if n.depth is None else n.depth, n.label)):
                row = self.topo_tree.insert(
                    "", tk.END, tags=(STATUS_COLOUR.get(node.state, "off"),),
                    values=("-" if node.depth is None else node.depth, node.label,
                            node.kind, ", ".join(node.setups) or "-", node.state,
                            ", ".join(node.mgmt_ips) or "-",
                            friendly_error(node.error) or node.error or "-"))
                self.topo_rows[row] = node
            self.status.done(topology.summary(), "UP" if topology.nodes else "DOWN")
        else:
            self.status.done(friendly_error(self.last_error) or "Topology failed", "DOWN")
        cut = str(getattr(topology, "stopped_because", "") or "")
        if html_path:
            # "Topology ready" after somebody pressed Stop reads as if
            # the button did nothing. Say what the map actually is.
            title = "Map cut short" if cut else "Map ready"
            ask = "Open it in the browser?"
            if cut:
                ask = ("This map is INCOMPLETE - %s." % cut
                       ) + chr(10) * 2 + ask
            if messagebox.askyesno(title, ask):
                # The served map, not the file beside it. Setups and the
                # NetBox comparison are done on the picture, and the saved
                # file cannot talk back to this window.
                self.open_live_map()

    def stop_topology(self) -> None:
        if self.crawler is not None:
            self.crawler.stop()
            self.say("stopping - a capture in flight is cut short and its "
                     "connection closed, so this takes a few seconds, not the "
                     "rest of the window.")
            self.btn_stop.configure(state=tk.DISABLED)
            self.status.set_phase("Stopping...")

    def draw_topology_json(self) -> None:
        """A topology JSON already on disk, drawn as a map.

        Merge needs two files and Open needs an HTML, so a single saved scan
        had no way back to a picture without the command line.
        """
        from .render_topology import render_file

        src = filedialog.askopenfilename(title="Topology JSON to draw",
                                         filetypes=[("JSON", "*.json")])
        if not src:
            return
        stem = os.path.splitext(os.path.basename(src))[0]
        out = filedialog.asksaveasfilename(title="Save the map as",
                                           defaultextension=".html",
                                           initialfile=stem + ".html",
                                           filetypes=[("HTML", "*.html")])
        if not out:
            return
        try:
            render_file(src, out)
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            messagebox.showerror("Could not draw it",
                                 friendly_error(self.last_error) or self.last_error)
            return
        self.last_topology_html = out
        self.say("drew %s as %s" % (src, out))
        self.status.done("Map written", "UP")
        if messagebox.askyesno("Map", "Open it?"):
            webbrowser.open("file:///" + out.replace("\\", "/"))

    def _what_changed(self, topology) -> None:
        """Fold this map into what was known, and say only what moved.

        Off unless asked for: a tool that starts keeping records of a network
        without being told to is not one people leave running.
        """
        if not self.watching.get():
            return None
        from . import watch

        try:
            state = watch.load()
            state, events = watch.update(state, watch.read(topology.to_dict()))
            watch.save(state)
        except Exception as exc:
            self.say("could not work out what changed: %s: %s"
                     % (type(exc).__name__, exc))
            return None
        standing = watch.standing(state)
        self.say("watching %d thing(s): %d up, %d degraded, %d down, %d out of sight"
                 % (sum(standing.values()), standing["UP"], standing["DEGRADED"],
                    standing["DOWN"], standing["UNKNOWN"]))
        if not events:
            self.say("nothing changed since the last map")
            return []
        self.say("%d change(s) since the last map:" % len(events))
        for line in watch.describe(events):
            self.say("  " + line)
        return events

    def choose_setup_tags(self) -> None:
        """Which NetBox tags name a bench, out of the hundreds there are.

        Nothing becomes a setup by itself: most tags on this estate name a
        team or a project, and a tool that guessed would put half the
        inventory into benches nobody has.
        """
        from . import netbox
        from .engine import load_config, save_config

        seen = netbox.tags_seen(netbox.load())
        if not seen:
            messagebox.showinfo(
                "Setup tags",
                "No tags have been read from NetBox yet. Tick NetBox and draw "
                "a map once - the tags your devices carry are read with it.")
            return
        chosen = set(netbox.setup_tags(load_config()))

        window = tk.Toplevel(self.root)
        window.title("Which tags name a setup")
        window.transient(self.root)
        ttk.Label(window, wraplength=460, justify=tk.LEFT, padding=(12, 10),
                  text="Tick the tags that name a bench. The number beside "
                       "each is how many devices carry it.").pack(anchor=tk.W)
        frame = ttk.Frame(window, padding=(14, 0))
        frame.pack(fill=tk.BOTH, expand=True)
        box = tk.Listbox(frame, selectmode=tk.MULTIPLE, height=min(18, len(seen)),
                         activestyle="none", exportselection=False)
        box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        bar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=box.yview)
        bar.pack(side=tk.LEFT, fill=tk.Y)
        box.configure(yscrollcommand=bar.set)
        names = [str(name) for name, _count in seen]
        for row, (name, count) in enumerate(seen):
            box.insert(tk.END, "%s   (%d device(s))" % (name, count))
            if str(name) in chosen:
                box.selection_set(row)

        def keep():
            picked = [names[row] for row in box.curselection()]
            save_config({"netbox": dict((load_config().get("netbox") or {}),
                                        setup_tags=picked)})
            self.say("setup tags: %s" % (", ".join(picked) or "none"))
            window.destroy()

        doing = ttk.Frame(window, padding=(14, 10))
        doing.pack(fill=tk.X)
        ttk.Button(doing, text="Keep these", command=keep).pack(side=tk.LEFT)
        ttk.Button(doing, text="Cancel", command=window.destroy).pack(
            side=tk.LEFT, padx=8)

    def save_netbox_comparison(self) -> None:
        """The table of what is on the network against what NetBox holds.

        One row per device, both values side by side. It is written beside
        every map as well; this is the copy you hand to somebody, and it
        will work out the answer now if the last run did not.
        """
        held = getattr(self, "_netbox_findings", None)
        # NetBox being set up is the real requirement. Check it first, so a
        # map that is right there on screen is never answered with "go and
        # scan" - the honest reason is that NetBox has no address or token.
        if held is None:
            ready, why = self.netbox_ready()
            if not ready:
                messagebox.showinfo(
                    "NetBox is not set up",
                    "There is nothing to compare against until NetBox is set "
                    "up.\n\n%s\n\nFill its address and token in under "
                    "Advanced, then this report compares your scan against it."
                    "\n\n(To see what the report looks like meanwhile, use "
                    "File - Save a NetBox report example.)" % why)
                return
        if held is None and self.last_topology is None and not self.results:
            messagebox.showinfo(
                "Nothing to compare yet",
                "Scan some hosts or draw a map first - then this compares what "
                "was found against NetBox.")
            return
        if held is None:
            self.say("asking NetBox now - the last run did not")
            threading.Thread(target=self._compare_then_save, daemon=True).start()
            return
        self._write_netbox_csv(held)

    def _compare_then_save(self) -> None:
        """Ask NetBox off the main thread, then come back to write the file."""
        try:
            topo = self.last_topology
            if topo is None:
                from .cli import scanned_as_topology

                topo = scanned_as_topology(list(self.results or []))
            self._ask_netbox(topo)
        except Exception as problem:
            self.say("netbox: %s" % problem)
        found = getattr(self, "_netbox_findings", None)
        self._later(lambda: self._write_netbox_csv(found))

    def _write_netbox_csv(self, found) -> None:
        if not found:
            messagebox.showinfo("Nothing came back",
                                "NetBox was asked and said nothing about "
                                "these devices.")
            return
        from . import findings as findings_csv

        path = filedialog.asksaveasfilename(
            title="Save what NetBox does not agree with",
            defaultextension=".csv", initialfile="netbox-vs-network.csv",
            filetypes=[("A table for a spreadsheet", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as out:
                out.write(findings_csv.as_csv(found,
                                              findings_csv.NETBOX_COLUMNS))
        except OSError as exc:
            messagebox.showerror("Could not write it", str(exc))
            return
        self.say("%d device(s) compared with NetBox, written to %s"
                 % (len(found), path))

    def open_files_folder(self) -> None:
        """Open the folder a walk writes its map and reports into.

        It simply opens the folder. Trying to select one file inside it with
        `explorer /select,` was passed as two arguments, which Explorer reads
        as nothing and silently did nothing at all - so the menu item looked
        broken. Opening the folder is what was wanted anyway.
        """
        import subprocess

        from . import history

        where = os.path.abspath(history.folder())
        if not os.path.isdir(where):
            messagebox.showinfo(
                "Nothing written yet",
                "Draw a map first. It writes the picture, the table and the "
                "NetBox comparison into this folder together:\n\n%s" % where)
            return
        opened = False
        try:
            if sys.platform.startswith("win"):
                os.startfile(where)                       # noqa: S606
                opened = True
            elif sys.platform == "darwin":
                opened = subprocess.Popen(["open", where]).pid is not None
            else:
                opened = subprocess.Popen(["xdg-open", where]).pid is not None
        except Exception as problem:
            messagebox.showinfo(
                "Where the files are",
                "The maps and reports are in:\n\n%s\n\n(could not open it for "
                "you automatically: %s)" % (where, problem))
            return
        if not opened:
            messagebox.showinfo("Where the files are",
                                "The maps and reports are in:\n\n%s" % where)
            return
        self.say("the files from the last map are in %s" % where)

    def _wheel_advanced(self, event) -> None:
        try:
            self.advanced_canvas.yview_scroll(
                -1 if getattr(event, "delta", 0) > 0 else 1, "units")
        except tk.TclError:
            pass

    def _secret(self, box) -> None:
        """Remember a field that hides what is typed in it.

        A row added after the switch was turned on has to come up showing,
        or the one password somebody is in the middle of typing is the one
        still hidden.
        """
        held = getattr(self, "secret_boxes", None)
        if held is None:
            held = self.secret_boxes = []
        held.append(box)
        if getattr(self, "show_secrets", None) is not None \
                and self.show_secrets.get():
            try:
                box.configure(show="")
            except tk.TclError:
                pass

    def reveal_secrets(self) -> None:
        """Every password and the NetBox token, shown or hidden together.

        One switch rather than an eye on each field: they are all the same
        decision, and six of them is six things to find.
        """
        mask = "" if self.show_secrets.get() else "•"
        for box in getattr(self, "secret_boxes", []):
            try:
                box.configure(show=mask)
            except tk.TclError:
                pass                      # a row that has since been removed

    def netbox_settings(self):
        """What the window has been told, before the environment or the file."""
        from . import netbox
        from .engine import load_config

        def typed(key):
            return self.var[key].get().strip() if key in self.var else ""

        return netbox.settings(typed("netbox_url"), typed("netbox_token"),
                               load_config())

    def netbox_ready(self):
        """Whether anything NetBox can be offered at all, and why not.

        A control that is present and does nothing is worse than one that is
        absent: it promises. Until an address and a token exist, every NetBox
        action is switched off and says what is missing.
        """
        try:
            url, token = self.netbox_settings()
        except Exception:
            return False, "NetBox is not set up"
        if not url:
            return False, ("NetBox is not set up - put its address and token "
                           "under Advanced")
        if not token:
            return False, "NetBox has an address but no token - see Advanced"
        return True, ""

    def _show_netbox_state(self) -> None:
        """Keep the one line about NetBox honest as the fields are typed in."""
        if not hasattr(self, "netbox_state"):
            return
        ready, why = self.netbox_ready()
        try:
            self.netbox_state.configure(
                text="NetBox ready" if ready else why)
        except Exception:
            pass

    def remember_netbox(self, say: bool = True) -> bool:
        """Keep the NetBox address and token, sealed, for next time."""
        from . import vault
        from .engine import save_config

        url, token = self.netbox_settings()
        if not url and not token:
            return False
        sealed = vault.protect(token) if token else ""
        if token and not sealed:
            if say:
                self.say("netbox: the token was NOT saved - %s" % vault.why_not())
            return False
        try:
            save_config({"netbox": {"url": url, "token": sealed or ""}})
        except OSError as why:
            if say:
                self.say("netbox: could not save it - %s" % why)
            return False
        if say:
            self.say("netbox: address and token saved - they will be here "
                     "next time (%s)" % vault.describe())
        return True

    def forget_netbox(self) -> None:
        """Take the address and token back out of the defaults file."""
        from .engine import save_config

        if not messagebox.askyesno(
                "Forget NetBox",
                "Remove the saved NetBox address and token from this machine?"):
            return
        try:
            save_config({"netbox": {}})
        except OSError as why:
            self.say("netbox: could not remove it - %s" % why)
            return
        for key in ("netbox_url", "netbox_token"):
            if key in self.var:
                self.var[key].set("")
        self._show_netbox_state()
        self.say("netbox: address and token removed from this machine")

    def test_netbox(self) -> None:
        """Answer the only question that matters: does this work, and if not why."""
        from . import netbox

        url, token = self.netbox_settings()
        if not url:
            messagebox.showwarning(
                "NetBox", "No address. Put one in NetBox address, or set %s."
                % netbox.ENV_URL)
            return
        wrong = netbox.token_warning(token)
        if wrong:
            messagebox.showwarning("NetBox", wrong)
            return
        client = netbox.Netbox(url, token, verify=False)
        why = client.reachable()
        if why:
            self.say("netbox: %s" % why)
            messagebox.showerror("NetBox", why[:400])
            return
        self.say("netbox: %s answered" % url)
        self.remember_netbox()
        self._show_netbox_state()
        # There is no tick box any more: if NetBox is set up, it is
        # used. Telling somebody to tick a control that was removed
        # is worse than saying nothing.
        messagebox.showinfo(
            "NetBox",
            ("%s answered." % url) + chr(10) * 2
            + "Every scan from now on looks its devices up there and "
            + "says where the two disagree. Nothing is ever written "
            + "to NetBox.")

    def open_help(self) -> None:
        """The same bundled page the command line opens with --help-page."""
        from .cli import show_help_page

        if show_help_page("") != 0:
            messagebox.showinfo(
                "Help", "This build carries no help page.")
        else:
            self.say("the guide is open in your browser")

    def open_another_map(self) -> None:
        """Swap the map being shown for one from a file.

        The server follows self.last_topology, so replacing it is the whole
        of the work; the browser shows the new one on its next request.
        """
        if not self._open_a_saved_map():
            return
        if getattr(self, "map_server", None) is not None:
            import webbrowser

            webbrowser.open(self.map_server.url)
            self.say("the map now shows the file you opened - reload the tab")
        else:
            self.open_live_map()

    def _open_a_saved_map(self) -> bool:
        """Nothing drawn yet: offer to show one from disk rather than refuse.

        This is what the three separate buttons used to be for. A person who
        presses Map wants to see a map; where it comes from is our problem.
        """
        from . import history
        from .topology import Topology, load_topology

        where = filedialog.askopenfilename(
            title="Open a saved map",
            initialdir=history.folder(),
            filetypes=[("LinkFinder maps", "*.topology.json"),
                       ("Map data", "*.json"), ("Saved page", "*.html"),
                       ("All files", "*.*")])
        if not where:
            return False
        if where.lower().endswith((".html", ".htm")):
            import webbrowser

            webbrowser.open("file:///" + os.path.abspath(where).replace(
                chr(92), "/"))
            self.say("opened %s - a saved page is a viewer, so it cannot mark"
                     % where)
            return False
        try:
            # load_topology returns the plain record. Everything downstream
            # expects a Topology object and calls .to_dict() on it, so the
            # two must not be confused - they were, and every button that
            # touched the map raised.
            fresh = Topology()
            fresh.merge_from(load_topology(where))
            self.last_topology = fresh
        except Exception as why:
            messagebox.showerror("Map", "Could not read it: %s" % why)
            return False
        self.say("opened %s" % where)
        return True

    def open_live_map(self) -> None:
        """Serve the last map to the browser, with this window answering it.

        The page cannot write to disk; this window can. So rather than the page
        saving a file for somebody to import, it asks the window, and a setup
        is written the moment it is typed.
        """
        import webbrowser

        from . import setups as setup_store
        from .render_topology import render_html
        from .serve import MapServer, Pages

        if self.last_topology is None and not self._open_a_saved_map():
            return
        if getattr(self, "map_server", None) is not None:
            webbrowser.open(self.map_server.url)
            return

        # Never capture the map itself. The first version of this held the
        # topology in the closure, so the server went on serving the first
        # scan for ever: a second scan, or a different file, changed nothing
        # in the browser and there was no way to tell from the window.
        def held():
            return self.last_topology

        def page():
            return render_html(held().to_dict(), live=self.map_server.base)

        def state():
            from . import shared

            ready, why = self.netbox_ready()
            return {"setups": setup_store.load(),
                    "where": shared.say_where(),
                    "netbox": {"ready": ready, "why": why}}

        def put(device, names):
            node = held().nodes.get(device)
            record = {"id": device, "name": getattr(node, "name", ""),
                      "chassis_id": getattr(node, "chassis_id", ""),
                      "serial": getattr(node, "serial", ""),
                      "mgmt_ips": list(getattr(node, "mgmt_ips", []) or [])}
            if names:
                setup_store.remember([record], names)
            else:
                setup_store.forget([record])
            if node is not None:
                node.setups = list(names)
            # say() is a queue the window drains; touching Tk from this
            # thread is not safe and killed the browser's request outright.
            self.say("setup: %s is now in %s"
                     % (record["name"] or device, ", ".join(names) or "no setup"))
            return {"saved": device, "setups": list(names)}

        def tags():
            from . import netbox
            from .engine import load_config

            return {"seen": netbox.tags_seen(netbox.load()),
                    "chosen": netbox.setup_tags(load_config())}

        def set_tags(names):
            from .engine import load_config, save_config

            save_config({"netbox": dict((load_config().get("netbox") or {}),
                                        setup_tags=list(names))})
            self.say("setup tags: %s" % (", ".join(names) or "none"))
            return {"chosen": list(names)}

        def table():
            from .render_topology import topology_csv

            return topology_csv(held().to_dict())

        def ports():
            from . import netbox, switchports
            from .engine import load_config

            config = load_config()
            url, token = netbox.settings("", "", config)
            if not url:
                return {"proposals": [], "note": "NetBox is not set up here"}
            client = netbox.Netbox(url, token,
                                   verify=not bool((config.get("netbox") or {})
                                                   .get("insecure")))
            return {"proposals": switchports.proposals(held().to_dict(), client,
                                                       config)}

        def confirm_ports(name):
            from . import netbox, switchports
            from .engine import load_config, save_config

            config = load_config()
            url, token = netbox.settings("", "", config)
            client = netbox.Netbox(url, token,
                                   verify=not bool((config.get("netbox") or {})
                                                   .get("insecure")))
            for plan in switchports.proposals(held().to_dict(), client, config):
                if plan["switch"] == name and plan.get("matched"):
                    save_config(switchports.remember(config, name, plan))
                    self.say("switch ports: %s confirmed, %d port(s)"
                             % (name, plan["covers"]))
                    return {"confirmed": name, "covers": plan["covers"]}
            return {"error": "no rule could be worked out for %s" % name}

        def changes():
            from . import history

            return history.drift(held().to_dict())

        def mark_many(lines, names):
            from . import setups as setup_store

            data = held().to_dict()
            # A sheet pasted whole carries a setup per row; a plain list takes
            # the name typed above it. One box, either kind.
            marked, missing = setup_store.mark_from_text(
                data, chr(10).join(lines), names)
            # The map object is what the page is drawn from, so the names go
            # back onto it too, not only into the store.
            for node in held().nodes.values():
                fresh = setup_store.lookup({"id": node.id,
                                            "serial": node.serial,
                                            "chassis_id": node.chassis_id,
                                            "mgmt_ips": list(node.mgmt_ips)})
                if fresh:
                    node.setups = fresh
            self.say("setups: %d device(s) in %s, %d line(s) not recognised"
                     % (len(marked), ", ".join(names) or "no setup",
                        len(missing)))
            # The page cannot know what a bulk change did to forty devices,
            # and an action with no visible result gets done twice. Hand it
            # the new state so it can show it without a redraw.
            now = dict((node.id, list(node.setups))
                       for node in held().nodes.values())
            return {"marked": marked, "missing": missing, "setups": now}

        def setup_diff(wanted):
            from . import netbox, setupdiff
            from .engine import load_config

            config = load_config()
            url, token = netbox.settings("", "", config)
            if not url:
                return {"rows": [], "note": "NetBox is not set up here"}
            client = netbox.Netbox(url, token,
                                   verify=not bool((config.get("netbox") or {})
                                                   .get("insecure")))
            pairs = (config.get("setup_tags_named") or {}
                     if isinstance(config.get("setup_tags_named"), dict)
                     else {})
            rows = setupdiff.review(held().to_dict(), client, wanted,
                                    tag_of=pairs,
                                    beyond_reach=setupdiff.out_of_reach(config))
            # The columns travel with the rows so the page can offer the
            # whole comparison as a CSV without another round trip.
            return {"rows": rows, "summary": setupdiff.summary(rows),
                    "columns": list(setupdiff.COLUMNS)}

        def open_netbox_setup():
            """The page cannot hold a token, but it can fetch the window.

            Telling somebody "it is under Advanced" when Advanced is in a
            window they may not be looking at is not an answer.
            """
            def show():
                try:
                    if not self.advanced_open:
                        self.toggle_advanced()
                    self.root.deiconify()
                    self.root.lift()
                    self.root.focus_force()
                    box = self.var.get("netbox_url")
                    if box is not None:
                        self.say("netbox: put the address and token in "
                                 "Advanced, then press Test")
                except Exception:
                    pass
            self._later(show)
            return {"opened": True}

        def suspicious_now():
            """What does not add up, for the map's own door."""
            from . import suspicious

            found = suspicious.gather(topology=held().to_dict(),
                                      results=self.results)
            return {"found": found, "summary": suspicious.summary(found)}

        def netbox_find(wanted):
            """One lookup, for the map's NetBox door."""
            from . import netbox
            from .engine import load_config

            config = load_config()
            url, token = netbox.settings("", "", config)
            if not url:
                return {"records": [], "note": "NetBox is not set up here"}
            client = netbox.Netbox(
                url, token,
                verify=not bool((config.get("netbox") or {}).get("insecure")))
            records = netbox.find(client, wanted)
            return {"records": records,
                    "addresses": netbox.addresses_for(client, records)}

        def sheet():
            from . import setups as setup_store

            return setup_store.template_csv(held().to_dict())

        def guide():
            from .render_topology import assets_dir

            try:
                with open(os.path.join(assets_dir(), "help.html"), "r",
                          encoding="utf-8") as handle:
                    return handle.read()
            except OSError:
                return "<p>this build carries no guide</p>"

        self.map_server = MapServer(Pages(page, state, put, self._netbox_todo,
                                          tags=tags, set_tags=set_tags,
                                          table=table, ports=ports,
                                          confirm_ports=confirm_ports,
                                          changes=changes,
                                          mark_many=mark_many,
                                          setup_diff=setup_diff,
                                          sheet=sheet, help=guide,
                                          netbox_find=netbox_find,
                                          netbox_setup=open_netbox_setup,
                                          suspicious=suspicious_now))
        url = self.map_server.start()
        self.say("the map is open at %s (this window is answering it)"
                 % url.split("/m/")[0])
        webbrowser.open(url)

    def _netbox_todo(self):
        """What NetBox does not know, collected when somebody asks for it."""
        from . import netbox

        if self.last_topology is None:
            return {"changes": [], "note": "no map has been drawn yet"}
        data = self.last_topology.to_dict()
        inventory = netbox.load()
        if not inventory.get("devices"):
            return {"changes": [],
                    "note": "NetBox has not been read yet - tick NetBox and "
                            "draw a map first"}
        found = netbox.sort_review(netbox.review(data, inventory))
        return {"changes": [f for f in found if f["status"] != netbox.MATCH],
                "proposals": netbox.propose(data, inventory),
                "note": "Nothing here was sent to NetBox."}

    def _ask_netbox(self, topology) -> None:
        """The same look-up the command line does, so the two agree."""
        from . import netbox
        from .engine import load_config

        url, token = self.netbox_settings()
        if not url or not token:
            # A tick that silently does nothing is worse than no tick: say it
            # where it cannot be missed, not only in the log.
            said = ("NetBox is ticked, but there is no %s. Fill it in under "
                    "Advanced, or untick NetBox."
                    % ("address" if not url else "token"))
            self.say("netbox: " + said)
            self._later(lambda: messagebox.showwarning("NetBox", said))
            return
        wrong = netbox.token_warning(token)
        if wrong:
            self.say("netbox: %s" % wrong)
            self._later(lambda: messagebox.showwarning("NetBox", wrong))
            return
        client = netbox.Netbox(url, token, verify=False)
        why = client.reachable()
        if why:
            self.say("netbox: %s" % why)
            return
        data = topology.to_dict()
        inventory = netbox.fetch(client, data, self.say)
        netbox.save(inventory)
        allow = netbox.setup_tags(load_config())
        if not allow:
            self.say("netbox: no setup tags named yet - press Setup tags... to "
                     "choose from the %d tag(s) your devices carry"
                     % len(netbox.tags_seen(inventory)))
        put = netbox.apply(data, inventory, allow=allow)
        known = {n.get("id"): n for n in data.get("nodes") or []}
        for node in topology.nodes.values():
            fresh = known.get(node.id) or {}
            if fresh.get("setups"):
                node.setups = list(fresh["setups"])
            if fresh.get("netbox"):
                node.extra["netbox"] = fresh["netbox"]
        self.say("netbox: %d device(s) took their setup from its tags" % put)
        findings = netbox.sort_review(netbox.review(data, inventory))
        self._netbox_findings = findings
        # Put the verdict on each device so the picture can show it. A list
        # of disagreements is read; a coloured map is seen.
        netbox.stamp_verdicts(data, findings)
        for node in topology.nodes.values():
            fresh = known.get(node.id) or {}
            if fresh.get("netbox_state"):
                node.extra["netbox_state"] = fresh["netbox_state"]
        standing = netbox.standing(findings)
        self.say("netbox: %s" % ", ".join("%d %s" % (n, netbox.in_words(s))
                                          for s, n in sorted(standing.items())))
        for found in findings:
            if found["status"] == netbox.MATCH:
                continue
            self.say("netbox: %s  %s - %s"
                     % (found["status"], found["label"], found["detail"]))
            self.say("        %s | confidence %s%s"
                     % (found["evidence"], found["confidence"],
                        " | needs your confirmation" if found["confirm"] else ""))
        return findings

    def _stamp_setups(self, topology) -> None:
        """Carry the setup names onto a fresh crawl.

        A device marked last week is the same device today, so the label is
        looked up by chassis id and address rather than by its place in the
        list, and a host list that names its setups is honoured as well.
        """
        from . import setups as setup_store

        marked = setup_store.stamp(topology, self.target_setups or {})
        if marked:
            self.say("setup names carried onto %d device(s)" % marked)

    def _login_for(self, host: str):
        """Everything Advanced holds for this host: login, key and the spares.

        The scan already resolves it this way - its own login, then the shared
        one, then the extras after a rejection - so a shell opened from here
        must resolve it the same way or the two front ends disagree about a
        machine that is plainly reachable.
        """
        key = self.var["key"].get().strip() if "key" in self.var else ""
        own = self.per_host_table().get(host)
        if own is not None and (own.user or own.password or getattr(own, "key_file", "")):
            return (own.user, own.password,
                    getattr(own, "key_file", "") or key, self.extra_creds(),
                    own.port or self.ssh_port())
        return (self.var["user"].get().strip(), self.var["password"].get(),
                key, self.extra_creds(), self.ssh_port())

    def open_ssh(self) -> None:
        """A shell on the devices picked in the list.

        The address and the login are already here; making somebody find them
        again in another program is the kind of friction that stops a person
        checking the one thing that would have answered the question.
        """
        from .sshwindow import SshWindow

        chosen = [self.topo_rows[r] for r in self.topo_tree.selection()
                  if r in self.topo_rows]
        if not chosen:
            messagebox.showinfo("SSH", "Select a device in the list first.")
            return
        reachable = [n for n in chosen if n.mgmt_ips]
        silent = [n.label for n in chosen if not n.mgmt_ips]
        for name in silent:
            self.say("%s: no management address was ever advertised - nothing to "
                     "connect to" % name)
        if not reachable:
            messagebox.showinfo("SSH", "None of the selected devices published an "
                                       "address to connect to.")
            return
        if len(reachable) > 3 and not messagebox.askyesno(
                "SSH", "Open %d windows?" % len(reachable)):
            return
        for node in reachable:
            host = node.mgmt_ips[0]
            user, password, key_file, spares, port = self._login_for(host)
            SshWindow(self.root, host, user, password, port=port,
                      title="%s  -  %s" % (node.label,
                                           host if port == 22
                                           else "%s:%d" % (host, port)),
                      note="LinkFinder opened this session. The tool itself only "
                           "reads; this window is yours.",
                      key_file=key_file, spares=spares)
            self.say("opened a shell on %s" % host)

    def merge_topologies(self) -> None:
        from .render_topology import write_html
        from .topology import merge_files

        from . import history

        paths = filedialog.askopenfilenames(
            title="Pick the maps to merge (two or more)",
            initialdir=history.folder(),
            filetypes=[("LinkFinder maps", "*.topology.json"),
                       ("Map data", "*.json")])
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

    def save_something(self) -> None:
        """One dialog. What you pick it to be is what it is written as.

        This used to ask Yes/No/Cancel first and then open a dialog, which
        is a menu wearing the wrong buttons. The file type already says what
        you want, so nothing else needs asking.

        The table comes first because drawing a map already writes the page
        and its data alongside. What saving is for is carrying the findings
        somewhere a person can sort, filter and hand on.
        """
        from .render_topology import render_html, topology_csv

        if self.last_topology is None and not self.results:
            messagebox.showinfo("Save", "Scan or draw a map first.")
            return
        where = filedialog.asksaveasfilename(
            title="Save this as", defaultextension=".csv",
            initialfile="linkfinder.csv",
            filetypes=[("A table for a spreadsheet", "*.csv"),
                       ("The data itself", "*.json"),
                       ("A page to send somebody", "*.html")])
        if not where:
            return
        kind = os.path.splitext(where)[1].lower()
        try:
            if kind == ".csv":
                text = (topology_csv(self.last_topology.to_dict())
                        if self.last_topology is not None
                        else render_csv(self.results))
            elif kind == ".json":
                # A scan with no map still holds findings worth keeping, and
                # refusing here turned them away in exactly the case where
                # somebody had asked for them.
                import json

                text = (json.dumps(self.last_topology.to_dict(), indent=2)
                        if self.last_topology is not None
                        else results_to_json(self.results))
            else:
                if self.last_topology is None:
                    messagebox.showinfo("Save", "Draw a map first for this.")
                    return
                text = render_html(self.last_topology.to_dict())
            with open(where, "w", encoding="utf-8", newline="") as out:
                out.write(text)
        except Exception as why:
            messagebox.showerror("Save", "Could not write it: %s" % why)
            return
        self.say("written to %s" % where)

    def export_csv(self) -> None:
        """The table, from whichever of the two the window last produced.

        A map run leaves a topology and no per-host results, so asking for
        a scan first was a refusal in exactly the case the table is wanted.
        """
        from .render_topology import topology_csv

        if self.results:
            text = render_csv(self.results)
        elif self.last_topology is not None:
            text = topology_csv(self.last_topology.to_dict())
        else:
            messagebox.showinfo("Nothing yet", "Run a scan or draw a map first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")])
        if path:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            self.say("table written to %s" % path)

    def export_json(self) -> None:
        """The data, from whichever of the two the window last produced.

        The table learned this already: a map run leaves a topology and no
        per-host results, so demanding a scan refused the request in the
        very case the data was wanted.
        """
        if self.results:
            text = results_to_json(self.results)
        elif self.last_topology is not None:
            import json

            text = json.dumps(self.last_topology.to_dict(), indent=2)
        else:
            messagebox.showinfo("Nothing yet", "Run a scan or draw a map first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                            filetypes=[("JSON", "*.json")])
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
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
    def shut(*_a):
        # The server lives and dies with this window, by design.
        if getattr(app, "map_server", None) is not None:
            app.map_server.stop()
            app.map_server = None
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", shut)
    root.mainloop()

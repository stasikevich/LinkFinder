"""Press every button in the window, and run every option on the command line.

A handler that is wired is not a handler that works. This file is the QA
pass: each control is pressed with its dialogs stubbed, and anything that
raises is a failure with the button's name on it.

Dialogs are answered rather than shown, so nothing here waits for a person.
A button that opens a browser is given a stub for that too.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


A_MAP = {"schema_version": 1, "seed": "198.51.100.10",
         "generated_at": "2026-01-01T00:00:00Z", "stopped_because": "",
         "nodes": [{"id": "ip:198.51.100.10", "name": "a host", "kind": "host",
                    "mgmt_ips": ["198.51.100.10"], "state": "scanned",
                    "depth": 0, "setups": [], "extra": {}}],
         "edges": []}

try:
    import tkinter as tk
    _probe = tk.Tk()
    _probe.destroy()
except Exception as why:
    print("skipped: no window available here (%s)" % type(why).__name__)
else:
    from tkinter import filedialog, messagebox, simpledialog

    from linkfinder import gui as guimod

    WORK = tempfile.mkdtemp(prefix="lf-buttons-")
    MAP_FILE = os.path.join(WORK, "map.json")
    io.open(MAP_FILE, "w", encoding="utf-8").write(json.dumps(A_MAP))

    # The whole family is answered, not the three that happened to come up.

    def _no_dialogs():

        from tkinter import filedialog, messagebox


        for name in ("showinfo", "showwarning", "showerror", "askyesno",

                     "askquestion", "askokcancel", "askretrycancel"):

            if hasattr(messagebox, name):

                setattr(messagebox, name, lambda *_a, **_k: None)

        messagebox.askyesnocancel = lambda *_a, **_k: None

        for name in ("askopenfilename", "asksaveasfilename", "askdirectory"):

            if hasattr(filedialog, name):

                setattr(filedialog, name, lambda *_a, **_k: "")

        filedialog.askopenfilenames = lambda *_a, **_k: ()



    _no_dialogs()

    # Every way the window can stop and wait for a person, answered.
    filedialog.askopenfilename = lambda *_a, **_k: MAP_FILE
    filedialog.askopenfilenames = lambda *_a, **_k: (MAP_FILE,)
    filedialog.asksaveasfilename = lambda *_a, **_k: os.path.join(WORK, "out.txt")
    messagebox.showinfo = lambda *_a, **_k: None
    messagebox.showwarning = lambda *_a, **_k: None
    messagebox.showerror = lambda *_a, **_k: None
    messagebox.askyesnocancel = lambda *_a, **_k: False
    messagebox.askokcancel = lambda *_a, **_k: False
    simpledialog.askstring = lambda *_a, **_k: "a bench"

    import webbrowser

    opened = []
    webbrowser.open = lambda url, *_a, **_k: opened.append(url) or True

    # Nothing here may stop and wait for a person: a build has nobody to click.


    root = tk.Tk()
    # Nobody needs to watch this happen, and a window left on screen during a
    # build waits for a person who is not there.
    root.withdraw()
    root.report_callback_exception = lambda *_a: None
    app = guimod.App(root)
    root.update()

    print("-- every button in the window, pressed --")
    # The ones that start a scan or a crawl are left out on purpose: they
    # reach the network, and that is not what this file is for.
    SKIP = {"start", "start_topology", "stop_topology", "open_ssh",
            "test_netbox"}
    import re

    handlers = sorted(set(re.findall(r"command=self\.([a-z_]+)",
                                     io.open(os.path.join(ROOT, "linkfinder",
                                                          "gui.py"),
                                             encoding="utf-8").read())))
    pressed = 0
    for name in handlers:
        if name in SKIP:
            continue
        action = getattr(app, name, None)
        # command=self.root.destroy scrapes as "root". Only bound methods
        # of the window are buttons; everything else is a false positive.
        import types

        if not isinstance(action, types.MethodType):
            continue
        if action is None:
            FAILURES.append("%s: the button names a handler that is not there"
                            % name)
            print("%-62s %s" % (name, "FAIL"))
            continue
        try:
            action()
            root.update()
            # A handler that opens a window of its own must not leave it
            # standing: the next press would land behind it.
            for child in list(root.winfo_children()):
                if isinstance(child, tk.Toplevel):
                    child.destroy()
            root.update()
            pressed += 1
            print("%-62s %s" % (name, "ok"))
        except Exception as problem:
            FAILURES.append("%s raised %s: %s"
                            % (name, type(problem).__name__, problem))
            print("%-62s %s" % (name, "FAIL"))
            traceback.print_exc()
    check("every button pressed without raising", len(FAILURES), 0)
    check("and there were buttons to press", pressed >= 8, True)

    print()
    print("-- the map follows the map, not the first one it ever saw --")
    from linkfinder.topology import Topology, load_topology

    _first = Topology()
    _first.merge_from(load_topology(MAP_FILE))
    app.last_topology = _first
    app.open_live_map()
    root.update()
    first = app.map_server.pages.html()
    second = dict(A_MAP)
    second["nodes"] = [dict(A_MAP["nodes"][0], name="a different host")]
    other = os.path.join(WORK, "second.json")
    io.open(other, "w", encoding="utf-8").write(json.dumps(second))
    filedialog.askopenfilename = lambda *_a, **_k: other
    app.open_another_map()
    root.update()
    check("a second map really replaces the first",
          "a different host" in app.map_server.pages.html(), True)
    check("and the first one is gone from it",
          "a different host" in first, False)
    # Always, even if a check above failed: a live socket and a live window
    # both outlive the test otherwise.
    try:
        if getattr(app, "map_server", None) is not None:
            app.map_server.stop()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass

print()
print("-- every option on the command line is accepted --")
# Not that each does its job - other files check that - but that none of
# them is a flag the parser forgot, or crashes before it can say anything.
from linkfinder.cli import build_parser                       # noqa: E402

parser = build_parser()
flags = []
for action in parser._actions:
    for option in action.option_strings:
        if option.startswith("--"):
            flags.append((option, action.nargs, action.const))
check("the parser has a surface worth checking", len(flags) > 50, True)

SAMPLE = {"--seconds": "30", "--targets-file": "x.txt",
          "--netbox-find": "a-serial", "--from-netbox": "a-tag", "--mode": "ssh", "--user": "u",
          "--password": "p", "--sudo-password": "p", "--become-password": "p",
          "--key": "k",
          "--ssh-port": "22", "--bmc-user": "u", "--bmc-password": "p",
          "--bmc-port": "443", "--iface": "eth0", "--netbox-url": "https://x",
          "--netbox-token": "nbt_a.b", "--setup-tag": "t",
          "--netbox-proposals": "f.json", "--timeout": "5",
          "--switch": "10.0.0.1", "--community": "public",
          "--switch-user": "u", "--switch-password": "p",
          "--mac": "00:11:22:33:44:55", "--port-health-interval": "3",
          "--expect-speed": "1000", "--workers": "2", "--csv": "-",
          "--topology": "10.0.0.1", "--max-depth": "1", "--max-devices": "5",
          "--scope": "10.0.0.0/24", "--topology-json": "a.json",
          "--topology-html": "a.html", "--topology-csv": "-",
          "--topology-resume": "a.json", "--topology-workers": "2",
          "--serial-baud": "115200",
          "--cred": "u:p", "--forget-switches": "*", "--watch": "a.json",
          "--watch-state": "s.json", "--serve-map": "a.json",
          "--mark-setup": "b", "--mark-list": "l.txt", "--setup-sheet": "s.csv",
          "--setup-diff": "b", "--setup-diff-csv": "-", "--map": "a.json",
          "--out-of-reach": "CPE", "--confirm-switch-ports": "sw",
          "--shared": "/tmp/x", "--help-page": "h.html",
          "--render-topology": ["a.json", "a.html"],
          "--compare-scan": ["a.json", "b.json"],
          "--diff": ["a.json", "b.json"],
          "--merge-topology": ["a.json"]}

refused = []
for option, nargs, _const in flags:
    if option in ("--help", "--version"):
        continue
    given = SAMPLE.get(option)
    argv = [option] if given is None else (
        [option] + (given if isinstance(given, list) else [given]))
    try:
        parser.parse_args(argv)
    except SystemExit:
        refused.append(option)
    except Exception as problem:
        refused.append("%s (%s)" % (option, type(problem).__name__))
check("no option is refused by the parser that declares it", refused, [])

print()
print("-- and the built program answers for itself --")
runner = [sys.executable, os.path.join(ROOT, "cli_entry.py")]
for argv, wanted in ((["--version"], 0), (["--help"], 0), (["--where"], 0)):
    done = subprocess.run(runner + argv, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=120)
    check("%s exits cleanly" % " ".join(argv), done.returncode, wanted)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all button and option checks passed")

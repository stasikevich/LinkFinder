"""The window as it is actually built, not as the source reads.

Everything else that checks the window greps `gui.py` for a string. That
catches a button being deleted and nothing else: it cannot see a control
pushed off the edge, a field with no label beside it, a button with no
explanation, or a window that does not fit on a laptop screen. Those are
the faults a person meets first, and until now the only way to find one
was to open the tool and look.

So this builds the real App with a hidden root, lets Tk work out every
size and position, then walks the whole tree and asks of each widget what
a person would ask of it.

Where there is no display - a build server, a session over SSH - it says
so and passes, rather than failing for a reason that is not the code.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:
    print("tkinter is not installed - skipping the layout checks")
    sys.exit(0)

try:
    _root = tk.Tk()
    _root.withdraw()
except Exception as exc:
    print("no display available (%s) - skipping the layout checks" % exc)
    sys.exit(0)

from linkfinder import gui as gui_mod
from linkfinder.gui import App

gui_mod.load_config = lambda: {}
gui_mod.save_config = lambda updates: "defaults.json"
gui_mod.config_path = lambda: "defaults.json"

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


app = App(_root)
_root.update_idletasks()


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        for one in walk(child):
            yield one


ALL = list(walk(_root))
BUTTONS = [w for w in ALL if isinstance(w, (ttk.Button, ttk.Checkbutton,
                                            ttk.Radiobutton))]
ENTRIES = [w for w in ALL if isinstance(w, (ttk.Entry, ttk.Combobox,
                                            ttk.Spinbox))]


def label_of(widget):
    try:
        return str(widget.cget("text"))
    except tk.TclError:
        return ""


print("-- the window is a window somebody can actually open --")
print("   %d widgets, %d buttons, %d fields"
      % (len(ALL), len(BUTTONS), len(ENTRIES)))
# A 1366x768 laptop is the smallest thing this is opened on. A window that
# wants more than that comes up with its bottom row off the screen, and the
# bottom row is where the buttons are.
wide, tall = _root.winfo_reqwidth(), _root.winfo_reqheight()
print("   it asks for %dx%d" % (wide, tall))
check("it fits across a 1366-wide screen", wide <= 1366, True)
check("and down a 768-tall one", tall <= 768, True)

print()
print("-- every button says what it is and what it does --")
nameless = [str(b) for b in BUTTONS if not label_of(b).strip()]
check("none of them is unlabelled", nameless, [])
# Tooltips are attached by the helper, which binds <Enter>. A button with
# no binding has no explanation anywhere.
quiet = [label_of(b) for b in BUTTONS
         if label_of(b).strip() and not b.bind("<Enter>")]
check("and the ones in the main row carry an explanation",
      [n for n in quiet if n in ("Scan", "Map Network", "Stop", "Open Map",
                                 "SSH", "List...", "show")], [])

print()
print("-- every field can be read back --")
# Comboboxes are left out: the device picker is filled as results arrive
# and is read with .get(), which no inspection can see from here.
loose = []
for box in ENTRIES:
    if isinstance(box, ttk.Combobox):
        continue
    try:
        if not str(box.cget("textvariable")):
            loose.append(str(box))
    except tk.TclError:
        pass
check("nothing is typed into a field nobody reads", loose, [])

print()
print("-- nothing is pushed off the edge of what holds it --")
# A control wider than its parent is a control with its right-hand end
# cut off. Tk will not say so; it just draws it clipped.
clipped = []
for widget in ALL:
    parent = widget.master
    if parent is None or not widget.winfo_ismapped():
        continue
    # A frame held inside a canvas is sized by the canvas when the window
    # is on screen; with a withdrawn root it reads as far too wide.
    if isinstance(parent, tk.Canvas):
        continue
    try:
        if widget.winfo_reqwidth() > parent.winfo_reqwidth() + 2 \
                and parent.winfo_reqwidth() > 1:
            clipped.append("%s (%d) in %s (%d)"
                           % (label_of(widget) or widget.winfo_class(),
                              widget.winfo_reqwidth(),
                              parent.winfo_class(), parent.winfo_reqwidth()))
    except tk.TclError:
        pass
check("nothing is wider than the frame holding it", clipped, [])

print()
print("-- the passwords are hidden until somebody asks --")
masked = [b for b in ENTRIES if str(b.cget("show"))]
check("every secret field starts masked",
      sorted({str(b.cget("show")) for b in masked}), ["•"])
check("and there are several of them", len(masked) >= 4, True)

print()
print("-- the two pages and the menu are all reachable --")
check("the work page and the log are both built",
      sorted(set(app.page)), ["device", "log", "map", "work"])
# winfo_ismapped is 0 for everything here - the root is withdrawn, which
# is what makes this runnable without a screen. What can be asked is which
# page the geometry manager is actually holding.
for mode in ("work", "log"):
    app.mode.set(mode)
    app._show_mode()
    _root.update_idletasks()
    check("mode %r shows its page, and only its page" % mode,
          [str(s) for s in app.pages.pack_slaves()], [str(app.page[mode])])
app.mode.set("work")
app._show_mode()
_root.update_idletasks()

print()
print("-- Advanced opens without pushing its own bottom off the screen --")
before = _root.winfo_reqheight()
app.toggle_advanced()
_root.update_idletasks()
opened = _root.winfo_reqheight()
print("   closed %d tall, open %d" % (before, opened))
check("opening it makes the window taller", opened > before, True)
# The panel is 1500 pixels of settings and a laptop screen is 768, so it
# scrolls rather than growing without limit - and the window itself is
# told it may never exceed the screen, whatever it would like to be.
_cap_w, _cap_h = _root.maxsize()
print("   the window may not exceed %dx%d (screen %dx%d)"
      % (_cap_w, _cap_h, _root.winfo_screenwidth(), _root.winfo_screenheight()))
check("the window is capped at the screen it is on",
      _cap_h <= _root.winfo_screenheight(), True)
check("and the panel itself scrolls instead of growing",
      app.advanced_canvas.winfo_reqheight() <= 400, True)
app.toggle_advanced()
_root.update_idletasks()
check("closing it puts the height back", _root.winfo_reqheight(), before)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all window layout checks passed")

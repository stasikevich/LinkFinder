"""The window itself: does Generate start from the scan already taken?
Run: python tests/test_gui.py

This drives the real App with a hidden root window. Where there is no display
(a CI box, a server over SSH) it says so and passes rather than failing for a
reason that has nothing to do with the code.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
except ImportError:
    print("tkinter is not installed - skipping the window checks")
    sys.exit(0)

try:
    _root = tk.Tk()
    _root.withdraw()
except Exception as exc:
    print("no display available (%s) - skipping the window checks" % exc)
    sys.exit(0)

from linkfinder import gui as gui_mod, vault
from linkfinder.engine import per_host_from_config, per_host_to_config
from linkfinder.gui import App
from linkfinder.model import Neighbor, TargetResult
from linkfinder.sshx import Creds

# the window reads the user's defaults file on startup; a test must not
SAVED = {}
gui_mod.load_config = lambda: {}
gui_mod.save_config = lambda updates: (SAVED.update(updates), "defaults.json")[1]
gui_mod.config_path = lambda: "defaults.json"

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-58s %s" % (label, "ok" if got == want else "FAIL"))


app = App(_root)

print("-- the hint before anything has been scanned --")
app.var["targets"].set("198.51.100.1, 198.51.100.2, 198.51.100.3")
app.mode.set("map")
app._show_mode()
hint = app.map_hint.cget("text")
check("nothing is claimed to be reused", "Starting from the scan" in hint, False)
check("it speaks of addresses, not one address", "address(es)" in hint, True)

print()
print("-- a value put in the field without typing is still read --")
check("the placeholder check looks at the field",
      app.targets_text(), "198.51.100.1, 198.51.100.2, 198.51.100.3")

print()
print("-- after a scan of all three --")
results = []
for last in (1, 2, 3):
    result = TargetResult(target="198.51.100.%d" % last, kind="linux", error="",
                          device_name="host%d" % last)
    result.add(Neighbor(local_iface="eth0", switch="sw1",
                        chassis_id="aa:bb:cc:00:00:01", port="Gi1/%d" % last,
                        proto="LLDP", source="test"))
    results.append(result)
app._finish(results)
app.mode.set("map")
app._show_mode()
hint = app.map_hint.cget("text")
check("the map page says it will reuse them",
      "Starting from the scan" in hint, True)
check("and counts all three", "3 device(s)" in hint, True)
check("and says how fresh they are", "just now" in hint, True)

print()
print("-- a seed may carry its own login, as a scan target may --")
app.var["targets"].set("root@198.51.100.1, admin:secret@198.51.100.2")
from linkfinder.engine import targets_with_logins

seeds, logins = targets_with_logins([app.targets_text()])
check("the login is stripped off the seed", seeds,
      ["198.51.100.1", "198.51.100.2"])
check("and kept against its host", logins["198.51.100.2"].password, "secret")
app.mode.set("map")
app._show_mode()
check("so the scan of those hosts is still recognised",
      "2 device(s)" in app.map_hint.cget("text"), True)
app.var["targets"].set("198.51.100.1, 198.51.100.2, 198.51.100.3")

print()
print("-- which results get reused --")
check("exactly the addresses being mapped",
      len(app._reusable(["198.51.100.1", "198.51.100.2", "198.51.100.3"])), 3)
check("an address that was never scanned is not claimed",
      len(app._reusable(["198.51.100.9"])), 0)
check("a partial overlap reuses only the overlap",
      len(app._reusable(["198.51.100.1", "198.51.100.9"])), 1)
app.results = []
check("with nothing scanned there is nothing to reuse",
      len(app._reusable(["198.51.100.1"])), 0)

print()
print("-- per-host logins: the table in Advanced --")
check("it starts with one empty row", len(app.cred_rows), 1)
check("an empty row contributes nothing", app.per_host_table(), {})

app.cred_rows[0]["host"].set("198.51.100.1")
app.cred_rows[0]["user"].set("root")
ROW_PASSWORD = "Kv4-this-cannot-collide-8M"
app.cred_rows[0]["password"].set(ROW_PASSWORD)
app.add_cred_row("198.51.100.2", "admin", "two")
table = app.per_host_table()
check("two hosts carry their own login", sorted(table),
      ["198.51.100.1", "198.51.100.2"])
check("with the right user", table["198.51.100.2"].user, "admin")
check("and the right password", table["198.51.100.1"].password,
      ROW_PASSWORD)

app.add_cred_row("198.51.100.3", "", "")
check("a host with neither user nor password is ignored",
      len(app.per_host_table()), 2)

print()
print("-- a host given its own login is a host to ask --")
# Reported twice from the bench: four hosts typed into the table with their
# users and passwords were never visited, and the map came back without
# them. The table used to decide only which password to use IF a walk
# happened to arrive. Typing a host in is asking for it.
class _Opts(object):
    per_host = {"10.0.0.9": Creds(user="root", password="x"),
                "10.0.0.8": Creds(user="root", password="y")}


asked = app._with_per_host_targets(["10.0.0.1"], _Opts())
check("the typed target is still first", asked[0], "10.0.0.1")
check("and every host with its own login is asked too",
      sorted(asked), ["10.0.0.1", "10.0.0.8", "10.0.0.9"])
check("one already in Target is not asked twice",
      app._with_per_host_targets(["10.0.0.9", "10.0.0.1"], _Opts()).count("10.0.0.9"),
      1)
check("nor when it is spelled with a login or other capitals",
      len(app._with_per_host_targets(["root@10.0.0.9", "10.0.0.8"], _Opts())), 2)
check("with no table at all nothing is added",
      app._with_per_host_targets(["10.0.0.1"], type("E", (), {"per_host": {}})()),
      ["10.0.0.1"])

print()
print("-- a password can be read back before it costs an attempt --")
# Asked for after a mistyped password on a bench host: network gear drops
# the session after two or three refusals, so a typo found by the far end
# is expensive. All of them answer to one switch - the token as well,
# because nbt_<key>.<secret> is long, pasted, and easy to cut short.
MASK = "•"
boxes = app.secret_boxes
check("every password field is hidden to begin with",
      sorted({str(b.cget("show")) for b in boxes}), [MASK])
check("and there are several of them, so this can fail", len(boxes) >= 4, True)
app.show_secrets.set(True)
app.reveal_secrets()
check("the switch shows all of them at once",
      sorted({str(b.cget("show")) for b in boxes}), [""])
app.add_cred_row("198.51.100.9", "root", "typed later")
check("a row added while they are showing is not left hidden",
      str(app.secret_boxes[-1].cget("show")), "")
app.show_secrets.set(False)
app.reveal_secrets()
check("and turning it off hides every one again",
      sorted({str(b.cget("show")) for b in boxes}), [MASK])
app.remove_cred_row(app.cred_rows[-1])

print()
print("-- the NetBox comparison can be handed to somebody --")
# Asked for during a demonstration: the table of what is on the network
# against what the inventory holds is written beside every map, and "it is
# in a folder, the path is in the log" is not an answer when somebody is
# standing there waiting for it.
with open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "linkfinder", "gui.py"),
        encoding="utf-8") as _handle:
    _window_source = _handle.read()
check("the menu offers the NetBox report",
      'label="Save the NetBox-vs-network report..."' in _window_source, True)
check("and it is a real handler",
      callable(getattr(app, "save_netbox_comparison", None)), True)
check("the file it writes carries both sides in their own columns",
      [head for head, _key in
       __import__("linkfinder.findings", fromlist=["x"]).NETBOX_COLUMNS],
      ["verdict", "device", "field", "on the network", "in NetBox",
       "what it means", "evidence", "confidence", "needs confirming",
       "netbox record"])
# Nothing scanned must explain rather than write an empty table: a file
# saying NetBox agreed about nothing at all is worse than no file.
_told = []
_was_info, _was_save = gui_mod.messagebox.showinfo, gui_mod.filedialog.asksaveasfilename
gui_mod.messagebox.showinfo = lambda title, body: _told.append(title)
gui_mod.filedialog.asksaveasfilename = lambda **k: ""
_held, _topo, _res = (getattr(app, "_netbox_findings", None),
                      app.last_topology, app.results)
_was_ready = app.netbox_ready
# NetBox not set up: the honest blocker, even with a map on screen. It must
# not send somebody off to scan when the real reason is a missing token.
app.netbox_ready = lambda: (False, "no NetBox address or token")
app._netbox_findings, app.last_topology, app.results = None, None, []
app.save_netbox_comparison()
check("NetBox not set up is named as the reason, not 'go scan'",
      _told, ["NetBox is not set up"])
# NetBox ready but nothing scanned: now it is honest to ask for a scan.
_told[:] = []
app.netbox_ready = lambda: (True, "")
app.save_netbox_comparison()
check("with NetBox ready but nothing scanned, it asks for a scan",
      _told, ["Nothing to compare yet"])
app.netbox_ready = _was_ready
app._netbox_findings, app.last_topology, app.results = _held, _topo, _res
gui_mod.messagebox.showinfo, gui_mod.filedialog.asksaveasfilename = _was_info, _was_save

print()
print("-- the example list it hands out is one it reads without complaint --")
# The format was only ever described in prose, so everybody wrote their
# first list wrong and found out from a page of complaints. An example
# that the tool itself refuses would be worse than none, so it is read
# back here rather than eyeballed.
import tempfile as _tempfile

from linkfinder.engine import targets_with_logins as _read_list
from linkfinder.targets import setups as _setups_in

_path = os.path.join(_tempfile.gettempdir(), "lf-example-list.txt")
with open(_path, "w", encoding="utf-8", newline="\n") as _out:
    _out.write(App.LIST_EXAMPLE)
_said = []
_hosts, _logins = _read_list([], targets_file=_path, log=_said.append)
check("it is read with nothing to complain about",
      [m for m in _said if "skipped" in m], [])
check("and the log names what it accepted",
      any(m.startswith("accepted ") for m in _said), True)
check("a plain host is taken", "198.51.100.11" in _hosts, True)
check("a host with its own login keeps it",
      _logins.get("198.51.100.10") and _logins["198.51.100.10"].user, "root")
check("a network in it is opened out",
      "198.51.100.17" in _hosts and "198.51.100.18" in _hosts, True)
check("and a heading puts the hosts under it into a setup",
      _setups_in([], _path).get("198.51.100.21"), "bench-a")
os.remove(_path)

print()
print("-- a filled-in table with an empty Target is not nothing to do --")
# Reported from the bench: five hosts typed into the table, each with its
# own login, Target left empty - and the tool answered "enter a target
# address first". It was reading its own form rather than the person
# filling it in; the check for an empty field ran before anything looked
# at the table.
app.var["targets"].set("")
check("every host typed into the table is found",
      app._typed_per_host(),
      ["198.51.100.1", "198.51.100.2", "198.51.100.3"])
# The third has no login of its own. It is still a host somebody typed
# in, so it is still asked - with the shared login from the top bar. That
# is why this list and per_host_table() are deliberately not the same: one
# answers "what to scan", the other "with which password".
check("including one with no login of its own, which uses the shared one",
      "198.51.100.3" in app._typed_per_host()
      and "198.51.100.3" not in app.per_host_table(), True)
check("a row with no host in it is not one of them",
      "" in app._typed_per_host(), False)
app.var["targets"].set("198.51.100.1, 198.51.100.2, 198.51.100.3")

print()
print("-- what wins when the same host is given twice --")
merged = app._merged_per_host({"198.51.100.1": Creds(user="typed", password="inline")})
check("a login typed into Target beats the table",
      (merged["198.51.100.1"].user, merged["198.51.100.1"].password),
      ("typed", "inline"))
check("the other rows survive it", merged["198.51.100.2"].user, "admin")

print()
print("-- remembering is opt-in --")
SAVED.clear()
app.remember_logins.set(False)
app._merged_per_host({})
check("unticked writes nothing at all", SAVED, {})
app.remember_logins.set(True)
app._merged_per_host({})
check("ticked writes the table", sorted(SAVED.get("per_host", {})),
      ["198.51.100.1", "198.51.100.2"])
# What is written is a handle from the system store, never the password.
check("and the password goes with it, sealed",
      vault.looks_protected(SAVED["per_host"]["198.51.100.1"]["password"])
      if vault.available() else
      "password" not in SAVED["per_host"]["198.51.100.1"], True)
check("the password itself is not in what was written",
      ROW_PASSWORD in json.dumps(SAVED), False)
app.remember_logins.set(False)

print()
print("-- the file format round-trips --")
# The password here is long and unusual on purpose. A two-character one
# would turn up inside a base64 blob by chance roughly once in twenty-five
# runs, and the check would be a coin toss rather than a check.
NOT_BY_CHANCE = "Zx9-correct-horse-battery-staple-7Q"
shaped = per_host_to_config({"198.51.100.5": Creds(user="ops",
                                                   password=NOT_BY_CHANCE)})
check("written as a user and a sealed password", shaped["198.51.100.5"]["user"],
      "ops")
check("and the password as typed is nowhere in the row",
      NOT_BY_CHANCE in json.dumps(shaped["198.51.100.5"]), False)
back = per_host_from_config({"per_host": shaped})
check("and read back the same", (back["198.51.100.5"].user,
                                 back["198.51.100.5"].password),
      ("ops", NOT_BY_CHANCE))
check("passwords can be left out on request",
      per_host_to_config({"h": Creds(user="ops", password="pw")},
                         with_passwords=False), {"h": {"user": "ops"}})
check("a user:pass string in the file is understood too",
      per_host_from_config({"per_host": {"h": "ops:pw"}})["h"].password, "pw")
check("a defaults file with no table gives an empty one",
      per_host_from_config({}), {})

print()
print("-- the Target field advertises the syntax --")
check("the hint shows a host with its own login",
      "root@" in app.target_entry.placeholder, True)

app.clear_per_host()
check("Clear leaves one empty row", (len(app.cred_rows), app.per_host_table()),
      (1, {}))

print()
print("-- extra logins, tried only after a rejection --")
check("it starts with one empty row", len(app.extra_rows), 1)
check("an empty row is not a credential", app.extra_creds(), [])

app.extra_rows[0]["user"].set("admin")
app.extra_rows[0]["password"].set("fallback")
app.add_extra_row("operator", "second")
got = app.extra_creds()
check("both are offered", [c.user for c in got], ["admin", "operator"])
check("in the order they are written", got[0].password, "fallback")

app.remove_extra_row(app.extra_rows[0])
check("removing one leaves the other",
      [c.user for c in app.extra_creds()], ["operator"])
app.remove_extra_row(app.extra_rows[0])
check("removing the last leaves one empty row to type into",
      (len(app.extra_rows), app.extra_creds()), (1, []))

print()
print("-- they reach the engine in the right order --")
app.cred_rows[0]["host"].set("198.51.100.1")
app.cred_rows[0]["user"].set("own")
app.cred_rows[0]["password"].set("ownpw")
app.extra_rows[0]["user"].set("spare")
app.extra_rows[0]["password"].set("sparepw")
app.var["user"].set("shared")
app.var["password"].set("sharedpw")
opts = app.build_options()
opts.per_host = app._merged_per_host({})
from linkfinder.neighbors import credential_sets

order = [c.user for c in credential_sets(opts, "198.51.100.1")]
check("the host's own login, then the shared one, then the spare",
      order, ["own", "shared", "spare"])
check("a host with no row of its own starts from the shared login",
      [c.user for c in credential_sets(opts, "198.51.100.9")], ["shared", "spare"])

print("-- the table fills as hosts land, not when the last one does --")
app.results = []
first = TargetResult(target="198.51.100.21", kind="linux", error="",
                     device_name="alpha")
first.add(Neighbor(local_iface="eth0", switch="sw1", chassis_id="aa:bb:cc:00:00:01",
                   port="Gi1/1", proto="LLDP", source="test"))
app._arrived(first, 3)
check("the first host is on screen straight away", len(app.results), 1)
check("and it is the one shown", app.device_pick.get().startswith("198.51.100.21"),
      True)
check("the count says how far along it is",
      "1 of 3 device(s) done" in app.scan_note.cget("text"), True)

second = TargetResult(target="198.51.100.22", kind="linux", error="",
                      device_name="beta")
app._arrived(second, 3)
check("the next one joins it", len(app.results), 2)
check("without stealing the view",
      app.device_pick.get().startswith("198.51.100.21"), True)
check("and the count moves", "2 of 3" in app.scan_note.cget("text"), True)

app._arrived(second, 3)
check("the same host arriving twice is not counted twice", len(app.results), 2)
app.results = []

_root.destroy()

print()
print()
print("-- a button says what it does, and keeps no second copy of the docs --")
import inspect
import re
from linkfinder import gui as guimod
src = inspect.getsource(guimod)
# Three buttons used to show a map - one for the live one, one for a saved
# page, one for a JSON on disk. They are one button now, and it works out
# which of the three it is looking at.
# Count buttons, not the string. "Map" is also written when the button is
# re-enabled after a walk, and matching that called it a second button.
check("one button shows a map, not three",
      len(re.findall(r'ttk\.Button\([^)]*text="Open Map"', src)) == 1
      and 'text="Draw HTML"' not in src and 'text="Open map"' not in src, True)
check("and with nothing drawn it offers a saved one",
      "_open_a_saved_map" in src, True)
# Counting buttons was a proxy, and it broke the moment the redesign
# removed some on purpose - which was the goal, not a regression. Name the
# actions instead: each of these must carry its own explanation.
MAIN = ("Scan", "Map Network", "Stop", "Open Map", "SSH", "List...")
bare = [name for name in MAIN
        if not re.search(r'tip\(ttk\.Button\([^)]*text="%s"' % re.escape(name),
                         src)]
check("every main action carries an explanation", bare, [])
check("a tooltip disappears when the pointer leaves",
      "_hide" in src and 'bind("<Leave>"' in src, True)

print()
print("-- the files a walk wrote can be found without reading the log --")
# Asked during a demonstration: "where is the CSV of what is on the
# network against what NetBox holds?" It had been written an hour before,
# correct, in a folder nobody would guess, with the path announced in a
# tab nobody had open.
check("the menu offers to open the folder",
      'label="Open the folder with maps && reports"' in src, True)
check("and it is a real handler", callable(getattr(app, "open_files_folder", None)),
      True)
check("which points at the folder the maps go to",
      "history.folder()" in src, True)

print()
print("-- no action has two ways in under two different names --")
# He asked "save button - что он делает?" and the honest answer was: less
# than nothing. File already had Save..., and the button asked which format
# - a question with no answer now that a walk writes all of them by itself.
# Two more turned up beside it. One action under two labels is worse than
# one under none, because the second label reads like a second thing.
_menu = set(re.findall(
    r'add_command\(\s*label="([^"]+)",\s*\n?\s*command=self\.(\w+)', src))
_btn = re.findall(
    r'ttk\.Button\([^)]*?text="([^"]+)"[^)]*?command=self\.(\w+)', src, re.S)
_by_action, _by_label = {}, {}
for _label, _cmd in _btn:
    _by_action.setdefault(_cmd, []).append(_label)
    _by_label.setdefault(_label, set()).add(_cmd)
check("no action sits on two buttons",
      sorted(c for c, labels in _by_action.items() if len(labels) > 1), [])
# The other half, and the one he spotted: three buttons said some version
# of "forget" and meant the NetBox token, the remembered switches and the
# saved logins. A label that does not say what it forgets is a dare.
check("and no label means two different things",
      sorted(l for l, cmds in _by_label.items() if len(cmds) > 1), [])
check("nothing is both a menu entry and a button",
      sorted(set(c for _l, c in _menu) & set(_by_action)), [])
check("and there are buttons to check at all, so this can fail",
      len(_btn) > 10, True)

print()
print("-- the window and the map must offer the same things --")
import io as _io
from linkfinder.render_topology import assets_dir as _assets
_page = _io.open(os.path.join(_assets(), "topology.html"), encoding="utf-8").read()
_gui = _io.open(os.path.join(os.path.dirname(_assets()), "gui.py"),
                encoding="utf-8").read()
# One place, which is the picture. The window used to offer the same actions
# and that is exactly how a button appeared saying "no map has been drawn yet".
check("the map can mark a setup", 'id="pickmark"' in _page, True)
check("and the window no longer competes with it",
      'text="Setup..."' in _gui, False)
# Marking used to end in a downloaded file that somebody imported. The window
# answers the page directly now, so neither exists - and nothing may bring them
# back without this failing.
check("nothing is downloaded any more", "picksave" in _page, False)
check("and there is nothing to import", "Import marks..." in _gui, False)
check("the page writes through the tool instead",
      "tellTheTool" in _page and "LIVE + '/setups'" in _page, True)
check("the window can open the map and answer it",
      'text="Open Map"' in _gui and "MapServer" in _gui, True)
check("a saved copy of the page cannot mark at all, and says nothing about it",
      "if (LIVE) { document.getElementById('markbox')" in _page, True)
check("the map can open a shell", 'ssh://' in _page, True)
check("and so can the window", 'text="SSH"' in _gui, True)
# NetBox stopped being a tick box: an option whose right answer is always
# yes is not an option. If it is set up, it is used, and the window says so.
check("NetBox is no longer something to remember to tick",
      "self.ask_netbox = tk.BooleanVar(value=True)" in _gui, True)
check("and the window says where it stands instead",
      "netbox_state" in _gui, True)

print()
print("-- nothing NetBox can be pressed until NetBox exists --")
# A control that is present and does nothing is a promise the tool cannot
# keep. The map used to carry four of them; they are gone, and what they
# answered is written as CSV beside the map instead.
check("the page is told whether it may offer them",
      '"netbox": {"ready": ready, "why": why}' in _gui, True)
check("the strip is gone, not shrunk",
      _page.count('<button id="nb'), 0)
check("and its answers are written where they can be read later",
      "findings as findings_csv" in _gui and "beside_map(" in _gui, True)
check("a scan does not ask when there is nowhere to ask",
      "self.ask_netbox.get() and self.netbox_ready()[0]" in _gui, True)
# Advanced is four named groups now; NetBox is one of them.
check("and the settings live in a group of their own under Advanced",
      'group("NetBox")' in _gui and 'self.var["netbox_token"]' in _gui, True)
check("and the command line too",
      "--netbox" in _io.open(os.path.join(os.path.dirname(_assets()), "cli.py"),
                             encoding="utf-8").read(), True)

print()
print("-- somewhere to put the NetBox address and token --")
check("the window has an address field", "netbox_url" in app.var, True)
check("and a token field", "netbox_token" in app.var, True)
app.var["netbox_url"].set("https://nb.example")
app.var["netbox_token"].set("nbt_key.secret")
check("what is typed is what is used",
      app.netbox_settings(), ("https://nb.example", "nbt_key.secret"))
app.var["netbox_url"].set("")
app.var["netbox_token"].set("")
# The fallback chain is flag, then environment, then the defaults file -
# and saving it there is the point. A test that reads the real file is
# testing this machine, not the behaviour, so it gets its own.
from linkfinder import engine as _engine

_was_paths = _engine.CONFIG_PATHS[:]
_engine.CONFIG_PATHS[:] = [os.path.join(tempfile.gettempdir(),
                                        "lf_gui_netbox_check.json")]
if os.path.exists(_engine.CONFIG_PATHS[0]):
    os.remove(_engine.CONFIG_PATHS[0])
try:
    check("an empty box with nothing saved invents nothing",
          app.netbox_settings()[0],
          os.environ.get("LINKFINDER_NETBOX_URL", ""))
    _engine.save_config({"netbox": {"url": "https://saved.example",
                                    "token": ""}})
    check("but a saved address is used, which is why it is saved",
          app.netbox_settings()[0] in ("https://saved.example",
                                       os.environ.get(
                                           "LINKFINDER_NETBOX_URL", "")),
          True)
finally:
    _engine.CONFIG_PATHS[:] = _was_paths
    if os.path.exists(os.path.join(tempfile.gettempdir(),
                                   "lf_gui_netbox_check.json")):
        os.remove(os.path.join(tempfile.gettempdir(),
                               "lf_gui_netbox_check.json"))
check("there is a way to find out whether it works",
      callable(getattr(app, "test_netbox", None)), True)

print()
print("-- a Setup is a tag somebody named, and the window is where you name it --")
# Choosing them moved onto the map: the answer is obvious while looking at the
# devices that carry the tags, and the window could only ever have offered it
# with no map in front of you.
# It moved onto the map once, into a panel that has since been taken out.
# Nowhere at all is not an option: without it every comparison reports
# "no setup tags named yet" and there is no way to answer.
check("the window offers it again", 'text="Setup tags..."' in _gui, True)
check("and it is a real chooser, not a stub",
      callable(getattr(app, "choose_setup_tags", None)), True)
check("and the window still answers the map about them",
      "set_tags=set_tags" in _gui, True)
# Nothing may become a Setup by itself: this estate has hundreds of tags and
# most of them are teams, not benches.
from linkfinder import engine as _engine
from linkfinder import netbox as _nb
check("nothing is a setup until it is named", _nb.setup_tags({}), [])
check("the window reads the same list the command line does",
      "netbox.setup_tags(load_config())" in _gui, True)
check("and what NetBox does not know goes into a file, not a dialog",
      ("What to add in NetBox" not in _gui)
      and (".netbox.csv" in open(os.path.join(
          os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
          "linkfinder", "findings.py"), encoding="utf-8").read()), True)
check("and it offers only the tags this scan actually saw",
      "netbox.tags_seen(" in _gui, True)

print()
print("-- the window answers the map, and writes what it is told --")
import json as _json
import urllib.request as _url
from linkfinder import setups as _store
from linkfinder.topology import Topology as _Topology

_topo = _Topology()
_node = _topo.add_node(mgmt_ip="198.51.100.10", name="a bench nuc", kind="host",
                       state="scanned", serial="LIVE1234")
app.last_topology = _topo
# open_live_map imports webbrowser inside itself, so the module has to be
# replaced where it will be looked up - nothing should actually open here.
_opened = []
import types as _types
sys.modules["webbrowser"] = _types.SimpleNamespace(open=_opened.append)
app.open_live_map()
_srv = app.map_server
check("Open map starts a server", bool(_srv), True)
check("and sends the browser to it", _opened and _srv.token in _opened[0], True)


def _ask(path, data=None):
    req = _url.Request("http://127.0.0.1:%d%s" % (_srv.port, path), data=data,
                       headers={"Content-Type": "application/json"})
    with _url.urlopen(req, timeout=10) as answer:
        return answer.status, answer.read()


_code, _body = _ask(_srv.base + "/map")
check("the real page is served", (_code, b"LinkFinder topology" in _body),
      (200, True))
check("and it is told it can write", b'const LIVE = "/m/' in _body, True)

_where = os.path.join(tempfile.gettempdir(), "lf_live_store.json")
if os.path.exists(_where):
    os.remove(_where)
# A change arrives on the server's thread, not the window's. Answering it by
# touching Tk from there killed the request outright, so this pins it.
_code, _body = _ask(_srv.base + "/setups",
                    _json.dumps({"device": _node.id,
                                 "setups": ["a live bench"]}).encode("utf-8"))
check("a setup typed on the map is answered, not dropped", _code, 200)
check("the device really is in it now", _node.setups, ["a live bench"])
check("it survives in the store for the next scan",
      _store.lookup(_node, known=_store.load()), ["a live bench"])
check("and the window was told, through its own queue",
      any("a live bench" in m for m in list(app.messages.queue)), True)

_code, _ = _ask(_srv.base + "/setups",
                _json.dumps({"device": _node.id, "setups": []}).encode("utf-8"))
check("taking it out again is written too",
      (_code, _store.lookup(_node, known=_store.load())), (200, []))

_srv.stop()
app.map_server = None

print()
print("-- closing the window mid-scan is not an error --")
# Seen in the build log: a worker thread finishes its NetBox comparison
# after the window has gone and dies on root.after with "main thread is
# not in main loop". A GUI build has no console, so that traceback goes
# nowhere - and in the test log it sits on top of the real failures.
import threading as _threading

class _Window(object):
    """Only what _later touches, so the real window need not be rebuilt."""


_ran = []
_live = _Window()
_live.root = tk.Tk()
_live.root.withdraw()
App._later(_live, lambda: _ran.append("live"))
_live.root.update()
check("a live window still gets the work handed back", _ran, ["live"])

_closed = _Window()
_closed.root = _live.root
_closed.root.destroy()

_raised = []
try:
    App._later(_closed, lambda: _ran.append("closed"))
except Exception as _exc:          # noqa: BLE001 - the point is to see any
    _raised.append("%s: %s" % (type(_exc).__name__, _exc))
check("a closed one swallows it from the main thread", _raised, [])

_from_thread = []


def _worker():
    try:
        App._later(_closed, lambda: _ran.append("thread"))
    except Exception as _exc:      # noqa: BLE001
        _from_thread.append("%s: %s" % (type(_exc).__name__, _exc))


_t = _threading.Thread(target=_worker)
_t.start()
_t.join(5)
check("and from a worker thread, which is where it happened",
      _from_thread, [])
check("nothing was run after the window went", _ran, ["live"])

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all window checks passed")

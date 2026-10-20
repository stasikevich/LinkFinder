"""Checks for logical setups: a bench is a label a person puts on, and keeps.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import setups as store
from linkfinder.targets import setups as setups_in_file, split

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


HOSTS = os.path.join(tempfile.gettempdir(), "lf_hosts_test.txt")
STORE = os.path.join(tempfile.gettempdir(), "lf_setups_test.json")
io.open(HOSTS, "w", encoding="utf-8").write(
    "# two benches\n"
    "[bench-c]\n"
    "root@198.51.100.10\n"
    "198.51.100.11\n"
    "\n"
    "[bench 2]\n"
    "admin:secret@198.51.100.12\n")

print("-- a host list can name the bench each address stands on --")
hosts, logins, bad = split([], HOSTS)
check("every address is still a target", hosts,
      ["198.51.100.10", "198.51.100.11", "198.51.100.12"])
check("a heading is not mistaken for a host", any("[" in h for h in hosts), False)
check("per-host logins still work", sorted(logins), ["198.51.100.10", "198.51.100.12"])
check("and each address knows its setup", setups_in_file([], HOSTS),
      {"198.51.100.10": "bench-c", "198.51.100.11": "bench-c",
       "198.51.100.12": "bench 2"})
check("a plain list without headings behaves as before",
      setups_in_file(["203.0.113.5, 203.0.113.6"]), {})

print()
print("-- a label is put on several devices at once, and survives a rescan --")
if os.path.exists(STORE):
    os.remove(STORE)
PICKED = [{"id": "ip:198.51.100.10", "chassis_id": "", "mgmt_ips": ["198.51.100.10"]},
          {"id": "chassis:aa:bb:cc:00:00:01", "chassis_id": "aa:bb:cc:00:00:01",
           "mgmt_ips": []}]
check("one action marks the whole selection", store.remember(PICKED, ["bench-c"], STORE), 2)

FRESH = {"nodes": [
    {"id": "ip:198.51.100.10", "chassis_id": "", "mgmt_ips": ["198.51.100.10"]},
    {"id": "chassis:aa:bb:cc:00:00:01", "chassis_id": "AA:BB:CC:00:00:01",
     "mgmt_ips": ["198.51.100.250"]},
    {"id": "ip:198.51.100.99", "chassis_id": "", "mgmt_ips": ["198.51.100.99"]}]}
store.apply(FRESH, {"198.51.100.99": "bench 2"}, STORE)
check("the host is found again by its address", FRESH["nodes"][0]["setups"], ["bench-c"])
check("the switch is found again by its chassis id, whatever its address now is",
      FRESH["nodes"][1]["setups"], ["bench-c"])
check("a host named only by the list is stamped too",
      FRESH["nodes"][2]["setups"], ["bench 2"])
check("both names are offered to a picker", store.names(FRESH), ["bench 2", "bench-c"])

print()
print("-- a device can stand in two benches, and can be taken out --")
store.remember([PICKED[1]], ["bench-c", "bench 2"], STORE)
check("a shared uplink keeps both", store.lookup(PICKED[1], path=STORE),
      ["bench 2", "bench-c"])
store.forget([PICKED[1]], STORE)
check("and forgetting it leaves nothing behind", store.lookup(PICKED[1], path=STORE), [])
check("while the other device is untouched", store.lookup(PICKED[0], path=STORE),
      ["bench-c"])

print()
print("-- the map can be asked for one bench --")
from linkfinder.render_topology import _asset
PAGE = _asset("topology.html")
check("the page offers a setup picker", "id=\"setup\"" in PAGE, True)
check("and hides it when no setup is named", "setupbox" in PAGE, True)
check("a shared device stays visible under either name",
      "(data.setups || []).indexOf(want) >= 0" in PAGE, True)

print()
print("-- the window marks a whole selection, and keeps it over a rescan --")
try:
    import tkinter as tk
    _probe = tk.Tk()
    _probe.destroy()
except Exception as why:          # a build host without a display or tkinter
    print("skipped: no window available here (%s)" % type(why).__name__)
else:
    from linkfinder import gui as guimod
    from linkfinder.topology import Node, Topology

    GUI_STORE = os.path.join(tempfile.gettempdir(), "lf_setups_gui.json")
    if os.path.exists(GUI_STORE):
        os.remove(GUI_STORE)
    store.store_path = lambda path=None: path or GUI_STORE

    topo = Topology(seed="198.51.100.10")
    topo.nodes["ip:198.51.100.10"] = Node(
        id="ip:198.51.100.10", kind="host", name="node-a",
        mgmt_ips=["198.51.100.10"], state="scanned", depth=0)
    topo.nodes["chassis:aa:bb:cc:00:00:01"] = Node(
        id="chassis:aa:bb:cc:00:00:01", kind="switch", name="sw1",
        chassis_id="aa:bb:cc:00:00:01", state="unscanned", depth=1)

    # Nothing here may stop and wait for a person: a build has nobody to click.

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

    root = tk.Tk()
    root.withdraw()            # a test needs no window on screen
    root.report_callback_exception = lambda *_a: None
    app = guimod.App(root)
    root.update()
    app._topology_done(topo, "")
    root.update()
    check("every device is listed", len(app.topo_rows), 2)
    check("the list has a Setup column",
          "setup" in app.topo_tree.cget("columns"), True)
    check("and more than one row can be picked at once",
          str(app.topo_tree.cget("selectmode")), "extended")

    # Marking happens on the map now, so the test drives the route the map
    # drives: the window serves the page and answers it on loopback.
    import webbrowser

    webbrowser.open = lambda *_a, **_k: True
    app.open_live_map()
    check("the window serves the map it drew", bool(app.map_server), True)

    def mark(device, names):
        status, _kind, payload = app.map_server.route(
            "POST", app.map_server.base + "/setups",
            json.dumps({"device": device, "setups": names}).encode("utf-8"))
        root.update()
        return status, json.loads(payload.decode("utf-8"))

    status, said = mark("ip:198.51.100.10", ["bench-c", "bench 2"])
    check("the map's answer is accepted", status, 200)
    check("one action marked both", sorted(topo.nodes["ip:198.51.100.10"].setups),
          ["bench 2", "bench-c"])
    check("and the page is told what was saved", said.get("saved"),
          "ip:198.51.100.10")
    check("it was written down, not just shown",
          store.lookup({"id": "ip:198.51.100.10", "chassis_id": "",
                        "mgmt_ips": ["198.51.100.10"]}, path=GUI_STORE),
          ["bench 2", "bench-c"])

    again = Topology(seed="198.51.100.10")
    again.nodes["ip:198.51.100.10"] = Node(
        id="ip:198.51.100.10", kind="host", name="node-a",
        mgmt_ips=["198.51.100.10"], state="scanned", depth=0)
    app.target_setups = {}
    app._stamp_setups(again)
    check("a later scan finds the same device again",
          again.nodes["ip:198.51.100.10"].setups, ["bench 2", "bench-c"])

    mark("ip:198.51.100.10", [])
    check("and an empty answer takes them out",
          topo.nodes["ip:198.51.100.10"].setups, [])

    from tkinter import filedialog, messagebox
    filedialog.askopenfilename = lambda *_a, **_k: HOSTS
    messagebox.showinfo = lambda *_a, **_k: None
    app.load_per_host()
    check("a host list fills the target box",
          "198.51.100.12" in app.var["targets"].get(), True)
    check("and the setups it names are carried into the next map",
          app.target_setups.get("198.51.100.12"), "bench 2")

    from_file = Topology(seed="198.51.100.12")
    from_file.nodes["ip:198.51.100.12"] = Node(
        id="ip:198.51.100.12", kind="host", mgmt_ips=["198.51.100.12"],
        state="scanned", depth=0)
    app._stamp_setups(from_file)
    check("so a device listed under a heading is stamped without being marked",
          from_file.nodes["ip:198.51.100.12"].setups, ["bench 2"])
    try:
        root.destroy()
    except tk.TclError:
        pass

print()
print("-- the screenshot tool is part of the tree, not a one-off --")
import ast
SHOTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "make_shots.py")
check("it is there", os.path.exists(SHOTS), True)
ast.parse(io.open(SHOTS, encoding="utf-8").read())
check("and it compiles", True, True)


print()
print("-- a serial is the strongest thing to remember a device by --")
keys = store.keys_for({"id": "ip:198.51.100.10", "chassis_id": "aa:bb:cc:dd:ee:01",
                        "serial": "ABC1234", "mgmt_ips": ["198.51.100.10"]})
check("the serial comes first", keys[0], "serial:abc1234")
check("the chassis and the address still follow",
      keys[1:3], ["chassis:aa:bb:cc:dd:ee:01", "ip:198.51.100.10"])

MOVED_STORE = os.path.join(tempfile.gettempdir(), "lf_setup_serial.json")
if os.path.exists(MOVED_STORE):
    os.remove(MOVED_STORE)
store.remember([{"id": "ip:198.51.100.10", "serial": "ABC1234",
                  "mgmt_ips": ["198.51.100.10"]}], ["bench one"],
                path=MOVED_STORE)
later = store.lookup({"id": "ip:198.51.100.77", "serial": "ABC1234",
                       "mgmt_ips": ["198.51.100.77"]},
                      known=store.load(MOVED_STORE))
check("so a device that changed address keeps its setup", later, ["bench one"])

print()
# There used to be a marks file here: the map downloaded what you marked and
# somebody imported it. The window answers the map directly now, so the file,
# the reader and the import flag are all gone - and the store is written the
# moment a setup is typed.
print("-- a setup typed on the map is written straight into the store --")
STORE2 = os.path.join(tempfile.gettempdir(), "lf_setup_live.json")
if os.path.exists(STORE2):
    os.remove(STORE2)
store.remember([{"id": "ip:198.51.100.10", "serial": "ABC1234",
                 "mgmt_ips": ["198.51.100.10"]}], ["bench one", "spare"],
               path=STORE2)
store.remember([{"id": "chassis:usb:aaa1", "chassis_id": "usb:aaa1",
                 "serial": "aaa1"}], ["bench one"], path=STORE2)
check("a handset is remembered by its serial",
      store.lookup({"id": "chassis:usb:aaa1", "serial": "aaa1"},
                   known=store.load(STORE2)), ["bench one"])
check("and taking a device out of every setup is remembered too",
      (store.forget([{"id": "chassis:usb:aaa1", "serial": "aaa1"}], path=STORE2) > 0,
       store.lookup({"id": "chassis:usb:aaa1", "serial": "aaa1"},
                    known=store.load(STORE2))), (True, []))

print("-- the window and the command line must answer the same --")


class FakeNode(object):
    def __init__(self, node_id, serial="", chassis_id="", ips=None):
        self.id = node_id
        self.serial = serial
        self.chassis_id = chassis_id
        self.mgmt_ips = ips or []
        self.setups = []


class FakeTopology(object):
    def __init__(self, nodes):
        self.nodes = {n.id: n for n in nodes}


topo = FakeTopology([FakeNode("ip:198.51.100.10", serial="ABC1234",
                              ips=["198.51.100.10"]),
                     FakeNode("ip:198.51.100.50", serial="NOTHING")])
carried = store.stamp(topo, path=STORE2)
check("a remembered setup lands on a fresh crawl", carried, 1)
check("on the device it was marked on",
      topo.nodes["ip:198.51.100.10"].setups, ["bench one", "spare"])
check("and nothing is invented for a device nobody marked",
      topo.nodes["ip:198.51.100.50"].setups, [])
check("an empty store changes nothing",
      store.stamp(FakeTopology([FakeNode("x")]),
                   path=os.path.join(tempfile.gettempdir(), "lf_no_setups.json")), 0)


print()
print("-- the sheet a person actually edits --")
SHEET_MAP = {"nodes": [
    {"id": "ip:198.51.100.5", "name": "server-a", "serial": "SER-1",
     "mgmt_ips": ["198.51.100.5"], "kind": "host", "state": "scanned",
     "setups": ["bench 1"]},
    {"id": "chassis:aa:bb:cc:00:00:09", "name": "radio-9", "serial": "",
     "chassis_id": "aa:bb:cc:00:00:09", "mgmt_ips": [], "kind": "radio",
     "state": "unscanned", "setups": []}]}
SHEET_STORE = os.path.join(tempfile.gettempdir(), "lf_sheet_checks.json")
if os.path.exists(SHEET_STORE):
    os.remove(SHEET_STORE)

text = store.template_csv(SHEET_MAP)
rows = text.strip().split(chr(10))
check("a header and one row per device", len(rows), 3)
check("the setup is the first column, to type in",
      rows[0].split(",")[0], "setup")
check("and today's setups are already in it",
      rows[1].startswith("bench 1,"), True)

filled = chr(10).join([rows[0], rows[1],
                       "bench 2," + rows[2].split(",", 1)[1]])
marked, missing = store.mark_from_text(SHEET_MAP, filled, path=SHEET_STORE)
check("a typed setup is applied", len(missing), 0)
check("to the device on that row",
      SHEET_MAP["nodes"][1]["setups"], ["bench 2"])
check("and the one already set is left as it was",
      SHEET_MAP["nodes"][0]["setups"], ["bench 1"])

cleared = chr(10).join([rows[0], "," + rows[1].split(",", 1)[1],
                        "bench 2," + rows[2].split(",", 1)[1]])
store.mark_from_text(SHEET_MAP, cleared, path=SHEET_STORE)
check("clearing the cell takes the device out",
      SHEET_MAP["nodes"][0]["setups"], [])

check("a plain list is still understood",
      store.read_marks("198.51.100.5" + chr(10) + "# a note")[1], False)
check("and a sheet is told apart by its header",
      store.read_marks(text)[1], True)
lost, was_sheet = store.read_marks(rows[0] + chr(10) + "bench 9,ghost,,,,,")
check("a row naming nothing is still read", len(lost), 1)
_m, gone = store.mark_from_text(SHEET_MAP, rows[0] + chr(10)
                                + "bench 9,ghost,,,,,ip:10.9.9.9",
                                path=SHEET_STORE)
check("and reported back rather than guessed at", gone, ["ip:10.9.9.9"])
if os.path.exists(SHEET_STORE):
    os.remove(SHEET_STORE)

print()
if FAILURES:
    print("FAILURES:")
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all setup checks passed")

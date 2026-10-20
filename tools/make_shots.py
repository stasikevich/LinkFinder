"""Re-take the README screenshots from the window as it is today.

Run: python tools/make_shots.py

A screenshot goes stale the moment a tab moves, and a picture that contradicts
the product is worse than none. Driving the real window with invented devices
keeps the pictures honest and keeps a bench out of them: 198.51.100.x is the
RFC 5737 documentation range and every name below is made up.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys
import time
import tkinter as tk

from PIL import ImageGrab

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
OUT_DIR = os.path.join(HERE, "docs", "img")

from linkfinder.gui import App
from linkfinder.model import Neighbor, TargetResult


def nb(host, iface, mac, speed, role, switch, port, ip="", vlan="",
       source="networkctl"):
    return Neighbor(host=host, local_iface=iface, local_mac=mac,
                    local_speed=speed, local_role=role, local_mtu="1500",
                    switch=switch, port=port, mgmt_ip=ip, vlan=vlan,
                    proto="LLDP", source=source)


def host(target, name, ports):
    result = TargetResult(target=target, kind="linux", device_name=name)
    for port in ports:
        result.add(port)
    return result


def devices():
    """Three invented servers behind two invented switches."""
    return [
        host("198.51.100.10", "node-a", [
            nb("198.51.100.10", "eno1", "02:00:00:00:00:0a", "1000Mb/s", "mgmt",
               "sw-access-1", "1/1/13", vlan="100"),
            nb("198.51.100.10", "enp23s0f2", "02:00:00:00:00:0b", "10000Mb/s",
               "physical", "sw-core-1", "10GigabitEthernet 1/13",
               ip="198.51.100.1", source="tcpdump")]),
        host("198.51.100.11", "node-b", [
            nb("198.51.100.11", "eno1", "02:00:00:00:00:1a", "1000Mb/s", "mgmt",
               "sw-access-1", "1/1/12", vlan="100")]),
        host("198.51.100.12", "node-c", [
            nb("198.51.100.12", "eno1", "02:00:00:00:00:2a", "1000Mb/s", "mgmt",
               "sw-access-1", "1/1/7", vlan="100"),
            nb("198.51.100.12", "enp101s0f1", "02:00:00:00:00:2b", "10000Mb/s",
               "physical", "sw-core-1", "10GigabitEthernet 1/16",
               ip="198.51.100.1", source="tcpdump")]),
    ]


def add_findings(results):
    """A port the switch disagrees with, and one with nothing on the far end."""
    checked = results[0].neighbors[1]
    checked.extra["checks"] = {
        "mtu": {"verdict": "mismatch", "a": "9000", "b": "1500",
                "detail": "the switch port is 1500, this port is 9000"},
        "speed": {"verdict": "match", "a": "10000", "b": "10000"}}
    results[0].add(nb("198.51.100.10", "enp23s0f3", "02:00:00:00:00:0c",
                      "10000Mb/s", "physical", "", ""))


def shoot(name, health):
    results = devices()
    if health:
        add_findings(results)
    root = tk.Tk()
    # Timers still pending when the window closes are not a failure here.
    root.report_callback_exception = lambda *_a: None
    app = App(root)
    app.var["targets"].set(", ".join(r.target for r in results))
    if health:
        app.port_health.set(True)
    root.update()
    app._finish(results)
    rows = app.tree.get_children()
    if len(rows) > 1:
        app.tree.selection_set(rows[1])
        app.tree.focus(rows[1])
        app._show_port_detail()
    root.lift()
    root.attributes("-topmost", True)
    root.update()
    time.sleep(1.2)
    root.update()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    w, h = root.winfo_width(), root.winfo_height()
    path = os.path.join(OUT_DIR, name)
    ImageGrab.grab(bbox=(x, y - 31, x + w, y + h)).save(path)
    print("%-18s %s" % (name, path))
    try:
        root.destroy()
    except tk.TclError:
        pass          # a timer that outlived the window is not a failure


if __name__ == "__main__":
    shoot("neighbors.png", False)
    shoot("port-health.png", True)

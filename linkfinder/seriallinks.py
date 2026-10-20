"""Radios reached over a serial console become nodes on the map.

A radio on the bench is often reached only by a USB-to-serial console cable -
no network path, no MAC the fronthaul ever sees. serialports.capture() drives
each likely-radio COM/tty and, when one answers, records the name it printed on
its own shell. That answer is proof a radio is on the far end of that cable, so
this draws it: a radio node named by what it said, cabled to the host on the
COM/tty port it answered on, with the port as the label on the host side.

Unlike a fronthaul radio - keyed by the MAC it answers on over NETCONF - a
console radio has only the name it told us, so that name is its identity. Two
cables that report the same name are one radio with two consoles, not two
radios, so they fold onto a single node with a cable each.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from typing import Callable, Optional


def connect(topology, log: Optional[Callable[[str], None]] = None) -> int:
    """Draw a radio for every serial console port that answered on a host."""
    say = log or (lambda _m: None)
    added = 0
    # A snapshot: the loop adds radio nodes, which must not be walked as hosts.
    for node in list(topology.nodes.values()):
        ports = (getattr(node, "extra", None) or {}).get("serial") or {}
        if not isinstance(ports, dict):
            continue
        for rec in ports.values():
            if not isinstance(rec, dict) or not rec.get("answered"):
                continue
            name = str(rec.get("radio_name") or "").strip()
            if not name:
                continue
            com = str(rec.get("port") or "").strip()
            radio = topology.add_node(name=name, vendor=name, kind="radio",
                                      state="unscanned", source="serial-console")
            # The USB cable's own serial, so the node carries the same hardware
            # serial the host's Serial ports panel shows for that port. It is
            # the adapter's serial, not the radio's, so it stays in extra and
            # never becomes node.serial - a serial merge must not fire on it.
            cable = str(rec.get("serial") or "").strip()
            if cable:
                radio.extra.setdefault("console_cable_serial", cable)
            radio.extra.setdefault("reached_over", "serial console")
            # A name that happened to match the host folds onto it - never draw
            # a device cabled to itself.
            if radio.id == node.id:
                continue
            topology.add_edge(a=node.id, a_iface=com, b=radio.id, b_iface="",
                              proto="serial-console", source="serial-console",
                              reported_by=node.id)
            added += 1
    if added:
        say("  serial console: %d radio(s) drawn from a console that answered"
            % added)
    return added

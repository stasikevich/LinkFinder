"""The order evidence is believed in, written down once.

Several things can claim that two ports are cabled together, and they are
not equally good. A switch announcing itself over LLDP is near proof. A
MAC sitting in an IPv4 neighbour cache is a hint that happens to be right
most of the time. The rule has always been that the stronger evidence goes
first and a port it has settled is not revisited by anything weaker.

That rule used to live nowhere. Each caller imported the builders it
remembered and ran them in the order they were written in, so the crawl and
a plain host list drew different maps from the same facts - one of them
knew about PTP and the other did not. The order is the rule, so it belongs
in one list that both walk.

Rungs, strongest first:

  1. PTP - a clock hierarchy names its own parent, and says so on purpose.
  2. IPv6 neighbours - a link-local address is per cable, not per subnet.
  3. IPv4 ARP - the last rung, and only for ports nothing above settled.
  4. Serial console - a radio that answered on its console cable, drawn to
     the host on the COM/tty port it answered on.
  5. LACP partner - not a cable but a switch, named by its aggregator.

Then three that read the finished map rather than the probe output, so
they come after everything that can add to it.

Nothing here is fatal. A rung that cannot run says so and the rest carry
on, because a map missing one kind of edge is worth far more than no map.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional, Tuple

# Imported by name, on purpose. The first version of this list held module
# names as strings and reached them with __import__, which reads well and
# shipped a broken product: PyInstaller finds what a program imports by
# reading the source, a name assembled at run time is invisible to it, and
# three builders were simply left out of the binary. The map came out of
# the tool missing every ARP link and every PTP link, and said so only in
# a log nobody had open. An import statement is the thing that is seen.
from . import arplinks, consistency, ipmi, lag, neighbor6, ptp_links, seriallinks, vmlinks

# (what to call it in a message, the step, whether it needs the probe output)
RUNGS: Tuple[Tuple[str, Any, bool], ...] = (
    # A tap carrying a guest's own hardware address is not evidence about
    # a cable - it is the cable. Nothing weaker can contradict it, so it
    # goes first.
    ("virtual machine search", vmlinks.connect, False),
    ("ptp link search", ptp_links.connect, True),
    ("ptp clock roots", ptp_links.add_clock_roots, False),
    ("neighbour link search", neighbor6.connect, True),
    ("neighbour cache search", arplinks.connect, True),
    # A radio that answered on its console cable is a device for certain, named
    # by what it said. It reads the finished map's nodes, not the probe output.
    ("serial console search", seriallinks.connect, False),
    ("lacp partner search", lag.connect_partners, True),
    ("controller cross-check", ipmi.confirm_from_map, False),
    ("consistency check", consistency.find, False),
    ("lag folding", lag.fold, False),
)


def walk(topology, views: List[Tuple[str, Any]],
         log: Optional[Callable[[str], None]] = None) -> None:
    """Run every rung over this map, strongest evidence first."""
    say = log or (lambda _m: None)
    for label, step, wants_views in RUNGS:
        try:
            if wants_views:
                step(topology, views, log=say)
            else:
                step(topology, log=say)
        except Exception as problem:      # never lose a finished map over one
            say("  %s skipped: %s" % (label, problem))

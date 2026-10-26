"""Command line entry point.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from typing import List, Optional

from . import __copyright__, __license__, shared, store, vault
from .branding import signature
from .engine import (Options, discover_many, load_config,
                     per_host_from_config, targets_with_logins)
from .model import TargetResult, results_to_json
from .sshx import Creds
from .userinput import check_walk, cost_note


def _local_label(nb) -> str:
    name = nb.local_iface or "-"
    if nb.local_role == "mgmt":
        return name + " [mgmt]"
    if nb.local_role == "mgmt-leg":
        return name + " [mgmt-leg]"
    return name


def _checks_cell(nb) -> str:
    from .linkcheck import summarise

    return summarise(nb.extra.get("checks") or {}) or "-"


COLUMNS = [
    ("target", "TARGET", lambda n: n.host),
    ("iface", "LOCAL PORT", lambda n: _local_label(n)),
    ("switch", "SWITCH", lambda n: n.switch or "-"),
    ("port", "SWITCH PORT", lambda n: n.best_port or "-"),
    ("switchip", "SWITCH IP", lambda n: n.mgmt_ip or "-"),
    ("desc", "PORT DESC", lambda n: n.port_desc or "-"),
    ("vlan", "VLAN", lambda n: n.vlan or "-"),
    ("via", "VIA", lambda n: "/".join(x for x in (n.proto, n.source) if x) or "-"),
    ("checks", "CHECKS", lambda n: _checks_cell(n)),
    ("ptp", "PTP", lambda n: (n.extra.get("ptp") or {}).get("summary") or "-"),
    ("peer", "PEER", lambda n: n.extra.get("ptp_peer") or "-"),
    ("conf", "CONF", lambda n: n.confidence),
    ("note", "NOTE", lambda n: n.note or "-"),
]


def render_table(results: List[TargetResult], wide: bool = False) -> str:
    rows = []
    for result in results:
        for nb in result.neighbors:
            rows.append([getter(nb) for _k, _h, getter in COLUMNS])
    if not rows:
        return ""
    headers = [h for _k, h, _g in COLUMNS]
    keys = [k for k, _h, _g in COLUMNS]
    if not wide:
        drop = {"desc", "note"}
        for optional in ("checks", "ptp", "peer"):
            column = keys.index(optional)
            if all(row[column] == "-" for row in rows):
                drop.add(optional)
        keep = [i for i, k in enumerate(keys) if k not in drop]
        headers = [headers[i] for i in keep]
        rows = [[r[i] for i in keep] for r in rows]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    limit = 44
    widths = [min(w, limit) for w in widths]

    def line(cells):
        out = []
        for i, cell in enumerate(cells):
            text = str(cell)
            if len(text) > widths[i]:
                text = text[:widths[i] - 1] + "…"
            out.append(text.ljust(widths[i]))
        return "  ".join(out).rstrip()

    parts = [line(headers), "  ".join("-" * w for w in widths)]
    parts.extend(line(r) for r in rows)
    return "\n".join(parts)


def render_csv(results: List[TargetResult]) -> str:
    buf = io.StringIO()
    fields = ["target", "device", "serial", "setup",
              "kind", "local_iface", "local_mac", "local_speed", "local_parent",
              "switch", "switch_ip", "switch_port", "switch_port_id_raw", "port_desc", "vlan",
              "chassis_id", "switch_model", "proto", "source", "confidence", "note"]
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(fields)
    from . import setups as setup_store

    for result in results:
        named = setup_store.lookup({"id": "ip:%s" % result.target,
                                    "serial": result.device_serial or "",
                                    "chassis_id": result.device_chassis or "",
                                    "mgmt_ips": [result.target]}) or []
        for nb in result.neighbors:
            writer.writerow([nb.host, result.device_name or "",
                             result.device_serial or "", ", ".join(named),
                             result.kind, nb.local_iface, nb.local_mac, nb.local_speed,
                             nb.local_parent, nb.switch, nb.mgmt_ip, nb.best_port, nb.port,
                             nb.port_desc, nb.vlan, nb.chassis_id, nb.sys_desc, nb.proto,
                             nb.source, nb.confidence, nb.note])
    return buf.getvalue()


def topology_confirm(auto: bool):
    """Confirm mode is the default; --auto-discover is the explicit opt-out."""
    def ask(depth, candidates):
        if auto:
            return True
        print("", file=sys.stderr)
        print("Found %d new network device(s) at depth %d:"
              % (len(candidates), depth), file=sys.stderr)
        for ip, label in candidates[:20]:
            print("   %-16s %s" % (ip, label), file=sys.stderr)
        if len(candidates) > 20:
            print("   ... and %d more" % (len(candidates) - 20), file=sys.stderr)
        if not sys.stdin.isatty():
            print("Not a terminal, so stopping here. Use --auto-discover to keep going.",
                  file=sys.stderr)
            return False
        try:
            answer = input("Add and scan discovered neighbours? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")
    return ask


def render_topology_table(topology) -> str:
    headers = ["DEPTH", "DEVICE", "KIND", "STATE", "MANAGEMENT IP", "CHASSIS"]
    rows = []
    for node in sorted(topology.nodes.values(),
                       key=lambda n: (99 if n.depth is None else n.depth, n.label)):
        rows.append(["-" if node.depth is None else str(node.depth), node.label,
                     node.kind, node.state, ", ".join(node.mgmt_ips) or "-",
                     node.chassis_id or "-"])
    if not rows:
        return ""
    widths = [min(40, max(len(h), *(len(r[i]) for r in rows)))
              for i, h in enumerate(headers)]

    def line(cells):
        return "  ".join(str(c)[:widths[i]].ljust(widths[i])
                         for i, c in enumerate(cells)).rstrip()

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out.extend(line(r) for r in rows)
    return "\n".join(out)


def render_summary(results: List[TargetResult]) -> str:
    lines = []
    # A host where nothing resolved used to print one line and swallow every
    # port note. That is exactly the host with something to say: a cable no
    # protocol reports is found per port, not per host.
    for result in results:
        unresolved = [nb for nb in result.neighbors if not nb.resolved]
        if not unresolved:
            # A host that produced no port at all is the one most worth
            # naming: nothing else in the output mentions it, because every
            # other line is per port, and it never got as far as having one.
            if result.error:
                lines.append("  %-16s %s" % (result.target, result.error))
            continue
        told = [nb for nb in unresolved if nb.note]
        if not told:
            lines.append("  %-16s %s" % (result.target,
                                         result.error or "no neighbour found"))
            continue
        for nb in told:
            lines.append("  %-16s %s: %s"
                         % (result.target, _local_label(nb), nb.note))
    if not lines:
        return ""
    return "Not resolved:\n" + "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="linkfinder",
        description="%s - find which switch and which switch port every link "
                    "is plugged into." % signature(),
        epilog="Defaults can live in ~/.linkfinder.json "
               "(keys: user, password, bmc_user, bmc_password, community, switches, "
               "switch_user, switch_password).")
    p.add_argument("targets", nargs="*", help="host IPs or names (space or comma separated)")
    p.add_argument("--targets-file", default="", help="file with one target per line")
    p.add_argument("-m", "--mode", default="auto",
                   choices=["auto", "ssh", "linux", "esxi", "windows", "windows-local", "bmc",
                            "switch", "switch-cli", "pdu"],
                   help="force an access path instead of auto-detecting it")
    p.add_argument("-u", "--user", default="", help="SSH username")
    p.add_argument("-p", "--password", default="", help="SSH password (also used for sudo)")
    p.add_argument("--sudo-password", default="", help="sudo password if it differs")
    p.add_argument("--become-password", default="",
                   help="root password for `su -`, for a login that may not "
                        "sudo. Without it such a host can be logged into but "
                        "never listened on")
    p.add_argument("-i", "--key", default="", help="SSH private key file")
    p.add_argument("--ssh-port", type=int, default=22,
                   help="SSH port to use when a host does not name its own "
                        "(default 22). Per-host ports live in the defaults "
                        "file, so one appliance on 2222 does not move the rest")
    p.add_argument("--bmc-user", default="", help="BMC/Redfish user (defaults to --user)")
    p.add_argument("--bmc-password", default="", help="BMC/Redfish password")
    p.add_argument("--bmc-port", type=int, default=443)
    p.add_argument("--no-enable-lldp", action="store_true",
                   help="never PATCH the BMC to switch its LLDP receiver on")
    p.add_argument("--iface", action="append", default=None,
                   help="limit to these interfaces (repeatable); bonds expand to their slaves")
    p.add_argument("--no-ptp", dest="ptp", action="store_false",
                   help="skip the PTP read; by default the timing role of each "
                        "port is reported, and PTP is used to name the far end "
                        "of a cable that carries no LLDP")
    p.add_argument("--no-identify", dest="identify", action="store_false",
                   help="do not ask an unknown device what it is. The probe "
                        "opens a TCP connection to it and reads the version "
                        "string it offers; it never logs in and never writes, "
                        "but it is the only thing here that touches a device "
                        "nobody gave us credentials for")
    p.add_argument("--no-neighbour-probe", dest="neighbours", action="store_false",
                   help="never send anything: skip the one IPv6 query that finds a "
                        "cable between two hosts when no protocol will say so")
    p.add_argument("--netbox", action="store_true",
                   help="look the scanned devices up in NetBox: their setups "
                        "come from the tags you name, and anything the two "
                        "disagree about is reported. Read-only - it never "
                        "writes")
    p.add_argument("--netbox-url", default="",
                   help="NetBox address (or LINKFINDER_NETBOX_URL, or the "
                        "defaults file)")
    p.add_argument("--netbox-token", default="",
                   help="NetBox API token (or LINKFINDER_NETBOX_TOKEN). On "
                        "NetBox 4.5 that is nbt_<key>.<secret> - the secret "
                        "half alone is refused as an invalid token")
    p.add_argument("--netbox-insecure", action="store_true",
                   help="do not verify the NetBox TLS certificate, for an "
                        "internal CA")
    p.add_argument("--setup-tag", action="append", default=[], metavar="TAG",
                   help="a NetBox tag that names a Setup, repeatable. Nothing "
                        "is a Setup unless it is named here: this estate has "
                        "985 tags and most are teams and programmes, not "
                        "benches. --netbox-tags lists the ones your scan saw")
    p.add_argument("--netbox-tags", action="store_true",
                   help="list the NetBox tags carried by the devices this scan "
                        "matched, so you can pick which of them are Setups")
    p.add_argument("--switch-ports", action="store_true",
                   help="show how each switch's port names line up with "
                        "NetBox, with the evidence. Nothing is assumed until "
                        "you confirm it")
    p.add_argument("--confirm-switch-ports", default="", metavar="SWITCH",
                   help="accept the proposed rule for this switch ('*' for "
                        "every certain one). One yes covers all its ports")
    p.add_argument("--netbox-proposals", default="", metavar="FILE",
                   help="write the corrections a person could apply to NetBox. "
                        "Nothing is sent; only where a device is and whether "
                        "it is alive is ever proposed, never what it is")
    p.add_argument("--adb", action="store_true",
                   help="name the handsets plugged into a host by asking adb on "
                        "it. USB devices are always read - that is free and "
                        "changes nothing - but adb may start its own server on "
                        "the host, so asking it is a choice you make")
    p.add_argument("--read-serial", dest="read_serial", action="store_true",
                   default=True,
                   help="ask each serial port that looks like a radio console "
                        "who it is: open it, log in with the console logins the "
                        "build carries if it asks, and read the radio's name off "
                        "its shell. On by default; a port that answers is drawn "
                        "as a radio cabled to the host. A rack server is skipped "
                        "(a radio console cable lives on a bench NUC/desktop, not "
                        "a server)")
    p.add_argument("--no-read-serial", dest="read_serial", action="store_false",
                   help="do not touch the serial ports at all - only list which "
                        "ports exist, never open one")
    p.add_argument("--serial-baud", type=int, default=115200, metavar="BAUD",
                   help="the rate the serial console probe talks at (default 115200)")
    p.add_argument("--mgmt-only", action="store_true",
                   help="only the interface carrying the target IP (old LLDP_Checker behaviour)")
    p.add_argument("-t", "--seconds", type=int, default=90,
                   help="capture window when a capture is needed (default 90: "
                        "one CDP cycle of 60s plus margin; LLDP ticks every 30s). "
                        "Lower for a quicker scan, higher to be sure of a slow "
                        "advertiser")
    p.add_argument("--switch", action="append", default=[],
                   help="switch to ask over SNMP, repeatable; use ip@community to override")
    p.add_argument("--community", default="", help="SNMP v2c community (default public)")
    p.add_argument("--switch-user", default="", help="switch SSH user, for the CLI fallback")
    p.add_argument("--switch-password", default="", help="switch SSH password")
    p.add_argument("--mac", default="", help="MAC to look for switch-side instead of resolving it")

    health = p.add_argument_group("port status and health (opt-in)")
    health.add_argument("--port-health", action="store_true",
                        help="additionally read the switch port table over SNMP and judge "
                             "each port; off by default, and nothing extra is read without it")
    health.add_argument("--port-health-interval", type=float, default=3.0, metavar="SECONDS",
                        help="gap between the two counter reads used to tell a growing "
                             "error from an old one (default 3)")
    health.add_argument("--expect-speed", type=int, default=0, metavar="MBPS",
                        help="flag a port as degraded when it negotiates below this")
    health.add_argument("--all-interfaces", action="store_true",
                        help="include logical interfaces, not just physical ports")
    p.add_argument("-j", "--workers", type=int, default=8, help="parallel targets (default 8)")
    p.add_argument("--json", dest="as_json", action="store_true", help="emit JSON")
    p.add_argument("--csv", dest="as_csv", default="", metavar="FILE",
                   help="write a CSV inventory ('-' for stdout)")
    p.add_argument("-w", "--wide", action="store_true", help="show every column")
    p.add_argument("-q", "--quiet", action="store_true", help="results only, no progress log")
    p.add_argument("--gui", action="store_true", help="open the window instead")
    p.add_argument("--shared", default="", metavar="FOLDER",
                   help="keep setups and the maps it remembers in a folder "
                        "the team can reach, instead of this machine only. "
                        "Set it once in the defaults file under 'shared'")
    p.add_argument("--netbox-find", action="append", default=[],
                   metavar="SERIAL-OR-TAG",
                   help="look one thing up in NetBox: a serial, a tag, a "
                        "name or an asset tag. It works out which and says "
                        "how it matched. Repeatable")
    p.add_argument("--from-netbox", default="", metavar="SERIAL-OR-TAG",
                   help="take the addresses NetBox holds for that serial or "
                        "tag and scan them. You have a bench name, not a "
                        "list of addresses - this is what removes the typing")
    p.add_argument("--where", action="store_true",
                   help="say where everything this tool remembers is kept")
    p.add_argument("--help-page", nargs="?", const="", default=None,
                   metavar="FILE",
                   help="open the full help in your browser, or write it to "
                        "FILE. It is one self-contained page and needs no "
                        "network")
    p.add_argument("-V", "--version", action="version",
                   version="%s - %s licence - %s"
                           % (signature(), __license__, __copyright__))
    p.add_argument("--list-switches", action="store_true",
                   help="print the switches the tool has remembered, then exit")
    p.add_argument("--forget-switches", nargs="?", const="*", metavar="IP",
                   help="forget one remembered switch, or all of them when given no value")
    # The window could do this and the command line could not, which on a
    # server means the saved logins cannot be removed at all. It went
    # unnoticed because the parity table pointed at the wrong button.
    p.add_argument("--forget-logins", action="store_true",
                   help="remove the saved per-host logins and extra "
                        "credentials from the defaults file, then exit")
    p.add_argument("--learn", action="store_true",
                   help="remember the switches found (addresses only) in "
                        "~/.linkfinder-switches.json and reuse them next time; off by default")
    topo = p.add_argument_group("topology discovery")
    topo.add_argument("--topology", metavar="SEED",
                      help="discover the topology recursively starting from this "
                           "device; several seeds may be given, comma separated")
    topo.add_argument("--max-depth", type=int, default=0,
                      help="seed is depth 0; devices at this depth are still probed "
                           "but their neighbours are not queued. Default 0: scan "
                           "only the hosts you name and draw their neighbours from "
                           "what those hosts report, without scanning the neighbours. "
                           "Raise it to walk outward and discover more")
    topo.add_argument("--max-devices", type=int, default=25,
                      help="stop after probing this many devices (default 25)")
    topo.add_argument("--scope", action="append", default=[], metavar="CIDR",
                      help="only connect to devices inside these networks, repeatable; "
                           "neighbours outside are still drawn, just not scanned")
    topo.add_argument("--auto-discover", action="store_true",
                      help="keep descending without asking (confirm mode is the default)")
    topo.add_argument("--topology-json", default="", metavar="FILE",
                      help="write the topology JSON here")
    topo.add_argument("--topology-html", default="", metavar="FILE",
                      help="write a self-contained Cytoscape page here")
    topo.add_argument("--since-last", action="store_true",
                      help="say what moved since the previous map this tool "
                           "drew - a cable that moved is invisible in any "
                           "single scan")
    topo.add_argument("--topology-csv", default="", metavar="FILE",
                      help="write the map as one row per link, for a "
                           "spreadsheet ('-' for stdout)")
    topo.add_argument("--mark-setup", default="", metavar="NAME",
                      help="put a whole list of devices in one setup at once. "
                           "Use an empty name to take them out")
    topo.add_argument("--mark-list", default="", metavar="FILE",
                      help="the devices to mark. Either the sheet from "
                           "--setup-sheet, where each row carries its own "
                           "setup, or a plain list of addresses, serials, "
                           "MACs or names ('-' reads it from stdin)")
    topo.add_argument("--setup-sheet", default="", metavar="FILE",
                      help="write every device in the map as a spreadsheet "
                           "with its setup ready to be typed. Edit it and "
                           "hand it back with --mark-list")
    topo.add_argument("--setup-diff", action="append", default=[],
                      metavar="SETUP",
                      help="one bench, both ways: what NetBox records "
                           "against what is actually there. Repeatable. The "
                           "NetBox tag is assumed to be the same word; write "
                           "'my bench=their-tag' when it is not")
    topo.add_argument("--suspicious", action="store_true",
                      help="what does not add up on this map: one address "
                           "with two owners, one MAC in two places, a ring "
                           "in the cabling, a cable whose ends disagree")
    topo.add_argument("--out-of-reach", action="append", default=[],
                      metavar="ROLE",
                      help="a NetBox role this tool has no way into - an "
                           "attenuator, a chamber. Those stop being "
                           "reported as missing. '*' accepts every class "
                           "suggested; '-CPE' takes one back")
    topo.add_argument("--setup-diff-csv", default="", metavar="FILE",
                      help="write that comparison as a spreadsheet "
                           "('-' for stdout)")
    topo.add_argument("--map", default="", metavar="FILE",
                      help="the map these act on; the newest one this tool "
                           "kept is used when this is left out")
    topo.add_argument("--serve-map", default="", metavar="FILE",
                      help="open a saved map in your browser with this "
                           "process answering it, so setups typed on the "
                           "picture are written straight away. Loopback only")
    topo.add_argument("--render-topology", nargs=2, metavar=("JSON", "HTML"),
                      help="only render: turn an existing topology JSON into HTML")
    topo.add_argument("--compare-scan", nargs=2, metavar=("SCAN", "MAP"),
                      help="check a saved scan (--json) against a topology "
                           "JSON: every link the scan resolved must be on the "
                           "map. Exits 3 if any is missing")
    topo.add_argument("--watch", metavar="TOPOLOGY_JSON",
                      help="fold a topology JSON into the record of how things "
                           "stood last time and print only what changed. Exits 4 "
                           "if anything went down or out of sight")
    topo.add_argument("--watch-state", default="", metavar="FILE",
                      help="where that record lives (default: beside your other "
                           "local state)")
    topo.add_argument("--loop-check", action="store_true",
                      help="sample each switch twice to see whether spanning "
                           "tree is churning right now; off by default "
                           "because it reads the forwarding table twice")
    topo.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"), default=None,
                      help="compare two topology JSON files and report what moved")
    topo.add_argument("--merge-topology", nargs="+", default=[], metavar="JSON",
                      help="combine several topology files into one site map")
    topo.add_argument("--topology-resume", default="", metavar="JSON",
                      help="carry an earlier scan forward and only fill in the gaps")
    topo.add_argument("--topology-workers", type=int, default=4,
                      help="devices interrogated in parallel per level (default 4)")
    p.add_argument("--cred", action="append", default=[], metavar="USER:PASSWORD",
                   help="an extra credential set to try when the first is rejected, "
                        "repeatable")
    p.add_argument("--remember-credentials", action="store_true",
                   help="with --learn, also store the SNMP community that worked; off by default")
    return p


def options_from_args(args: argparse.Namespace) -> Options:
    cfg = load_config()

    def pick(cli_value, cfg_key, env_key="", default=""):
        if cli_value:
            return cli_value
        if env_key and os.environ.get(env_key):
            return os.environ[env_key]
        return cfg.get(cfg_key, default)

    ssh = Creds(user=pick(args.user, "user", "LINKFINDER_USER"),
                password=pick(args.password, "password", "LINKFINDER_PASSWORD"),
                key_file=pick(args.key, "key", "LINKFINDER_KEY"),
                port=args.ssh_port,
                sudo_password=pick(args.sudo_password, "sudo_password"),
                become_password=pick(args.become_password, "become_password"))
    switch_creds = Creds(user=pick(args.switch_user, "switch_user"),
                         password=pick(args.switch_password, "switch_password"))
    extra_creds = []
    for spec in list(args.cred):
        user, _sep, password = str(spec).partition(":")
        if user:
            extra_creds.append(Creds(user=user.strip(), password=password,
                                     port=args.ssh_port))
    for entry in (cfg.get("creds") or []):
        if isinstance(entry, dict) and entry.get("user"):
            extra_creds.append(Creds(user=entry["user"], password=entry.get("password", ""),
                                     key_file=entry.get("key", ""), port=args.ssh_port))
    switches = list(args.switch) or list(cfg.get("switches", []))
    return Options(
        mode=args.mode, ssh=ssh, per_host=per_host_from_config(cfg),
        bmc_user=pick(args.bmc_user, "bmc_user", "LINKFINDER_BMC_USER"),
        bmc_password=pick(args.bmc_password, "bmc_password", "LINKFINDER_BMC_PASSWORD"),
        bmc_port=args.bmc_port, enable_lldp=not args.no_enable_lldp,
        community=pick(args.community, "community", "LINKFINDER_COMMUNITY", "public"),
        switches=switches, switch_creds=switch_creds, seconds=args.seconds,
        ifaces=args.iface, mgmt_only=args.mgmt_only, mac=args.mac,
        workers=args.workers, verbose=not args.quiet, learn=args.learn,
        remember_credentials=args.remember_credentials, cred_list=extra_creds,
        ptp=args.ptp, neighbours=args.neighbours, adb=args.adb,
        read_serial=args.read_serial, serial_baud=args.serial_baud,
                   identify=args.identify,
        port_health=args.port_health, port_health_interval=args.port_health_interval,
        expect_speed=args.expect_speed, all_interfaces=args.all_interfaces)


def say_drift(data, args) -> None:
    """What moved since the previous map, before this one joins the history."""
    from . import history

    report = history.drift(data)
    if report.get("note") and not report.get("changes"):
        print("since the last map: %s" % report["note"], file=sys.stderr)
    elif report.get("changes"):
        print("")
        print("Since the last map (%s):" % (report.get("summary") or "changes"))
        for change in report["changes"]:
            print("  %-16s %s%s" % (change["kind"], change["what"],
                                    "  " + change["detail"]
                                    if change.get("detail") else ""))
    history.keep(data)


def write_topology(topology, args, label: str = "topology") -> None:
    """Never silently lose a scan: fall back to a temp file when no path is given."""
    import tempfile

    from .render_topology import write_html

    json_path, html_path = args.topology_json, args.topology_html
    table_path = getattr(args, "topology_csv", "")
    if not json_path and not html_path:
        base = os.path.join(tempfile.gettempdir(), "linkfinder.%s" % label)
        json_path, html_path = base + ".json", base + ".html"
        # The page is for looking at; the table and the data are what the
        # work is done with. When nobody said where, all three go together.
        table_path = table_path or base + ".csv"
        print("no output path given - saving under %s" % tempfile.gettempdir(),
              file=sys.stderr)
    if json_path:
        with open(json_path, "w", encoding="utf-8") as handle:
            handle.write(topology.to_json())
        print("topology JSON written to %s" % json_path, file=sys.stderr)
    if html_path:
        write_html(topology.to_dict(), html_path)
        print("topology page written to %s" % html_path, file=sys.stderr)
    if getattr(args, "since_last", False):
        say_drift(topology.to_dict(), args)
    if table_path:
        from .render_topology import topology_csv

        text = topology_csv(topology.to_dict())
        if table_path == "-":
            write_utf8(text)
        else:
            with open(table_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            print("topology table written to %s" % table_path, file=sys.stderr)


def report_topology(topology, args, label: str = "topology") -> int:
    from . import setups as setup_store
    carried = setup_store.stamp(topology, getattr(args, "_setups", None))
    if carried:
        print("setup names carried onto %d device(s)" % carried, file=sys.stderr)

    if getattr(args, "netbox", False):
        ask_netbox(topology, args)

    table = render_topology_table(topology)
    if table:
        print(table)
    rings = (getattr(topology, "extra", {}) or {}).get("rings") or []
    if rings:
        from .loops import render as render_loops
        print("")
        print(render_loops(rings))
    print("", file=sys.stderr)
    print(topology.summary(), file=sys.stderr)
    if topology.stopped_because:
        print("stopped because: %s" % topology.stopped_because, file=sys.stderr)
    write_topology(topology, args, label=label)
    return 0 if topology.nodes else 1


def run_merge(args) -> int:
    from .topology import merge_files

    combined = merge_files(args.merge_topology)
    print("merged %d file(s)" % len(args.merge_topology), file=sys.stderr)
    return report_topology(combined, args, label="merged")


def run_port_health(args, opts, hosts, results=None) -> None:
    """Opt-in only. A failure here is reported and then ignored - the discovery
    result the user asked for is already on screen."""
    from .port_health import collect_safely, render_report, switches_worth_asking
    from .probe_snmp import build_switches

    specs = switches_worth_asking(results=results, explicit=args.switch, targets=hosts)
    if not specs:
        print("", file=sys.stderr)
        print("Port health: no switch to ask. Nothing we found publishes a usable "
              "management address; name one with --switch.", file=sys.stderr)
        return
    answered = False
    for switch in build_switches(specs, opts.community):
        if not switch.identify():
            continue
        answered = True
        report = collect_safely(
            switch, interval=args.port_health_interval, expect_speed=args.expect_speed,
            physical_only=not args.all_interfaces,
            log=(lambda m: print(m, file=sys.stderr)) if opts.verbose else None)
        print("")
        print(render_report(report))
    if not answered:
        print("", file=sys.stderr)
        print("Port health: none of %s answered SNMP (check --community, or name "
              "the switch with --switch)" % ", ".join(specs), file=sys.stderr)


def run_loop_report(args, opts, results) -> None:
    """Rings come free with a topology; the churn sample is asked for."""
    from .loops import render as render_loops, sample_instability
    from .port_health import switches_worth_asking
    from .probe_snmp import build_switches

    churn = []
    if args.loop_check:
        for address in switches_worth_asking(results=results,
                                             explicit=args.switch, targets=[]):
            switch = build_switches([address], opts.community)[0]
            if switch.identify():
                churn.append(sample_instability(switch))
    text = render_loops([], churn)
    if text:
        print("")
        print(text)


def run_ptp_report(results, log=None) -> None:
    """The clock tree, after the per-port table has had its say."""
    from .ptp import clock_identity_to_mac, known_port_index, normalise_mac
    from .ptp_tree import build, render

    domains = build(results)
    if not domains:
        return
    index = known_port_index(results)

    def name_of(clock):
        hit = index.get(normalise_mac(clock_identity_to_mac(clock)))
        return ("%s %s" % hit).strip() if hit else ""

    text = render(domains, name_of=name_of)
    if text:
        print("")
        print(text)


def run_switch_ptp(args, opts, hosts, results=None) -> None:
    """Ask each switch found what it says about PTP, where it says anything.

    This is the rung that needs the SNMP community rather than nothing at all,
    so it only runs over devices already identified as switches, and a device
    with no PTP objects is reported as exactly that.
    """
    from .port_health import switches_worth_asking
    from .probe_snmp import build_switches
    from .ptp_snmp import read_switch_ptp_safely, render as render_switch_ptp

    specs = switches_worth_asking(results=results, explicit=args.switch, targets=[])
    if not specs:
        return
    reports = []
    for switch in build_switches(specs, opts.community):
        if not switch.identify():
            continue
        reports.append(read_switch_ptp_safely(switch))
    text = render_switch_ptp(reports)
    if text:
        print("")
        print(text)


def run_topology(args) -> int:
    """Seed -> crawl -> topology JSON -> optional HTML, in that order."""
    from .crawl import Limits, crawl
    from .topology import load_topology

    opts = options_from_args(args)
    log = (lambda m: print(m, file=sys.stderr)) if opts.verbose else None
    settings, problems = check_walk(args.max_depth, args.max_devices,
                                    list(args.scope))
    if problems:
        for why in problems:
            print("  %s" % why, file=sys.stderr)
        print("nothing has been contacted", file=sys.stderr)
        return 2
    limits = Limits(**settings)
    print(cost_note(settings["max_depth"], settings["max_devices"],
                    settings["scope"]), file=sys.stderr)
    if opts.verbose:
        print("topology from %s: max depth %d, max devices %d, scope %s, %s, %d in parallel"
              % (args.topology, limits.max_depth, limits.max_devices,
                 ", ".join(limits.scope) or "any",
                 "auto-discover" if args.auto_discover else "confirm each level",
                 args.topology_workers), file=sys.stderr)

    previous = None
    if args.topology_resume:
        previous = load_topology(args.topology_resume)
        print("resuming from %s" % args.topology_resume, file=sys.stderr)

    seeds, per_host = targets_with_logins(
        [args.topology], log=lambda m: print(m, file=sys.stderr))
    if not seeds:
        print("no usable seed address", file=sys.stderr)
        return 2
    if per_host:
        merged = dict(getattr(opts, "per_host", {}) or {})
        merged.update(per_host)
        opts.per_host = merged

    topology = crawl(seeds, opts, limits=limits,
                     confirm=topology_confirm(args.auto_discover), log=log,
                     workers=args.topology_workers, previous=previous)
    if opts.verbose:
        print("", file=sys.stderr)
    return report_topology(topology, args)


def chosen_map(args):
    """The map an action works on: the one named, else the newest kept."""
    import json

    from . import history

    named = getattr(args, "map", "") or ""
    if named:
        try:
            with io.open(named, "r", encoding="utf-8") as handle:
                return json.load(handle), named
        except (OSError, ValueError) as why:
            print("could not read %s: %s" % (named, why), file=sys.stderr)
            return None, named
    kept = history.previous()
    if kept is None:
        print("no map to work on - draw one first, or name it with --map",
              file=sys.stderr)
    return kept, "the newest map kept"


def write_setup_sheet(args) -> int:
    """Hand the person a filled-in sheet rather than a blank page."""
    from . import setups as setup_store

    topology, where = chosen_map(args)
    if topology is None:
        return 2
    text = setup_store.template_csv(topology)
    if args.setup_sheet == "-":
        write_utf8(text)
        return 0
    try:
        with open(args.setup_sheet, "w", encoding="utf-8", newline="") as out:
            out.write(text)
    except OSError as why:
        print("could not write %s: %s" % (args.setup_sheet, why),
              file=sys.stderr)
        return 2
    print("%d device(s) written to %s (from %s)"
          % (len(topology.get("nodes") or []), args.setup_sheet, where),
          file=sys.stderr)
    print("Type a setup next to the rows that need one, save, then:",
          file=sys.stderr)
    print("   --mark-list %s" % args.setup_sheet, file=sys.stderr)
    return 0


def mark_a_setup(args) -> int:
    """Put a list of devices in one setup, in one go."""
    from . import setups as setup_store

    topology, where = chosen_map(args)
    if topology is None:
        return 2
    if args.mark_list == "-":
        lines = sys.stdin.read().splitlines()
    elif args.mark_list:
        try:
            with io.open(args.mark_list, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError as why:
            print("could not read %s: %s" % (args.mark_list, why),
                  file=sys.stderr)
            return 2
    else:
        print("--mark-setup needs --mark-list", file=sys.stderr)
        return 2

    names = [n.strip() for n in args.mark_setup.split(",") if n.strip()]
    text = chr(10).join(lines)
    marked, missing = setup_store.mark_from_text(topology, text, names)
    _pairs, was_sheet = setup_store.read_marks(text)
    print("%s %d device(s)%s (from %s)"
          % ("read" if was_sheet else ("marked" if names else "took"),
             len(marked),
             "" if was_sheet else " in %s" % (", ".join(names) or "no setup"),
             where), file=sys.stderr)
    for name in marked:
        print("   %s" % name)
    if missing:
        print("")
        print("not recognised in that map, so not marked:")
        for line in missing:
            print("   %s" % line)
    return 0


def show_suspicious(args) -> int:
    """What does not add up, from a map already drawn."""
    from . import suspicious

    # chosen_map hands back (record, where it came from) - taking only the
    # first was the whole of the failure the honest report just surfaced.
    topology, _where = chosen_map(args)
    if topology is None:
        return 2
    found = suspicious.gather(topology=topology)
    print("")
    print(suspicious.summary(found))
    for one in found:
        print("")
        print("  %-18s %s" % (one.get("kind", ""), one.get("what", "")))
        if one.get("verdict") or one.get("confidence"):
            print("  %-18s %s%s" % ("", one.get("verdict", ""),
                                    (" - confidence %s" % one["confidence"])
                                    if one.get("confidence") else ""))
        if one.get("detail"):
            print("  %-18s %s" % ("", one["detail"]))
        if one.get("evidence"):
            print("  %-18s %s" % ("", one["evidence"]))
    return 0 if not found else 1


def show_setup_diff(args) -> int:
    """One bench, both ways: what is recorded against what is there."""
    from . import netbox, setupdiff
    from .engine import load_config

    topology, _where = chosen_map(args)
    if topology is None:
        return 2
    config = load_config()
    url, token = netbox.settings(args.netbox_url, args.netbox_token, config)
    if not url:
        print("netbox: no address given (--netbox-url or %s)" % netbox.ENV_URL,
              file=sys.stderr)
        return 2
    client = netbox.Netbox(url, token, verify=not args.netbox_insecure)
    why = client.reachable()
    if why:
        print("netbox: %s" % why, file=sys.stderr)
        return 2

    pairs = dict((config.get("setup_tags_named") or {})
                 if isinstance(config.get("setup_tags_named"), dict) else {})
    beyond = setupdiff.out_of_reach(config)
    rows = setupdiff.review(topology, client, args.setup_diff, tag_of=pairs,
                            say=lambda m: print("netbox: %s" % m,
                                                file=sys.stderr),
                            beyond_reach=beyond)
    offer = setupdiff.roles_never_matched(rows)
    wanted = [str(r) for r in (getattr(args, "out_of_reach", []) or [])]
    giving_back = [w for w in wanted if w.startswith("-")]
    taking = ([role for role, _n in offer] if "*" in wanted
              else [role for role, _n in offer
                    if role.lower() in {w.lower() for w in wanted}])
    if giving_back:
        from .engine import save_config

        save_config(setupdiff.remember_out_of_reach(load_config(),
                                                    giving_back))
        print("no longer out of reach: %s"
              % ", ".join(w[1:] for w in giving_back), file=sys.stderr)
        beyond = setupdiff.out_of_reach(load_config())
        rows = setupdiff.review(topology, client, args.setup_diff,
                                tag_of=pairs, beyond_reach=beyond)
    if taking:
        from .engine import save_config

        save_config(setupdiff.remember_out_of_reach(load_config(), taking))
        print("marked out of reach: %s - they will not be reported as "
              "missing again" % ", ".join(taking), file=sys.stderr)
        rows = setupdiff.review(topology, client, args.setup_diff,
                                tag_of=pairs,
                                beyond_reach=beyond + taking)
    if beyond:
        print("")
        print("Marked out of reach, so never reported as missing: %s"
              % ", ".join(beyond))
        print("   take one back with --out-of-reach \"-%s\"" % beyond[0])
    if taking:
        pass
    elif offer:
        print("")
        print("Nothing of these kinds was matched in this scan:")
        for role, count in offer:
            print("  %-28s %d record(s)" % (role, count))
        print("If this tool has no way into them - an attenuator, a chamber "
              "- say so once:")
        print("   --out-of-reach %s" % (offer[0][0] if offer else "<role>"))
        print("   --out-of-reach \"*\"   accepts all of the above")

    print("")
    for setup in [setupdiff.split_name(x)[0] for x in args.setup_diff]:
        mine = [r for r in rows if r["setup"] == setup]
        print("%s: %s" % (setup, setupdiff.summary(mine)))
        for row in mine:
            print("  %-14s %-28s %-16s %-22s %s"
                  % (row["verdict"], row["netbox name"][:28],
                     row.get("netbox role", "")[:16],
                     row["found as"][:22],
                     row["matched by"] or row["needs a look"]))
        print("")
    where = getattr(args, "setup_diff_csv", "")
    if where == "-":
        write_utf8(setupdiff.to_csv(rows))
    elif where:
        with open(where, "w", encoding="utf-8", newline="") as handle:
            handle.write(setupdiff.to_csv(rows))
        print("comparison written to %s" % where, file=sys.stderr)
    return 0


def _addresses_from_netbox(args):
    """The addresses NetBox holds for a serial or a tag, or None on a problem."""
    from . import netbox
    from .engine import load_config

    config = load_config()
    url, token = netbox.settings(getattr(args, "netbox_url", "") or "",
                                 getattr(args, "netbox_token", "") or "",
                                 config)
    if not url:
        print("no NetBox address - set it with --netbox-url first",
              file=sys.stderr)
        return None
    wrong = netbox.token_warning(token)
    if wrong:
        print(wrong, file=sys.stderr)
        return None
    client = netbox.Netbox(url, token,
                           verify=not getattr(args, "netbox_insecure", False))
    records = netbox.find(client, args.from_netbox)
    if not records:
        print("nothing in NetBox carries %r" % args.from_netbox,
              file=sys.stderr)
        return []
    found = netbox.addresses_for(client, records)
    # Saying "1 address" after finding ten records reads as "the bench has
    # one device". The gap is the inventory's, and naming it is the useful
    # half of the answer.
    if len(found) < len(records):
        print("from netbox: %d record(s) carry %r, but an address is "
              "recorded for only %d of them - the rest cannot be scanned "
              "until somebody adds one" % (len(records), args.from_netbox,
                                           len(found)), file=sys.stderr)
    return found


def netbox_find(args) -> int:
    """One lookup in NetBox, with no scan and no map involved."""
    from . import netbox
    from .engine import config_path, load_config

    config = load_config()
    url, token = netbox.settings(getattr(args, "netbox_url", "") or "",
                                 getattr(args, "netbox_token", "") or "",
                                 config)
    if not url:
        print("no NetBox address - set it with --netbox-url or in %s"
              % config_path(), file=sys.stderr)
        return 2
    wrong = netbox.token_warning(token)
    if wrong:
        print(wrong, file=sys.stderr)
        return 2
    client = netbox.Netbox(url, token,
                           verify=not getattr(args, "netbox_insecure", False))
    worst = 1
    for wanted in args.netbox_find:
        found = netbox.find(client, wanted)
        print("")
        print("%s: %d record(s)" % (wanted, len(found)))
        if not found:
            print("   nothing in NetBox carries that serial, tag, name or "
                  "asset tag")
            continue
        worst = 0
        for record in found:
            print("   %-30s %-14s %-18s %s"
                  % (str(record.get("name") or "")[:30],
                     str(record.get("serial") or "-")[:14],
                     str(record.get("role") or "-")[:18],
                     "found by %s" % record.get("found_by", "")))
            where = " / ".join(x for x in (record.get("site"),
                                           record.get("location"),
                                           record.get("rack")) if x)
            if where:
                print("   %-30s %s" % ("", where))
            if record.get("url"):
                print("   %-30s %s" % ("", record["url"]))
    return worst


def say_where() -> int:
    """Everything this tool remembers, and the file it is in."""
    from . import history, setups as setup_store, vault
    from .engine import config_path

    print("settings and logins   %s" % config_path())
    print("passwords             %s" % vault.describe())
    print("setups                %s"
          % (shared.mine(shared.SETUPS) or setup_store.store_path()))
    print("maps it remembers     %s (%d kept)"
          % (history.folder(), len(history.listing())))
    print("switches it learned   %s" % store.DEFAULT_PATH)
    print("")
    print(shared.say_where())
    others = [os.path.basename(p)[:-5] for p in shared.everyones(shared.SETUPS)]
    if len(others) > 1:
        print("marking here as well: %s"
              % ", ".join(n for n in others if n != shared.who()))
    return 0


def show_help_page(where: str) -> int:
    """Put the bundled help somewhere readable and open it.

    The page is inside the binary, so there is nothing to download and
    nothing to keep in step by hand.
    """
    import shutil
    import tempfile
    import webbrowser

    from .render_topology import assets_dir

    source = os.path.join(assets_dir(), "help.html")
    if not os.path.exists(source):
        print("this build carries no help page (tools/make_help.py builds it)",
              file=sys.stderr)
        return 2
    target = where or os.path.join(tempfile.gettempdir(), "linkfinder-help.html")
    try:
        if os.path.abspath(target) != os.path.abspath(source):
            shutil.copyfile(source, target)
    except OSError as why:
        print("could not write %s: %s" % (target, why), file=sys.stderr)
        return 2
    print("help written to %s" % target, file=sys.stderr)
    if not where:
        webbrowser.open("file:///" + target.replace("\\", "/"))
    return 0


def serve_map(path: str, open_browser: bool = True) -> int:
    """Serve a saved map so it can be marked, with no window involved.

    The window has done this since the file dance was removed; a server has
    no window, so without this the whole of marking on the picture is absent
    from the only build Linux gets.
    """
    import json
    import webbrowser

    from . import setups as setup_store
    from .render_topology import render_html, topology_csv
    from .serve import MapServer, Pages

    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            topology = json.load(handle)
    except (OSError, ValueError) as why:
        print("could not read %s: %s" % (path, why), file=sys.stderr)
        return 2

    nodes = {n.get("id"): n for n in topology.get("nodes") or []}

    def put(device, names):
        node = nodes.get(device) or {}
        record = {"id": device, "name": node.get("name") or "",
                  "chassis_id": node.get("chassis_id") or "",
                  "serial": node.get("serial") or "",
                  "mgmt_ips": list(node.get("mgmt_ips") or [])}
        if names:
            setup_store.remember([record], names)
        else:
            setup_store.forget([record])
        node["setups"] = list(names)
        print("setup: %s is now in %s"
              % (record["name"] or device, ", ".join(names) or "no setup"),
              file=sys.stderr)
        return {"saved": device, "setups": list(names)}

    server = MapServer(Pages(
        lambda: render_html(topology, live=server.base),
        lambda: {"setups": setup_store.load()},
        put, table=lambda: topology_csv(topology)))
    url = server.start()
    print("the map is open at %s" % url, file=sys.stderr)
    print("setups you type there are written here. Ctrl-C to stop.",
          file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("", file=sys.stderr)
    finally:
        server.stop()
    return 0


# What a probe answers with is the way in - linux, esxi, windows - and that
# is not what the device IS. Leaving it on the node made the map unable to
# draw it and kept every Linux server out of the NetBox comparison, because
# neither list has ever heard of a kind called "linux".
AS_A_HOST = ("linux", "esxi", "windows", "windows-local", "ssh", "auto", "")


def _asset_kind(said: str) -> str:
    text = str(said or "").strip().lower()
    return "host" if text in AS_A_HOST else text


def scanned_as_topology(results):
    """The hosts a plain scan reached, in the shape the comparison expects.

    --netbox used to be read only on a topology walk and ignored in silence
    everywhere else, which looks exactly like a broken feature. A scan of a
    host list knows each machine's serial and address, and that is all the
    comparison needs.
    """
    from .topology import Node, Topology

    topo = Topology(seed=(results[0].target if results else ""))
    for result in results:
        address = str(result.target or "")
        # Every address that was tried belongs on the map. Dropping the ones
        # that failed - or that answered and heard nothing - meant scanning
        # fourteen and seeing eight, with no way to tell which eight.
        node = Node(id="ip:%s" % address, kind=_asset_kind(result.kind),
                    name=str(result.device_name or ""),
                    chassis_id=str(result.device_chassis or ""),
                    serial=str(result.device_serial or ""),
                    mgmt_ips=[address] if address else [],
                    state=("scanned" if _was_reached(result)
                           else "unreachable"),
                    depth=0)
        node.error = str(result.error or "")
        node.extra = dict(result.extra or {})
        topo.nodes[node.id] = node

    # The switches the scan landed on belong in the map too. Without them a
    # host scan can compare serials but not cabling, and the port each link
    # uses - the thing this tool is named for - would be thrown away.
    for result in results:
        if result.error:
            continue
        near = "ip:%s" % result.target
        for found in result.neighbors or []:
            switch = str(getattr(found, "switch", "") or "").strip()
            chassis = str(getattr(found, "chassis_id", "") or "").strip()
            port = str(getattr(found, "port", "") or "").strip()
            if not (switch or chassis) or not port:
                continue
            far = ("chassis:%s" % chassis.lower() if chassis
                   else "name:%s" % switch)
            if far not in topo.nodes:
                topo.nodes[far] = Node(id=far, kind="switch", name=switch,
                                       chassis_id=chassis, state="unscanned",
                                       depth=1)
            topo.add_edge(near, str(getattr(found, "local_iface", "") or ""),
                          far, port,
                          proto=str(getattr(found, "proto", "") or ""),
                          source=str(getattr(found, "source", "") or ""),
                          reported_by=near,
                          vlan=str(getattr(found, "vlan", "") or ""),
                          speed=str(getattr(found, "local_speed", "") or ""))
    # A neighbour named by a port MAC is a device already on this map, not a
    # new one. The crawl has always folded those; a plain scan did not, so
    # the same box appeared twice - once scanned, once as a bare name.
    topo.fold_known_macs()

    # A plain scan drew only what a protocol announced, so a pair of hosts
    # that hold each other's hardware address - and nothing else - came back
    # as two devices and no cable. The crawl has worked this out for a while;
    # there is no reason a host list should not get the same answer.
    # This list used to be kept here as well, and it had already drifted:
    # the crawl knew about PTP and this did not, so the same facts drew two
    # different maps. The order is the rule, and it is declared in one place.
    from .ladder import walk as walk_evidence

    views = [("ip:%s" % str(result.target or ""), result) for result in results]
    walk_evidence(topo, views,
                  log=lambda line: print(line, file=sys.stderr))
    return topo


def _was_reached(result) -> bool:
    """Whether the host answered, as opposed to whether it heard anything.

    These were one flag, so a server that let us in and reported its ports,
    optics and bonds was drawn "unreachable" because no switch spoke LLDP at
    it. A person reads that as a dead machine and goes to the rack.
    """
    if not getattr(result, "error", ""):
        return True
    return bool(getattr(result, "extra", None)
                or getattr(result, "neighbors", None))


def show_switch_ports(data, client, args) -> None:
    """Which switch port the scan saw is which port on record.

    The rule is per switch, so a single yes settles every port on it. Until
    that yes it is a proposal and nothing uses it - a confidently wrong port
    is worse than none, because somebody unplugs a cable over it.
    """
    from . import switchports
    from .engine import load_config, save_config

    config = load_config()
    plans = switchports.proposals(data, client, config)
    wanted = str(getattr(args, "confirm_switch_ports", "") or "")
    if not plans:
        held = switchports.confirmed_rules(config)
        print("switch ports: %s"
              % ("every switch this map touched is already confirmed (%d)"
                 % len(held) if held else
                 "no switch in this map is in NetBox under the name it "
                 "announces"), file=sys.stderr)
        return

    print("")
    print("Switch ports, scan against NetBox:")
    for plan in plans:
        print("  %-18s %s" % (plan["switch"], plan.get("example") or "-"))
        print("      %s%s" % (plan["standing"],
                              ", covers %d port(s)" % plan["covers"]
                              if plan.get("covers") else ""))
        if plan.get("unmatched"):
            print("      did not land: %s" % ", ".join(plan["unmatched"]))

    taking = [p for p in plans
              if p.get("matched") and (wanted == "*" or wanted == p["switch"])]
    if wanted and wanted != "*":
        taking = [p for p in taking if p["switch"] == wanted]
    if wanted == "*":
        taking = [p for p in taking if p["standing"] == switchports.CERTAIN]
    for plan in taking:
        save_config(switchports.remember(load_config(), plan["switch"], plan))
        print("confirmed: %s - its %d port(s) now have a name on both sides"
              % (plan["switch"], plan["covers"]), file=sys.stderr)
    if not wanted:
        print("")
        print("Confirm one with --confirm-switch-ports <switch>, or every "
              "certain one with '*'.")


def compare_map_with_netbox(args) -> int:
    """A saved map against the inventory, without going near the lab."""
    from .topology import Topology

    record, where = chosen_map(args)
    if record is None:
        return 2
    graph = Topology()
    try:
        graph.merge_from(record)
    except Exception as why:
        print("could not read %s: %s" % (where, why), file=sys.stderr)
        return 2
    ask_netbox(graph, args)
    return 0


def ask_netbox(topology, args) -> None:
    """Fold the inventory into the map, and say where the two disagree.

    Done before the map is written so the setups NetBox already holds are in
    the JSON, and the map's filter offers them without anyone marking a thing.
    """
    from . import netbox
    from .engine import load_config

    url, token = netbox.settings(args.netbox_url, args.netbox_token,
                                 load_config())
    if not url:
        print("netbox: no address given (--netbox-url or %s)" % netbox.ENV_URL,
              file=sys.stderr)
        return
    wrong = netbox.token_warning(token)
    if wrong:
        print("netbox: %s" % wrong, file=sys.stderr)
        return
    client = netbox.Netbox(url, token, verify=not args.netbox_insecure)
    why = client.reachable()
    if why:
        print("netbox: %s" % why, file=sys.stderr)
        return

    data = topology.to_dict()
    inventory = netbox.fetch(client, data,
                             lambda m: print(m, file=sys.stderr))
    netbox.save(inventory)

    if getattr(args, "netbox_tags", False):
        print("")
        print("NetBox tags on the devices this scan matched:")
        for name, count in netbox.tags_seen(inventory):
            print("  %-40s on %d device(s)" % (name, count))
        print("")
        print("Name the ones that are Setups with --setup-tag, or in the "
              "defaults file under netbox.setup_tags.")

    allow = list(args.setup_tag) or netbox.setup_tags(load_config())
    if not allow:
        print("netbox: no setup tags named, so no device was put in a Setup "
              "(--netbox-tags lists the candidates)", file=sys.stderr)
    put = netbox.apply(data, inventory, allow=allow)

    known = {n.get("id"): n for n in data.get("nodes") or []}
    # fall through: the nodes carry the setups and the record now
    for node in topology.nodes.values():
        fresh = known.get(node.id) or {}
        if fresh.get("setups"):
            node.setups = list(fresh["setups"])
        if fresh.get("netbox"):
            node.extra["netbox"] = fresh["netbox"]
    print("netbox: %d device(s) took their setup from its tags" % put,
          file=sys.stderr)

    if getattr(args, "switch_ports", False) or             getattr(args, "confirm_switch_ports", ""):
        show_switch_ports(data, client, args)

    findings = netbox.sort_review(netbox.review(data, inventory))
    netbox.stamp_verdicts(data, findings)
    for node in topology.nodes.values():
        fresh = (known.get(node.id) or {}).get("netbox_state")
        if fresh:
            node.extra["netbox_state"] = fresh
    standing = netbox.standing(findings)
    worth = [f for f in findings if f["status"] != netbox.MATCH]
    counted = ", ".join("%d %s" % (n, s) for s, n in sorted(standing.items()))
    if counted:
        print("netbox: %s" % counted, file=sys.stderr)
    if worth:
        print("")
        print("Against NetBox:")
        for found in worth:
            print("  %-18s %-24s %s" % (found["status"],
                                        str(found["label"])[:24],
                                        found["detail"]))
            print("      %s | confidence %s%s"
                  % (found["evidence"], found["confidence"],
                     " | needs your confirmation" if found["confirm"] else ""))
    else:
        print("netbox: everything it could check agrees", file=sys.stderr)

    if args.netbox_proposals:
        proposals = netbox.propose(data, inventory)
        where = netbox.save_proposals(proposals, args.netbox_proposals)
        print("netbox: %d correction(s) a person could apply written to %s"
              % (len(proposals), where), file=sys.stderr)


def write_utf8(text: str) -> None:
    """Put JSON on stdout as UTF-8 whatever the console is set to.

    A redirected stdout takes the locale encoding, which on Windows is a
    single-byte codepage. One ellipsis from a switch port name was enough to
    make the file unreadable as JSON - the bytes went out as cp1252 while
    every reader expects UTF-8.
    """
    data = text + chr(10)
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        sys.stdout.write(data)
        return
    stream.write(data.encode("utf-8"))
    stream.flush()


def _move_marks_to_shared(say) -> None:
    """Carry marks made before sharing was turned on into the team's copy."""
    from . import setups as setup_store

    try:
        moved = setup_store.move_to_shared()
    except Exception:
        return
    if moved:
        say("moved %d mark(s) made on this machine into the shared folder"
            % len(moved))


def _seal_old_passwords(say) -> None:
    """Move any password left in the defaults file into the system store.

    It runs once per machine: after the first run there is nothing plain
    left to move, so this costs a dictionary lookup thereafter.
    """
    from .engine import seal_stored_passwords, seal_stored_token

    try:
        sealed, why = seal_stored_passwords()
    except Exception:
        return
    if seal_stored_token():
        say("moved the saved NetBox token into %s" % vault.describe())
    if sealed:
        say("moved the saved password for %s into %s"
            % (", ".join(sealed), vault.describe()))
    elif why:
        say("warning: %s" % why)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "shared", ""):
        os.environ[shared.ENV] = args.shared
    if getattr(args, "where", False):
        return say_where()
    if getattr(args, "netbox_find", []):
        return netbox_find(args)
    _seal_old_passwords(print)
    # Silence about this was the fault: a person had no way to know their
    # marks were private to their own laptop.
    print(shared.say_where(), file=sys.stderr)
    _move_marks_to_shared(lambda m: print(m, file=sys.stderr))
    if args.list_switches:
        print(store.describe())
        return 0
    if args.forget_switches:
        target = "" if args.forget_switches == "*" else args.forget_switches
        print("forgot %d switch(es)" % store.forget(target))
        return 0
    if getattr(args, "forget_logins", False):
        from .engine import config_path, load_config, save_config

        held = load_config()
        how_many = len(held.get("per_host") or {}) + len(held.get("creds") or [])
        save_config({"per_host": {}, "creds": []})
        print("forgot %d saved login(s) - %s" % (how_many, config_path()))
        return 0
    if args.setup_sheet:
        return write_setup_sheet(args)
    if args.mark_setup or args.mark_list:
        return mark_a_setup(args)
    if args.setup_diff:
        return show_setup_diff(args)
    if getattr(args, "suspicious", False):
        return show_suspicious(args)
    # Comparing a map already drawn, with no scan at all. This is the cheap
    # path - four requests, three seconds - and it was reachable from
    # nowhere: --netbox only ever applied while scanning.
    if getattr(args, "netbox", False) and getattr(args, "map", ""):
        return compare_map_with_netbox(args)
    if args.help_page is not None:
        return show_help_page(args.help_page)
    if args.serve_map:
        return serve_map(args.serve_map)
    if args.render_topology:
        from .render_topology import render_file
        source, destination = args.render_topology
        print("topology page written to %s" % render_file(source, destination))
        return 0
    if args.watch:
        from . import watch

        with open(args.watch, encoding="utf-8") as handle:
            topology = json.load(handle)
        where = args.watch_state or None
        state = watch.load(where)
        state, events = watch.update(state, watch.read(topology))
        watch.save(state, where)
        standing = watch.standing(state)
        print("watching %d thing(s): %d up, %d degraded, %d down, %d out of sight"
              % (sum(standing.values()), standing["UP"], standing["DEGRADED"],
                 standing["DOWN"], standing["UNKNOWN"]))
        if not events:
            print("nothing changed since last time")
            return 0
        for line in watch.describe(events):
            print(line)
        worse = [e for e in events if e["to"] in ("DOWN", "UNKNOWN")]
        return 4 if worse else 0
    if args.compare_scan:
        from .crosscheck import compare
        from .model import results_from_json
        scan_path, map_path = args.compare_scan
        with open(scan_path, encoding="utf-8") as handle:
            results = results_from_json(handle.read())
        with open(map_path, encoding="utf-8") as handle:
            topology = json.load(handle)
        lost = compare(results, topology)
        if not lost:
            print("the map carries every link the scan found (%d device(s) checked)"
                  % len(results))
            return 0
        print("the scan found %d link(s) the map does not have:" % len(lost))
        for line in lost:
            print("  " + line)
        return 3
    if args.diff:
        from .diff import compare_files, render as render_diff
        before, after = args.diff
        report = compare_files(before, after)
        print(render_diff(report))
        return 0 if report.quiet else 1
    if args.merge_topology:
        return run_merge(args)
    # A bench arrives as a name, not as a list of addresses. Turn the name
    # into addresses here, before anything decides what to scan, so every
    # path below - one device or a whole map - works the same way.
    if getattr(args, "from_netbox", ""):
        found = _addresses_from_netbox(args)
        if found is None:
            return 2
        if not found:
            print("NetBox holds no address for %r - nothing to scan"
                  % args.from_netbox, file=sys.stderr)
            return 1
        print("from netbox: %d address(es) for %r - %s"
              % (len(found), args.from_netbox, ", ".join(found[:6])
                 + (", ..." if len(found) > 6 else "")), file=sys.stderr)
        if args.topology:
            args.topology = found[0]
        else:
            args.targets = list(found) + list(args.targets or [])
    if args.topology:
        return run_topology(args)
    if args.gui or (not args.targets and not args.targets_file and sys.stdin.isatty()
                    and len(sys.argv) == 1):
        from .gui import run_gui
        run_gui()
        return 0

    targets, per_host = targets_with_logins(
        args.targets, args.targets_file,
        log=lambda m: print(m, file=sys.stderr))
    if not targets:
        if args.targets or args.targets_file:
            print("nothing left to scan - every target given was rejected above",
                  file=sys.stderr)
        else:
            build_parser().print_help()
        return 2

    opts = options_from_args(args)
    opts.per_host = per_host
    log = (lambda m: print(m, file=sys.stderr)) if opts.verbose else None
    results = discover_many(targets, opts, log=log)

    if args.as_json:
        write_utf8(results_to_json(results))
    else:
        table = render_table(results, wide=args.wide)
        if table:
            if opts.verbose:
                print("", file=sys.stderr)
            print(table)
        from .diagnose import banner, shared_login_refused

        pattern = shared_login_refused(results, getattr(opts, "per_host", {}))
        if pattern:
            print("", file=sys.stderr)
            print(banner(pattern), file=sys.stderr)
        summary = render_summary(results)
        if summary:
            print("\n" + summary, file=sys.stderr)
    # Asking for a map while scanning a list of hosts used to be ignored in
    # silence: the file simply never appeared. The scan already knows every
    # link it found, so it can write one.
    if args.topology_json or args.topology_html:
        reached = scanned_as_topology(results)
        if reached.nodes:
            report_topology(reached, args, label="scan")
        else:
            print("nothing was reached, so there is no map to write",
                  file=sys.stderr)
    if getattr(args, "netbox", False):
        reached = scanned_as_topology(results)
        if reached.nodes:
            ask_netbox(reached, args)
        else:
            print("netbox: nothing was reached, so there is nothing to compare",
                  file=sys.stderr)

    for result in results:
        found = (getattr(result, "extra", {}) or {}).get("duplicates")
        if found:
            from .duplicates import render as render_duplicates
            text = render_duplicates(found)
            if text:
                print("")
                print(text)
            break
    run_loop_report(args, opts, results)
    if args.ptp:
        run_ptp_report(results)
        run_switch_ptp(args, opts, targets, results)
    if args.port_health:
        run_port_health(args, opts, targets, results)

    if args.as_csv:
        text = render_csv(results)
        if args.as_csv == "-":
            print(text)
        else:
            with open(args.as_csv, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            print("CSV written to %s" % args.as_csv, file=sys.stderr)

    return 0 if any(r.resolved for r in results) else 1

"""Command line entry point.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from typing import List, Optional

from . import __author__, __copyright__, __license__, __title__, __version__, store
from .engine import Options, discover_many, expand_targets, load_config
from .model import TargetResult, results_to_json
from .sshx import Creds


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
    ("via", "VIA", lambda n: "%s/%s" % (n.proto, n.source) if n.source else "-"),
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
    fields = ["target", "kind", "local_iface", "local_mac", "local_speed", "local_parent",
              "switch", "switch_ip", "switch_port", "switch_port_id_raw", "port_desc", "vlan",
              "chassis_id", "switch_model", "proto", "source", "confidence", "note"]
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(fields)
    for result in results:
        for nb in result.neighbors:
            writer.writerow([nb.host, result.kind, nb.local_iface, nb.local_mac, nb.local_speed,
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
    for result in results:
        if not result.resolved:
            lines.append("  %-16s %s" % (result.target, result.error or "no neighbour found"))
            continue
        for nb in result.neighbors:
            if nb.resolved:
                continue
            lines.append("  %-16s %s: %s"
                         % (result.target, _local_label(nb), nb.note or "no neighbour found"))
    if not lines:
        return ""
    return "Not resolved:\n" + "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="linkfinder",
        description="%s %s - find which switch and which switch port every link is "
                    "plugged into. Written by %s." % (__title__, __version__, __author__),
        epilog="Defaults can live in ~/.linkfinder.json "
               "(keys: user, password, bmc_user, bmc_password, community, switches, "
               "switch_user, switch_password).")
    p.add_argument("targets", nargs="*", help="host IPs or names (space or comma separated)")
    p.add_argument("--targets-file", default="", help="file with one target per line")
    p.add_argument("-m", "--mode", default="auto",
                   choices=["auto", "ssh", "linux", "esxi", "windows", "windows-local", "bmc",
                            "switch", "switch-cli"],
                   help="force an access path instead of auto-detecting it")
    p.add_argument("-u", "--user", default="", help="SSH username")
    p.add_argument("-p", "--password", default="", help="SSH password (also used for sudo)")
    p.add_argument("--sudo-password", default="", help="sudo password if it differs")
    p.add_argument("-i", "--key", default="", help="SSH private key file")
    p.add_argument("--ssh-port", type=int, default=22)
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
    p.add_argument("--mgmt-only", action="store_true",
                   help="only the interface carrying the target IP (old LLDP_Checker behaviour)")
    p.add_argument("-t", "--seconds", type=int, default=65,
                   help="capture window when a capture is needed (default 65, LLDP ticks every 30)")
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
    p.add_argument("-V", "--version", action="version",
                   version="%s %s - by %s - %s licence - %s"
                           % (__title__, __version__, __author__, __license__,
                              __copyright__))
    p.add_argument("--list-switches", action="store_true",
                   help="print the switches the tool has remembered, then exit")
    p.add_argument("--forget-switches", nargs="?", const="*", metavar="IP",
                   help="forget one remembered switch, or all of them when given no value")
    p.add_argument("--learn", action="store_true",
                   help="remember the switches found (addresses only) in "
                        "~/.linkfinder-switches.json and reuse them next time; off by default")
    topo = p.add_argument_group("topology discovery")
    topo.add_argument("--topology", metavar="SEED",
                      help="discover the topology recursively starting from this device")
    topo.add_argument("--max-depth", type=int, default=2,
                      help="seed is depth 0; devices at this depth are still probed "
                           "but their neighbours are not queued (default 2)")
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
    topo.add_argument("--render-topology", nargs=2, metavar=("JSON", "HTML"),
                      help="only render: turn an existing topology JSON into HTML")
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
                sudo_password=pick(args.sudo_password, "sudo_password"))
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
        mode=args.mode, ssh=ssh,
        bmc_user=pick(args.bmc_user, "bmc_user", "LINKFINDER_BMC_USER"),
        bmc_password=pick(args.bmc_password, "bmc_password", "LINKFINDER_BMC_PASSWORD"),
        bmc_port=args.bmc_port, enable_lldp=not args.no_enable_lldp,
        community=pick(args.community, "community", "LINKFINDER_COMMUNITY", "public"),
        switches=switches, switch_creds=switch_creds, seconds=args.seconds,
        ifaces=args.iface, mgmt_only=args.mgmt_only, mac=args.mac,
        workers=args.workers, verbose=not args.quiet, learn=args.learn,
        remember_credentials=args.remember_credentials, cred_list=extra_creds,
        ptp=args.ptp,
        port_health=args.port_health, port_health_interval=args.port_health_interval,
        expect_speed=args.expect_speed, all_interfaces=args.all_interfaces)


def write_topology(topology, args, label: str = "topology") -> None:
    """Never silently lose a scan: fall back to a temp file when no path is given."""
    import tempfile

    from .render_topology import write_html

    json_path, html_path = args.topology_json, args.topology_html
    if not json_path and not html_path:
        base = os.path.join(tempfile.gettempdir(), "linkfinder.%s" % label)
        json_path, html_path = base + ".json", base + ".html"
        print("no output path given - saving under %s" % tempfile.gettempdir(),
              file=sys.stderr)
    if json_path:
        with open(json_path, "w", encoding="utf-8") as handle:
            handle.write(topology.to_json())
        print("topology JSON written to %s" % json_path, file=sys.stderr)
    if html_path:
        write_html(topology.to_dict(), html_path)
        print("topology page written to %s" % html_path, file=sys.stderr)


def report_topology(topology, args, label: str = "topology") -> int:
    table = render_topology_table(topology)
    if table:
        print(table)
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
    limits = Limits(max_depth=args.max_depth, max_devices=args.max_devices,
                    scope=list(args.scope))
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

    topology = crawl(args.topology, opts, limits=limits,
                     confirm=topology_confirm(args.auto_discover), log=log,
                     workers=args.topology_workers, previous=previous)
    if opts.verbose:
        print("", file=sys.stderr)
    return report_topology(topology, args)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_switches:
        print(store.describe())
        return 0
    if args.forget_switches:
        target = "" if args.forget_switches == "*" else args.forget_switches
        print("forgot %d switch(es)" % store.forget(target))
        return 0
    if args.render_topology:
        from .render_topology import render_file
        source, destination = args.render_topology
        print("topology page written to %s" % render_file(source, destination))
        return 0
    if args.diff:
        from .diff import compare_files, render as render_diff
        before, after = args.diff
        report = compare_files(before, after)
        print(render_diff(report))
        return 0 if report.quiet else 1
    if args.merge_topology:
        return run_merge(args)
    if args.topology:
        return run_topology(args)
    if args.gui or (not args.targets and not args.targets_file and sys.stdin.isatty()
                    and len(sys.argv) == 1):
        from .gui import run_gui
        run_gui()
        return 0

    targets = expand_targets(args.targets, args.targets_file)
    if not targets:
        build_parser().print_help()
        return 2

    opts = options_from_args(args)
    log = (lambda m: print(m, file=sys.stderr)) if opts.verbose else None
    results = discover_many(targets, opts, log=log)

    if args.as_json:
        print(results_to_json(results))
    else:
        table = render_table(results, wide=args.wide)
        if table:
            if opts.verbose:
                print("", file=sys.stderr)
            print(table)
        summary = render_summary(results)
        if summary:
            print("\n" + summary, file=sys.stderr)

    for result in results:
        found = (getattr(result, "extra", {}) or {}).get("duplicates")
        if found:
            from .duplicates import render as render_duplicates
            text = render_duplicates(found)
            if text:
                print("")
                print(text)
            break
    if args.ptp:
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

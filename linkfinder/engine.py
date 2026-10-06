"""Target dispatch: pick the access path, run the ladder, fall back to the switch.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import socket
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import store
from .model import Neighbor, ProbeAttempt, TargetResult
from .sshx import Creds, normalise_mac

CONFIG_PATHS = [
    os.path.join(os.path.expanduser("~"), ".linkfinder.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "linkfinder.json"),
]


@dataclass
class Options:
    mode: str = "auto"
    ssh: Creds = field(default_factory=Creds)
    bmc_user: str = ""
    bmc_password: str = ""
    bmc_port: int = 443
    enable_lldp: bool = True
    community: str = "public"
    switches: List[str] = field(default_factory=list)
    switch_creds: Creds = field(default_factory=Creds)
    seconds: int = 65
    ifaces: Optional[List[str]] = None
    mgmt_only: bool = False
    mac: str = ""
    workers: int = 8
    verbose: bool = True
    learn: bool = False
    remember_credentials: bool = False
    cred_list: List[Creds] = field(default_factory=list)
    port_health: bool = False
    port_health_interval: float = 3.0
    expect_speed: int = 0
    all_interfaces: bool = False
    ptp: bool = True


def load_config() -> Dict[str, object]:
    for path in CONFIG_PATHS:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (ValueError, OSError):
                continue
    return {}


def port_open(host: str, port: int, timeout: float = 2.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def macs_over_ssh(target: str, creds: Creds, say: Callable[[str], None]) -> List[str]:
    """A switch keyed on MACs; if we can log in, read them rather than demanding --mac."""
    import shlex

    from .sshx import IFACE_SCRIPT, Ssh, SshError, parse_ifaces
    try:
        with Ssh(target, creds, timeout=10) as ssh:
            out = ssh.run("sh -c %s" % shlex.quote(IFACE_SCRIPT), timeout=30)[1]
    except SshError as exc:
        say("  could not read MACs off %s over SSH (%s)" % (target, exc))
        return []
    found = [i.mac for i in parse_ifaces(out).values()
             if i.physical and not i.virtual_prefix and i.mac]
    if found:
        say("  read %d MAC(s) off %s over SSH" % (len(found), target))
    return found


def _switch_side(target: str, opts: Options, result: TargetResult,
                 say: Callable[[str], None], macs: Optional[List[str]] = None) -> None:
    from .probe_snmp import build_switches, probe_switch_side
    from .probe_switchcli import probe_switch_cli

    mac_iface = {normalise_mac(n.local_mac): (n.local_iface, n.local_role)
                 for n in result.neighbors if n.local_mac}
    if opts.mac:
        macs = [normalise_mac(opts.mac)]
    elif macs is None:
        macs = [normalise_mac(n.local_mac) for n in result.neighbors if n.local_mac]
    from_frames = [n.mgmt_ip for n in result.neighbors if n.mgmt_ip]
    if opts.learn:
        for ip in from_frames:
            if store.learn(ip, seen_from="%s/%s" % (target, next(
                    (n.local_iface for n in result.neighbors if n.mgmt_ip == ip), "?"))):
                say("  remembered a new switch: %s" % ip)
    remembered = store.candidates(opts.community) if opts.learn else []
    direct = list(opts.switches) + from_frames
    by_ip: Dict[str, str] = {}
    for spec in direct + remembered:
        ip, _sep, community = spec.partition("@")
        ip = ip.strip()
        if ip and (ip not in by_ip or (community and not by_ip[ip])):
            by_ip[ip] = community.strip()
    specs = ["%s@%s" % (ip, c) if c else ip for ip, c in by_ip.items()]
    if not specs:
        return
    known = {s.partition("@")[0] for s in direct}
    extra = [ip for ip in by_ip if ip not in known]
    if extra:
        say("  plus %d remembered switch(es): %s" % (len(extra), ", ".join(extra)))
    if not macs and opts.ssh.user:
        macs = [normalise_mac(m) for m in macs_over_ssh(target, opts.ssh, say)]
    say("switch side: asking %s" % ", ".join(sorted(by_ip)))
    switches = build_switches(specs, opts.community)
    before = len(result.neighbors)
    discovered: List[tuple] = []
    probe_switch_side(target, switches, result, macs=macs, learned=discovered, log=say)
    if opts.learn:
        for ip, name, descr, snmp_ok in discovered:
            if snmp_ok is None:
                if store.learn(ip, name=name, descr=descr, seen_from="%s (switch peer)" % target):
                    say("  remembered a new switch seen by its neighbour: %s (%s)" % (ip, name or "?"))
            else:
                community = next((sw.community for sw in switches if sw.host == ip), opts.community)
                store.mark_snmp(ip, snmp_ok, community=community, name=name, descr=descr,
                                store_community=opts.remember_credentials)
    for nb in result.neighbors[before:]:
        name, role = mac_iface.get(nb.local_mac, ("", ""))
        if name and not nb.local_iface:
            nb.local_iface, nb.local_role = name, role
    resolved_ifaces = {n.local_iface for n in result.neighbors if n.resolved}
    result.neighbors = [n for n in result.neighbors
                        if n.resolved or n.local_iface not in resolved_ifaces]
    asked = ", ".join(sw.host for sw in switches)
    for nb in result.neighbors:
        if not nb.resolved and nb.local_mac and normalise_mac(nb.local_mac) in (macs or []):
            nb.note = ("no LLDP on this port, and its MAC is not in the forwarding table of %s "
                       "- it faces a different switch; pass that one with --switch" % asked)
    if len(result.neighbors) == before and opts.switch_creds.user and macs:
        for spec in specs:
            host = spec.split("@")[0]
            probe_switch_cli(host, opts.switch_creds, macs[0], result,
                             target_ip=target, log=say)


def discover_one(target: str, opts: Options, log: Optional[Callable[[str], None]] = None) -> TargetResult:
    say = log or (lambda _m: None)
    result = TargetResult(target=target)
    say("=== %s ===" % target)

    from .probe_redfish import looks_like_redfish

    mode = opts.mode
    is_bmc = False
    if mode == "auto":
        is_bmc = port_open(target, opts.bmc_port, timeout=2.0) and \
            looks_like_redfish(target, opts.bmc_port)
        if target in ("localhost", "127.0.0.1", ".") and sys.platform.startswith("win"):
            mode = "windows-local"
        elif is_bmc:
            mode = "bmc"
        elif port_open(target, opts.ssh.port or 22):
            mode = "ssh"
        elif port_open(target, opts.bmc_port):
            mode = "bmc"
        elif opts.switches or (opts.learn and store.candidates(opts.community)):
            mode = "switch"
        else:
            result.error = ("nothing to talk to on %s: no SSH (%d), no HTTPS (%d) and no "
                            "--switch given" % (target, opts.ssh.port or 22, opts.bmc_port))
            return result
        say("auto-selected access path: %s%s"
            % (mode, " (it answers Redfish)" if is_bmc else ""))

    try:
        if mode == "windows-local":
            from .probe_windows import probe_windows_local
            probe_windows_local(result, seconds=opts.seconds, log=say)
        elif mode in ("ssh", "linux", "esxi", "windows"):
            from .probe_linux import run_linux_target
            got = run_linux_target(target, opts.ssh, want_ifaces=opts.ifaces,
                                   mgmt_only=opts.mgmt_only, seconds=opts.seconds,
                                   ptp=opts.ptp, log=say)
            result.kind = got.kind
            result.error = got.error
            result.device_name = got.device_name
            result.device_chassis = got.device_chassis
            result.device_descr = got.device_descr
            result.attempts.extend(got.attempts)
            result.extra.update(got.extra)
            for nb in got.neighbors:
                result.add(nb)
        elif mode == "bmc":
            from .probe_redfish import probe_redfish
            probe_redfish(target, opts.bmc_user or opts.ssh.user,
                          opts.bmc_password or opts.ssh.password, result,
                          allow_enable=opts.enable_lldp, log=say, port=opts.bmc_port)
        elif mode == "switch-cli":
            from .probe_switchcli import probe_switch_as_target
            creds = opts.switch_creds if opts.switch_creds.user else opts.ssh
            probe_switch_as_target(target, creds, result, log=say)
        elif mode == "switch":
            result.kind = "switch-side"
            _switch_side(target, opts, result, say)
        else:
            result.error = "unknown mode %r" % mode
            return result
    except Exception as exc:
        result.error = "%s: %s" % (type(exc).__name__, exc)

    known = store.load().get(target) if opts.learn else None
    if known and not result.resolved:
        say("note: %s is itself a switch I already know (%s, seen from %s)"
            % (target, known.get("name") or "name unknown",
               ", ".join(known.get("seen_from", [])) or "?"))
        result.error = ("%s is a switch, not a host - LinkFinder can say what is plugged "
                        "INTO it with --switch-user/--switch-password, but to find where "
                        "IT is plugged in you need the switch above it" % target)

    ssh_rejected = result.error and ("rejected the credentials" in result.error.lower()
                                     or "ssh did not get us in" in result.error.lower())
    if not result.resolved and mode == "ssh" and ssh_rejected and \
            port_open(target, opts.bmc_port) and looks_like_redfish(target, opts.bmc_port):
        say("SSH refused us, but %s answers Redfish - retrying it as a BMC" % target)
        from .probe_redfish import probe_redfish
        result.error = ""
        probe_redfish(target, opts.bmc_user or opts.ssh.user,
                      opts.bmc_password or opts.ssh.password, result,
                      allow_enable=opts.enable_lldp, log=say, port=opts.bmc_port)

    unresolved = [n for n in result.neighbors if not n.resolved and n.local_mac]
    known_switches = (opts.switches or [n.mgmt_ip for n in result.neighbors if n.mgmt_ip]
                      or (store.candidates(opts.community) if opts.learn else []))
    if mode != "switch" and known_switches and (not result.resolved or unresolved):
        say("%d interface(s) still unresolved - asking the switch about them" % len(unresolved)
            if unresolved else "host side did not resolve - asking the switch")
        _switch_side(target, opts, result, say,
                     macs=[normalise_mac(n.local_mac) for n in unresolved] or None)

    if result.resolved:
        result.error = ""
    return result


def _finish_results(results: List[TargetResult], opts: Options,
                    log: Optional[Callable[[str], None]] = None
                    ) -> List[TargetResult]:
    """Everything that can only be decided once every target is in.

    A clock heard on one host is often a port on another host in the run,
    and a link can only be compared end to end after both ends are known.
    Neither may take the discovery down with it.
    """
    try:
        from .ptp import name_peers
        name_peers(results)
    except Exception as exc:
        if log:
            log("  naming PTP peers failed: %s" % exc)
    try:
        from .linkcheck import run_link_checks
        run_link_checks(results, community=opts.community, log=log)
    except Exception as exc:
        if log:
            log("  link checks failed: %s" % exc)
    try:
        from .optics import match_optics
        match_optics(results, log=log)
    except Exception as exc:
        if log:
            log("  optics comparison failed: %s" % exc)
    try:
        from .duplicates import find as find_duplicates
        find_duplicates(results, log=log)
    except Exception as exc:
        if log:
            log("  duplicate search failed: %s" % exc)
    return results


def discover_many(targets: List[str], opts: Options,
                  log: Optional[Callable[[str], None]] = None) -> List[TargetResult]:
    if len(targets) == 1:
        return _finish_results([discover_one(targets[0], opts, log=log)],
                               opts, log=log)
    say = log or (lambda _m: None)
    buffers: Dict[str, List[str]] = {t: [] for t in targets}
    results: Dict[str, TargetResult] = {}

    def work(target: str) -> TargetResult:
        return discover_one(target, opts, log=lambda m: buffers[target].append(m))

    workers = max(1, min(opts.workers, len(targets)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, t): t for t in targets}
        for future in concurrent.futures.as_completed(futures):
            target = futures[future]
            try:
                results[target] = future.result()
            except Exception as exc:
                failed = TargetResult(target=target)
                failed.error = "%s: %s" % (type(exc).__name__, exc)
                results[target] = failed
            for line in buffers[target]:
                say(line)
    return _finish_results([results[t] for t in targets], opts, log=say)


def expand_targets(values: List[str], targets_file: str = "") -> List[str]:
    out: List[str] = []
    for value in values:
        out.extend(part for part in value.replace(",", " ").split() if part)
    if targets_file:
        with open(targets_file, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.split("#")[0].strip()
                if line:
                    out.append(line)
    return list(dict.fromkeys(out))

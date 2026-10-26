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
from typing import Callable, Dict, List, Optional, Tuple

from . import store, vault
from .model import Neighbor, TargetResult
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
    # Long enough for two CDP intervals (60s each) and four LLDP ones
    # (30s): one interval is a coin toss, and a missed frame costs a
    # whole rescan. Only captures spend this - a host with lldpctl is
    # not slowed at all.
    seconds: int = 90
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
    neighbours: bool = True
    identify: bool = True
    adb: bool = False
    read_serial: bool = True
    serial_baud: int = 115200
    per_host: Dict[str, Creds] = field(default_factory=dict)


def load_config() -> Dict[str, object]:
    for path in CONFIG_PATHS:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (ValueError, OSError):
                continue
    return {}


def config_path() -> str:
    """Where defaults are written: the file already in use, else the one in $HOME."""
    for path in CONFIG_PATHS:
        if os.path.exists(path):
            return path
    return CONFIG_PATHS[0]


def save_config(updates: Dict[str, object]) -> str:
    """Merge into the defaults file without disturbing what is already there.

    The file is hand-written by whoever set the tool up, so a key we do not
    know about must survive being saved over.
    """
    path = config_path()
    data = load_config()
    data.update(updates)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    return path


def per_host_from_config(cfg: Optional[Dict[str, object]] = None) -> Dict[str, Creds]:
    """Logins remembered per host, if the defaults file carries any.

    A password written before this tool had a secret store is still readable
    and still works; `plaintext_passwords` finds those rows so they can be
    sealed and the clear copy removed from disk.
    """
    table = (cfg if cfg is not None else load_config()).get("per_host") or {}
    out: Dict[str, Creds] = {}
    if not isinstance(table, dict):
        return out
    for host, entry in table.items():
        if isinstance(entry, dict):
            try:
                port = int(entry.get("port", 22) or 22)
            except (TypeError, ValueError):
                port = 22
            out[str(host)] = Creds(user=str(entry.get("user", "")),
                                   password=vault.unprotect(
                                       str(entry.get("password", ""))),
                                   key_file=str(entry.get("key_file", "")),
                                   port=port)
        elif isinstance(entry, str):
            user, _sep, password = entry.partition(":")
            out[str(host)] = Creds(user=user, password=password)
    return out


def per_host_to_config(per_host: Dict[str, Creds],
                       with_passwords: bool = True,
                       lost: Optional[List[str]] = None) -> Dict[str, dict]:
    """The table in the shape the defaults file keeps it.

    `lost` collects the hosts whose password could not be stored, because
    this machine has no secret store. Dropping it silently is worse than
    refusing: the row stays, the login stays, and the host quietly falls
    back to the shared password with nobody told.
    """
    out: Dict[str, dict] = {}
    for host, creds in (per_host or {}).items():
        port = int(getattr(creds, "port", 22) or 22)
        # A row that says nothing but "this one listens on 2222" is still worth
        # keeping - the port is as much a fact about a host as its login.
        if not host or not (creds.user or creds.password or creds.key_file
                            or port != 22):
            continue
        entry = {"user": creds.user}
        if with_passwords and creds.password:
            # Never write the password itself. If this machine has no store,
            # the row keeps the user and the port and says so in the log -
            # a silent plaintext fallback is the thing being fixed here.
            sealed = vault.protect(creds.password)
            if sealed:
                entry["password"] = sealed
            elif lost is not None:
                lost.append(host)
        if creds.key_file:
            entry["key_file"] = creds.key_file
        if port != 22:
            entry["port"] = port
        out[host] = entry
    return out


def plaintext_passwords(cfg: Optional[Dict[str, object]] = None) -> List[str]:
    """Hosts whose password is still sitting in the file as typed."""
    table = (cfg if cfg is not None else load_config()).get("per_host") or {}
    if not isinstance(table, dict):
        return []
    return sorted(str(h) for h, e in table.items()
                  if isinstance(e, dict) and e.get("password")
                  and not vault.looks_protected(str(e["password"])))


def seal_stored_token() -> bool:
    """Move a NetBox token written before this into the system store.

    It was kept as typed while per-host passwords were already sealed, and
    it is the more dangerous of the two: one token reads the whole
    inventory.
    """
    cfg = load_config()
    held = cfg.get("netbox") or {}
    token = str(held.get("token") or "")
    if not token or vault.looks_protected(token) or not vault.available():
        return False
    sealed = vault.protect(token)
    if not sealed:
        return False
    save_config({"netbox": dict(held, token=sealed)})
    return True


def seal_stored_passwords() -> Tuple[List[str], str]:
    """Move every plaintext password into the system store, once.

    Returns the hosts that were sealed and, when there is nowhere to put
    them, the reason - the caller prints it rather than failing quietly.
    """
    cfg = load_config()
    hosts = plaintext_passwords(cfg)
    if not hosts:
        return [], ""
    if not vault.available():
        return [], vault.why_not()
    save_config({"per_host": per_host_to_config(per_host_from_config(cfg))})
    return hosts, ""


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
    # Only speak where nothing better has. Another probe may already have
    # named the far end, and "it faces a different switch" is worse than a
    # MAC that was actually heard on the wire.
    generic = ("", "probed, no LLDP/CDP frame received")
    for nb in result.neighbors:
        if nb.resolved or (nb.note or "") not in generic:
            continue
        if nb.local_mac and normalise_mac(nb.local_mac) in (macs or []):
            nb.note = ("no LLDP on this port, and its MAC is not in the forwarding table of %s "
                       "- it faces a different switch; pass that one with --switch" % asked)
    if len(result.neighbors) == before and opts.switch_creds.user and macs:
        for spec in specs:
            host = spec.split("@")[0]
            probe_switch_cli(host, opts.switch_creds, macs[0], result,
                             target_ip=target, log=say)


def host_key(value: str) -> Tuple[str, str]:
    """A per-host row split into the host it keys on and any user in it.

    The lookup used to be an exact string match, so a row typed as
    root@10.0.0.5, or with different capitals from the target, matched
    nothing and was passed over without a word - the password was simply
    never tried and the host failed to log in.
    """
    text = str(value or "").strip()
    user = ""
    if "@" in text:
        login, _sep, text = text.rpartition("@")
        user = login.partition(":")[0]
    return text.casefold(), user


def _row_for(opts: Options, target: str) -> Tuple[Optional[Creds], str]:
    table = getattr(opts, "per_host", None) or {}
    own = table.get(target)
    if own is not None:
        return own, ""
    wanted, _mine = host_key(target)
    for host, creds in table.items():
        key, user = host_key(host)
        if key == wanted:
            return creds, user
    return None, ""


def unused_logins(targets: List[str], opts: Options) -> List[str]:
    """Per-host rows that match nothing being scanned.

    A login nobody asked for is harmless; a login that was meant for a host
    and silently matched none of them is a failed scan with no explanation.
    """
    wanted = {host_key(t)[0] for t in targets or []}
    return sorted(host for host in (getattr(opts, "per_host", None) or {})
                  if host_key(host)[0] not in wanted)


def creds_for(opts: Options, target: str) -> Creds:
    """The login this host was given, or the one everything else uses.

    A target written as root@host carries its own; everything else falls back
    to the shared pair, so the common case of one password for the rack is
    unchanged.
    """
    own, spelled = _row_for(opts, target)
    if own is None:
        return opts.ssh
    return Creds(user=own.user or spelled or opts.ssh.user,
                 password=own.password or opts.ssh.password,
                 key_file=own.key_file or opts.ssh.key_file,
                 port=own.port or opts.ssh.port,
                 sudo_password=own.sudo_password or own.password
                 or opts.ssh.sudo_password,
                 become_password=own.become_password or opts.ssh.become_password)


def _login_sequence(opts: Options, first: Creds) -> List[Creds]:
    """The logins to try for one host, in order.

    When the person gave a login for this host - its own per-host row, the
    shared pair, or an extra set - ONLY those are tried. A login supplied by
    hand is used as given, and if it is refused the host is reported refused:
    the tool does not then reach for every default it knows behind the
    person's back, which could lock an account and hides a typo. The baked
    defaults are a fallback for a host the person said nothing about - nothing
    more - and exist only in the internal build.

    A credential with no username is dropped, never attempted: SSH cannot log
    in without one, and attempting it made paramiko fall back to the local
    account. If that leaves the hand-given set empty, the sequence is empty and
    the caller reports it rather than quietly using the defaults.
    """
    from .baked import default_creds

    gave = bool(first and (first.user or first.password or first.key_file)) \
        or bool(opts.cred_list)
    pool = ([first] + list(opts.cred_list or [])) if gave else list(default_creds())
    out, seen = [], set()
    for creds in pool:
        if not creds or not creds.user:
            continue
        mark = (creds.user, creds.password, creds.key_file, creds.become_password)
        if mark in seen:
            continue
        seen.add(mark)
        out.append(creds)
    return out


def _switch_first(opts: Options):
    """A resolver that asks a known switch where a host's ports are, up front.

    The slow part of a scan is listening for LLDP on a host that runs no LLDP
    daemon - 125 seconds a host. But the switch it plugs into already knows,
    in its MAC table, which port each of the host's addresses is on, and
    answers in under a tenth of a second. So where a switch is known, its
    table is read first and those ports are not listened to at all; only the
    ports the switch cannot explain - a dark cable, a direct server-to-server
    link - still fall through to the capture. Returns None when no switch is
    known, and then nothing changes: the host is listened to as before.
    """
    specs = list(opts.switches)
    if opts.learn:
        specs += store.candidates(opts.community)
    if not specs:
        return None

    def resolve(target_ip: str, mac_by_iface: Dict[str, str]) -> List[Neighbor]:
        from .probe_snmp import build_switches, probe_switch_side

        wanted = {normalise_mac(m): iface for iface, m in mac_by_iface.items()
                  if m}
        if not wanted:
            return []
        tmp = TargetResult(target=target_ip)
        try:
            switches = build_switches(specs, opts.community)
            probe_switch_side(target_ip, switches, tmp, macs=list(wanted),
                              log=lambda _m: None)
        except Exception:
            return []
        out = []
        for nb in tmp.neighbors:
            iface = wanted.get(normalise_mac(nb.local_mac))
            if iface and nb.resolved:
                nb.local_iface = iface
                out.append(nb)
        return out

    return resolve


def discover_one(target: str, opts: Options, log: Optional[Callable[[str], None]] = None,
                 cancel=None) -> TargetResult:
    say = log or (lambda _m: None)
    from . import cancel as _cancel
    if _cancel.cancelled(cancel):
        result = TargetResult(target=target)
        result.error = "stopped"
        return result
    ssh_creds = creds_for(opts, target)
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
        elif port_open(target, ssh_creds.port or 22):
            # A rack PDU answers on 22 and then has no shell at all: the
            # ordinary probe logs in, is handed nothing back and reports a
            # machine that says nothing. Its SSH server names the library
            # it is built on, which no general-purpose host does, so this
            # costs one connection and no login.
            from .probe_apc import banner_of, banner_says_no_shell

            mode = "pdu" if banner_says_no_shell(
                banner_of(target, ssh_creds.port or 22)) else "ssh"
        elif port_open(target, opts.bmc_port):
            mode = "bmc"
        elif opts.switches or (opts.learn and store.candidates(opts.community)):
            mode = "switch"
        else:
            result.error = ("nothing to talk to on %s: no SSH (%d), no HTTPS (%d) and no "
                            "--switch given" % (target, ssh_creds.port or 22, opts.bmc_port))
            return result
        say("auto-selected access path: %s%s"
            % (mode, " (it answers Redfish)" if is_bmc else ""))

    try:
        if mode == "windows-local":
            from .probe_windows import probe_windows_local
            probe_windows_local(result, seconds=opts.seconds, log=say,
                                read_serial=opts.read_serial,
                                serial_baud=opts.serial_baud, cancel=cancel)
        elif mode in ("ssh", "linux", "esxi", "windows"):
            from .probe_linux import run_linux_target
            got = None
            resolve_switch = _switch_first(opts)
            sequence = _login_sequence(opts, ssh_creds)
            if not sequence:
                # Nothing usable to log in with. A password or key typed with
                # no username lands here: say so plainly instead of failing on
                # "authentication" and leaving the person guessing.
                if ssh_creds.password or ssh_creds.key_file:
                    result.error = ("a password was given but no username - SSH needs "
                                    "a username too (or write it as user@%s)" % target)
                else:
                    result.error = "no username or password to log in to %s with" % target
                return result
            for index, creds in enumerate(sequence):
                got = run_linux_target(target, creds, want_ifaces=opts.ifaces,
                                       mgmt_only=opts.mgmt_only,
                                       seconds=opts.seconds, ptp=opts.ptp,
                                       neighbours=opts.neighbours,
                                       identify=opts.identify,
                                       adb=opts.adb,
                                       read_serial=opts.read_serial,
                                       serial_baud=opts.serial_baud,
                                       resolve_switch=resolve_switch, log=say,
                                       cancel=cancel)
                # Only a refused password is worth another login. A timeout or
                # a dropped session means the next one would be refused the
                # same way, and some gear locks an account after a few tries.
                if "rejected the credentials" not in (got.error or "").lower():
                    break
                # Say "next one" only when there is one. The hand-given login
                # is tried alone, so on its refusal there is nothing further -
                # claiming otherwise read as if defaults were being tried.
                if index + 1 < len(sequence):
                    say("  that login was refused - trying the next one known")
            result.kind = got.kind
            result.error = got.error
            result.device_name = got.device_name
            result.device_chassis = got.device_chassis
            result.device_descr = got.device_descr
            result.device_serial = got.device_serial
            result.attempts.extend(got.attempts)
            result.extra.update(got.extra)
            for nb in got.neighbors:
                result.add(nb)
        elif mode == "bmc":
            from .probe_redfish import probe_redfish
            probe_redfish(target, opts.bmc_user or ssh_creds.user,
                          opts.bmc_password or ssh_creds.password, result,
                          allow_enable=opts.enable_lldp, log=say, port=opts.bmc_port)
        elif mode == "pdu":
            from .probe_apc import read as read_pdu

            found = read_pdu(target, ssh_creds.user, ssh_creds.password,
                             port=ssh_creds.port or 22, log=say)
            if not found:
                result.error = ("it answers SSH but offers no shell, and it "
                                "is not an APC card either")
            else:
                result.kind = "pdu"
                result.device_name = str(found.get("model number") or "")
                result.device_serial = str(found.get("serial number") or "")
                result.device_chassis = str(found.get("mac") or "")
                result.device_descr = "APC %s" % (found.get("model number") or "")
                result.extra["pdu"] = found
        elif mode == "switch-cli":
            from .probe_switchcli import probe_switch_as_target
            creds = opts.switch_creds if opts.switch_creds.user else ssh_creds
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
        probe_redfish(target, opts.bmc_user or ssh_creds.user,
                      opts.bmc_password or ssh_creds.password, result,
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
        from .neighbor6 import name_peers as name_neighbours
        name_neighbours(results)
    except Exception as exc:
        if log:
            log("  naming IPv6 neighbours failed: %s" % exc)
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
    try:
        from .ptp_tree import apply as apply_ptp, build as build_ptp
        apply_ptp(results, build_ptp(results))
    except Exception as exc:
        if log:
            log("  ptp tree failed: %s" % exc)
    try:
        from .sensors import apply as apply_sensors
        apply_sensors(results, log=log)
    except Exception as exc:
        if log:
            log("  sensor read failed: %s" % exc)
    return results


def discover_many(targets: List[str], opts: Options,
                  log: Optional[Callable[[str], None]] = None,
                  on_result: Optional[Callable[[TargetResult], None]] = None
                  ) -> List[TargetResult]:
    """Interrogate every target, calling *on_result* as each one lands.

    A capture window is a minute per host. Holding the first host's answer
    until the last one finishes means staring at an empty table for as long
    as the slowest device takes, with everything already known.
    """
    def arrived(result: TargetResult) -> None:
        if on_result is None:
            return
        try:
            on_result(result)
        except Exception:
            pass

    for host in unused_logins(targets, opts):
        (log or (lambda _m: None))(
            "the login saved for %s was not used - nothing being scanned goes "
            "by that name, so check how it is spelled" % host)

    if len(targets) == 1:
        one = discover_one(targets[0], opts, log=log)
        arrived(one)
        return _finish_results([one], opts, log=log)
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
            arrived(results[target])
    return _finish_results([results[t] for t in targets], opts, log=say)


def targets_with_logins(values: List[str], targets_file: str = "",
                        log: Optional[Callable[[str], None]] = None
                        ) -> Tuple[List[str], Dict[str, Creds]]:
    """Addresses worth trying, and the logins any of them carried.

    Anything that cannot be an address or a hostname is dropped here with a
    reason, rather than being handed to the probes to fail as "nothing to talk
    to" - which sends a person to check a firewall when the fault is a typo.
    """
    from .ranges import expand_all
    from .targets import complain, split

    # A network or a run of addresses is how people hold a bench. Opening
    # them out here means every path below - one device, a map, a lookup -
    # gets the same treatment, and nothing is dropped without a word.
    opened, said, refused = expand_all(list(values or []))
    if log:
        for line in said:
            log("range: %s" % line)
        for line in refused:
            log("range: %s" % line)
    values = opened
    hosts, logins, bad = split(values, targets_file)
    if log:
        # Say what went in and what did not, side by side, so a typo in one
        # address is seen at a glance rather than hunted for - the scan lists
        # what it accepted, then each one it refused and why.
        if hosts:
            log("accepted %d address(es): %s" % (len(hosts), ", ".join(hosts)))
        for line in complain(bad):
            log(line)
        if bad and not hosts:
            log("nothing valid to scan - fix the address(es) above and retry")
    return hosts, {host: Creds(user=user, password=password)
                   for host, (user, password) in logins.items()}


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


def exposed_by_git(path: str) -> str:
    """Warn when the defaults file sits in a git work tree that would take it.

    A home directory is a surprisingly common place to find a repository, and
    this file holds passwords and, now, an API token. Being untracked is not
    safety: one `git add .` publishes it. Returns the reason, or "" when it is
    either not in a repository or properly ignored.
    """
    folder = os.path.dirname(os.path.abspath(path)) or "."
    try:
        from .proc import run_quiet
    except Exception:
        return ""
    # As a list, never as one string. A string with shell=False runs on
    # Windows and on Linux looks for a file called "git -C ... rev-parse",
    # so this whole warning silently never fired on a server.
    rc, out = run_quiet(["git", "-C", folder, "rev-parse",
                         "--is-inside-work-tree"])
    if rc != 0 or "true" not in (out or "").lower():
        return ""
    rc, _out = run_quiet(["git", "-C", folder, "check-ignore", "-q",
                          os.path.abspath(path)])
    if rc == 0:
        return ""           # git has been told to leave it alone
    rc, remote = run_quiet(["git", "-C", folder, "remote", "get-url", "origin"])
    where = (remote or "").strip().splitlines()[0] if (remote or "").strip() else ""
    return ("%s is inside a git repository%s and is not ignored - a plain "
            "`git add .` there would commit it"
            % (path, (" whose remote is " + where) if where else ""))


def _quoted(text: str) -> str:
    return '"%s"' % str(text).replace('"', '')

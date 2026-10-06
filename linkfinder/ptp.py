"""What a NIC port is doing for timing, and who is on the other end of it.

Two questions this answers that LLDP cannot. First, is PTP running on this port
and in which role - a port that hands out time and a port that takes it look
identical in `ip link`. Second, and more useful: a fronthaul port is usually
cabled straight into another server or a radio, with no switch in the path and
no LLDP anywhere, so the normal ladder reports it as unresolved. PTP frames are
on that cable regardless, and they carry the far end's clock identity, which is
derived from its MAC. That is enough to name the far end.

Evidence, not guesswork: a clock identity we can match to a port we scanned
ourselves is reported as a confirmed peer; a clock identity we have never seen
is reported as exactly that, with the caveat that a transparent clock can
forward frames without changing the source.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
import shlex
import struct
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

PTP_ETHERTYPE = 0x88F7
PTP_EVENT_PORT = 319
PTP_GENERAL_PORT = 320
PTP_FILTER = "ether proto 0x88f7 or udp port 319 or udp port 320"

MESSAGE_TYPE = {
    0x0: "Sync", 0x1: "Delay_Req", 0x2: "Pdelay_Req", 0x3: "Pdelay_Resp",
    0x8: "Follow_Up", 0x9: "Delay_Resp", 0xA: "Pdelay_Resp_Follow_Up",
    0xB: "Announce", 0xC: "Signaling", 0xD: "Management",
}

ROLE_FLAGS = (
    ("--clientOnly=1", "slave"), ("--slaveOnly=1", "slave"),
    ("-s ", "slave"), ("--serverOnly=1", "master"), ("--masterOnly=1", "master"),
)

HEADER_LEN = 34
ANNOUNCE_GM_OFFSET = HEADER_LEN + 19


@dataclass
class PtpPeer:
    """A clock heard on one of our ports."""

    mac: str = ""
    clock_identity: str = ""
    port_number: int = 0
    domain: int = -1
    gm_identity: str = ""
    messages: List[str] = field(default_factory=list)
    frames: int = 0

    @property
    def label(self) -> str:
        return self.clock_identity or self.mac or "?"


@dataclass
class PtpPort:
    """PTP as seen from one local interface."""

    iface: str = ""
    phc: str = ""
    role: str = ""              # master | slave | "" when no daemon claims it
    domain: int = -1
    uds: str = ""
    transport: str = ""         # L2 | UDPv4 | UDPv6
    state: str = ""             # MASTER / SLAVE / LISTENING, straight from pmc
    port_identity: str = ""
    gm_identity: str = ""
    gm_present: Optional[bool] = None
    offset_ns: Optional[int] = None
    peers: List[PtpPeer] = field(default_factory=list)
    error: str = ""

    @property
    def active(self) -> bool:
        return bool(self.role or self.state or self.peers)

    @property
    def summary(self) -> str:
        if self.state and self.domain >= 0:
            out = "%s dom %d" % (self.state, self.domain)
        elif self.role and self.domain >= 0:
            out = "%s dom %d" % (self.role, self.domain)
        elif self.role:
            out = self.role
        elif self.peers:
            return "frames seen"
        else:
            return ""
        if self.offset_ns is not None:
            out += " %+dns" % self.offset_ns
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iface": self.iface, "phc": self.phc, "role": self.role,
            "domain": self.domain, "transport": self.transport, "state": self.state,
            "summary": self.summary,
            "port_identity": self.port_identity, "gm_identity": self.gm_identity,
            "gm_present": self.gm_present, "offset_ns": self.offset_ns,
            "peers": [{"mac": p.mac, "clock_identity": p.clock_identity,
                       "port_number": p.port_number, "domain": p.domain,
                       "gm_identity": p.gm_identity, "frames": p.frames,
                       "messages": list(p.messages)} for p in self.peers],
            "error": self.error,
        }


def clock_identity(raw: bytes) -> str:
    """The IEEE 1588 clockIdentity in the form linuxptp prints: aabbcc.fffe.ddeeff."""
    if len(raw) != 8:
        return ""
    return "%s.%s.%s" % (raw[0:3].hex(), raw[3:5].hex(), raw[5:8].hex())


def clock_identity_to_mac(cid: str) -> str:
    """Undo the EUI-64 padding a clock identity is built from.

    linuxptp derives the identity from the interface MAC by inserting ff:fe in
    the middle; some vendors insert ff:ff. Anything else is a clock identity
    that was not built from a MAC, and guessing would invent a device.
    """
    digits = re.sub(r"[^0-9a-fA-F]", "", cid or "").lower()
    if len(digits) != 16:
        return ""
    middle = digits[6:10]
    if middle not in ("fffe", "ffff"):
        return ""
    octets = digits[:6] + digits[10:]
    return ":".join(octets[i:i + 2] for i in range(0, 12, 2))


def decode_ptp(payload: bytes) -> Optional[Dict[str, Any]]:
    """Header fields of one PTPv2 message, or None when it is not one."""
    if len(payload) < HEADER_LEN:
        return None
    version = payload[1] & 0x0F
    if version != 2:
        return None
    msg_type = payload[0] & 0x0F
    out = {
        "type": MESSAGE_TYPE.get(msg_type, "type-%d" % msg_type),
        "domain": payload[4],
        "clock_identity": clock_identity(payload[20:28]),
        "port_number": struct.unpack("!H", payload[28:30])[0],
        "gm_identity": "",
    }
    if msg_type == 0xB and len(payload) >= ANNOUNCE_GM_OFFSET + 8:
        out["gm_identity"] = clock_identity(
            payload[ANNOUNCE_GM_OFFSET:ANNOUNCE_GM_OFFSET + 8])
    return out


def ptp_from_frame(frame: bytes) -> Optional[Tuple[str, Dict[str, Any]]]:
    """(source MAC, decoded header) for a captured PTP frame, else None."""
    from .decode import mac_str, strip_vlans

    ethertype, payload, _vlan, _dst, src = strip_vlans(frame)
    if ethertype == PTP_ETHERTYPE:
        decoded = decode_ptp(payload)
        return (mac_str(src), decoded) if decoded else None
    if ethertype == 0x0800 and len(payload) >= 20:
        ihl = (payload[0] & 0x0F) * 4
        if payload[9] != 17 or len(payload) < ihl + 8:
            return None
        udp = payload[ihl:]
        dport = struct.unpack("!H", udp[2:4])[0]
        if dport not in (PTP_EVENT_PORT, PTP_GENERAL_PORT):
            return None
        decoded = decode_ptp(udp[8:])
        return (mac_str(src), decoded) if decoded else None
    return None


def peers_from_capture(data: bytes, our_macs: Optional[List[str]] = None) -> List[PtpPeer]:
    """Fold a pcap into one entry per clock heard, ignoring our own frames."""
    from .decode import iter_capture

    mine = {m.lower() for m in (our_macs or []) if m}
    found: Dict[str, PtpPeer] = {}
    for frame in iter_capture(data):
        parsed = ptp_from_frame(frame)
        if not parsed:
            continue
        src, header = parsed
        if src.lower() in mine:
            continue
        key = header["clock_identity"] or src
        peer = found.get(key)
        if peer is None:
            peer = PtpPeer(mac=src, clock_identity=header["clock_identity"],
                           port_number=header["port_number"], domain=header["domain"])
            found[key] = peer
        peer.frames += 1
        if header["type"] not in peer.messages:
            peer.messages.append(header["type"])
        if header["gm_identity"] and not peer.gm_identity:
            peer.gm_identity = header["gm_identity"]
    return sorted(found.values(), key=lambda p: -p.frames)


def _flag(words: List[str], names: Tuple[str, ...]) -> str:
    """Value of --name=x, --name x or -nx, whichever form was used."""
    for i, word in enumerate(words):
        for name in names:
            if word == name and i + 1 < len(words):
                return words[i + 1]
            if word.startswith(name + "="):
                return word.split("=", 1)[1]
            if len(name) == 2 and name.startswith("-") and word.startswith(name) \
                    and len(word) > 2:
                return word[2:]
    return ""


def parse_ptp4l_processes(text: str) -> List[PtpPort]:
    """One PtpPort per running ptp4l instance, read off its command line.

    A host commonly runs two: one taking time on the fronthaul port and one
    handing it out on another. Reading the command line is how we learn which
    interface and which domain, and the uds_address is the only way to make pmc
    talk to the right instance - without it pmc queries domain 0 on the default
    socket and silently returns nothing.
    """
    out: List[PtpPort] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "ptp4l" not in line:
            continue
        try:
            words = shlex.split(line)
        except ValueError:
            words = line.split()
        if not any(w.endswith("ptp4l") or w == "ptp4l" for w in words[:2]):
            continue
        iface = _flag(words, ("-i", "--interface"))
        if not iface:
            continue
        domain = _flag(words, ("-d", "--domain"))
        port = PtpPort(iface=iface, uds=_flag(words, ("--uds_address",)))
        port.domain = int(domain) if domain.isdigit() else 0
        if "-2" in words:
            port.transport = "L2"
        elif "-4" in words:
            port.transport = "UDPv4"
        elif "-6" in words:
            port.transport = "UDPv6"
        joined = " " + " ".join(words) + " "
        for flag, role in ROLE_FLAGS:
            if flag.strip() and flag in joined:
                port.role = role
                break
        out.append(port)
    return out


PMC_FIELD = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*)\s+(.+?)\s*$")


def parse_pmc(text: str) -> Dict[str, str]:
    """Flatten pmc's indented key/value output into a plain dict."""
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        if not line.startswith((" ", "\t")):
            continue
        match = PMC_FIELD.match(line)
        if match:
            out.setdefault(match.group(1), match.group(2).strip())
    return out


def apply_pmc(port: PtpPort, port_data: str, time_status: str) -> PtpPort:
    fields = parse_pmc(port_data)
    port.state = fields.get("portState", "")
    port.port_identity = fields.get("portIdentity", "")
    status = parse_pmc(time_status)
    port.gm_identity = status.get("gmIdentity", "")
    present = status.get("gmPresent", "")
    if present:
        port.gm_present = present.lower() == "true"
    offset = status.get("master_offset", "")
    try:
        port.offset_ns = int(offset)
    except ValueError:
        port.offset_ns = None
    return port


PS_COMMAND = "ps -eo args 2>/dev/null | grep '[p]tp4l'"
PHC_COMMAND = (
    "for i in /sys/class/net/*/; do n=$(basename $i); [ -e $i/device ] || continue; "
    "p=$(ls $i/device/ptp 2>/dev/null | head -1); "
    "[ -n \"$p\" ] && echo \"$n $p\"; done")


def _pmc_command(port: PtpPort, message: str) -> str:
    base = "pmc -u -b 0 -d %d" % max(port.domain, 0)
    if port.uds:
        base += " -s %s" % shlex.quote(port.uds)
    return "timeout 6 %s %s 2>/dev/null" % (base, shlex.quote("GET " + message))


def parse_phc_map(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def collect_ptp(run: Callable[[str], str], ifaces: Optional[List[str]] = None,
                log: Optional[Callable[[str], None]] = None) -> Dict[str, PtpPort]:
    """Daemon state per interface. Costs two cheap reads plus one pmc per daemon.

    `run` is anything that takes a shell command and returns its output, so this
    works over the existing SSH session and is trivial to drive from a test.
    """
    say = log or (lambda _m: None)
    ports: Dict[str, PtpPort] = {}
    try:
        phc = parse_phc_map(run(PHC_COMMAND))
    except Exception:
        phc = {}
    try:
        daemons = parse_ptp4l_processes(run(PS_COMMAND))
    except Exception as exc:
        say("  ptp: could not read the process list (%s)" % exc)
        daemons = []
    for port in daemons:
        if ifaces and port.iface not in ifaces:
            continue
        port.phc = phc.get(port.iface, "")
        try:
            apply_pmc(port, run(_pmc_command(port, "PORT_DATA_SET")),
                      run(_pmc_command(port, "TIME_STATUS_NP")))
        except Exception as exc:
            port.error = "pmc failed: %s" % exc
        ports[port.iface] = port
        say("  ptp on %s: %s" % (port.iface, port.summary or "running, no state"))
    for name, index in phc.items():
        if name not in ports and (not ifaces or name in ifaces):
            ports[name] = PtpPort(iface=name, phc=index)
    return ports


def describe_peer(peer: PtpPeer, known: Optional[Dict[str, Tuple[str, str]]] = None) -> str:
    """Name the far end if we have scanned it, else say honestly what we heard."""
    known = known or {}
    hit = known.get((peer.mac or "").lower())
    if not hit and peer.clock_identity:
        hit = known.get(clock_identity_to_mac(peer.clock_identity).lower())
    if hit:
        host, iface = hit
        return "%s %s" % (host, iface)
    return peer.clock_identity or peer.mac or "?"


def known_port_index(results) -> Dict[str, Tuple[str, str]]:
    """MAC -> (host, interface) for every port we interrogated ourselves."""
    index: Dict[str, Tuple[str, str]] = {}
    for result in results or []:
        host = getattr(result, "target", "")
        for nb in getattr(result, "neighbors", []) or []:
            mac = (getattr(nb, "local_mac", "") or "").lower()
            if mac:
                index.setdefault(mac, (host, getattr(nb, "local_iface", "")))
    return index


def _own_clock(port: Dict[str, Any]) -> str:
    """portIdentity is "507c6f.fffe.851098-1"; the clock is everything before the dash."""
    return (port.get("port_identity") or "").split("-")[0].strip().lower()


def name_peers(results) -> None:
    """Say who each port hears, and how strong that evidence actually is.

    A PTP event frame is multicast. Hearing one proves the two ports share a
    layer-2 segment; it does not prove a cable between them. The giveaway is
    arithmetic: if the same clock is audible from two of our ports, at least one
    of those cannot be a direct link. So a direct cable is only claimed when the
    hearing is one to one in both directions, and even then it is called likely
    rather than proven. Everything weaker is reported as a shared segment, which
    is still the answer to "what is this port plugged into" - just an honest one.
    """
    index = known_port_index(results)
    ports: List[Tuple[Any, Dict[str, Any]]] = []
    for result in results or []:
        for nb in getattr(result, "neighbors", []) or []:
            port = (getattr(nb, "extra", {}) or {}).get("ptp")
            if port:
                ports.append((nb, port))

    heard: Dict[str, List[Tuple[Any, Dict[str, Any]]]] = {}
    for nb, port in ports:
        for raw in port.get("peers") or []:
            key = (raw.get("clock_identity") or raw.get("mac") or "").lower()
            if key:
                heard.setdefault(key, []).append((nb, port))
    by_clock = {_own_clock(port): (nb, port) for nb, port in ports if _own_clock(port)}

    reciprocal: Dict[int, List[str]] = {}
    for key, listeners in heard.items():
        far = by_clock.get(key)
        if far is None:
            continue
        for nb, _port in listeners:
            host = getattr(nb, "host", "")
            iface = getattr(nb, "local_iface", "")
            if host or iface:
                reciprocal.setdefault(id(far[0]), []).append(
                    ("%s %s" % (host, iface)).strip())

    for nb, port in ports:
        labels, best = [], ""
        for raw in port.get("peers") or []:
            peer = PtpPeer(mac=raw.get("mac", ""),
                           clock_identity=raw.get("clock_identity", ""),
                           port_number=raw.get("port_number", 0))
            key = (peer.clock_identity or peer.mac or "").lower()
            name = describe_peer(peer, index)
            listeners = len(heard.get(key, []))
            far = by_clock.get(key)
            mutual = False
            if far is not None:
                ours = _own_clock(port)
                mutual = any((p.get("clock_identity") or "").lower() == ours
                             for p in (far[1].get("peers") or []))
            if name != peer.label and listeners == 1 and mutual:
                labels.append(name)
                best = best or "direct"
            elif name != peer.label:
                labels.append(name)
                best = best or "segment"
            else:
                labels.append(peer.label)
        if not labels:
            others = sorted(set(reciprocal.get(id(nb), [])))
            if not others:
                continue
            nb.extra["ptp_peer"] = ", ".join(others)
            nb.extra["ptp_peer_evidence"] = "heard-by"
            if not nb.resolved:
                nb.note = ("no LLDP here; %s hears this port's clock, so they share a "
                           "segment" % ", ".join(others))
            continue
        nb.extra["ptp_peer"] = ", ".join(labels)
        nb.extra["ptp_peer_evidence"] = best or "heard"
        if nb.resolved:
            continue
        if best == "direct":
            nb.note = ("no LLDP here; PTP says this is most likely a direct cable to "
                       "%s - each end hears only the other" % ", ".join(labels))
        elif best == "segment":
            nb.note = ("no LLDP here; PTP puts this port on the same segment as %s "
                       "(a multicast frame does not prove a direct cable)"
                       % ", ".join(labels))
        else:
            role = port.get("state") or port.get("role")
            nb.note = ("no LLDP here; PTP %s, hears %s"
                       % (role or "frames", ", ".join(labels)))

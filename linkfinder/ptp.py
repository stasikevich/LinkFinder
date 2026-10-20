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
ANNOUNCE_LEN = 64
TLV_PATH_TRACE = 0x0008
TLV_ALTERNATE_TIME_OFFSET = 0x0009

# G.8275.1 is the profile this was built against; the values below are the ones
# that profile actually puts on the wire. Anything else is reported by number
# rather than guessed at.
CLOCK_CLASS = {
    6: "locked to a primary reference",
    7: "holdover, within spec",
    13: "locked to an application-specific source",
    14: "holdover from an application-specific source",
    52: "holdover, out of spec",
    58: "holdover, out of spec (app-specific)",
    135: "boundary clock, holdover within spec",
    140: "boundary clock, holdover out of spec",
    150: "boundary clock, degraded A",
    160: "boundary clock, degraded B",
    165: "boundary clock, free-running",
    187: "holdover, out of spec (category 2)",
    193: "holdover, out of spec (category 3)",
    248: "default, never synchronised",
    255: "slave only",
}

TIME_SOURCE = {
    0x10: "atomic clock", 0x20: "GNSS", 0x30: "terrestrial radio",
    0x39: "serial time code", 0x40: "PTP", 0x50: "NTP", 0x60: "hand set",
    0x90: "other", 0xA0: "internal oscillator",
}

CLOCK_ACCURACY = {
    0x20: "25 ns", 0x21: "100 ns", 0x22: "250 ns",
    0x23: "1 us", 0x24: "2.5 us", 0x25: "10 us", 0x26: "25 us",
    0x27: "100 us", 0x28: "250 us",
    0x29: "1 ms", 0x2A: "2.5 ms", 0x2B: "10 ms", 0x2C: "25 ms",
    0x2D: "100 ms", 0x2E: "250 ms",
    0x2F: "1 s", 0x30: "10 s", 0x31: "over 10 s",
    0xFE: "unknown",
}


def describe_clock_class(value):
    """A number nobody remembers, turned into what it means for the clock."""
    if value is None:
        return ""
    known = CLOCK_CLASS.get(value)
    return "%d (%s)" % (value, known) if known else "%d" % value


def normalise_mac(text: str) -> str:
    """02-55-66-00-00-01, 0255.6600.0001 and 02:55:66:00:00:01 are one address."""
    digits = re.sub(r"[^0-9a-fA-F]", "", text or "").lower()
    if len(digits) != 12:
        return ""
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


@dataclass
class PtpPeer:
    """A clock heard on one of our ports."""

    mac: str = ""
    clock_identity: str = ""
    port_number: int = 0
    domain: int = -1
    gm_identity: str = ""
    gm_class: Optional[int] = None
    gm_accuracy: Optional[int] = None
    gm_priority1: Optional[int] = None
    gm_priority2: Optional[int] = None
    steps_removed: Optional[int] = None
    time_source: Optional[int] = None
    path_trace: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    frames: int = 0

    @property
    def label(self) -> str:
        return self.clock_identity or self.mac or "?"


@dataclass
class PtpPort:
    """PTP as seen from one local interface."""

    iface: str = ""
    pid: str = ""
    phc: str = ""
    role: str = ""              # master | slave | "" when no daemon claims it
    domain: int = -1
    uds: str = ""
    transport: str = ""         # L2 | UDPv4 | UDPv6
    state: str = ""             # MASTER / SLAVE / LISTENING, straight from pmc
    port_identity: str = ""
    gm_identity: str = ""
    gm_present: Optional[bool] = None
    gm_class: Optional[int] = None
    gm_accuracy: Optional[int] = None
    parent_port: str = ""
    offset_ns: Optional[float] = None
    mean_path_delay_ns: Optional[float] = None
    steps_removed: Optional[int] = None
    utc_offset: Optional[int] = None
    time_source: Optional[int] = None
    time_traceable: Optional[bool] = None
    frequency_traceable: Optional[bool] = None
    source: str = ""
    peers: List[PtpPeer] = field(default_factory=list)
    responders: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""

    @property
    def active(self) -> bool:
        return bool(self.role or self.state or self.peers or self.responders)

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
            out += " %+gns" % self.offset_ns
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iface": self.iface, "phc": self.phc, "role": self.role,
            "domain": self.domain, "transport": self.transport, "state": self.state,
            "summary": self.summary,
            "port_identity": self.port_identity, "gm_identity": self.gm_identity,
            "gm_present": self.gm_present, "gm_class": self.gm_class,
            "gm_accuracy": self.gm_accuracy, "parent_port": self.parent_port,
            "offset_ns": self.offset_ns,
            "mean_path_delay_ns": self.mean_path_delay_ns,
            "steps_removed": self.steps_removed, "utc_offset": self.utc_offset,
            "time_source": self.time_source,
            "time_traceable": self.time_traceable,
            "frequency_traceable": self.frequency_traceable,
            "source": self.source,
            "responders": [dict(r) for r in self.responders],
            "peers": [{"mac": p.mac, "clock_identity": p.clock_identity,
                       "port_number": p.port_number, "domain": p.domain,
                       "gm_identity": p.gm_identity, "frames": p.frames,
                       "gm_class": p.gm_class, "gm_accuracy": p.gm_accuracy,
                       "gm_priority1": p.gm_priority1,
                       "gm_priority2": p.gm_priority2,
                       "steps_removed": p.steps_removed,
                       "time_source": p.time_source,
                       "path_trace": list(p.path_trace),
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


def path_trace(payload: bytes) -> List[str]:
    """The clock identities an Announce has passed through, if the TLV is on.

    PATH_TRACE is the one thing in the protocol that hands over the timing
    topology directly: every boundary clock appends itself, so one frame names
    the whole chain back to the grandmaster. It is optional, so an empty list
    means "not enabled", never "no boundary clocks".
    """
    out: List[str] = []
    at = ANNOUNCE_LEN
    while at + 4 <= len(payload):
        kind, length = struct.unpack("!HH", payload[at:at + 4])
        body = payload[at + 4:at + 4 + length]
        if len(body) < length:
            break
        if kind == TLV_PATH_TRACE:
            for i in range(0, len(body) - 7, 8):
                out.append(clock_identity(body[i:i + 8]))
        at += 4 + length + (length % 2)
    return out


def decode_announce(payload: bytes) -> Dict[str, Any]:
    """Everything an Announce says about the grandmaster and the path to it."""
    if len(payload) < ANNOUNCE_LEN:
        return {}
    quality = payload[48:52]
    return {
        "gm_identity": clock_identity(payload[53:61]),
        "gm_priority1": payload[47],
        "gm_priority2": payload[52],
        "gm_class": quality[0],
        "gm_accuracy": quality[1],
        "steps_removed": struct.unpack("!H", payload[61:63])[0],
        "time_source": payload[63],
        "utc_offset": struct.unpack("!h", payload[44:46])[0],
        "path_trace": path_trace(payload),
    }


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
    if msg_type == 0xB:
        out.update(decode_announce(payload))
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
        if header.get("gm_identity") and not peer.gm_identity:
            peer.gm_identity = header["gm_identity"]
            peer.gm_class = header.get("gm_class")
            peer.gm_accuracy = header.get("gm_accuracy")
            peer.gm_priority1 = header.get("gm_priority1")
            peer.gm_priority2 = header.get("gm_priority2")
            peer.steps_removed = header.get("steps_removed")
            peer.time_source = header.get("time_source")
            peer.path_trace = list(header.get("path_trace") or [])
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
        said = words[0] if words and words[0].isdigit() else ""
        port = PtpPort(iface=iface, pid=said,
                       uds=_flag(words, ("--uds_address",)))
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


def _number(text: str) -> Optional[float]:
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return int(value) if value == int(value) else value


def _truth(text: str) -> Optional[bool]:
    """A MIB-ish boolean: 1/true, 0/false, anything else is not an answer."""
    text = str(text).strip().lower()
    if text in ("1", "true"):
        return True
    if text in ("0", "false"):
        return False
    return None


def apply_datasets(port: PtpPort, answers: Dict[str, str]) -> PtpPort:
    """Fill a port from whichever data sets answered, standard ones first.

    IEEE 1588 defines DEFAULT, CURRENT, PARENT, TIME_PROPERTIES and PORT. Those
    are the ones any conforming implementation has. TIME_STATUS_NP is a
    linuxptp extension - the NP is literally "non-portable" - so it is read
    last and only fills gaps the standard sets left. Reading the offset from it
    alone, as the first version did, would have produced nothing at all on a
    boundary clock that is not Linux.
    """
    port_ds = parse_pmc(answers.get("PORT_DATA_SET", ""))
    current = parse_pmc(answers.get("CURRENT_DATA_SET", ""))
    parent = parse_pmc(answers.get("PARENT_DATA_SET", ""))
    props = parse_pmc(answers.get("TIME_PROPERTIES_DATA_SET", ""))
    extra = parse_pmc(answers.get("TIME_STATUS_NP", ""))

    port.state = port_ds.get("portState", port.state)
    port.port_identity = port_ds.get("portIdentity", port.port_identity)

    if "offsetFromMaster" in current:
        port.offset_ns = _number(current["offsetFromMaster"])
        port.source = "IEEE 1588 data sets"
    port.mean_path_delay_ns = _number(current.get("meanPathDelay", ""))
    if "stepsRemoved" in current:
        port.steps_removed = _number(current["stepsRemoved"])

    port.gm_identity = parent.get("grandmasterIdentity", port.gm_identity)
    port.parent_port = parent.get("parentPortIdentity", port.parent_port)
    if "gm.ClockClass" in parent:
        port.gm_class = _number(parent["gm.ClockClass"])
    accuracy = parent.get("gm.ClockAccuracy")
    if accuracy:
        try:
            port.gm_accuracy = int(str(accuracy), 0)
        except ValueError:
            port.gm_accuracy = None

    if "currentUtcOffset" in props:
        port.utc_offset = _number(props["currentUtcOffset"])
    port.time_traceable = _truth(props.get("timeTraceable", ""))
    port.frequency_traceable = _truth(props.get("frequencyTraceable", ""))
    source = props.get("timeSource")
    if source:
        try:
            port.time_source = int(str(source), 0)
        except ValueError:
            port.time_source = None

    if port.offset_ns is None and "master_offset" in extra:
        port.offset_ns = _number(extra["master_offset"])
        port.source = "linuxptp TIME_STATUS_NP"
    if not port.gm_identity:
        port.gm_identity = extra.get("gmIdentity", "")
    present = extra.get("gmPresent", "")
    if present:
        port.gm_present = present.lower() == "true"
    return port


def apply_pmc(port: PtpPort, port_data: str, time_status: str) -> PtpPort:
    """Kept for the two-argument call; the dataset form is the real one."""
    return apply_datasets(port, {"PORT_DATA_SET": port_data,
                                 "TIME_STATUS_NP": time_status})


# The first four are IEEE 1588; the last is linuxptp's own and is allowed to
# be missing.
LOCAL_DATASETS = ("PORT_DATA_SET", "CURRENT_DATA_SET", "PARENT_DATA_SET",
                  "TIME_PROPERTIES_DATA_SET", "TIME_STATUS_NP")

PS_COMMAND = "ps -eo pid,args 2>/dev/null | grep '[p]tp4l'"
PHC_COMMAND = (
    "for i in /sys/class/net/*/; do n=$(basename $i); [ -e $i/device ] || continue; "
    "p=$(ls $i/device/ptp 2>/dev/null | head -1); "
    "[ -n \"$p\" ] && echo \"$n $p\"; done")


def _pmc_command(port: PtpPort, message: str, inside: bool = False) -> str:
    """Ask one daemon for one data set, from the host or from inside it.

    A host that runs ptp4l in a container often has neither pmc nor the
    daemon's socket in its own namespaces, and the plain call then returns
    nothing at all rather than failing. Entering the daemon's own mount and
    network namespaces finds both, and costs nothing when it is not needed.
    """
    base = "pmc -u -b 0 -d %d" % max(port.domain, 0)
    if port.uds:
        base += " -s %s" % shlex.quote(port.uds)
    ask = shlex.quote("GET " + message)
    if inside and port.pid:
        return ("timeout 10 nsenter -t %s -m -n -- %s %s 2>/dev/null"
                % (port.pid, base, ask))
    return "timeout 6 %s %s 2>/dev/null" % (base, ask)


ANSWERED = "RESPONSE MANAGEMENT"


def ask_datasets(run: Callable[[str], str], port: PtpPort) -> Dict[str, str]:
    """Every data set, trying the host first and the daemon's namespace next."""
    first = LOCAL_DATASETS[0]
    answers = {first: run(_pmc_command(port, first))}
    if ANSWERED not in (answers[first] or "") and port.pid:
        answers = {first: run(_pmc_command(port, first, inside=True))}
        if ANSWERED in (answers[first] or ""):
            answers.update({name: run(_pmc_command(port, name, inside=True))
                            for name in LOCAL_DATASETS[1:]})
            return answers
    answers.update({name: run(_pmc_command(port, name))
                    for name in LOCAL_DATASETS[1:]})
    return answers


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
            apply_datasets(port, ask_datasets(run, port))
        except Exception as exc:
            port.error = "pmc failed: %s" % exc
        ports[port.iface] = port
        say("  ptp on %s: %s" % (port.iface, port.summary or "running, no state"))
    for name, index in phc.items():
        if name not in ports and (not ifaces or name in ifaces):
            ports[name] = PtpPort(iface=name, phc=index)
    return ports


def describe_peer(peer: PtpPeer, known: Optional[Dict[str, Tuple[str, str]]] = None) -> str:
    """Name the far end if this run can place it, else say what we actually heard."""
    known = known or {}
    hit = known.get(normalise_mac(peer.mac or ""))
    if not hit and peer.clock_identity:
        hit = known.get(normalise_mac(clock_identity_to_mac(peer.clock_identity)))
    if hit:
        host, iface = hit
        return ("%s %s" % (host, iface)).strip()
    return peer.clock_identity or peer.mac or "?"


def describe_grandmaster(peer: PtpPeer,
                         known: Optional[Dict[str, Tuple[str, str]]] = None) -> str:
    """The grandmaster an Announce named, with its quality and distance."""
    if not peer.gm_identity:
        return ""
    known = known or {}
    hit = known.get(normalise_mac(clock_identity_to_mac(peer.gm_identity)))
    name = ("%s %s" % hit).strip() if hit else peer.gm_identity
    bits = []
    if peer.gm_class is not None:
        bits.append("class %s" % describe_clock_class(peer.gm_class))
    if peer.time_source is not None:
        bits.append(TIME_SOURCE.get(peer.time_source,
                                    "source 0x%02x" % peer.time_source))
    if peer.gm_accuracy is not None and peer.gm_accuracy != 0xFE:
        bits.append(CLOCK_ACCURACY.get(peer.gm_accuracy,
                                       "accuracy 0x%02x" % peer.gm_accuracy))
    if peer.steps_removed:
        bits.append("%d hop(s) away" % peer.steps_removed)
    elif peer.steps_removed == 0:
        bits.append("directly attached")
    return "%s (%s)" % (name, ", ".join(bits)) if bits else name


def known_port_index(results) -> Dict[str, Tuple[str, str]]:
    """MAC -> (device, interface) for everything this run can put a name to.

    Not just our own NICs: the switches found over LLDP count too. A switch
    acting as boundary clock derives its clock identity from its chassis MAC,
    which is the same MAC it publishes as its LLDP chassis id - so the
    grandmaster our servers sync to can be named instead of printed as hex.
    """
    index: Dict[str, Tuple[str, str]] = {}
    for result in results or []:
        host = getattr(result, "device_name", "") or getattr(result, "target", "")
        for nb in getattr(result, "neighbors", []) or []:
            mac = normalise_mac(getattr(nb, "local_mac", "") or "")
            if mac:
                index.setdefault(mac, (host, getattr(nb, "local_iface", "")))
    for result in results or []:
        for nb in getattr(result, "neighbors", []) or []:
            chassis = normalise_mac(getattr(nb, "chassis_id", "") or "")
            name = getattr(nb, "switch", "") or chassis
            if chassis and name:
                index.setdefault(chassis, (name, ""))
    return index


def _own_clock(port: Dict[str, Any]) -> str:
    """portIdentity is "021122.fffe.000098-1"; the clock is everything before the dash."""
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
    owner: Dict[int, str] = {}
    for result in results or []:
        name = getattr(result, "device_name", "") or getattr(result, "target", "")
        for nb in getattr(result, "neighbors", []) or []:
            owner[id(nb)] = name
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
    reciprocal_ports: Dict[int, List[List[str]]] = {}
    for key, listeners in heard.items():
        far = by_clock.get(key)
        if far is None:
            continue
        for nb, _port in listeners:
            host = owner.get(id(nb)) or getattr(nb, "host", "")
            iface = getattr(nb, "local_iface", "")
            if host or iface:
                reciprocal.setdefault(id(far[0]), []).append(
                    ("%s %s" % (host, iface)).strip())
                reciprocal_ports.setdefault(id(far[0]), []).append([host, iface])

    for nb, port in ports:
        labels, best = [], ""
        placed: List[List[str]] = []
        # The arithmetic cuts both ways. One clock audible from two of our
        # ports rules out a cable on at least one of them; one port hearing
        # two clocks rules out a cable on that port, whatever the far ends
        # think. A splitter or a hub looks exactly like this.
        alone = len({(raw.get("clock_identity") or raw.get("mac") or "").lower()
                     for raw in port.get("peers") or []}) == 1
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
            if name != peer.label:
                # Keep the device and interface, not only the text: the next
                # check along wants to look the far port up, and parsing a
                # display string back into two fields is a trap.
                seat = index.get(normalise_mac(peer.mac or "")) or index.get(
                    normalise_mac(clock_identity_to_mac(peer.clock_identity)))
                if seat and seat[1]:
                    placed.append([seat[0], seat[1]])
            if name != peer.label and listeners == 1 and mutual and alone:
                labels.append(name)
                best = best or "direct"
            elif name != peer.label:
                labels.append(name)
                best = best or "segment"
            else:
                labels.append(peer.label)
        if placed:
            nb.extra["ptp_peer_ports"] = placed
        best_gm = ""
        for raw in port.get("peers") or []:
            if not raw.get("gm_identity"):
                continue
            best_gm = describe_grandmaster(
                PtpPeer(gm_identity=raw.get("gm_identity"),
                        gm_class=raw.get("gm_class"),
                        gm_accuracy=raw.get("gm_accuracy"),
                        steps_removed=raw.get("steps_removed"),
                        time_source=raw.get("time_source")), index)
            break
        if best_gm:
            nb.extra["ptp_grandmaster"] = best_gm
        if not labels:
            others = sorted(set(reciprocal.get(id(nb), [])))
            if not others:
                continue
            nb.extra["ptp_peer"] = ", ".join(others)
            nb.extra["ptp_peer_evidence"] = "heard-by"
            seats = reciprocal_ports.get(id(nb)) or []
            if seats:
                nb.extra["ptp_peer_ports"] = seats
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


RESPONSE_LINE = re.compile(
    r"^\s+(?P<clock>[0-9a-fA-F.]+)-(?P<port>\d+)\s+seq\s+\d+\s+"
    r"RESPONSE\s+MANAGEMENT\s+(?P<dataset>[A-Z_0-9]+)")

SEGMENT_DATASETS = ("DEFAULT_DATA_SET", "PARENT_DATA_SET",
                    "TIME_PROPERTIES_DATA_SET")


def parse_pmc_responses(text: str) -> List[Dict[str, Any]]:
    """Every clock that answered, and what each one said.

    pmc prints one block per responder, so a single query to the segment can
    come back with several clocks. The header line names the clock and the data
    set; the indented lines under it are the fields.
    """
    out: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in (text or "").splitlines():
        match = RESPONSE_LINE.match(line)
        if match:
            current = {"clock_identity": match.group("clock").lower(),
                       "port": int(match.group("port")),
                       "dataset": match.group("dataset"), "fields": {}}
            out.append(current)
            continue
        if current is None or not line.startswith((" ", "\t")):
            continue
        field_match = PMC_FIELD.match(line)
        if field_match:
            current["fields"].setdefault(field_match.group(1),
                                         field_match.group(2).strip())
    return out


def _segment_command(iface: str, domain: int, hops: int,
                     datasets: Tuple[str, ...]) -> str:
    asks = " ".join(shlex.quote("GET " + d) for d in datasets)
    return ("timeout 10 pmc -2 -i %s -d %d -b %d %s 2>/dev/null"
            % (shlex.quote(iface), max(domain, 0), max(hops, 1), asks))


def query_segment(run: Callable[[str], str], iface: str, domain: int = 0,
                  hops: int = 1, datasets: Tuple[str, ...] = SEGMENT_DATASETS,
                  log: Optional[Callable[[str], None]] = None
                  ) -> List[Dict[str, Any]]:
    """Ask the wire itself: who else is a PTP clock on this segment?

    This is the protocol's own management mechanism rather than anything
    vendor-specific, so it needs no credentials and no SNMP community - the
    query goes out as a PTP message and whatever implements the responder
    answers. Implementing it is optional, so silence means "this device does
    not answer management messages", never "there is no clock there": the
    passive Announce read already proved otherwise.

    GET only. Nothing here ever changes a clock's configuration.
    """
    say = log or (lambda _m: None)
    try:
        text = run(_segment_command(iface, domain, hops, datasets))
    except Exception as exc:
        say("  ptp: segment query on %s failed: %s" % (iface, exc))
        return []
    merged: Dict[str, Dict[str, Any]] = {}
    for answer in parse_pmc_responses(text):
        entry = merged.setdefault(answer["clock_identity"],
                                  {"clock_identity": answer["clock_identity"],
                                   "port": answer["port"], "datasets": {}})
        entry["datasets"][answer["dataset"]] = answer["fields"]
    return list(merged.values())


def describe_responder(entry: Dict[str, Any],
                       known: Optional[Dict[str, Tuple[str, str]]] = None) -> str:
    """One line per clock that answered the segment query."""
    known = known or {}
    clock = entry.get("clock_identity", "")
    hit = known.get(normalise_mac(clock_identity_to_mac(clock)))
    name = ("%s %s" % hit).strip() if hit else clock
    sets = entry.get("datasets") or {}
    default = sets.get("DEFAULT_DATA_SET") or {}
    parent = sets.get("PARENT_DATA_SET") or {}
    bits = []
    if default.get("slaveOnly") == "1":
        bits.append("slave only")
    if default.get("clockClass"):
        try:
            bits.append("class %s" % describe_clock_class(int(default["clockClass"])))
        except ValueError:
            bits.append("class %s" % default["clockClass"])
    if default.get("numberPorts"):
        bits.append("%s port(s)" % default["numberPorts"])
    gm = parent.get("grandmasterIdentity")
    if gm:
        gm_hit = known.get(normalise_mac(clock_identity_to_mac(gm)))
        bits.append("grandmaster %s" % (("%s %s" % gm_hit).strip() if gm_hit else gm))
    return "%s (%s)" % (name, ", ".join(bits)) if bits else name

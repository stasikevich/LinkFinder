"""PTP: daemon state, wire decoding, and how far the evidence is allowed to go.
Run: python tests/test_ptp.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.model import Neighbor, TargetResult
from linkfinder.ptp import (ANNOUNCE_LEN, PtpPeer, PtpPort, apply_pmc,
                            clock_identity, describe_clock_class,
                            describe_grandmaster, describe_responder,
                            normalise_mac, parse_pmc_responses, query_segment,
                            clock_identity_to_mac, collect_ptp, decode_ptp,
                            describe_peer, known_port_index, name_peers,
                            parse_phc_map, parse_pmc, parse_ptp4l_processes,
                            peers_from_capture, ptp_from_frame)

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-62s %s" % (label, "ok" if got == want else "FAIL"))


def ptp_message(msg_type, domain, clock, port_number=1, gm=None):
    body = bytearray(34)
    body[0] = msg_type
    body[1] = 0x02
    body[4] = domain
    body[20:28] = clock
    struct.pack_into("!H", body, 28, port_number)
    if gm is not None:
        body += bytes(19) + gm + bytes(3)
    return bytes(body)


def ether(src, payload, dst=b"\x01\x1b\x19\x00\x00\x00", ethertype=0x88F7):
    return dst + src + struct.pack("!H", ethertype) + payload


def pcap(frames):
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    for frame in frames:
        out += struct.pack("<IIII", 0, 0, len(frame), len(frame)) + frame
    return out


PS_OUTPUT = """
ptp4l -2 --uds_address=/run/ptp4l-enp23s0_slave -i enp23s0f2 --dataset_comparison=G.8275.x --domain=24 --clientOnly=1
phc2sys -senp23s0f2 -cCLOCK_REALTIME -z/run/ptp4l-enp23s0_slave -n24
ptp4l -2 --uds_address=/run/ptp4l-enp23s0_master -i enp23s0f0 --domain=24 --serverOnly=1
"""

print("-- reading what the daemons are actually doing --")
ports = parse_ptp4l_processes(PS_OUTPUT)
check("one entry per ptp4l, phc2sys ignored", len(ports), 2)
by_iface = {p.iface: p for p in ports}
check("the slave side is found", by_iface["enp23s0f2"].role, "slave")
check("the master side is found", by_iface["enp23s0f0"].role, "master")
check("domain comes off the command line", by_iface["enp23s0f0"].domain, 24)
check("so does the socket pmc has to use", by_iface["enp23s0f2"].uds,
      "/run/ptp4l-enp23s0_slave")
check("and the transport", by_iface["enp23s0f2"].transport, "L2")
check("a host with no ptp4l yields nothing", parse_ptp4l_processes("bash\nsshd\n"), [])
check("a ptp4l without -i is not a port",
      parse_ptp4l_processes("ptp4l --domain=24 -f /etc/ptp.conf"), [])

print()
print("-- pmc output --")
PORT_DATA = """sending: GET PORT_DATA_SET
\t507c6f.fffe.85109a-1 seq 0 RESPONSE MANAGEMENT PORT_DATA_SET
\t\tportIdentity            507c6f.fffe.85109a-1
\t\tportState               SLAVE
\t\tlogMinDelayReqInterval  0
"""
TIME_STATUS = """sending: GET TIME_STATUS_NP
\t\tmaster_offset              -3
\t\tgmPresent                  true
\t\tgmIdentity                 000580.fffe.08355b
"""
port = apply_pmc(PtpPort(iface="enp23s0f2", domain=24), PORT_DATA, TIME_STATUS)
check("state", port.state, "SLAVE")
check("our own clock identity", port.port_identity, "507c6f.fffe.85109a-1")
check("the grandmaster", port.gm_identity, "000580.fffe.08355b")
check("offset is signed", port.offset_ns, -3)
check("gmPresent", port.gm_present, True)
check("summary reads like a status line", port.summary, "SLAVE dom 24 -3ns")
check("pmc that answered nothing leaves no state",
      apply_pmc(PtpPort(iface="x"), "sending: GET PORT_DATA_SET\n", "").state, "")
check("parse_pmc ignores the unindented echo line",
      "sending:" in parse_pmc(PORT_DATA), False)

print()
print("-- clock identity is an EUI-64 of the MAC --")
check("raw bytes to the printed form",
      clock_identity(bytes.fromhex("507c6ffffe851098")), "507c6f.fffe.851098")
check("and back to the MAC", clock_identity_to_mac("507c6f.fffe.851098"),
      "50:7c:6f:85:10:98")
check("ff:ff padding is also used by some vendors",
      clock_identity_to_mac("00e0ed.ffff.f9b9b1"), "00:e0:ed:f9:b9:b1")
check("an identity not built from a MAC is not turned into one",
      clock_identity_to_mac("0102030405060708"), "")
check("nonsense in, nothing out", clock_identity_to_mac("rubbish"), "")

print()
print("-- decoding PTP off the wire --")
CLOCK = bytes.fromhex("00e0edfffef9b9b1")
GM = bytes.fromhex("000580fffe08355b")
sync = decode_ptp(ptp_message(0x00, 24, CLOCK, 3))
check("message type", sync["type"], "Sync")
check("domain", sync["domain"], 24)
check("source clock", sync["clock_identity"], "00e0ed.fffe.f9b9b1")
check("source port number", sync["port_number"], 3)
announce = decode_ptp(ptp_message(0x0B, 24, CLOCK, 1, gm=GM))
check("an Announce also carries the grandmaster", announce["gm_identity"],
      "000580.fffe.08355b")
check("a Sync does not pretend to", sync["gm_identity"], "")
check("a v1 frame is refused", decode_ptp(b"\x00\x01" + bytes(40)), None)
check("a short frame is refused", decode_ptp(b"\x00\x02"), None)

over_udp = (b"\x45\x00" + bytes(7) + b"\x11" + bytes(10)
            + struct.pack("!HHHH", 1234, 319, 0, 0) + ptp_message(0x00, 24, CLOCK))
check("PTP over UDP is decoded too",
      ptp_from_frame(ether(b"\xaa\xbb\xcc\x00\x00\x01", over_udp,
                           ethertype=0x0800))[1]["domain"], 24)
check("an LLDP frame is not PTP",
      ptp_from_frame(ether(b"\xaa\xbb\xcc\x00\x00\x01", b"\x02\x07", ethertype=0x88CC)),
      None)

print()
print("-- folding a capture into peers --")
frames = [ether(b"\x00\xe0\xed\xf9\xb9\xb1", ptp_message(0x00, 24, CLOCK)),
          ether(b"\x00\xe0\xed\xf9\xb9\xb1", ptp_message(0x0B, 24, CLOCK, gm=GM)),
          ether(b"\x50\x7c\x6f\x85\x10\x98", ptp_message(0x00, 24,
                                                         bytes.fromhex("507c6ffffe851098")))]
peers = peers_from_capture(pcap(frames), our_macs=["50:7c:6f:85:10:98"])
check("our own transmissions are not peers", len(peers), 1)
check("the peer is named by its clock", peers[0].clock_identity, "00e0ed.fffe.f9b9b1")
check("both message types are remembered", sorted(peers[0].messages),
      ["Announce", "Sync"])
check("frames are counted", peers[0].frames, 2)
check("the grandmaster is picked up from the Announce", peers[0].gm_identity,
      "000580.fffe.08355b")
check("an empty capture gives no peers", peers_from_capture(pcap([])), [])

print()
print("-- naming the far end only as strongly as the evidence allows --")


def host(target, iface, mac, clock, peers):
    result = TargetResult(target=target)
    nb = Neighbor(host=target, local_iface=iface, local_mac=mac)
    nb.extra["ptp"] = PtpPort(
        iface=iface, port_identity=clock + "-1", domain=24,
        peers=[PtpPeer(clock_identity=c, mac=clock_identity_to_mac(c))
               for c in peers]).to_dict()
    result.neighbors.append(nb)
    return result


A = "507c6f.fffe.851098"
B = "507c6f.fffe.851077"
C = "507c6f.fffe.8510a1"

pair = [host("10.0.0.1", "f0", "50:7c:6f:85:10:98", A, [B]),
        host("10.0.0.2", "f3", "50:7c:6f:85:10:77", B, [A])]
name_peers(pair)
first = pair[0].neighbors[0]
check("a mutual one-to-one pair is called a likely direct cable",
      first.extra["ptp_peer_evidence"], "direct")
check("and the far end is named by host and port", first.extra["ptp_peer"],
      "10.0.0.2 f3")
check("the wording does not claim proof", "direct cable" in first.note, True)
check("nor does it say proven", "proven" in first.note, False)

shared = [host("10.0.0.1", "f0", "50:7c:6f:85:10:98", A, [B]),
          host("10.0.0.2", "f3", "50:7c:6f:85:10:77", B, [A]),
          host("10.0.0.3", "f1", "50:7c:6f:85:10:a1", C, [B])]
name_peers(shared)
check("one clock heard by two of our ports is no longer a cable",
      shared[0].neighbors[0].extra["ptp_peer_evidence"], "segment")
check("and the row says a multicast frame proves no such thing",
      "does not prove a direct cable" in shared[0].neighbors[0].note, True)

lonely = [host("10.0.0.1", "f0", "50:7c:6f:85:10:98", A, ["000580.fffe.08355b"])]
name_peers(lonely)
check("a clock we have never scanned is reported as the identity we heard",
      lonely[0].neighbors[0].extra["ptp_peer"], "000580.fffe.08355b")
check("with no directness claimed",
      lonely[0].neighbors[0].extra["ptp_peer_evidence"], "heard")

quiet = [host("10.0.0.1", "f0", "50:7c:6f:85:10:98", A, [B]),
         host("10.0.0.2", "f3", "50:7c:6f:85:10:77", B, [])]
name_peers(quiet)
check("hearing is symmetric: the silent end is told who heard it",
      quiet[1].neighbors[0].extra["ptp_peer"], "10.0.0.1 f0")
check("and that is graded as heard-by, not as a cable",
      quiet[1].neighbors[0].extra["ptp_peer_evidence"], "heard-by")

print()
print("-- a resolved port keeps the answer LLDP already gave --")
solid = host("10.0.0.1", "f0", "50:7c:6f:85:10:98", A, [B])
solid.neighbors[0].switch = "sw-1"
solid.neighbors[0].port = "Eth1/1"
solid.neighbors[0].note = ""
name_peers([solid, host("10.0.0.2", "f3", "50:7c:6f:85:10:77", B, [A])])
check("the note is left alone", solid.neighbors[0].note, "")
check("but the peer is still recorded", solid.neighbors[0].extra["ptp_peer"],
      "10.0.0.2 f3")

print()
print("-- collecting over a fake shell --")
ANSWERS = [
    ("PORT_DATA_SET", PORT_DATA),
    ("TIME_STATUS_NP", TIME_STATUS),
    ("/sys/class/net", "enp23s0f0 ptp13\nenp23s0f2 ptp14\n"),
    ("[p]tp4l", PS_OUTPUT),
]


def fake_run(command):
    """Ordered on purpose: the pmc command line contains ptp4l as well."""
    for needle, answer in ANSWERS:
        if needle in command:
            return answer
    return ""


got = collect_ptp(fake_run, ["enp23s0f0", "enp23s0f2"])
check("both ports come back", sorted(got), ["enp23s0f0", "enp23s0f2"])
check("the PHC index is attached", got["enp23s0f0"].phc, "ptp13")
check("pmc state landed on the port", got["enp23s0f2"].state, "SLAVE")
check("an interface we did not ask about is skipped",
      "enp23s0f1" in collect_ptp(fake_run, ["enp23s0f0"]), False)


def exploding_run(_command):
    raise OSError("ssh died")


check("a shell that blows up does not take the scan with it",
      collect_ptp(exploding_run, ["eth0"]), {})
check("phc map parser ignores junk", parse_phc_map("eth0 ptp0\nrubbish\n"),
      {"eth0": "ptp0"})

print()
print("-- describing a peer --")
index = known_port_index([host("10.0.0.9", "eno5", "50:7c:6f:85:10:98", A, [])])
check("a MAC we scanned becomes a host and a port",
      describe_peer(PtpPeer(clock_identity=A), index), "10.0.0.9 eno5")
check("one we did not stays the clock identity",
      describe_peer(PtpPeer(clock_identity="aabbcc.fffe.ddeeff"), index),
      "aabbcc.fffe.ddeeff")


print()
print("-- the Announce body, which is where the grandmaster really lives --")
GM2 = bytes.fromhex("000580fffe08355b")


def announce(gm, cls=6, accuracy=0x21, steps=2, source=0x20, p1=128, p2=128,
             trace=()):
    body = bytearray(ANNOUNCE_LEN)
    body[0] = 0x0B
    body[1] = 0x02
    body[4] = 24
    body[20:28] = CLOCK
    struct.pack_into("!H", body, 28, 1)
    struct.pack_into("!h", body, 44, 37)
    body[47] = p1
    body[48] = cls
    body[49] = accuracy
    struct.pack_into("!H", body, 50, 0x4E5D)
    body[52] = p2
    body[53:61] = gm
    struct.pack_into("!H", body, 61, steps)
    body[63] = source
    out = bytes(body)
    if trace:
        value = b"".join(trace)
        out += struct.pack("!HH", 0x0008, len(value)) + value
    return out


full = decode_ptp(announce(GM2))
check("grandmaster identity", full["gm_identity"], "000580.fffe.08355b")
check("clock class", full["gm_class"], 6)
check("clock accuracy", full["gm_accuracy"], 0x21)
check("priority1", full["gm_priority1"], 128)
check("priority2", full["gm_priority2"], 128)
check("steps removed", full["steps_removed"], 2)
check("time source", full["time_source"], 0x20)
check("utc offset", full["utc_offset"], 37)
check("no path trace unless the TLV is on", full["path_trace"], [])

traced = decode_ptp(announce(GM2, trace=(GM2, CLOCK)))
check("path trace lists the chain", traced["path_trace"],
      ["000580.fffe.08355b", "00e0ed.fffe.f9b9b1"])
check("a Sync carries none of this", decode_ptp(ptp_message(0x00, 24, CLOCK)).get("gm_class"),
      None)

print()
print("-- clock class means something, and says so --")
check("locked to a primary reference", describe_clock_class(6),
      "6 (locked to a primary reference)")
check("free-running boundary clock", describe_clock_class(165),
      "165 (boundary clock, free-running)")
check("an unknown class is still reported", describe_clock_class(99), "99")
check("none means nothing", describe_clock_class(None), "")

print()
print("-- a switch is a device we can name --")
check("LLDP separators do not matter", normalise_mac("00-05-80-08-35-5B"),
      "00:05:80:08:35:5b")
check("so does the Cisco form", normalise_mac("0005.8008.355b"),
      "00:05:80:08:35:5b")
check("rubbish is not a MAC", normalise_mac("not-a-mac"), "")

switch_side = TargetResult(target="198.51.100.10")
switch_side.device_name = "node-a"
nb = Neighbor(host="198.51.100.10", local_iface="f2", local_mac="50:7c:6f:85:10:9a",
              switch="sw-core-1", chassis_id="00-05-80-08-35-5B", proto="LLDP")
switch_side.neighbors.append(nb)
index = known_port_index([switch_side])
check("the switch chassis MAC is in the index",
      index.get("00:05:80:08:35:5b"), ("sw-core-1", ""))
check("our own NIC is still there",
      index.get("50:7c:6f:85:10:9a"), ("node-a", "f2"))
check("a grandmaster that is the switch gets named, not printed as hex",
      describe_peer(PtpPeer(clock_identity="000580.fffe.08355b"), index), "sw-core-1")

peer = PtpPeer(clock_identity="507c6f.fffe.851077", gm_identity="000580.fffe.08355b",
               gm_class=6, gm_accuracy=0x21, steps_removed=2, time_source=0x20)
told = describe_grandmaster(peer, index)
check("the grandmaster line names the switch", told.startswith("sw-core-1"), True)
check("and carries its class", "locked to a primary reference" in told, True)
check("and the time source", "GNSS" in told, True)
check("and how far away it is", "2 hop(s) away" in told, True)
check("nothing to say without an Announce", describe_grandmaster(PtpPeer()), "")

print()
print("-- asking the segment, which needs no credentials --")
PMC_SEGMENT = """sending: GET DEFAULT_DATA_SET
\t507c6f.fffe.851077-1 seq 0 RESPONSE MANAGEMENT DEFAULT_DATA_SET
\t\ttwoStepFlag             1
\t\tslaveOnly               1
\t\tnumberPorts             1
\t\tclockClass              255
sending: GET PARENT_DATA_SET
\t507c6f.fffe.851077-1 seq 1 RESPONSE MANAGEMENT PARENT_DATA_SET
\t\tgrandmasterIdentity     000580.fffe.08355b
\t000580.fffe.08355b-3 seq 2 RESPONSE MANAGEMENT PARENT_DATA_SET
\t\tgrandmasterIdentity     000580.fffe.08355b
"""
rows = parse_pmc_responses(PMC_SEGMENT)
check("one entry per response block", len(rows), 3)
check("two different clocks answered",
      len({r["clock_identity"] for r in rows}), 2)
check("the data set is named", rows[0]["dataset"], "DEFAULT_DATA_SET")
check("and its fields are captured", rows[0]["fields"]["clockClass"], "255")
check("the echo line is not a response", parse_pmc_responses("sending: GET X"), [])
check("silence parses to nothing", parse_pmc_responses(""), [])

answers = query_segment(lambda _c: PMC_SEGMENT, "eth0", domain=24)
check("responders are merged per clock", len(answers), 2)
by_clock = {a["clock_identity"]: a for a in answers}
check("both data sets land on one clock",
      sorted(by_clock["507c6f.fffe.851077"]["datasets"]),
      ["DEFAULT_DATA_SET", "PARENT_DATA_SET"])
line = describe_responder(by_clock["507c6f.fffe.851077"], index)
check("a responder is described in words", "slave only" in line, True)
check("and its grandmaster is named", "grandmaster sw-core-1" in line, True)


def no_pmc(_command):
    raise OSError("pmc is not installed")


check("a device without pmc is not an error", query_segment(no_pmc, "eth0"), [])
check("a device that answers nothing is not an error",
      query_segment(lambda _c: "", "eth0"), [])

print()
print("-- the switch side over SNMP --")
from linkfinder.ptp_snmp import PTPBASE, read_switch_ptp_safely, render


class FakeAgent:
    def __init__(self, mib):
        self.mib = mib

    def walk(self, root, limit=4000):
        for oid in sorted(self.mib):
            if oid.startswith(root.rstrip(".") + "."):
                yield oid, self.mib[oid]


class FakePtpSwitch:
    def __init__(self, mib, name="sw-1"):
        self.host = "198.51.100.1"
        self.name = name
        self.client = FakeAgent(mib)


silent = read_switch_ptp_safely(FakePtpSwitch({}))
check("a switch with no PTP objects is reported as such", silent.supported, False)
check("and said plainly", "publishes no standard PTP MIB" in silent.summary(), True)
check("nothing is invented for it", silent.tables, {})

vendor_only = read_switch_ptp_safely(
    FakePtpSwitch({"1.3.6.1.4.1.9.9.760.1.1.1.0": 1}))
check("a vendor tree is noticed but not decoded", vendor_only.supported, False)
check("and named", vendor_only.vendor_trees, ["Cisco CISCO-PTP-MIB"])

standard = read_switch_ptp_safely(FakePtpSwitch({
    PTPBASE + ".1.1.6.1.3.1.1.1": 6,
    PTPBASE + ".1.1.6.1.9.1.1.1": b"" + bytes.fromhex("000580fffe08355b"),
    PTPBASE + ".1.1.5.1.4.1.1.1": b"grandmaster",
}))
check("a standard MIB is picked up", standard.supported, True)
check("and named as the source", standard.source, "PTPBASE-MIB (RFC 8173)")
check("tables are kept apart", sorted(standard.tables), ["default", "parent"])
check("a binary value is rendered the way a clock identity is written",
      standard.tables["default"]["1.9.1.1.1"], "00:05:80:ff:fe:08:35:5b")
check("the summary counts what was read", "3 object(s)" in standard.summary(), True)
check("rendering mentions the switch", "sw-1" in render([standard]), True)


class ExplodingSwitch:
    host = "198.51.100.1"
    name = "sw-boom"

    @property
    def client(self):
        raise RuntimeError("agent went away")


check("an SNMP blow-up is caught, not propagated",
      read_switch_ptp_safely(ExplodingSwitch()).error.startswith("RuntimeError"), True)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all ptp checks passed")

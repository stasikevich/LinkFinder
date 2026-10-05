"""Offline checks for the hand-rolled SNMPv2c codec, including a live loopback
exchange against a fake agent on 127.0.0.1. Run: python tests/test_snmp.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import snmp

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-26s %-40r %s" % (label, got, "ok" if got == want else "FAIL"))


for oid in ("1.3.6.1.2.1.1.5.0", "1.0.8802.1.1.2.1.4.1.1.9.0.5.1",
            "1.3.6.1.4.1.9.9.23.1.2.1.1.6.128.1", "1.3.6.1.2.1.17.4.3.1.2.0.17.34.51.68.85"):
    tag, body, _ = snmp.dec_tlv(snmp.enc_oid(oid), 0)
    check("oid roundtrip", snmp.dec_oid(body), oid)

for n in (0, 1, 127, 128, 255, 256, 65535, 2147483647, -1, -129, -256):
    tag, body, _ = snmp.dec_tlv(snmp.enc_int(n), 0)
    check("int roundtrip %d" % n, snmp.dec_int(body), n)

check("as_mac", snmp.as_mac(bytes.fromhex("001122334455")), "00:11:22:33:44:55")
check("as_text printable", snmp.as_text(b"sw-access-1"), "sw-access-1")
check("as_text binary", snmp.as_text(bytes.fromhex("00112233")), "00:11:22:33")
check("subtree yes", snmp.oid_in_subtree("1.3.6.1.2.1.1.5.0", "1.3.6.1.2.1.1.5"), True)
check("subtree no", snmp.oid_in_subtree("1.3.6.1.2.1.1.50.0", "1.3.6.1.2.1.1.5"), False)


MIB = {
    "1.3.6.1.2.1.1.5.0": ("octets", b"sw-access-1"),
    "1.3.6.1.2.1.31.1.1.1.1.1": ("octets", b"1/1/1"),
    "1.3.6.1.2.1.31.1.1.1.1.5": ("octets", b"1/1/5"),
    "1.3.6.1.2.1.31.1.1.1.1.9": ("octets", b"1/1/9"),
}


def _enc_value(kind, value):
    if kind == "octets":
        return snmp.enc_octets(value)
    return snmp.enc_int(value)


def fake_agent(sock, stop):
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        _t, body, _ = snmp.dec_tlv(data, 0)
        pos = 0
        _t, _ver, pos = snmp.dec_tlv(body, pos)
        _t, _comm, pos = snmp.dec_tlv(body, pos)
        ptag, pdu, _ = snmp.dec_tlv(body, pos)
        p = 0
        _t, rid_raw, p = snmp.dec_tlv(pdu, p)
        _t, _err, p = snmp.dec_tlv(pdu, p)
        _t, maxrep_raw, p = snmp.dec_tlv(pdu, p)
        _t, vbs, _ = snmp.dec_tlv(pdu, p)
        _t, one, _ = snmp.dec_tlv(vbs, 0)
        _t, oid_raw, _ = snmp.dec_tlv(one, 0)
        asked = snmp.dec_oid(oid_raw)
        keys = sorted(MIB, key=lambda o: [int(x) for x in o.split(".")])
        out = b""
        if ptag == snmp.PDU_GET:
            kind, value = MIB.get(asked, (None, None))
            out = snmp._tlv(snmp.TAG_SEQ, snmp.enc_oid(asked) + (
                _enc_value(kind, value) if kind else snmp._tlv(snmp.TAG_NO_SUCH_OBJECT, b"")))
        else:
            after = [k for k in keys if [int(x) for x in k.split(".")] > [int(x) for x in asked.split(".")]]
            for key in after[:snmp.dec_int(maxrep_raw) or 10]:
                kind, value = MIB[key]
                out += snmp._tlv(snmp.TAG_SEQ, snmp.enc_oid(key) + _enc_value(kind, value))
            if not out:
                out = snmp._tlv(snmp.TAG_SEQ, snmp.enc_oid(asked) + snmp._tlv(snmp.TAG_END_OF_MIB, b""))
        pdu_out = snmp.enc_int(snmp.dec_int(rid_raw)) + snmp.enc_int(0) + snmp.enc_int(0) + \
            snmp._tlv(snmp.TAG_SEQ, out)
        msg = snmp.enc_int(1) + snmp.enc_octets(b"public") + snmp._tlv(snmp.PDU_RESPONSE, pdu_out)
        sock.sendto(snmp._tlv(snmp.TAG_SEQ, msg), addr)


srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
srv.bind(("127.0.0.1", 0))
srv.settimeout(0.3)
stop = threading.Event()
thread = threading.Thread(target=fake_agent, args=(srv, stop))
thread.daemon = True
thread.start()

client = snmp.SnmpClient("127.0.0.1", "public", port=srv.getsockname()[1], timeout=2.0)
check("live get sysName", snmp.as_text(client.get_one("1.3.6.1.2.1.1.5.0")), "sw-access-1")
check("live alive", client.alive(), True)
check("live walk ifName",
      {k: snmp.as_text(v) for k, v in client.walk_dict("1.3.6.1.2.1.31.1.1.1.1").items()},
      {"1": "1/1/1", "5": "1/1/5", "9": "1/1/9"})
check("live walk bounded", len(list(client.walk("1.3.6.1.2.1.1.5"))), 1)

dead = snmp.SnmpClient("127.0.0.1", "public", port=srv.getsockname()[1] + 1, timeout=0.3, retries=0)
check("dead agent", dead.alive(), False)

stop.set()
thread.join(timeout=2)
srv.close()

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("all snmp checks passed")

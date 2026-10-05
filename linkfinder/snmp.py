"""Minimal SNMPv2c over UDP, pure stdlib.

Enough BER to do get / getnext / getbulk walks. Written by hand so the tool has
no pysnmp dependency and still freezes cleanly into a single .exe.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import os
import socket
import struct
from typing import Dict, Iterator, List, Optional, Tuple

TAG_INT, TAG_OCTETS, TAG_NULL, TAG_OID, TAG_SEQ = 0x02, 0x04, 0x05, 0x06, 0x30
TAG_IPADDR, TAG_COUNTER32, TAG_GAUGE32, TAG_TIMETICKS = 0x40, 0x41, 0x42, 0x43
TAG_OPAQUE, TAG_COUNTER64 = 0x44, 0x46
TAG_NO_SUCH_OBJECT, TAG_NO_SUCH_INSTANCE, TAG_END_OF_MIB = 0x80, 0x81, 0x82
PDU_GET, PDU_GETNEXT, PDU_RESPONSE, PDU_GETBULK = 0xA0, 0xA1, 0xA2, 0xA5

END_TAGS = (TAG_NO_SUCH_OBJECT, TAG_NO_SUCH_INSTANCE, TAG_END_OF_MIB)


class SnmpError(Exception):
    pass


def _enc_len(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = b""
    while length:
        raw = bytes([length & 0xFF]) + raw
        length >>= 8
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _enc_len(len(body)) + body


def enc_int(value: int) -> bytes:
    raw = b""
    negative = value < 0
    work = value
    while True:
        raw = bytes([work & 0xFF]) + raw
        work >>= 8
        if (not negative and work == 0 and not raw[0] & 0x80) or \
           (negative and work == -1 and raw[0] & 0x80):
            break
        if not negative and work == 0:
            raw = b"\x00" + raw
            break
    return _tlv(TAG_INT, raw)


def enc_octets(value: bytes) -> bytes:
    return _tlv(TAG_OCTETS, value)


def enc_oid(oid: str) -> bytes:
    arcs = [int(a) for a in oid.strip(".").split(".")]
    if len(arcs) < 2:
        raise SnmpError("bad oid %r" % oid)
    body = bytes([40 * arcs[0] + arcs[1]])
    for arc in arcs[2:]:
        if arc < 0x80:
            body += bytes([arc])
            continue
        chunk = b""
        while arc:
            chunk = bytes([(arc & 0x7F) | (0x80 if chunk else 0)]) + chunk
            arc >>= 7
        body += chunk
    return _tlv(TAG_OID, body)


def dec_tlv(data: bytes, pos: int) -> Tuple[int, bytes, int]:
    if pos + 2 > len(data):
        raise SnmpError("truncated TLV")
    tag = data[pos]
    first = data[pos + 1]
    pos += 2
    if first < 0x80:
        length = first
    else:
        nbytes = first & 0x7F
        length = int.from_bytes(data[pos:pos + nbytes], "big")
        pos += nbytes
    if pos + length > len(data):
        raise SnmpError("truncated value")
    return tag, data[pos:pos + length], pos + length


def dec_int(raw: bytes) -> int:
    return int.from_bytes(raw, "big", signed=True) if raw else 0


def dec_oid(raw: bytes) -> str:
    if not raw:
        return ""
    arcs = [raw[0] // 40, raw[0] % 40]
    value = 0
    for byte in raw[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            arcs.append(value)
            value = 0
    return ".".join(str(a) for a in arcs)


def dec_value(tag: int, raw: bytes):
    if tag == TAG_INT:
        return dec_int(raw)
    if tag in (TAG_COUNTER32, TAG_GAUGE32, TAG_TIMETICKS, TAG_COUNTER64):
        return int.from_bytes(raw, "big") if raw else 0
    if tag == TAG_OID:
        return dec_oid(raw)
    if tag == TAG_IPADDR:
        return ".".join(str(b) for b in raw)
    if tag == TAG_NULL:
        return None
    if tag in END_TAGS:
        return "__END__"
    return raw


def as_text(value) -> str:
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8").strip("\x00").strip()
        except UnicodeDecodeError:
            return ":".join("%02x" % b for b in value)
        if text and all(31 < ord(c) < 127 or c in "\t" for c in text):
            return text
        return ":".join("%02x" % b for b in value)
    return "" if value is None else str(value)


def as_mac(value) -> str:
    if isinstance(value, bytes) and len(value) == 6:
        return ":".join("%02x" % b for b in value)
    return as_text(value)


def oid_in_subtree(oid: str, root: str) -> bool:
    return oid == root or oid.startswith(root.rstrip(".") + ".")


class SnmpClient:
    def __init__(self, host: str, community: str = "public", port: int = 161,
                 timeout: float = 2.0, retries: int = 2):
        self.host = host
        self.community = community
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self._rid = int.from_bytes(os.urandom(2), "big") + 1

    def _build(self, pdu_type: int, varbinds: List[str], non_rep: int = 0,
               max_rep: int = 0) -> Tuple[int, bytes]:
        self._rid = (self._rid + 1) & 0x7FFFFFFF
        vb = b"".join(_tlv(TAG_SEQ, enc_oid(o) + _tlv(TAG_NULL, b"")) for o in varbinds)
        pdu = enc_int(self._rid) + enc_int(non_rep) + enc_int(max_rep) + _tlv(TAG_SEQ, vb)
        msg = enc_int(1) + enc_octets(self.community.encode()) + _tlv(pdu_type, pdu)
        return self._rid, _tlv(TAG_SEQ, msg)

    def _parse(self, data: bytes) -> Tuple[int, int, List[Tuple[str, object]]]:
        _tag, body, _ = dec_tlv(data, 0)
        pos = 0
        _t, _ver, pos = dec_tlv(body, pos)
        _t, _comm, pos = dec_tlv(body, pos)
        tag, pdu, _pos = dec_tlv(body, pos)
        if tag != PDU_RESPONSE:
            raise SnmpError("unexpected PDU 0x%02x" % tag)
        p = 0
        _t, rid_raw, p = dec_tlv(pdu, p)
        _t, err_raw, p = dec_tlv(pdu, p)
        _t, _idx, p = dec_tlv(pdu, p)
        _t, vbs, _p = dec_tlv(pdu, p)
        out: List[Tuple[str, object]] = []
        q = 0
        while q < len(vbs):
            _t, one, q = dec_tlv(vbs, q)
            r = 0
            _t, oid_raw, r = dec_tlv(one, r)
            vtag, vraw, _r = dec_tlv(one, r)
            out.append((dec_oid(oid_raw), dec_value(vtag, vraw)))
        return dec_int(rid_raw), dec_int(err_raw), out

    def _exchange(self, packet: bytes, rid: int) -> List[Tuple[str, object]]:
        last: Optional[Exception] = None
        for _ in range(self.retries + 1):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
            try:
                sock.sendto(packet, (self.host, self.port))
                data, _addr = sock.recvfrom(65535)
                got_rid, err, varbinds = self._parse(data)
                if got_rid != rid:
                    continue
                if err:
                    raise SnmpError("agent error-status %d" % err)
                return varbinds
            except socket.timeout as exc:
                last = exc
            except OSError as exc:
                last = exc
            finally:
                sock.close()
        raise SnmpError("no SNMP response from %s (%s)" % (self.host, last))

    def get(self, *oids: str) -> List[Tuple[str, object]]:
        rid, packet = self._build(PDU_GET, list(oids))
        return self._exchange(packet, rid)

    def get_one(self, oid: str) -> object:
        res = self.get(oid)
        if not res:
            return None
        value = res[0][1]
        return None if value == "__END__" else value

    def walk(self, root: str, limit: int = 4000) -> Iterator[Tuple[str, object]]:
        current = root
        count = 0
        while count < limit:
            rid, packet = self._build(PDU_GETBULK, [current], non_rep=0, max_rep=25)
            varbinds = self._exchange(packet, rid)
            if not varbinds:
                return
            progressed = False
            for oid, value in varbinds:
                if value == "__END__" or not oid_in_subtree(oid, root):
                    return
                yield oid, value
                current = oid
                count += 1
                progressed = True
            if not progressed:
                return

    def walk_dict(self, root: str, limit: int = 4000) -> Dict[str, object]:
        out: Dict[str, object] = {}
        for oid, value in self.walk(root, limit=limit):
            out[oid[len(root.rstrip(".")) + 1:]] = value
        return out

    def alive(self) -> bool:
        try:
            return self.get_one("1.3.6.1.2.1.1.5.0") is not None
        except SnmpError:
            return False

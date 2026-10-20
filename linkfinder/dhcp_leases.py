"""Who asked this host for an address, read from the files it already keeps.

A device that answers nothing can still be known. Where a host hands out
addresses it writes down every client that ever asked: the MAC, the address
it was given and when the lease was last renewed. That costs no packets, and
it works on a link where nothing replies - which is the normal state of a
fronthaul port whose radio is quiet.

O-RAN makes this the documented path rather than a trick: an O-RU resolves
its management plane over DHCP and identifies itself to the server, so the
server's own records are the authority on what is attached.

A lease is evidence that a device asked, not that it is there now. The
renewal time is kept so nobody has to guess which.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import re
from typing import Any, Callable, Dict, List, Optional

LEASE_AT = re.compile(r"^lease\s+(\S+)\s*\{")
HARDWARE = re.compile(r"hardware\s+ethernet\s+([0-9a-fA-F:]{17})\s*;")
HOSTNAME = re.compile(r'client-hostname\s+"([^"]*)"')
# dhcpd writes what the client said about itself as `set NAME = "value"`,
# and O-RAN names the leaf vendor-class-data rather than -identifier.
VENDOR = re.compile(r'vendor-class-(?:identifier|data)\s*=?\s*"([^"]*)"')
ENDS = re.compile(r"^\s*(?:ends|cltt)\s+\d+\s+([0-9/]+\s+[0-9:]+)\s*;")
BINDING = re.compile(r"^\s*binding state\s+(\w+)\s*;")

KNOWN_FILES = ("/var/lib/dhcp/dhcpd.leases", "/var/lib/dhcpd/dhcpd.leases",
               "/var/lib/misc/dnsmasq.leases", "/var/lib/dnsmasq/dnsmasq.leases")


def lease_files(run: Callable[[str], str]) -> List[str]:
    """Where this host keeps its leases, asking the running server first.

    A server started with -lf writes somewhere nobody would guess, which is
    exactly the case on a packaged base station. Its own command line says
    where, so read that before falling back to the usual places.
    """
    found: List[str] = []
    try:
        listing = run("ps -eo args= 2>/dev/null | grep -E 'dhcpd|dnsmasq' "
                      "| grep -v grep")
    except Exception:
        listing = ""
    for line in (listing or "").splitlines():
        parts = line.split()
        for flag in ("-lf", "--dhcp-leasefile"):
            if flag in parts:
                at = parts.index(flag) + 1
                if at < len(parts):
                    found.append(parts[at])
        for part in parts:
            if part.startswith("--dhcp-leasefile="):
                found.append(part.split("=", 1)[1])
    for path in KNOWN_FILES:
        found.append(path)
    seen, out = set(), []
    for path in found:
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def parse_isc(text: str) -> Dict[str, Dict[str, Any]]:
    """ISC dhcpd leases. The file is appended to, so a later record wins."""
    out: Dict[str, Dict[str, Any]] = {}
    address, record = "", {}
    for line in (text or "").splitlines():
        start = LEASE_AT.match(line.strip())
        if start:
            address, record = start.group(1), {}
            continue
        if not address:
            continue
        if line.strip().startswith("}"):
            mac = record.get("mac", "")
            if mac:
                out[mac] = dict(record, address=address)
            address, record = "", {}
            continue
        hit = HARDWARE.search(line)
        if hit:
            record["mac"] = hit.group(1).lower()
        hit = HOSTNAME.search(line)
        if hit and hit.group(1):
            record["hostname"] = hit.group(1)
        hit = VENDOR.search(line)
        if hit and hit.group(1):
            record["vendor"] = hit.group(1)
        hit = ENDS.match(line)
        if hit:
            record["renewed"] = hit.group(1)
        hit = BINDING.match(line)
        if hit:
            record["state"] = hit.group(1)
    return out


def parse_dnsmasq(text: str) -> Dict[str, Dict[str, Any]]:
    """dnsmasq leases: one line of expiry, MAC, address, name, client id."""
    out: Dict[str, Dict[str, Any]] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        mac = parts[1].lower()
        if len(mac) != 17:
            continue
        record: Dict[str, Any] = {"mac": mac, "address": parts[2],
                                  "state": "active"}
        if len(parts) > 3 and parts[3] not in ("*", ""):
            record["hostname"] = parts[3]
        out[mac] = record
    return out


def collect(run: Callable[[str], str],
            log: Optional[Callable[[str], None]] = None
            ) -> Dict[str, Dict[str, Any]]:
    """Every client this host has leased an address to, by MAC."""
    say = log or (lambda _m: None)
    leases: Dict[str, Dict[str, Any]] = {}
    for path in lease_files(run):
        try:
            text = run("cat %s 2>/dev/null" % path)
        except Exception:
            continue
        if not text or not text.strip():
            continue
        found = parse_dnsmasq(text) if "dnsmasq" in path else parse_isc(text)
        if not found:
            found = parse_isc(text) or parse_dnsmasq(text)
        for mac, record in found.items():
            leases[mac] = dict(record, source=path)
    # A lease file is appended to and keeps every past holder, so one address
    # can carry two MACs - the client on it now and the one before it. Only the
    # newest holder is real; the file is in order, so the last MAC seen for an
    # address wins and the stale one is dropped. Per host: the same 169.254
    # link-local legitimately recurs on another host's own fronthaul.
    newest: Dict[str, str] = {}
    for mac, record in leases.items():
        addr = record.get("address") or ""
        if addr:
            newest[addr] = mac
    keep = set(newest.values())
    leases = {mac: rec for mac, rec in leases.items()
              if not rec.get("address") or mac in keep}
    if leases:
        say("  dhcp: this host has leased an address to %d device(s)"
            % len(leases))
    return leases

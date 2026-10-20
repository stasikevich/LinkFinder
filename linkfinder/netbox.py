"""What the inventory says should be there, next to what the scan found.

NetBox is a record of intent: somebody wrote down which box sits where, what it
is for, and which bench it belongs to. A scan is a record of fact. Neither is
authoritative on its own - the interesting output is where they disagree, and
that disagreement is usually somebody's afternoon saved.

Read-only, deliberately. What a tool measured must never overwrite what a person
recorded: a scan that missed a host for a silly reason would otherwise edit the
inventory to match its own mistake. Every write stays a proposal on disk that a
human applies. The client is built so a writing token can be handed to it later
without the rest of this changing.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import io
import json
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

CACHE = ".linkfinder-netbox.json"
ENV_URL = "LINKFINDER_NETBOX_URL"
ENV_TOKEN = "LINKFINDER_NETBOX_TOKEN"

# NetBox 4.5 issues VERSION 2 tokens, which are two halves: a key that stays
# visible and a secret shown once. The header wants both, joined by a dot and
# prefixed. Sending the secret alone answers 403 "Invalid v1 token", which
# reads like a wrong token and is really half of a right one.
TOKEN_PREFIX = "nbt_"

DEVICES = "/api/dcim/devices/"
IP_ADDRESSES = "/api/ipam/ip-addresses/"
INTERFACES = "/api/dcim/interfaces/"
STATUS = "/api/status/"

# Only the columns that are used. On an estate of ten thousand devices a full
# record is about 4 kB and most of it is links we never read.
DEVICE_FIELDS = ("id,name,serial,asset_tag,role,site,location,rack,tenant,"
                 "device_type,status,tags,primary_ip,url,display_url,last_updated")

# One request per batch, well under any sane URL length.
BATCH = 40
# Measured against a live 4.5 server: it serves 1000 per page and caps
# there, so limit=0 buys nothing and 250 was costing four times the
# round trips for no reason.
PAGE = 1000


def token_warning(token: str) -> str:
    """Say what is wrong with a token before a 403 does it less clearly."""
    text = (token or "").strip()
    if not text:
        return "no NetBox token given"
    if text.startswith(TOKEN_PREFIX):
        return "" if "." in text else (
            "that token has no secret half - a NetBox 4.5 token is"
            " nbt_<key>.<secret>, and the secret is shown only once")
    # An older NetBox issues a plain hexadecimal token and it is perfectly
    # good. Refusing it outright turned a working setup into an error, which
    # was measured: the server accepts it under the Token scheme.
    bare = text.replace("-", "")
    if len(bare) >= 32 and all(c in "0123456789abcdefABCDEF" for c in bare):
        return ""
    return ("that looks like the secret half only - a NetBox 4.5 token is"
            " nbt_<key>.<secret>, where the key is on the token's page."
            " An older NetBox issues a 40-character hexadecimal token,"
            " which is also accepted")


def setup_tags(config: Optional[Dict[str, Any]] = None) -> List[str]:
    """Which NetBox tags count as a Setup. Named one by one, on purpose.

    This estate has 985 tags and 952 of them are in use - teams, programmes,
    projects, benches, all mixed. Nothing in NetBox tells them apart: only 13
    are restricted by object type, and plenty of real benches never say
    "setup" anywhere. So treating every tag as a Setup would bury the few that
    are one, and there is no clever rule to be had. The list is given.
    """
    saved = (config or {}).get("netbox") or {}
    names = saved.get("setup_tags") or []
    if isinstance(names, str):
        names = [names]
    return [str(n).strip() for n in names if str(n).strip()]


def tags_seen(inventory: Dict[str, Any]) -> List[Tuple[str, int]]:
    """Tags carried by the devices this scan matched, commonest first.

    The point is a short list to choose from: a scan touches a few dozen
    devices, so this is a handful of tags rather than the estate's hundreds.
    """
    counted: Dict[str, int] = {}
    for record in inventory.get("devices") or []:
        for tag in record.get("tags") or []:
            if tag:
                counted[tag] = counted.get(tag, 0) + 1
    return sorted(counted.items(), key=lambda kv: (-kv[1], kv[0].lower()))


def settings(url: str = "", token: str = "",
             config: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """Flag, then environment, then the defaults file. Never a hard-coded one."""
    from . import vault

    saved = (config or {}).get("netbox") or {}
    url = (url or os.environ.get(ENV_URL, "") or saved.get("url", "")).strip()
    # The stored one is sealed by the operating system's own store. A token
    # reaches the whole inventory, so it is not kept in a file as typed.
    kept = vault.unprotect(str(saved.get("token", "") or ""))
    token = (token or os.environ.get(ENV_TOKEN, "") or kept).strip()
    return url.rstrip("/"), token


class Netbox:
    """A read-only window on one NetBox. It issues GET and nothing else."""

    def __init__(self, url: str, token: str, verify: bool = True,
                 timeout: int = 60, get: Optional[Callable] = None):
        self.url = (url or "").rstrip("/")
        self.token = (token or "").strip()
        self.verify = verify
        self.timeout = timeout
        self._get = get          # injected in tests; nothing reaches a network

    def scheme(self) -> str:
        """Bearer for a 4.5 token, Token for the older kind.

        Measured against a live 4.5 server: it accepts both for a v2 token,
        and an older server knows only Token. Each kind is sent in its own
        documented form rather than guessing one for both.
        """
        return "Bearer" if str(self.token).startswith(TOKEN_PREFIX) else "Token"

    def get(self, path: str, params: Any = None) -> Dict[str, Any]:
        if self._get is not None:
            return self._get(path, params)
        import requests
        if not self.verify:
            requests.packages.urllib3.disable_warnings()
        answer = requests.get(
            self.url + path, params=params, timeout=self.timeout,
            verify=self.verify,
            headers={"Authorization": self.scheme() + " " + self.token,
                     "Accept": "application/json"})
        if answer.status_code != 200:
            raise RuntimeError("NetBox answered %d for %s: %s"
                               % (answer.status_code, path,
                                  (answer.text or "")[:200]))
        return answer.json()

    def reachable(self) -> str:
        """"" when it answers, else why not. /api/status needs no token at all,
        so it is asked for something that does."""
        try:
            self.get(DEVICES, {"limit": 1})
        except Exception as problem:
            return str(problem)
        return ""

    def every(self, path: str, params: Any) -> List[Dict[str, Any]]:
        """Walk the pages. NetBox hands back count/next/results."""
        out: List[Dict[str, Any]] = []
        offset = 0
        while True:
            page = list(params) if isinstance(params, list) else dict(params or {})
            if isinstance(page, list):
                page += [("limit", PAGE), ("offset", offset)]
            else:
                page.update({"limit": PAGE, "offset": offset})
            body = self.get(path, page)
            found = body.get("results") or []
            out.extend(found)
            offset += len(found)
            if not found or offset >= int(body.get("count") or 0):
                return out

    def devices_by(self, field: str, values: Iterable[str]) -> List[Dict[str, Any]]:
        """Ask for many at once: NetBox treats a repeated parameter as OR."""
        wanted = [v for v in dict.fromkeys(values) if v]
        found: List[Dict[str, Any]] = []
        for start in range(0, len(wanted), BATCH):
            chunk = wanted[start:start + BATCH]
            found.extend(self.every(DEVICES, [(field, v) for v in chunk]
                                    + [("fields", DEVICE_FIELDS)]))
        return found

    def addresses_of(self, device_ids: Iterable[Any]) -> List[Dict[str, Any]]:
        """Every address NetBox holds for these devices, and on which port.

        A server here commonly has exactly one address on record and it is the
        BMC's. Comparing what answered against `primary_ip` alone therefore
        accuses the documentation of being wrong when it is merely incomplete.
        """
        wanted = [str(i) for i in dict.fromkeys(device_ids) if i]
        found: List[Dict[str, Any]] = []
        for start in range(0, len(wanted), BATCH):
            chunk = wanted[start:start + BATCH]
            found.extend(self.every(IP_ADDRESSES,
                                    [("device_id", v) for v in chunk]))
        return found

    def devices_with_tag(self, slug: str) -> List[Dict[str, Any]]:
        """Every device carrying one tag - the whole of a bench, as recorded.

        The rest of the comparison runs from the scan outwards and so can only
        see what answered. This is the other direction: what should be here.
        """
        if not slug:
            return []
        found = self.every(DEVICES, [("tag", slug), ("fields", DEVICE_FIELDS)])
        for record in found:
            record["addresses"] = []
        held = self.addresses_of([r.get("id") for r in found])
        by_id = {r.get("id"): r for r in found}
        for row in held:
            owner = ((row.get("assigned_object") or {}).get("device")
                     or {}).get("id")
            target = by_id.get(owner)
            if target is not None and row.get("address"):
                target["addresses"].append(str(row["address"]).split("/")[0])
        return found

    def ports_of(self, device_ids: Iterable[Any]) -> Dict[str, List[str]]:
        """The interface names recorded for these devices, by device id.

        Only the name is read. Matching a port the scan saw to the record of
        it needs nothing else, and a full interface record is mostly links.
        """
        wanted = [str(i) for i in dict.fromkeys(device_ids) if i]
        out: Dict[str, List[str]] = {}
        for start in range(0, len(wanted), BATCH):
            chunk = wanted[start:start + BATCH]
            for row in self.every(INTERFACES,
                                  [("device_id", v) for v in chunk]
                                  + [("fields", "name,device")]):
                owner = str(((row.get("device") or {}).get("id")) or "")
                if owner and row.get("name"):
                    out.setdefault(owner, []).append(str(row["name"]))
        return out

    def addresses(self, ips: Iterable[str]) -> List[Dict[str, Any]]:
        wanted = [v for v in dict.fromkeys(ips) if v]
        found: List[Dict[str, Any]] = []
        for start in range(0, len(wanted), BATCH):
            chunk = wanted[start:start + BATCH]
            found.extend(self.every(IP_ADDRESSES, [("address", v) for v in chunk]))
        return found


def _name_of(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("display") or "")
    return str(value or "")


def simplify(record: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the handful of fields a map and a comparison actually use."""
    primary = record.get("primary_ip") or record.get("primary_ip4") or {}
    # NetBox sends a nested object or null; be forgiving about a bare string so
    # an odd record cannot stop the whole read.
    if isinstance(primary, dict):
        address = str(primary.get("address") or "").split("/")[0]
    else:
        address = str(primary or "").split("/")[0]
    return {
        "id": record.get("id"),
        "name": _name_of(record.get("name") or record.get("display")),
        "serial": str(record.get("serial") or "").strip(),
        "asset_tag": str(record.get("asset_tag") or "").strip(),
        "role": _name_of(record.get("role") or record.get("device_role")),
        "site": _name_of(record.get("site")),
        "location": _name_of(record.get("location")),
        "rack": _name_of(record.get("rack")),
        "tenant": _name_of(record.get("tenant")),
        "model": _name_of((record.get("device_type") or {}).get("model")
                          or record.get("device_type")),
        "maker": _name_of((record.get("device_type") or {}).get("manufacturer")),
        "status": _name_of(record.get("status")) or str(
            (record.get("status") or {}).get("value", "")),
        "tags": sorted(_name_of(t) for t in (record.get("tags") or [])),
        "primary_ip": address,
        "url": record.get("display_url") or record.get("url") or "",
    }


def wanted_from(topology: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """The serials and addresses this scan can be asked about."""
    serials, ips = [], []
    for node in topology.get("nodes") or []:
        serial = str(node.get("serial") or "").strip()
        if serial:
            serials.append(serial)
        for address in node.get("mgmt_ips") or []:
            if address:
                ips.append(str(address))
    return list(dict.fromkeys(serials)), list(dict.fromkeys(ips))


def fetch(client: Netbox, topology: Dict[str, Any],
          say: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Look up only what this scan actually saw, rather than the whole estate."""
    talk = say or (lambda _m: None)
    serials, ips = wanted_from(topology)
    by_serial = client.devices_by("serial", serials) if serials else []
    found_ips = client.addresses(ips) if ips else []
    talk("  netbox: asked about %d serial(s) and %d address(es)"
         % (len(serials), len(ips)))

    devices = {d["id"]: simplify(d) for d in by_serial}
    holders: Dict[str, Dict[str, Any]] = {}
    for record in found_ips:
        holder = record.get("assigned_object") or {}
        device = holder.get("device") or {}
        address = str(record.get("address") or "").split("/")[0]
        if address and device:
            holders[address] = {"id": device.get("id"),
                                "name": _name_of(device),
                                "port": _name_of(holder.get("name"))}
    missing = [h["id"] for h in holders.values()
               if h.get("id") and h["id"] not in devices]
    if missing:
        for record in client.devices_by("id", [str(i) for i in missing]):
            devices[record["id"]] = simplify(record)
    held: Dict[Any, List[Dict[str, str]]] = {}
    if devices:
        try:
            for record in client.addresses_of(list(devices)):
                holder = record.get("assigned_object") or {}
                owner = (holder.get("device") or {}).get("id")
                address = str(record.get("address") or "").split("/")[0]
                if owner and address:
                    held.setdefault(owner, []).append(
                        {"address": address, "port": _name_of(holder.get("name"))})
        except Exception as problem:       # a reading never kills the scan
            talk("  netbox: could not read the addresses (%s)" % problem)
    for record in devices.values():
        record["addresses"] = held.get(record.get("id"), [])
    talk("  netbox: %d device(s) known, %d of the addresses are recorded"
         % (len(devices), len(holders)))

    return {"schema": "linkfinder-netbox", "version": 1,
            "url": client.url, "devices": list(devices.values()),
            "addresses": holders}


def save(inventory: Dict[str, Any], path: Optional[str] = None) -> str:
    where = path or os.path.join(os.path.expanduser("~"), CACHE)
    with io.open(where, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(inventory, indent=2, ensure_ascii=False))
    return where


def load(path: Optional[str] = None) -> Dict[str, Any]:
    where = path or os.path.join(os.path.expanduser("~"), CACHE)
    try:
        with io.open(where, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"devices": [], "addresses": {}}


# A NetBox status that does not mean "this should be running right now".
NOT_LIVE = ("offline", "planned", "staged", "inventory", "decommissioning",
            "failed")

# What an inventory is expected to hold. A console lead and a no-name USB
# adapter are not assets, and saying "not in NetBox" about every one of them
# would bury the handset that really is missing. They are still counted, so
# nothing disappears silently - they just do not each get a line.
TRACKED = ("host", "switch", "router", "bmc", "radio", "timing", "ap", "phone")


def apply(topology, inventory, allow=None, links=None):
    """Hang the inventory record on each node, and bring its setups across.

    A setup is something people already keep in NetBox as a tag, so the map
    reads theirs rather than asking anyone to mark the same thing twice - but
    only the tags named as setups. A name marked by hand is kept beside them,
    because the newest device on a bench is exactly the one nobody recorded.
    """
    wanted = {str(a).strip().lower() for a in (allow or []) if str(a).strip()}
    put = 0
    for node in topology.get("nodes") or []:
        record, signals, sure, ask = identify(node, inventory, links)
        if record is None:
            continue
        node["netbox"] = dict(record, matched_by=", ".join(signals),
                              confidence=sure, confirm=bool(ask))
        if ask:
            # Found only by a hint. Showing the record is useful; silently
            # moving the device into somebody's bench on that basis is not.
            continue
        tags = [t for t in (record.get("tags") or []) if t
                and str(t).strip().lower() in wanted]
        if tags:
            had = [s for s in (node.get("setups") or []) if s]
            node["setups"] = sorted(set(had) | set(tags))
            put += 1
    return put


# Who owns what, decided with the user and not to be quietly widened. A person
# from IT creates the record - what the asset IS, who owns it, what it is for -
# because a scan cannot know any of that. The tool only ever offers to correct
# WHERE a device is and whether it is alive, which is the part it measures. So
# nothing here proposes creating a device, and nothing here sends anything: a
# proposal is a file a human applies.
OWNED_BY_PEOPLE = ("create a device", "serial", "role", "tenant", "site",
                   "device_type", "asset_tag")


def propose(topology, inventory, links=None):
    """Corrections the tool is entitled to suggest, for a human to apply.

    Only for devices that ALREADY exist in NetBox. A device nobody recorded is
    reported as missing and left alone - creating it is IT's call, not a
    side effect of a scan.
    """
    out = []
    for node in topology.get("nodes") or []:
        if str(node.get("state") or "") != "scanned":
            continue
        record, _signals, _sure, ask = identify(node, inventory, links)
        if record is None or ask:
            # A human creates the record, or confirms a doubtful match. Neither
            # is something a scan may decide on its own.
            continue

        ours = [str(a) for a in (node.get("mgmt_ips") or []) if a]
        recorded = str(record.get("primary_ip") or "")
        if ours and recorded not in ours:
            out.append({
                "device": node.get("id"), "label": node.get("name") or "",
                "netbox_id": record.get("id"), "netbox": record.get("url", ""),
                "field": "primary_ip", "from": recorded or None, "to": ours[0],
                "why": "it answered on %s in this scan" % ours[0]})

        status = str(record.get("status") or "").strip().lower()
        if status in NOT_LIVE:
            out.append({
                "device": node.get("id"), "label": node.get("name") or "",
                "netbox_id": record.get("id"), "netbox": record.get("url", ""),
                "field": "status", "from": status, "to": "active",
                "why": "it answered, so it is not %s" % status})

        marked = [t for t in (node.get("setups") or []) if t]
        theirs = [t for t in (record.get("tags") or []) if t]
        added = [t for t in marked if t not in theirs]
        if added:
            out.append({
                "device": node.get("id"), "label": node.get("name") or "",
                "netbox_id": record.get("id"), "netbox": record.get("url", ""),
                "field": "tags", "from": theirs, "to": sorted(theirs + added),
                "why": "marked on the map as %s" % ", ".join(added)})
    return out


def save_proposals(proposals, path):
    """Write them out, with the rule they were made under written beside them."""
    body = {
        "schema": "linkfinder-netbox-proposals", "version": 1,
        "rule": ("A person from IT creates a device record; this tool only "
                 "offers to correct where a device is and whether it is "
                 "alive. Nothing here was sent to NetBox."),
        "never_proposed": list(OWNED_BY_PEOPLE),
        "changes": proposals,
    }
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(body, indent=2, ensure_ascii=False))
    return path


# ---------------------------------------------------------------------------
# Comparing what was found with what was written down.
#
# Five verdicts, not four. "We could not tell" has to be sayable, or every gap
# in the evidence becomes an accusation that the documentation is wrong - and a
# tool that cries wolf about NetBox is one nobody runs twice.
MATCH = "MATCH"
MISMATCH = "MISMATCH"
MISSING_IN_NETBOX = "MISSING_IN_NETBOX"
NOT_VERIFIED = "NOT_VERIFIED"
CONFLICT = "CONFLICT"

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"

# How a device was recognised. The serial names one physical unit and is the
# only thing here that settles identity on its own; this estate has a serial on
# 10 345 of 10 415 devices, so that is a key worth leaning on. An address or a
# hostname is a HINT - it says what answers there today, which is precisely why
# a swapped box is detectable at all.
BY_LINK = "a confirmed link"
BY_SERIAL = "its serial"
BY_SERIAL_IN_NAME = "its serial inside the NetBox name"
BY_SERIAL_IN_SERIAL = "its serial among the ones NetBox records"
BY_MAC = "a MAC"
BY_ADDRESS = "an address"
BY_NAME = "a hostname"

SETTLES_IT = (BY_LINK, BY_SERIAL, BY_SERIAL_IN_NAME, BY_SERIAL_IN_SERIAL)


def serial_parts(text):
    """The separate serials somebody typed into one NetBox serial field.

    Seen on a live record: "A450459X4703513 (1X200532408)" - the chassis and
    the product, both true. Comparing the whole string found nothing, so a
    device whose serial NetBox holds was reported as a disagreement and
    matched by its address instead, which is the weakest key there is.
    """
    return [part for part in re.split(r"[^A-Za-z0-9]+", str(text or ""))
            if len(part) >= 6]


def _macs_of(node):
    """Whatever MACs the device told us about. Never required - barely one
    NetBox interface in a hundred carries one - but free when it is there."""
    out = set()
    extra = node.get("extra") or {}
    for mac in (extra.get("macs") or {}).values():
        if mac:
            out.add(str(mac).strip().lower())
    chassis = str(node.get("chassis_id") or "").strip().lower()
    if chassis.count(":") == 5:
        out.add(chassis)
    return out


def identify(node, inventory, links=None):
    """Which NetBox record this is, how we know, and whether to trust it.

    Returns (record, signals, confidence, ask). `ask` is True when something
    was found but only by a hint, and a person should confirm it rather than
    the tool deciding on their behalf.
    """
    devices = inventory.get("devices") or []
    by_id = {d.get("id"): d for d in devices}
    hits = {}

    def mark(record_id, how):
        hits.setdefault(record_id, set()).add(how)

    kept = (links or {}).get(str(node.get("id") or ""))
    if kept and kept in by_id:
        mark(kept, BY_LINK)

    serial = str(node.get("serial") or "").strip()
    low = serial.lower()
    if serial:
        for record in devices:
            held = str(record.get("serial") or "").strip()
            if held.lower() == low:
                mark(record["id"], BY_SERIAL)
            elif low in [p.lower() for p in serial_parts(held)]:
                mark(record["id"], BY_SERIAL_IN_SERIAL)
        # People put the serial in the device name here - "bench-b-E907..."
        # A fifteen-character serial inside a name is not a coincidence.
        if len(serial) >= 6:
            for record in devices:
                if low in str(record.get("name") or "").lower():
                    mark(record["id"], BY_SERIAL_IN_NAME)

    ours = _macs_of(node)
    if ours:
        for record in devices:
            if ours & {m for m in (record.get("macs") or []) if m}:
                mark(record["id"], BY_MAC)

    holders = inventory.get("addresses") or {}
    for address in node.get("mgmt_ips") or []:
        found = holders.get(str(address))
        if found and found.get("id") in by_id:
            mark(found["id"], BY_ADDRESS)

    name = str(node.get("name") or "").strip().lower()
    if name:
        for record in devices:
            if str(record.get("name") or "").strip().lower() == name:
                mark(record["id"], BY_NAME)

    if not hits:
        return None, [], LOW, False
    if len(hits) > 1:
        # Two records answer to one device. Picking one would quietly attach a
        # scan to the wrong asset, which is worse than saying nothing.
        return None, sorted({h for v in hits.values() for h in v}), LOW, True

    found_id, how = list(hits.items())[0]
    signals = sorted(how)
    if how & set(SETTLES_IT):
        return by_id[found_id], signals, HIGH, False
    if BY_MAC in how:
        return by_id[found_id], signals, MEDIUM, len(how) == 1
    # Only hints. An address and a hostname agreeing is worth more than either
    # alone, but neither identifies a box, so a person decides.
    return by_id[found_id], signals, (MEDIUM if len(how) > 1 else LOW), True


def _finding(status, node, detail, evidence, sure, record=None, field="",
             actual=None, documented=None, ask=False):
    return {"status": status, "what": "device",
            "device": node.get("id"), "label": node.get("name") or node.get("id"),
            "detail": detail, "evidence": evidence, "confidence": sure,
            "field": field, "actual": actual, "documented": documented,
            "confirm": bool(ask),
            "netbox_id": (record or {}).get("id"),
            "netbox": (record or {}).get("url", "")}


def review(topology, inventory, links=None):
    """One verdict per scanned device, worst first.

    Devices nobody could reach are left out entirely: not getting in says
    nothing about whether the documentation is right.
    """
    out = []
    holders = inventory.get("addresses") or {}
    by_id = {d.get("id"): d for d in (inventory.get("devices") or [])}

    for node in topology.get("nodes") or []:
        if str(node.get("state") or "") != "scanned":
            continue
        if str(node.get("kind") or "") not in TRACKED:
            # A BMC's virtual keyboard and a console lead are not assets. Asking
            # NetBox about every one of them buries the handset that really is
            # missing, which is the only finding on the list worth acting on.
            continue
        serial = str(node.get("serial") or "").strip()
        record, signals, sure, ask = identify(node, inventory, links)
        how = ", ".join(signals) or "nothing"

        if record is None and signals:
            out.append(_finding(
                CONFLICT, node, "more than one NetBox record answers to this",
                "matched by %s" % how, LOW, ask=True))
            continue

        if record is None:
            if serial:
                out.append(_finding(
                    MISSING_IN_NETBOX, node,
                    "serial %s is not in NetBox" % serial,
                    "the serial was read from the device itself", HIGH,
                    field="serial", actual=serial))
            else:
                out.append(_finding(
                    NOT_VERIFIED, node,
                    "nothing reliable to look it up by - it published no serial",
                    "no serial could be read", LOW))
            continue

        # The box answering at a documented address is not the documented box.
        swapped = None
        for address in node.get("mgmt_ips") or []:
            there = by_id.get((holders.get(str(address)) or {}).get("id"))
            theirs = str((there or {}).get("serial") or "").strip()
            if there and serial and theirs and theirs.lower() != serial.lower():
                swapped = (address, theirs, there)
                break
        if swapped:
            address, theirs, there = swapped
            out.append(_finding(
                MISMATCH, node,
                "%s holds serial %s, but NetBox records %s there (%s)"
                % (address, serial, theirs, there.get("name")),
                "both serials read directly", HIGH, there,
                field="serial", actual=serial, documented=theirs))
            continue

        if ask:
            out.append(_finding(
                NOT_VERIFIED, node,
                "found by %s, which does not identify a box on its own" % how,
                "matched by %s" % how, sure, record, ask=True))
            continue

        differences = []
        ours = [str(a) for a in (node.get("mgmt_ips") or []) if a]
        theirs = record.get("addresses")
        if theirs is None:              # an older cache, before these were read
            primary = str(record.get("primary_ip") or "")
            theirs = [{"address": primary, "port": ""}] if primary else []
        known = {str(a.get("address") or "") for a in theirs}
        if ours and known and not (known & set(ours)):
            # NetBox knows this device but not the address it answered on. That
            # is a gap in the documentation, not a contradiction - very often
            # the only address on record is the BMC's, on an interface called
            # IPMI, which is a different port and perfectly correct.
            where = ", ".join("%s%s" % (a.get("address"),
                                        " on %s" % a["port"] if a.get("port") else "")
                              for a in theirs)
            out.append(_finding(
                MISSING_IN_NETBOX, node,
                "it answered on %s; NetBox records %s for it"
                % (ours[0], where or "no address"),
                "the address it answered on is not among the %d NetBox holds"
                % len(theirs), sure, record,
                field="ip_address", actual=ours[0], documented=where))
        state = str(record.get("status") or "").strip().lower()
        if state in NOT_LIVE:
            differences.append(("status", "active", state,
                                "it answered, so it is not %s" % state))

        for field, actual, documented, why in differences:
            out.append(_finding(
                MISMATCH, node, "%s: found %s, NetBox says %s"
                % (field, actual, documented or "nothing"),
                why, sure, record, field=field, actual=actual,
                documented=documented))
        if not differences:
            out.append(_finding(MATCH, node, "agrees with NetBox",
                                "matched by %s" % how, sure, record))
    return out


WORST_FIRST = {CONFLICT: 0, MISMATCH: 1, MISSING_IN_NETBOX: 2,
               NOT_VERIFIED: 3, MATCH: 4}


def sort_review(findings):
    return sorted(findings, key=lambda f: (WORST_FIRST.get(f["status"], 9),
                                           str(f.get("label") or "")))


def standing(findings):
    """How many of each verdict, for a line a person reads first."""
    out = {}
    for found in findings:
        out[found["status"]] = out.get(found["status"], 0) + 1
    return out

# One vocabulary. The per-device check and the per-bench comparison were
# saying the same four things in two different languages - MISSING_IN_NETBOX
# here, ONLY HERE there - and a person reading both had to translate.
IN_WORDS = {
    MATCH: "agrees",
    MISMATCH: "differs",
    MISSING_IN_NETBOX: "only here",
    NOT_VERIFIED: "not checked",
    CONFLICT: "ambiguous",
}


def in_words(status: str) -> str:
    return IN_WORDS.get(str(status), str(status).lower().replace("_", " "))


def stamp_verdicts(topology: Dict[str, Any],
                   findings: List[Dict[str, Any]]) -> int:
    """Put each device's verdict on the device, so the picture can show it.

    The disagreements were only ever a list to read. On a map of seventy
    devices the useful form is the one you can see without reading: colour
    the boxes, then the eye finds the three that differ.
    """
    worst = {MISMATCH: 0, CONFLICT: 1, MISSING_IN_NETBOX: 2,
             NOT_VERIFIED: 3, MATCH: 4}
    best: Dict[str, str] = {}
    for found in findings or []:
        device = str(found.get("device") or "")
        status = str(found.get("status") or "")
        if not device:
            continue
        if device not in best or worst.get(status, 9) < worst.get(best[device], 9):
            best[device] = status
    marked = 0
    for node in topology.get("nodes") or []:
        status = best.get(str(node.get("id") or ""))
        if status:
            node["netbox_state"] = in_words(status)
            marked += 1
    return marked

def find(client, wanted: str) -> List[Dict[str, Any]]:
    """Look one thing up in NetBox: a serial, a tag, a name or an asset tag.

    Asking a person which KIND of identifier they are holding is a question
    the tool can answer itself - it tries each and says which one landed.
    """
    said = str(wanted or "").strip()
    if not said:
        return []
    out: List[Dict[str, Any]] = []
    seen = set()

    def take(records, how):
        for record in records or []:
            if record.get("id") in seen:
                continue
            seen.add(record.get("id"))
            out.append(dict(simplify(record), found_by=how))

    for how, call in (
            ("its serial", lambda: client.devices_by("serial", [said])),
            ("its asset tag", lambda: client.devices_by("asset_tag", [said])),
            ("its name", lambda: client.devices_by("name", [said])),
            ("the tag %s" % said, lambda: client.devices_with_tag(said))):
        try:
            take(call(), how)
        except Exception:
            continue                 # one way failing must not stop the rest
    return out

def addresses_for(client, records: List[Dict[str, Any]]) -> List[str]:
    """Every address NetBox holds for these records, ready to be scanned.

    This is the point of looking a bench up: you have a tag, not a list of
    addresses, and typing them out of a browser is the job the tool exists
    to remove.
    """
    out: List[str] = []
    seen = set()

    def take(value):
        text = str(value or "").split("/")[0].strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)

    ids = [r.get("id") for r in records if r.get("id")]
    for record in records:
        take(record.get("primary_ip"))
        for held in record.get("addresses") or []:
            take(held.get("address") if isinstance(held, dict) else held)
    if ids:
        try:
            for held in client.addresses_of(ids):
                take(held.get("address"))
        except Exception:
            pass                     # the primary addresses are still useful
    return out


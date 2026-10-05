"""BMC side: Redfish for any vendor.

Three layers, applied in order to every port resource found by crawling the
service: the standard Port.Ethernet.LLDPReceive schema, a per-vendor OEM table
(Supermicro / HPE iLO / Dell iDRAC / Lenovo XCC / Inspur / Ampere), and finally
a generic recursive hunt for any object carrying chassis-id + port-id keys,
which is what keeps unknown vendors working.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import binascii
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    requests = None

from .model import Neighbor, ProbeAttempt, TargetResult

CHASSIS_KEYS = re.compile(r"^(chassisid|switchconnectionid|remotechassisid)$", re.I)
PORT_KEYS = re.compile(r"^(portid|switchportconnectionid|remoteportid)$", re.I)
NAME_KEYS = re.compile(r"^(systemname|remotesystemname|switchsystemname)$", re.I)
NOT_CONNECTED = ("not available", "no link", "not supported", "unknown", "n/a", "none",
                 "nottransmitted", "not transmitted", "")
TRANSMIT_SIDE = re.compile(r"transmit|^tx$|localport|selfid", re.I)


def decode_hex_port(value: Any) -> str:
    """Supermicro and some iLO builds return the port id as hex octets."""
    if not isinstance(value, str) or not value:
        return "" if value is None else str(value or "")
    clean = value.replace(":", "").replace("-", "").replace(" ", "")
    if len(clean) >= 4 and len(clean) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", clean):
        try:
            text = binascii.unhexlify(clean).decode("ascii")
            if text.isprintable() and text.strip():
                return text.strip()
        except (binascii.Error, UnicodeDecodeError):
            pass
    return value


class Redfish:
    def __init__(self, host: str, user: str, password: str, timeout: int = 15, port: int = 443):
        if requests is None:
            raise RuntimeError("requests is not installed (pip install requests urllib3)")
        self.base = "https://%s" % host if port == 443 else "https://%s:%d" % (host, port)
        self.session = requests.Session()
        self.session.auth = (user, password)
        self.session.verify = False
        self.timeout = timeout
        self.calls = 0

    def get(self, path: str) -> Optional[Dict[str, Any]]:
        """One retry: iLO drops the connection when it is asked for a lot at once."""
        url = path if path.startswith("http") else self.base + path
        if self.calls > 200:
            return None
        self.calls += 1
        resp = None
        for attempt in range(2):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                break
            except Exception:
                if attempt:
                    return None
                time.sleep(1.0)
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def patch(self, path: str, body: Dict[str, Any]) -> Tuple[bool, str]:
        url = path if path.startswith("http") else self.base + path
        try:
            resp = self.session.patch(url, json=body, timeout=self.timeout)
        except Exception as exc:
            return False, str(exc)
        return resp.status_code in (200, 202, 204), "HTTP %d" % resp.status_code

    def members(self, path: str) -> List[str]:
        doc = self.get(path)
        if not doc:
            return []
        out = []
        for m in doc.get("Members", []) or []:
            ref = m.get("@odata.id")
            if ref:
                out.append(ref)
        return out


def looks_like_redfish(host: str, port: int = 443, timeout: float = 4.0) -> bool:
    """Unauthenticated probe. BMCs often also listen on 22, so this has to decide first."""
    if requests is None:
        return False
    base = "https://%s" % host if port == 443 else "https://%s:%d" % (host, port)
    try:
        resp = requests.get(base + "/redfish/v1", timeout=timeout, verify=False)
    except Exception:
        return False
    if resp.status_code in (401, 403):
        return True
    if resp.status_code != 200:
        return False
    try:
        doc = resp.json()
    except ValueError:
        return False
    return bool(doc.get("RedfishVersion") or doc.get("@odata.id") or doc.get("Links"))


def detect_vendor(root: Optional[Dict[str, Any]]) -> str:
    if not root:
        return "unknown"
    oem = root.get("Oem") or {}
    for key in oem:
        low = key.lower()
        for vendor in ("supermicro", "hpe", "hp", "dell", "lenovo", "inspur", "ami", "ampere"):
            if vendor in low:
                return "hpe" if vendor == "hp" else vendor
    text = ("%s %s %s" % (root.get("Vendor", ""), root.get("Product", ""),
                          root.get("Name", ""))).lower()
    for vendor in ("supermicro", "hpe", "dell", "lenovo", "inspur", "ampere"):
        if vendor in text:
            return vendor
    return "unknown"


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in NOT_CONNECTED else text


MAC_LOOK = re.compile(r"^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.I)


def _looks_like_ip(text: str) -> bool:
    """Supermicro publishes ManagementAddressMAC next to the IPv4 one; a MAC is
    not an address we can connect to, and must never reach the switch-IP column."""
    if not text or MAC_LOOK.match(text):
        return False
    parts = text.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) < 256 for p in parts):
        return True
    return ":" in text and text.count(":") > 5 and not MAC_LOOK.match(text)


def neighbor_from_lldp_block(block: Dict[str, Any]) -> Optional[Neighbor]:
    chassis = port = ""
    name = desc = mgmt = vlan = mgmt_mac = ""
    for key, value in block.items():
        if not isinstance(value, (str, int, float)) or value is None:
            continue
        low = key.lower()
        if CHASSIS_KEYS.match(low) and not chassis:
            chassis = _clean(value)
        elif PORT_KEYS.match(low) and not port:
            port = decode_hex_port(_clean(value))
        elif NAME_KEYS.match(low):
            name = _clean(value)
        elif low in ("systemdescription", "remotesystemdescription", "portdescription"):
            desc = desc or _clean(value)
        elif "managementaddress" in low and low.endswith("mac"):
            mgmt_mac = mgmt_mac or _clean(value)
        elif "managementaddress" in low or low == "managementipaddress":
            candidate = _clean(value)
            if _looks_like_ip(candidate):
                mgmt = mgmt or candidate
        elif "vlan" in low and str(value).isdigit():
            vlan = vlan or str(value)
    if not port or not (chassis or name):
        return None
    nb = Neighbor(proto="LLDP", source="redfish", switch=name or chassis,
                  chassis_id=chassis, port=port, sys_desc=desc, mgmt_ip=mgmt, vlan=vlan)
    if not mgmt and mgmt_mac:
        nb.extra["mgmt_mac"] = mgmt_mac
        nb.note = ("the switch advertises only a MAC as its management address (%s), "
                   "no IP - so there is no switch IP to show" % mgmt_mac)
    return nb


def hunt_lldp(doc: Any, depth: int = 0) -> List[Dict[str, Any]]:
    """Recursively find objects that look like a remote-switch record.

    LLDPTransmit must never be followed: it carries what this machine advertises
    about itself, which otherwise reads as a neighbour whose chassis id is our own
    MAC address.
    """
    found: List[Dict[str, Any]] = []
    if depth > 6:
        return found
    if isinstance(doc, dict):
        keys = [k.lower() for k in doc]
        if any(CHASSIS_KEYS.match(k) for k in keys) or any(PORT_KEYS.match(k) for k in keys):
            found.append(doc)
        for key, value in doc.items():
            if TRANSMIT_SIDE.search(key):
                continue
            found.extend(hunt_lldp(value, depth + 1))
    elif isinstance(doc, list):
        for value in doc[:32]:
            found.extend(hunt_lldp(value, depth + 1))
    return found


def _port_facts(doc: Dict[str, Any]) -> Tuple[str, str, List[str], bool]:
    """(mac, speed, ipv4 addresses, link is up) for an ethernet-interface-ish resource."""
    mac = doc.get("MACAddress") or doc.get("PermanentMACAddress") or ""
    assoc = doc.get("AssociatedNetworkAddresses")
    if not mac and isinstance(assoc, list) and assoc:
        mac = assoc[0]
    raw_speed = doc.get("CurrentLinkSpeedMbps") or doc.get("SpeedMbps")
    ips = [a.get("Address") for a in (doc.get("IPv4Addresses") or [])
           if isinstance(a, dict) and a.get("Address") and a.get("Address") != "0.0.0.0"]
    state = str(doc.get("LinkStatus") or (doc.get("Status") or {}).get("State") or "")
    up = state.lower() in ("linkup", "up", "enabled")
    return str(mac or "").lower(), ("%sMb/s" % raw_speed if raw_speed else ""), ips, up


def _licence(rf: "Redfish") -> str:
    for ref in rf.members("/redfish/v1/Managers") or ["/redfish/v1/Managers/1"]:
        oem = ((rf.get(ref) or {}).get("Oem") or {})
        for block in oem.values():
            if isinstance(block, dict) and isinstance(block.get("License"), dict):
                return str(block["License"].get("LicenseString") or "")
    return ""


def _port_label(doc: Dict[str, Any], ref: str) -> str:
    name = str(doc.get("Name") or "").strip()
    if name and not name.isdigit():
        return name
    parts = ref.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else (name or parts[-1])


def crawl_ports(rf: Redfish) -> List[Tuple[str, Dict[str, Any]]]:
    """Collect every port-ish resource across Chassis, Systems and Managers."""
    seen: List[Tuple[str, Dict[str, Any]]] = []
    refs: List[str] = []
    for chassis_ref in rf.members("/redfish/v1/Chassis"):
        for adapter_ref in rf.members(chassis_ref + "/NetworkAdapters"):
            for sub in ("/Ports", "/NetworkPorts", "/NetworkDeviceFunctions"):
                refs.extend(rf.members(adapter_ref + sub))
        refs.extend(rf.members(chassis_ref + "/NetworkAdapters"))
    for system_ref in rf.members("/redfish/v1/Systems"):
        refs.extend(rf.members(system_ref + "/EthernetInterfaces"))
        refs.extend(rf.members(system_ref + "/NetworkInterfaces"))
    for mgr_ref in rf.members("/redfish/v1/Managers"):
        refs.extend(rf.members(mgr_ref + "/EthernetInterfaces"))
    for ref in dict.fromkeys(refs):
        doc = rf.get(ref)
        if doc:
            seen.append((ref, doc))
    return seen


VENDOR_LLDP_PATHS = {
    "supermicro": ["/redfish/v1/Managers/1/Oem/Supermicro/LLDP"],
    "hpe": ["/redfish/v1/Systems/1/EthernetInterfaces/1/"],
    "dell": ["/redfish/v1/Systems/System.Embedded.1/NetworkPorts"],
}


def enable_lldp(rf: Redfish, vendor: str, say: Callable[[str], None], wait: int = 45) -> bool:
    """Turn the BMC's LLDP receiver on when it is off. Reversible, but a BMC write."""
    changed = False
    if vendor == "supermicro":
        path = "/redfish/v1/Managers/1/Oem/Supermicro/LLDP"
        doc = rf.get(path)
        if doc is not None and not doc.get("LLDPEnabled", True):
            say("    LLDP is OFF on this BMC - enabling it (PATCH %s)" % path)
            ok, detail = rf.patch(path, {"LLDPEnabled": True})
            changed = ok
            if not ok:
                say("    could not enable LLDP: %s" % detail)
    elif vendor == "hpe":
        path = "/redfish/v1/Managers/1/NetworkProtocol/"
        doc = rf.get(path)
        hpe = ((doc or {}).get("Oem") or {}).get("Hpe") or {}
        if hpe.get("LldpEnabled") is False:
            say("    iLO LLDP is OFF - enabling it (PATCH %s)" % path)
            ok, detail = rf.patch(path, {"Oem": {"Hpe": {"LldpEnabled": True}}})
            changed = ok
            if not ok:
                say("    could not enable LLDP: %s" % detail)
    if changed:
        say("    waiting %ds for the next LLDP advertisement" % wait)
        time.sleep(wait)
    return changed


def probe_redfish(host: str, user: str, password: str, result: TargetResult,
                  allow_enable: bool = True, log: Optional[Callable[[str], None]] = None,
                  port: int = 443) -> TargetResult:
    say = log or (lambda _m: None)
    started = time.time()
    try:
        rf = Redfish(host, user, password, port=port)
    except RuntimeError as exc:
        result.error = str(exc)
        return result

    root = rf.get("/redfish/v1")
    if root is None:
        result.error = ("no Redfish service answered on %s (wrong credentials, or the BMC "
                        "needs an OOB/Advanced licence)" % host)
        result.attempts.append(ProbeAttempt("redfish-root", False, result.error,
                                            round(time.time() - started, 1)))
        return result
    vendor = detect_vendor(root)
    result.kind = "bmc:%s" % vendor
    say("  Redfish %s, vendor %s" % (root.get("RedfishVersion", "?"), vendor))

    if allow_enable:
        try:
            enable_lldp(rf, vendor, say)
        except Exception as exc:
            say("    enable-lldp skipped: %s" % exc)

    for path in VENDOR_LLDP_PATHS.get(vendor, []):
        doc = rf.get(path)
        if not doc:
            continue
        for block in hunt_lldp(doc):
            nb = neighbor_from_lldp_block(block)
            if nb:
                nb.local_iface = nb.local_iface or "bmc"
                nb.source = "redfish:%s-oem" % vendor
                result.add(nb)

    ports = crawl_ports(rf)
    say("  crawled %d port resources" % len(ports))
    inventory = []
    for ref, doc in ports:
        label = _port_label(doc, ref)
        mac, speed, ips, up = _port_facts(doc)
        answered = False
        for block in hunt_lldp(doc):
            nb = neighbor_from_lldp_block(block)
            if not nb:
                continue
            if mac and mac in (nb.chassis_id.lower(), nb.port.lower()):
                continue
            nb.local_iface, nb.local_mac, nb.local_speed = label, mac, speed
            nb.local_role = "mgmt" if host in ips else ""
            nb.source = nb.source or "redfish"
            result.add(nb)
            answered = True
        if not answered and mac:
            inventory.append((label, mac, speed, ips, up))

    for label, mac, speed, ips, up in inventory:
        is_mgmt = host in ips
        if not up and not is_mgmt:
            continue
        result.neighbors.append(Neighbor(
            host=host, local_iface=label, local_mac=mac, local_speed=speed,
            local_role="mgmt" if is_mgmt else "physical", confidence="none",
            note="this BMC publishes no LLDP for the port; look it up switch-side by "
                 "its MAC (--switch)"))
    result.neighbors.sort(key=lambda n: (0 if n.local_role == "mgmt" else 1,
                                         0 if n.resolved else 1, n.local_iface))

    ok = result.resolved
    result.attempts.append(ProbeAttempt("redfish", ok,
                                        "%d neighbour(s) from %d resources" % (len(result.neighbors), len(ports)),
                                        round(time.time() - started, 1)))
    if not ok and not result.error:
        licence = _licence(rf)
        reason = ("its licence is %s, so this is a firmware limitation, not a licence one"
                  % licence) if licence and "advanced" in licence.lower() else \
                 ("Supermicro needs SFT-OOB-LIC / HPE needs iLO Advanced for this"
                  if not licence else "its licence is %s" % licence)
        result.error = ("this BMC exposes no LLDP over Redfish (%d resources checked; %s). "
                        "%d NIC MAC(s) were collected instead - give --switch and the port "
                        "can be found switch-side." % (len(ports), reason, len(inventory)))
    return result

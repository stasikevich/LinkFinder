"""What is in the socket at each end of a fibre, and whether the two agree.

An optic mismatch is a cable that looks perfectly plugged in and does not work,
or works until it does not: a short-reach 850 nm module facing a long-reach
1310 nm one, multimode fibre into a single-mode transceiver, a 10G optic in a
1G port. None of it shows up in `ip link`, in LLDP, or in any counter.

`ethtool -m` already decodes the module EEPROM into readable fields, so this
reads that rather than re-implementing SFF-8472. Modules that do not implement
diagnostics simply have no power or temperature to report, which is said rather
than guessed: plenty of perfectly good optics are silent about themselves.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

SINGLE_MODE = "single-mode"
MULTI_MODE = "multimode"
COPPER = "copper"

# The form factor letter is what decides the fibre: LR/ER/ZR and the LX family
# run on single-mode glass, SR/SX/LRM on multimode, CR and T on copper.
MEDIA_BY_FORM = {
    "SR": MULTI_MODE, "SX": MULTI_MODE, "LRM": MULTI_MODE,
    "LR": SINGLE_MODE, "LX": SINGLE_MODE, "ER": SINGLE_MODE,
    "ZR": SINGLE_MODE, "LH": SINGLE_MODE,
    "CR": COPPER, "T": COPPER, "CX": COPPER,
}

FORM = re.compile(r"\b(\d+)?\s*BASE[- ]?(SR|SX|LRM|LR|LX|ER|ZR|LH|CR|CX|T)\b", re.I)
SPEED = re.compile(r"\b(\d+)\s*G\b", re.I)
NUMBER = re.compile(r"(-?\d+(?:\.\d+)?)")

FIELDS = {
    "Identifier": "identifier",
    "Connector": "connector",
    "Encoding": "encoding",
    "Laser wavelength": "wavelength_nm",
    "Vendor name": "vendor",
    "Vendor PN": "part",
    "Vendor SN": "serial",
    "Vendor rev": "revision",
    "Date code": "date_code",
    "Length (SMF,km)": "reach_smf_km",
    "Length (OM3)": "reach_om3_m",
    "Length (Copper)": "reach_copper_m",
    "BR, Nominal": "nominal_br_mbd",
    "Module temperature": "temperature_c",
    "Module voltage": "voltage_v",
    "Laser output power": "tx_dbm",
    "Rcvr signal avg optical power": "rx_dbm",
    "Receiver signal average optical power": "rx_dbm",
}

NUMERIC = ("wavelength_nm", "reach_smf_km", "reach_om3_m", "reach_copper_m",
           "nominal_br_mbd", "temperature_c", "voltage_v", "tx_dbm", "rx_dbm")


def _number(text: str) -> Optional[float]:
    match = NUMBER.search(text or "")
    if not match:
        return None
    value = float(match.group(1))
    return int(value) if value == int(value) else value


def parse_ethtool_m(text: str) -> Dict[str, Any]:
    """The readable half of `ethtool -m`, as a flat record.

    A module often announces more than one transceiver type - a 10G optic that
    also speaks 1G, say - so every one is kept and the fastest decides what the
    port is.
    """
    out: Dict[str, Any] = {}
    types: List[str] = []
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, _sep, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "Transceiver type":
            if value and value not in types:
                types.append(value)
            continue
        field = FIELDS.get(key)
        if not field or not value:
            continue
        out[field] = _number(value) if field in NUMERIC else value
    if types:
        out["types"] = types
        out.update(_classify(types))
    if out.get("identifier"):
        out["identifier"] = out["identifier"].split("(")[-1].rstrip(")")
    return out


def _classify(types: List[str]) -> Dict[str, Any]:
    """Form factor, media and speed, from whatever the module calls itself."""
    best_speed = 0
    form = ""
    for text in types:
        match = FORM.search(text)
        if not match:
            continue
        speed = 0
        digits = match.group(1)
        if digits and digits.isdigit():
            speed = int(digits) // 1000 if int(digits) >= 1000 else int(digits)
        fast = SPEED.search(text)
        if fast:
            speed = max(speed, int(fast.group(1)))
        if speed >= best_speed:
            best_speed, form = speed, match.group(2).upper()
    out: Dict[str, Any] = {}
    if form:
        out["form"] = form
        out["media"] = MEDIA_BY_FORM.get(form, "")
    if best_speed:
        out["speed_gbps"] = best_speed
    return out


def label(record: Dict[str, Any]) -> str:
    """One line a person reads: 10G-LR, 1310 nm, 10 km."""
    if not record:
        return ""
    bits = []
    speed = record.get("speed_gbps")
    form = record.get("form")
    if speed and form:
        bits.append("%dG-%s" % (speed, form))
    elif form:
        bits.append(form)
    if record.get("wavelength_nm"):
        bits.append("%s nm" % record["wavelength_nm"])
    if record.get("reach_smf_km"):
        bits.append("%s km" % record["reach_smf_km"])
    elif record.get("reach_om3_m"):
        bits.append("%s m OM3" % record["reach_om3_m"])
    return ", ".join(bits)


def diagnostics(record: Dict[str, Any]) -> str:
    """Power and temperature, or an honest word about their absence.

    A module with no digital diagnostics is normal, not broken, and must not be
    reported as a missing reading on a failing link.
    """
    bits = []
    if record.get("rx_dbm") is not None:
        bits.append("RX %s dBm" % record["rx_dbm"])
    if record.get("tx_dbm") is not None:
        bits.append("TX %s dBm" % record["tx_dbm"])
    if record.get("temperature_c") is not None:
        bits.append("%s C" % record["temperature_c"])
    return ", ".join(bits) if bits else "this module reports no diagnostics"


def check(local: Optional[Dict[str, Any]],
          remote: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compare two transceivers, hardest fault first.

    Fibre type is the one that cannot be argued with: single-mode glass into a
    multimode transceiver does not work and no amount of link state will say
    so. Reach is deliberately not a fault - a 10 km optic on a 2 m patch lead is
    a normal thing to find in a lab.
    """
    from .linkcheck import VERDICT_MATCH, VERDICT_MISMATCH, VERDICT_UNKNOWN, VERDICT_WARN

    a, b = local or {}, remote or {}
    out: Dict[str, Any] = {"a": label(a) or None, "b": label(b) or None}
    if not a or not b:
        out["verdict"] = VERDICT_UNKNOWN
        out["detail"] = ("remote optics unknown" if a else
                         "local optics unknown" if b else
                         "no transceiver data on either side")
        return out

    evidence = ["local %s" % label(a), "remote %s" % label(b)]
    if a.get("media") and b.get("media") and a["media"] != b["media"]:
        out.update(verdict=VERDICT_MISMATCH, confidence="high", evidence=evidence,
                   detail="%s against %s - the fibre types do not meet"
                          % (a["media"], b["media"]))
        return out
    if a.get("speed_gbps") and b.get("speed_gbps") and \
            a["speed_gbps"] != b["speed_gbps"]:
        out.update(verdict=VERDICT_MISMATCH, confidence="high", evidence=evidence,
                   detail="%dG one side, %dG the other"
                          % (a["speed_gbps"], b["speed_gbps"]))
        return out
    wave_a, wave_b = a.get("wavelength_nm"), b.get("wavelength_nm")
    if wave_a and wave_b and abs(float(wave_a) - float(wave_b)) > 50:
        out.update(verdict=VERDICT_MISMATCH, confidence="high", evidence=evidence,
                   detail="%s nm against %s nm" % (wave_a, wave_b))
        return out
    if a.get("form") and b.get("form") and a["form"] != b["form"]:
        out.update(verdict=VERDICT_WARN, confidence="medium", evidence=evidence,
                   detail="%s against %s - same fibre, different optic"
                          % (a["form"], b["form"]))
        return out
    out.update(verdict=VERDICT_MATCH, confidence="high")
    return out


COLLECT = r"""
for d in /sys/class/net/*/; do
  n=$(basename "$d"); [ -e "$d/device" ] || continue
  [ "$(cat "$d/carrier" 2>/dev/null)" = "1" ] || continue
  out=$(timeout 8 ethtool -m "$n" 2>/dev/null)
  [ -n "$out" ] || continue
  echo "OPTIC $n"
  echo "$out"
  echo "ENDOPTIC"
done
"""


def collect(run: Callable[[str], str],
            log: Optional[Callable[[str], None]] = None) -> Dict[str, Dict[str, Any]]:
    """One command for the whole host; ports with no module are simply absent."""
    say = log or (lambda _m: None)
    try:
        text = run(COLLECT)
    except Exception as exc:
        say("  optics: %s" % exc)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    name, buffer = "", []
    for line in (text or "").splitlines():
        if line.startswith("OPTIC "):
            name, buffer = line[6:].strip(), []
        elif line.strip() == "ENDOPTIC" and name:
            record = parse_ethtool_m("\n".join(buffer))
            if record:
                out[name] = record
            name, buffer = "", []
        elif name:
            buffer.append(line)
    return out


def match_optics(results, log=None) -> None:
    """Compare the transceivers at the two ends of every link we can place.

    The far end has to be a port this run actually looked at, which in practice
    means the direct cables - exactly the links where an optic mismatch is both
    likely and invisible. Everything else is left UNKNOWN rather than guessed:
    there is no way to read the module in a switch we cannot log into.
    """
    from .optics import check as check_optics

    say = log or (lambda _m: None)
    index = {}
    for result in results or []:
        host = getattr(result, "device_name", "") or getattr(result, "target", "")
        for iface, record in ((getattr(result, "extra", {}) or {}).get("optics")
                              or {}).items():
            index[(host, iface)] = record
            index[(getattr(result, "target", ""), iface)] = record
    if not index:
        return

    for result in results or []:
        host = getattr(result, "device_name", "") or getattr(result, "target", "")
        mine = ((getattr(result, "extra", {}) or {}).get("optics") or {})
        for nb in getattr(result, "neighbors", []) or []:
            local = mine.get(nb.local_iface)
            remote = None
            for far_host, far_iface in nb.extra.get("ptp_peer_ports") or []:
                remote = index.get((far_host, far_iface))
                if remote:
                    break
            if not local and not remote:
                continue
            verdict = check_optics(local, remote)
            nb.extra.setdefault("checks", {})["optics"] = verdict
            if local:
                nb.extra["optics"] = local
            if verdict.get("verdict") in ("mismatch", "warn"):
                say("  optics on %s %s: %s" % (host, nb.local_iface,
                                               verdict.get("detail", "")))

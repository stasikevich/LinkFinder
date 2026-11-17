"""The physical health of a port, read from the kernel's own counters.

A link can be up and still be sick: a dirty fibre or a failing SFP raises CRC
errors, a duplex mismatch quietly halves throughput, and a port that flaps
leaves a trail of carrier changes. None of these show as "down", so a map that
only draws the cable misses the fault the person is actually chasing. Every
value here is a plain read of /sys/class/net, nothing written.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

Run = Callable[[str], str]

# Only physical ports (those with a /device link) are read; a veth or a bridge
# has no cable to be sick. carrier_changes counts up+down since boot, so a big
# number on a long-lived host is not itself a fault - it is shown, not flagged.
SCRIPT = r"""
for d in /sys/class/net/*; do
  n=${d##*/}
  [ "$n" = "lo" ] && continue
  [ -e "$d/device" ] || continue
  printf 'PH|%s|%s|%s|%s|%s|%s|%s\n' "$n" \
    "$(cat "$d/carrier" 2>/dev/null)" \
    "$(cat "$d/duplex" 2>/dev/null)" \
    "$(cat "$d/carrier_changes" 2>/dev/null)" \
    "$(cat "$d/statistics/rx_crc_errors" 2>/dev/null)" \
    "$(cat "$d/statistics/rx_errors" 2>/dev/null)" \
    "$(cat "$d/statistics/tx_errors" 2>/dev/null)"
done
"""

# A port that has flipped more than this since boot has been flapping; below it
# the odd change is a reboot or a cable reseat, not a fault.
FLAP_CHANGES = 20


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse(text: str) -> Dict[str, Dict[str, Any]]:
    """One record per physical port, numbers kept as numbers."""
    out: Dict[str, Dict[str, Any]] = {}
    for line in (text or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) != 8 or parts[0] != "PH":
            continue
        _, name, carrier, duplex, changes, crc, rxerr, txerr = parts
        out[name] = {
            "carrier": carrier.strip(),
            "duplex": duplex.strip(),
            "carrier_changes": _int(changes),
            "rx_crc_errors": _int(crc),
            "rx_errors": _int(rxerr),
            "tx_errors": _int(txerr),
        }
    return out


def problems(ports: Dict[str, Dict[str, Any]]) -> List[Dict[str, str]]:
    """The findings worth a person's time, worst first - CRC, then duplex."""
    found: List[Dict[str, str]] = []
    for name, p in sorted(ports.items()):
        crc = p.get("rx_crc_errors")
        if crc:
            found.append({"iface": name, "kind": "crc", "severity": "mismatch",
                          "detail": "%d CRC error(s) on %s - a dirty fibre, a "
                          "failing SFP or a marginal cable" % (crc, name)})
        if p.get("duplex") == "half" and p.get("carrier") == "1":
            found.append({"iface": name, "kind": "duplex", "severity": "mismatch",
                          "detail": "%s negotiated HALF duplex - a duplex "
                          "mismatch, expect severe throughput loss" % name})
        changes = p.get("carrier_changes")
        if changes and changes >= FLAP_CHANGES:
            found.append({"iface": name, "kind": "flap", "severity": "warn",
                          "detail": "%s has changed carrier %d times - it may "
                          "be flapping" % (name, changes)})
        rxerr, txerr = p.get("rx_errors") or 0, p.get("tx_errors") or 0
        if (rxerr or txerr) and not crc:
            found.append({"iface": name, "kind": "errors", "severity": "warn",
                          "detail": "%s has %d rx / %d tx error(s)"
                          % (name, rxerr, txerr)})
    return found


def collect(run: Run, log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Per-port health plus the findings, or {} when there is nothing to say."""
    say = log or (lambda _m: None)
    ports = parse(run("sh -c " + _quote(SCRIPT)))
    if not ports:
        return {}
    issues = problems(ports)
    result: Dict[str, Any] = {"ports": ports}
    if issues:
        result["problems"] = issues
        say("  port health: %s" % "; ".join(i["detail"] for i in issues[:3]))
    return result


def _quote(text: str) -> str:
    return "'" + str(text).replace("'", "'\\''") + "'"

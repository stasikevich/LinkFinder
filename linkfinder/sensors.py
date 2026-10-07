"""Temperature, from the chip rather than from the port.

This does not answer "is the link healthy" - that is the counters' job, and
the optical power's where a module reports it. It answers the other question,
the one asked the morning after: why did that card fall over at three in the
morning.

The trap worth avoiding is pretending the reading is per-port. A four-port
card has one die and one sensor, registered against whichever function the
driver bound first, so reporting it beside `enp23s0f2` as though it were that
port's temperature would be a quiet lie. It is the card's temperature and is
labelled as the card's, with the ports that share it named.

Thresholds come from the same place as the reading. A bare number means
nothing to someone who does not know the part; 42 of 95 does.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

COLLECT = r"""
for d in /sys/class/net/*/; do
  n=$(basename "$d"); [ -e "$d/device" ] || continue
  pci=$(readlink -f "$d/device" 2>/dev/null | sed "s|.*/||")
  hw=$(ls -d "$d/device/hwmon/hwmon"* 2>/dev/null | head -1)
  if [ -n "$hw" ]; then
    echo "NIC|$n|$pci|$(cat "$hw/name" 2>/dev/null)|$(cat "$hw/temp1_input" 2>/dev/null)|$(cat "$hw/temp1_max" 2>/dev/null)|$(cat "$hw/temp1_crit" 2>/dev/null)"
  else
    echo "NIC|$n|$pci||||"
  fi
done
"""

WARM_FRACTION = 0.9


@dataclass
class Card:
    """One network chip, and every interface that lives on it."""

    chip: str = ""
    slot: str = ""
    celsius: Optional[float] = None
    max_celsius: Optional[float] = None
    critical_celsius: Optional[float] = None
    interfaces: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return "%s (%s)" % (self.chip or "NIC", self.slot or "?")

    def describe(self) -> str:
        if self.celsius is None:
            return "%s: no temperature" % self.label
        limit = self.max_celsius or self.critical_celsius
        reading = "%g C" % self.celsius
        if limit:
            reading += " of %g max" % limit
        shared = ", ".join(sorted(self.interfaces))
        return "%s: %s - shared by %s" % (self.label, reading, shared)

    def verdict(self) -> Dict[str, Any]:
        """Warm only against the card's own limit, never a number we picked."""
        from .linkcheck import VERDICT_MATCH, VERDICT_UNKNOWN, VERDICT_WARN

        if self.celsius is None:
            return {"verdict": VERDICT_UNKNOWN,
                    "detail": "this card reports no temperature"}
        limit = self.critical_celsius or self.max_celsius
        if not limit:
            return {"a": self.celsius, "verdict": VERDICT_UNKNOWN,
                    "detail": "%g C, but the card publishes no limit to judge it "
                              "against" % self.celsius}
        if self.celsius >= limit:
            return {"a": self.celsius, "b": limit, "verdict": VERDICT_WARN,
                    "confidence": "high",
                    "detail": "%g C, at or past the card's own limit of %g"
                              % (self.celsius, limit),
                    "evidence": ["hwmon temp1_input = %g" % self.celsius,
                                 "hwmon limit = %g" % limit]}
        if self.celsius >= limit * WARM_FRACTION:
            return {"a": self.celsius, "b": limit, "verdict": VERDICT_WARN,
                    "confidence": "medium",
                    "detail": "%g C, within %d%% of the card's limit of %g"
                              % (self.celsius, int((1 - WARM_FRACTION) * 100),
                                 limit),
                    "evidence": ["hwmon temp1_input = %g" % self.celsius,
                                 "hwmon limit = %g" % limit]}
        return {"a": self.celsius, "b": limit, "verdict": VERDICT_MATCH,
                "confidence": "high"}

    def to_dict(self) -> Dict[str, Any]:
        return {"chip": self.chip, "slot": self.slot, "celsius": self.celsius,
                "max_celsius": self.max_celsius,
                "critical_celsius": self.critical_celsius,
                "interfaces": list(self.interfaces)}


def _milli(text: str) -> Optional[float]:
    try:
        value = float(text) / 1000.0
    except (TypeError, ValueError):
        return None
    return int(value) if value == int(value) else round(value, 1)


def _slot_of(pci: str) -> str:
    """0000:17:00.3 and 0000:17:00.0 are the same card; the function is not."""
    return pci.rsplit(".", 1)[0] if "." in pci else pci


def parse(text: str) -> Dict[str, Card]:
    """Cards keyed by PCI slot, each knowing which interfaces sit on it."""
    cards: Dict[str, Card] = {}
    for line in (text or "").splitlines():
        if not line.startswith("NIC|"):
            continue
        parts = line.split("|")
        if len(parts) < 7:
            continue
        _tag, iface, pci, chip, value, top, crit = parts[:7]
        if not pci:
            continue
        slot = _slot_of(pci)
        card = cards.setdefault(slot, Card(slot=slot))
        if iface not in card.interfaces:
            card.interfaces.append(iface)
        if chip:
            card.chip = chip
        if value:
            card.celsius = _milli(value)
            card.max_celsius = _milli(top)
            card.critical_celsius = _milli(crit)
    return {slot: card for slot, card in cards.items() if card.chip or card.celsius}


def collect(run: Callable[[str], str],
            log: Optional[Callable[[str], None]] = None) -> Dict[str, Card]:
    """One read of sysfs; a machine whose driver registers no sensor says so."""
    say = log or (lambda _m: None)
    try:
        cards = parse(run(COLLECT))
    except Exception as exc:
        say("  sensors: %s" % exc)
        return {}
    for card in cards.values():
        if card.celsius is not None:
            say("  sensor: %s" % card.describe())
    return cards


def card_for(cards: Dict[str, Card], iface: str) -> Optional[Card]:
    for card in (cards or {}).values():
        if iface in card.interfaces:
            return card
    return None


def apply(results, log: Optional[Callable[[str], None]] = None) -> None:
    """Hang each card's reading on every interface that shares it."""
    for result in results or []:
        raw = (getattr(result, "extra", {}) or {}).get("sensors") or {}
        cards = {slot: Card(**spec) for slot, spec in raw.items()} if raw else {}
        if not cards:
            continue
        for nb in getattr(result, "neighbors", []) or []:
            card = card_for(cards, nb.local_iface)
            if card is None or card.celsius is None:
                continue
            nb.extra["nic_card"] = card.to_dict()
            nb.extra.setdefault("checks", {})["temperature"] = card.verdict()

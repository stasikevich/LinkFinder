"""What changed since last time, remembered between runs.

A map answers "how is it now". The question a lab lives with is "what moved",
and answering that needs memory: the state a thing was in, when it entered it,
and how many runs it has stayed there. Without that a check either says nothing
or says everything every time, and people stop reading it.

Three rules earn their place. A reading is not an event - a port that was down
and is still down has not changed. A fall is believed only when seen twice,
because one missed poll is not a fault, while a recovery is believed at once.
And a device nobody could reach is UNKNOWN, never DOWN: if a host does not
answer, its handsets did not all unplug themselves, we simply cannot see them.

Nothing here sends anything anywhere. It records; something else may read it.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import io
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

FILE = ".linkfinder-watch.json"
HISTORY = 500

UP = "UP"
DEGRADED = "DEGRADED"
DOWN = "DOWN"
UNKNOWN = "UNKNOWN"

# How many runs in a row a state must be seen before it is believed. Falling is
# doubted once; rising is not, because a port that came back is back.
CONFIRM = {UP: 1, DEGRADED: 2, DOWN: 2, UNKNOWN: 2}

# A receiver below this is being starved: 10GBASE-SR and -LR are both specified
# to about -14.4 dBm, so a link still passing traffic at -15 is living on
# margin that has already gone.
RX_DEGRADED_DBM = -14.0
# Where an optic is hot, when the module itself declares no maximum.
OPTIC_HOT_C = 70.0
# A clock in one of these is not serving time, but has not failed either.
PTP_SHAKY = ("HOLDOVER", "LISTENING", "UNCALIBRATED", "PASSIVE", "INITIALIZING")
# Things that hang off a host over USB. They are watched as part of that host,
# never on their own: a NUC nobody could reach has not had its phones unplugged.
USB_KINDS = ("phone", "peripheral", "serial")
# What adb says about a handset, and what that is worth.
ADB_STATE = {"device": UP, "unauthorized": DEGRADED, "offline": DEGRADED,
             "recovery": DEGRADED, "bootloader": DEGRADED}


def store_path(path: Optional[str] = None) -> str:
    return path or os.path.join(os.path.expanduser("~"), FILE)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load(path: Optional[str] = None) -> Dict[str, Any]:
    try:
        with io.open(store_path(path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("signals", {})
    data.setdefault("events", [])
    return data


def save(data: Dict[str, Any], path: Optional[str] = None) -> None:
    with io.open(store_path(path), "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2, sort_keys=True))


def _signal(key, device, label, kind, state, detail="", value=None,
            belongs=""):
    return {"key": key, "device": device, "label": label, "kind": kind,
            "state": state, "detail": detail, "value": value,
            "belongs": belongs}


def _port_state(port: Dict[str, Any]) -> Tuple[str, str]:
    said = str((port or {}).get("state") or "").lower()
    if said == "up":
        return UP, ""
    if said in ("down", "lowerlayerdown"):
        return DOWN, "the port is down"
    if not said:
        return UNKNOWN, "the port never said"
    return UNKNOWN, "the port says %s" % said


def _optic_state(optic: Dict[str, Any]) -> Tuple[str, str]:
    rx = optic.get("rx_dbm")
    if rx is None and optic.get("rx_mw") == 0:
        return DOWN, "no light on the receiver"
    if rx is not None and rx < RX_DEGRADED_DBM:
        return DEGRADED, "receiving %.1f dBm, under the %.0f dBm this type needs" % (
            rx, RX_DEGRADED_DBM)
    return UP, ""


def _heat_state(reading: Dict[str, Any]) -> Tuple[str, str]:
    now = reading.get("celsius")
    if now is None:
        now = reading.get("temperature_c")
    if now is None:
        return UNKNOWN, "no temperature was read"
    top = reading.get("max_celsius") or reading.get("critical_celsius") or OPTIC_HOT_C
    try:
        now, top = float(now), float(top)
    except (TypeError, ValueError):
        return UNKNOWN, "the temperature did not read as a number"
    if now >= top:
        return DOWN, "%g C, at or over its %g C limit" % (now, top)
    if now >= top - 10:
        return DEGRADED, "%g C, within 10 of its %g C limit" % (now, top)
    return UP, ""


def _clock_state(port: Dict[str, Any]) -> Tuple[str, str]:
    said = str((port or {}).get("state") or "").upper()
    if said in ("SLAVE", "MASTER"):
        return UP, ""
    if said == "FAULTY":
        return DOWN, "the clock port is faulty"
    if said in PTP_SHAKY:
        return DEGRADED, "the clock is %s, not serving time" % said.lower()
    if not said:
        return UNKNOWN, "the clock port said nothing"
    return DEGRADED, "the clock is %s" % said.lower()


def _unique_names(topology: Dict[str, Any]) -> Dict[str, str]:
    """A name per device that one person can say to another.

    Two benches running the same image hold two hosts called bench-c, and a
    line that names one of them without saying which is worse than no line.
    """
    seen: Dict[str, int] = {}
    for node in topology.get("nodes") or []:
        plain = node.get("name") or ""
        if plain:
            seen[plain] = seen.get(plain, 0) + 1
    out: Dict[str, str] = {}
    for node in topology.get("nodes") or []:
        node_id = str(node.get("id") or "")
        plain = node.get("name") or ""
        where = (node.get("mgmt_ips") or [""])[0] or node.get("chassis_id") or ""
        if not plain:
            out[node_id] = where or node_id
        elif seen.get(plain, 0) > 1 and where:
            out[node_id] = "%s (%s)" % (plain, where)
        else:
            out[node_id] = plain
    return out


def _usb_owners(topology: Dict[str, Any]) -> Dict[str, str]:
    """Map each USB-attached thing to the host it is plugged into."""
    kinds = {str(n.get("id") or ""): str(n.get("kind") or "")
             for n in topology.get("nodes") or []}
    owners: Dict[str, str] = {}
    for edge in topology.get("edges") or []:
        if "usb" not in (edge.get("protos") or []):
            continue
        a, b = str(edge.get("a") or ""), str(edge.get("b") or "")
        if kinds.get(a) in USB_KINDS and kinds.get(b) not in USB_KINDS:
            owners[a] = b
        elif kinds.get(b) in USB_KINDS and kinds.get(a) not in USB_KINDS:
            owners[b] = a
    return owners


def _handset_state(node: Dict[str, Any]) -> Tuple[str, str]:
    """A handset is up when it is attached AND adb may actually talk to it."""
    usb = (node.get("extra") or {}).get("usb") or {}
    said = str(usb.get("adb") or "")
    if not said:
        return UP, ""       # seen on the bus; adb was not asked
    state = ADB_STATE.get(said, UNKNOWN)
    if state == UP:
        return UP, ""
    return state, "adb says %s" % said


def read(topology: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Everything on this map worth an opinion, as it stands in this run."""
    out: Dict[str, Dict[str, Any]] = {}
    names = _unique_names(topology)
    owners = _usb_owners(topology)
    for node in topology.get("nodes") or []:
        node_id = str(node.get("id") or "")
        name = names.get(node_id, node_id)
        state = str(node.get("state") or "")
        alive = state == "scanned"
        if str(node.get("kind") or "") in USB_KINDS:
            # Counted onto the host it is plugged into, so losing the host is
            # one event and not one per handset.
            now_state, why = _handset_state(node)
            key = "%s::usb" % node_id
            out[key] = _signal(key, owners.get(node_id, node_id), name,
                               "handset", now_state if alive else UNKNOWN,
                               why if alive else "the host did not answer",
                               belongs=node_id)
            continue
        if state in ("scanned", "unreachable"):
            key = "%s::device" % node_id
            out[key] = _signal(key, node_id, name, "device",
                               UP if alive else DOWN,
                               "" if alive else
                               (node.get("error") or "it did not answer"))
        if not alive:
            # Nothing under a device we could not reach is known to be anything,
            # so none of its parts are reported at all this run.
            continue
        extra = node.get("extra") or {}
        for iface, port in (extra.get("ports") or {}).items():
            now_state, why = _port_state(port)
            if now_state == UNKNOWN:
                # A bridge, a tunnel or a vxlan reports operstate "unknown"
                # because it has no carrier to report. Watching it would mean
                # raising a hand at every one of them on every run, for ever.
                continue
            key = "%s::port::%s" % (node_id, iface)
            out[key] = _signal(key, node_id, "%s %s" % (name, iface), "port",
                               now_state, why)
        for iface, optic in (extra.get("optics") or {}).items():
            now_state, why = _optic_state(optic)
            key = "%s::optic::%s" % (node_id, iface)
            out[key] = _signal(key, node_id, "%s %s optic" % (name, iface),
                               "optic", now_state, why, optic.get("rx_dbm"))
            hot, why_hot = _heat_state(optic)
            if hot != UNKNOWN:
                key = "%s::optic-heat::%s" % (node_id, iface)
                out[key] = _signal(key, node_id,
                                   "%s %s optic temperature" % (name, iface),
                                   "heat", hot, why_hot,
                                   optic.get("temperature_c"))
        for slot, card in (extra.get("sensors") or {}).items():
            now_state, why = _heat_state(card)
            if now_state == UNKNOWN:
                continue
            key = "%s::heat::%s" % (node_id, slot)
            out[key] = _signal(key, node_id,
                               "%s %s" % (name, card.get("chip") or slot),
                               "heat", now_state, why, card.get("celsius"))
        for iface, port in (extra.get("ptp") or {}).items():
            now_state, why = _clock_state(port)
            key = "%s::clock::%s" % (node_id, iface)
            out[key] = _signal(key, node_id, "%s %s" % (name, iface), "clock",
                               now_state, why, port.get("state"))

    labels = {str(n.get("id") or ""): (n.get("name") or str(n.get("id") or ""))
              for n in topology.get("nodes") or []}
    for edge in topology.get("edges") or []:
        key = "link::%s" % edge.get("id")
        ends = "%s %s - %s %s" % (labels.get(edge.get("a"), edge.get("a")),
                                  edge.get("a_iface") or "?",
                                  labels.get(edge.get("b"), edge.get("b")),
                                  edge.get("b_iface") or "?")
        owner, hangs_off = edge.get("a"), ""
        if "usb" in (edge.get("protos") or []):
            for end in (edge.get("a"), edge.get("b")):
                if end in owners:
                    owner, hangs_off = owners[end], end
        out[key] = _signal(key, owner, ends, "link", UP, belongs=hangs_off)
    return out


def _remember(held: Dict[str, Any], signal: Dict[str, Any], now: str) -> None:
    held.update({"previous": held["state"], "state": signal["state"],
                 "since": now, "runs": 1, "detail": signal["detail"],
                 "pending": "", "pending_runs": 0})


def _owner_answered(seen: Dict[str, Dict[str, Any]], held: Dict[str, Any]) -> bool:
    """Did the device this thing hangs off answer in this run?"""
    device = held.get("device") or ""
    signal = seen.get("%s::device" % device)
    return bool(signal) and signal.get("state") == UP


def update(state: Dict[str, Any], seen: Dict[str, Dict[str, Any]],
           now: Optional[str] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Fold this run into what was known, and say what actually changed."""
    now = now or _now()
    known = state.setdefault("signals", {})
    events: List[Dict[str, Any]] = []

    for key, signal in seen.items():
        held = known.get(key)
        if held is None:
            known[key] = {"state": signal["state"], "since": now, "checked": now,
                          "runs": 1, "previous": "", "label": signal["label"],
                          "kind": signal["kind"], "device": signal["device"],
                          "detail": signal["detail"], "value": signal["value"],
                          "belongs": signal.get("belongs", ""),
                          "pending": "", "pending_runs": 0}
            continue        # the first sight of a thing is not a change in it
        held["checked"] = now
        held["label"] = signal["label"]
        held["value"] = signal["value"]
        if signal["state"] == held["state"]:
            held["runs"] = int(held.get("runs") or 0) + 1
            held["pending"], held["pending_runs"] = "", 0
            held["detail"] = signal["detail"]
            continue
        if held.get("pending") == signal["state"]:
            held["pending_runs"] = int(held.get("pending_runs") or 0) + 1
        else:
            held["pending"], held["pending_runs"] = signal["state"], 1
        if held["pending_runs"] < CONFIRM.get(signal["state"], 2):
            continue        # not believed yet, and not worth telling anyone
        events.append({"at": now, "key": key, "device": signal["device"],
                       "label": signal["label"], "kind": signal["kind"],
                       "belongs": signal.get("belongs", ""),
                       "from": held["state"], "to": signal["state"],
                       "detail": signal["detail"]})
        _remember(held, signal, now)

    for key, held in known.items():
        if key in seen or held.get("state") in (UNKNOWN, DOWN):
            continue
        held["checked"] = now
        missing = DOWN if (held.get("kind") == "handset"
                           and _owner_answered(seen, held)) else UNKNOWN
        if held.get("pending") == missing:
            held["pending_runs"] = int(held.get("pending_runs") or 0) + 1
        else:
            held["pending"], held["pending_runs"] = missing, 1
        if held["pending_runs"] < CONFIRM[missing]:
            continue
        gone = {"state": UNKNOWN, "detail": "it was not in this run at all"}
        if held.get("kind") == "handset" and _owner_answered(seen, held):
            # The NUC answered and the handset is not on it any more. That is
            # not "we could not look" - it is unplugged.
            gone = {"state": DOWN, "detail": "it is no longer plugged in"}
        events.append({"at": now, "key": key, "device": held.get("device", ""),
                       "label": held.get("label", key),
                       "kind": held.get("kind", ""),
                       "belongs": held.get("belongs", ""),
                       "from": held["state"],
                       "to": gone["state"], "detail": gone["detail"]})
        _remember(held, gone, now)

    events = _fold(events)
    if events:
        state.setdefault("events", []).extend(events)
        state["events"] = state["events"][-HISTORY:]
    state["last_run"] = now
    return state, events


def _fold(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One cause, one line.

    When a host stops answering, every port, optic and card on it goes out of
    sight in the same run. Twenty lines for one event is how a log stops being
    read, so the parts are counted onto the device that took them with it.
    """
    lost = {e["device"] for e in events
            if e["kind"] == "device" and e["to"] in (DOWN, UNKNOWN)}
    # The cable to a handset that left went with the handset. Saying so twice
    # is how one unplugged phone becomes two lines in the morning.
    pulled = {e.get("belongs") for e in events
              if e["kind"] == "handset" and e["to"] in (DOWN, UNKNOWN)
              and e.get("belongs")}
    if not (lost or pulled):
        return events
    kept, swallowed = [], {}
    for event in events:
        if (event["kind"] != "device" and event["to"] == UNKNOWN
                and event["device"] in lost):
            swallowed[event["device"]] = swallowed.get(event["device"], 0) + 1
            continue
        if event["kind"] == "link" and event.get("belongs") in pulled:
            continue
        kept.append(event)
    for event in kept:
        count = swallowed.get(event["device"], 0)
        if event["kind"] == "device" and count:
            event["detail"] = ("%s; %d thing(s) on it went out of sight with it"
                               % (event["detail"] or "it stopped answering", count))
    return kept


def describe(events: List[Dict[str, Any]]) -> List[str]:
    """One line each, in the order they happened, for a log a person reads."""
    words = {UP: "came back", DEGRADED: "degraded", DOWN: "went down",
             UNKNOWN: "cannot be seen"}
    out = []
    for event in events:
        line = "%s  %s %s" % (event["at"], event["label"],
                              words.get(event["to"], "became " + event["to"]))
        if event.get("detail"):
            line += " - %s" % event["detail"]
        out.append(line)
    return out


def standing(state: Dict[str, Any]) -> Dict[str, int]:
    """How many signals sit in each state right now."""
    out = {UP: 0, DEGRADED: 0, DOWN: 0, UNKNOWN: 0}
    for held in (state.get("signals") or {}).values():
        where = held.get("state", UNKNOWN)
        out[where] = out.get(where, 0) + 1
    return out

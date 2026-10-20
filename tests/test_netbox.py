"""Checks for reading an inventory and saying where it and reality differ.

Nothing here touches a network: the client takes its fetcher as an argument.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import netbox

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-66s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


def raw(ident, name, serial="", ip="", tags=(), status="active"):
    """A record shaped the way NetBox sends it, before simplify()."""
    return {"id": ident, "name": name, "serial": serial,
            "primary_ip": {"address": ip + "/24"} if ip else None,
            "tags": [{"name": t} for t in tags],
            "status": {"value": status, "label": status.title()},
            "display_url": "https://nb.example/dcim/devices/%d/" % ident}


def record(ident, name, serial="", ip="", tags=(), status="active", macs=()):
    """The same record as the tool keeps it, after simplify()."""
    out = netbox.simplify(raw(ident, name, serial, ip, tags, status))
    out["macs"] = list(macs)
    return out


def node(ident, name, serial="", ips=(), kind="host", state="scanned",
         chassis="", macs=None):
    out = {"id": ident, "name": name, "serial": serial, "kind": kind,
           "state": state, "mgmt_ips": list(ips), "chassis_id": chassis}
    if macs:
        out["extra"] = {"macs": macs}
    return out


print("-- a token says what is wrong with it before a 403 does --")
check("a whole one passes", netbox.token_warning("nbt_AbCd.secret"), "")
check("the secret half alone is named as such",
      "secret half only" in netbox.token_warning("secretsecret"), True)
check("a key with no secret is named too",
      "no secret half" in netbox.token_warning("nbt_AbCd"), True)
check("and nothing at all is not a mystery",
      netbox.token_warning(""), "no NetBox token given")

print()
print("-- where the address and token come from --")
os.environ.pop(netbox.ENV_URL, None)
os.environ.pop(netbox.ENV_TOKEN, None)
check("a flag wins",
      netbox.settings("https://a.example/", "flagtoken",
                      {"netbox": {"url": "https://b.example", "token": "saved"}}),
      ("https://a.example", "flagtoken"))
os.environ[netbox.ENV_URL] = "https://env.example"
os.environ[netbox.ENV_TOKEN] = "envtoken"
check("then the environment", netbox.settings("", "", {}),
      ("https://env.example", "envtoken"))
os.environ.pop(netbox.ENV_URL)
os.environ.pop(netbox.ENV_TOKEN)
check("then the defaults file",
      netbox.settings("", "", {"netbox": {"url": "https://b.example/",
                                          "token": "saved"}}),
      ("https://b.example", "saved"))
check("and nothing is hard-coded", netbox.settings("", "", {}), ("", ""))

print()
print("-- asking for many at once, and walking the pages --")
NUC_RAW = raw(11, "bench-nuc", serial="ABC1234", ip="198.51.100.10",
              tags=["bench-one", "4G"])
PHONE_RAW = raw(12, "handset one", serial="aaa1", tags=["bench-one"])
NUC = netbox.simplify(NUC_RAW)
asked = []


def fake(path, params):
    asked.append((path, list(params) if isinstance(params, list) else params))
    if path == netbox.DEVICES:
        pairs = params if isinstance(params, list) else []
        wanted = [v for k, v in pairs if k in ("serial", "id")]
        found = [d for d in (NUC_RAW, PHONE_RAW)
                 if d["serial"] in wanted or str(d["id"]) in wanted]
        return {"count": len(found), "results": found}
    if path == netbox.IP_ADDRESSES:
        return {"count": 1, "results": [{
            "address": "198.51.100.10/24",
            "assigned_object": {"name": "eno1",
                                "device": {"id": 11, "name": "bench-nuc"}}}]}
    return {"count": 0, "results": []}


client = netbox.Netbox("https://nb.example", "nbt_a.b", get=fake)
check("several serials go in one request",
      len(client.devices_by("serial", ["ABC1234", "aaa1"])), 2)
check("and they really were asked as one OR",
      len([1 for path, _p in asked if path == netbox.DEVICES]), 1)
check("only the columns that are used are asked for",
      any("fields" in dict(p) for _path, p in asked if isinstance(p, list)), True)


def paged(path, params):
    page = dict(params)
    offset = int(page.get("offset", 0))
    rows = [{"id": n, "serial": "S%d" % n} for n in range(offset, offset + 250)]
    return {"count": 600, "results": rows[:min(250, 600 - offset)]}


check("a long answer is walked to the end",
      len(netbox.Netbox("u", "t", get=paged).every(netbox.DEVICES, {})), 600)

print()
print("-- only what the scan saw is asked about --")
TOPOLOGY = {"nodes": [
    node("ip:198.51.100.10", "nuc1", serial="ABC1234", ips=["198.51.100.10"]),
    node("chassis:usb:aaa1", "a handset", serial="aaa1", kind="phone"),
    node("chassis:usb:zzz9", "FT230X UART", serial="ZZZ9", kind="serial"),
    node("ip:198.51.100.77", "never answered", serial="NOPE1",
         ips=["198.51.100.77"], state="unreachable")], "edges": []}
check("the serials and addresses worth asking about",
      netbox.wanted_from(TOPOLOGY),
      (["ABC1234", "aaa1", "ZZZ9", "NOPE1"], ["198.51.100.10", "198.51.100.77"]))
inventory = netbox.fetch(client, TOPOLOGY, lambda _m: None)
check("two of the four are known", len(inventory["devices"]), 2)

print()
print("-- what identifies a device, and what only hints at it --")
found, signals, sure, ask = netbox.identify(TOPOLOGY["nodes"][0], inventory)
check("a serial settles it", (found["id"], sure, ask), (11, netbox.HIGH, False))
check("and says so in words, listing everything that agreed",
      signals, sorted([netbox.BY_SERIAL, netbox.BY_ADDRESS]))

INV_NAMED = {"devices": [record(21, "bench-b-SN0000000X5304276",
                                serial="")], "addresses": {}}
found, signals, sure, ask = netbox.identify(
    node("x", "bench-b", serial="SN0000000X5304276"), INV_NAMED)
check("a serial inside the NetBox name settles it too",
      (found["id"], sure, ask), (21, netbox.HIGH, False))

HINTS = {"devices": [record(31, "nuc1", serial="OTHER9")],
         "addresses": {"198.51.100.10": {"id": 31, "name": "nuc1"}}}
found, signals, sure, ask = netbox.identify(
    node("y", "nuc1", serial="", ips=["198.51.100.10"]), HINTS)
check("an address and a hostname together are still only hints",
      (found["id"], ask), (31, True))
check("so the tool asks rather than deciding", ask, True)

found, signals, sure, ask = netbox.identify(
    node("z", "nobody", serial="", ips=["198.51.100.99"]), HINTS)
check("nothing matching is nothing, not a guess", found, None)

TWO = {"devices": [record(41, "bench-c", serial="AAA1"),
                   record(42, "bench-c", serial="BBB2")],
       "addresses": {}}
found, signals, sure, ask = netbox.identify(
    node("w", "bench-c", serial="AAA1"), TWO)
check("two records answering one device is a conflict, not a coin toss",
      (found, ask), (None, True))

LINKED = {"devices": [record(51, "whatever", serial="")], "addresses": {}}
found, _s, sure, ask = netbox.identify(node("n1", "x"), LINKED,
                                       links={"n1": 51})
check("a link confirmed earlier settles it", (found["id"], sure), (51, netbox.HIGH))

print()
print("-- a MAC helps when it is there, and is never required --")
WITHMAC = {"devices": [dict(record(61, "box", serial=""),
                            macs=["aa:bb:cc:dd:ee:01"])], "addresses": {}}
found, signals, sure, ask = netbox.identify(
    node("m", "a name nobody shares", macs={"eno1": "AA:BB:CC:DD:EE:01"}),
    WITHMAC)
check("a MAC is worth something", (found["id"], sure), (61, netbox.MEDIUM))
check("but on its own it is still worth confirming", ask, True)

print()
print("-- the five verdicts --")
found = netbox.review(TOPOLOGY, inventory)
by_label = {f["label"]: f for f in found}
check("a device that agrees says so", by_label["nuc1"]["status"], netbox.MATCH)
check("a handset in NetBox agrees too",
      by_label["a handset"]["status"], netbox.MATCH)
check("a console lead is not an asset and is not judged",
      "FT230X UART" in by_label, False)
check("a host nobody reached is not judged either",
      "never answered" in by_label, False)
check("every verdict carries its evidence",
      all(f["evidence"] for f in found), True)

GONE = {"nodes": [node("ip:1", "orphan", serial="NOTTHERE1", ips=["198.51.100.5"])],
        "edges": []}
check("a serial NetBox never heard of is missing documentation",
      netbox.review(GONE, inventory)[0]["status"], netbox.MISSING_IN_NETBOX)

NOSERIAL = {"nodes": [node("ip:2", "quiet", serial="")], "edges": []}
check("a device that published no serial cannot be checked",
      netbox.review(NOSERIAL, inventory)[0]["status"], netbox.NOT_VERIFIED)

SWAP = {"nodes": [node("ip:198.51.100.10", "nuc1", serial="DIFFERENT9",
                       ips=["198.51.100.10"])], "edges": []}
swapped = netbox.review(SWAP, inventory)[0]
check("a box swapped for another at a documented address is a mismatch",
      swapped["status"], netbox.MISMATCH)
check("and the line names both serials",
      "DIFFERENT9" in swapped["detail"] and "ABC1234" in swapped["detail"], True)
check("with the field it disagrees about", swapped["field"], "serial")

# A server here commonly has ONE address on record and it is the BMC's, on an
# interface called IPMI. Judging the address it answered on against that was
# calling the documentation wrong when it was merely incomplete - and it fired
# on real devices before this was fixed.
IPMI_ONLY = {"devices": [dict(NUC, primary_ip="10.0.0.9", addresses=[
    {"address": "10.0.0.9", "port": "IPMI"}])], "addresses": {}}
told = netbox.review({"nodes": [TOPOLOGY["nodes"][0]], "edges": []}, IPMI_ONLY)[0]
check("an address NetBox never recorded is a gap, not a contradiction",
      told["status"], netbox.MISSING_IN_NETBOX)
check("and the line says what NetBox does hold, and on which port",
      "10.0.0.9 on IPMI" in told["detail"], True)
check("naming the field so it can be acted on", told["field"], "ip_address")

BOTH = {"devices": [dict(NUC, addresses=[
    {"address": "10.0.0.9", "port": "IPMI"},
    {"address": "198.51.100.10", "port": "eno1"}])], "addresses": {}}
check("a device whose address IS recorded somewhere agrees",
      netbox.review({"nodes": [TOPOLOGY["nodes"][0]], "edges": []},
                    BOTH)[0]["status"], netbox.MATCH)

NO_ADDRESSES = {"devices": [dict(NUC, addresses=[])], "addresses": {}}
check("a device with no address on record at all is not accused either",
      netbox.review({"nodes": [TOPOLOGY["nodes"][0]], "edges": []},
                    NO_ADDRESSES)[0]["status"], netbox.MATCH)

RETIRED = {"devices": [dict(NUC, status="offline")], "addresses": {}}
told = netbox.review({"nodes": [TOPOLOGY["nodes"][0]], "edges": []}, RETIRED)
check("NetBox calling a live device offline is a mismatch",
      (told[0]["status"], told[0]["field"]), (netbox.MISMATCH, "status"))

doubt = netbox.review({"nodes": [node("y", "nuc1", ips=["198.51.100.10"])],
                       "edges": []}, HINTS)[0]
check("a device found only by hints is not verified",
      doubt["status"], netbox.NOT_VERIFIED)
check("and it is flagged for a person to confirm", doubt["confirm"], True)

clash = netbox.review({"nodes": [node("w", "bench-c", serial="AAA1")],
                       "edges": []}, TWO)[0]
check("two candidate records is a conflict", clash["status"], netbox.CONFLICT)

print()
print("-- worst first, and a count to read before the list --")
MIXED = {"nodes": [TOPOLOGY["nodes"][0], node("ip:9", "orphan", serial="NOPE9"),
                   node("w", "bench-c", serial="AAA1")], "edges": []}
both = dict(inventory)
both["devices"] = inventory["devices"] + TWO["devices"]
order = [f["status"] for f in netbox.sort_review(netbox.review(MIXED, both))]
check("conflicts come before anything else", order[0], netbox.CONFLICT)
check("and agreement comes last", order[-1], netbox.MATCH)
check("the standing counts each verdict",
      sorted(netbox.standing(netbox.review(MIXED, both))),
      sorted([netbox.CONFLICT, netbox.MISSING_IN_NETBOX, netbox.MATCH]))

print()
print("-- a Setup is a tag somebody named, not every tag --")
check("nothing is a setup until it is named",
      netbox.setup_tags({}), [])
check("the named ones come back",
      netbox.setup_tags({"netbox": {"setup_tags": ["bench-one"]}}), ["bench-one"])
check("one name need not be a list",
      netbox.setup_tags({"netbox": {"setup_tags": "bench-one"}}), ["bench-one"])
check("the tags this scan actually saw are a short list to choose from",
      netbox.tags_seen(inventory), [("bench-one", 2), ("4G", 1)])

fresh = json.loads(json.dumps(TOPOLOGY))
check("with nothing named, no device is put in a setup",
      netbox.apply(fresh, inventory, allow=[]), 0)
check("but the record is still shown",
      fresh["nodes"][0]["netbox"]["name"], "bench-nuc")

fresh = json.loads(json.dumps(TOPOLOGY))
put = netbox.apply(fresh, inventory, allow=["bench-one"])
check("naming one brings it across", put, 2)
check("and only that one - not the programme tag it also carries",
      fresh["nodes"][0]["setups"], ["bench-one"])

again = netbox.apply(fresh, inventory, allow=["bench-one"])
check("running it twice changes nothing", fresh["nodes"][0]["setups"], ["bench-one"])

HAND = json.loads(json.dumps(TOPOLOGY))
HAND["nodes"][0]["setups"] = ["my own name"]
netbox.apply(HAND, inventory, allow=["bench-one"])
check("a name marked by hand survives the import",
      HAND["nodes"][0]["setups"], ["bench-one", "my own name"])

HINTED = {"nodes": [node("y", "nuc1", ips=["198.51.100.10"])], "edges": []}
netbox.apply(HINTED, HINTS, allow=["bench-one"])
check("a device found only by a hint is never moved into somebody's bench",
      HINTED["nodes"][0].get("setups"), None)
check("though its doubtful match is still shown",
      HINTED["nodes"][0]["netbox"]["confirm"], True)

print()
print("-- a proposal corrects a place, and never creates anything --")
check("nothing to fix when the record is already right",
      netbox.propose(TOPOLOGY, inventory), [])
WRONG = {"devices": [dict(NUC, primary_ip="198.51.100.99", status="offline")],
         "addresses": {}}
proposals = netbox.propose(TOPOLOGY, WRONG)
check("the address and the status are offered",
      sorted(p["field"] for p in proposals), ["primary_ip", "status"])
check("each one says why", all(p["why"] for p in proposals), True)
check("a device nobody recorded is never proposed for creation",
      any(p.get("label") == "FT230X UART" for p in proposals), False)
check("identity is left to people",
      any(p["field"] in netbox.OWNED_BY_PEOPLE for p in proposals), False)
check("and a doubtful match proposes nothing at all",
      netbox.propose(HINTED, HINTS), [])

WHERE = os.path.join(tempfile.gettempdir(), "lf_netbox_proposals.json")
netbox.save_proposals(proposals, WHERE)
body = json.load(io.open(WHERE, encoding="utf-8"))
check("the file carries the rule it was made under",
      "creates a device record" in body["rule"], True)
check("and says nothing was sent", "was sent" in body["rule"], True)

print()
print("-- when NetBox is not there --")


def dead(_path, _params):
    raise RuntimeError("connection refused")


broken = netbox.Netbox("https://nb.example", "nbt_a.b", get=dead)
check("it says why rather than raising",
      "connection refused" in broken.reachable(), True)
check("and a scan that asked for it still gets its devices back",
      netbox.fetch(broken, {"nodes": [], "edges": []}, lambda _m: None)["devices"],
      [])


def refuses(path, params):
    if path == netbox.IP_ADDRESSES:
        raise RuntimeError("403 Forbidden")
    return fake(path, params)


half = netbox.Netbox("https://nb.example", "nbt_a.b", get=refuses)
try:
    netbox.fetch(half, TOPOLOGY, lambda _m: None)
    check("a token refused one endpoint does not lose the others", "raised", "handled")
except Exception as problem:
    check("a token refused one endpoint does not lose the others",
          "403" in str(problem), True)

print()
print("-- the inventory survives a restart --")
STORE = os.path.join(tempfile.gettempdir(), "lf_netbox_cache.json")
netbox.save(inventory, STORE)
check("what was read is read back",
      len(netbox.load(STORE)["devices"]), len(inventory["devices"]))
check("and a missing cache is not a crash",
      netbox.load(os.path.join(tempfile.gettempdir(), "lf_no_such.json")),
      {"devices": [], "addresses": {}})
check("no token is ever written into it",
      "nbt_" in io.open(STORE, encoding="utf-8").read(), False)

print()
print("-- NetBox is optional, and must stay that way --")
# A lab without NetBox, or with NetBox down, has to get exactly the same map.
# So nothing about it may be imported, reached or required unless asked for.
import builtins as _builtins

_real = _builtins.__import__


def _no_requests(name, *a, **k):
    if name == "requests" or name.startswith("requests."):
        raise ImportError("no requests on this machine")
    return _real(name, *a, **k)


for _mod in [m for m in list(sys.modules) if m.startswith("requests")]:
    del sys.modules[_mod]
_builtins.__import__ = _no_requests
try:
    import importlib
    importlib.reload(netbox)
    check("the module imports with no HTTP library present", True, True)
    check("and still answers the questions that need no network",
          netbox.settings("", "", {}), ("", ""))
    check("asking NetBox then says why rather than crashing",
          "no requests" in netbox.Netbox("https://x", "nbt_a.b").reachable(), True)
finally:
    _builtins.__import__ = _real
    import importlib
    importlib.reload(netbox)

check("the HTTP library is pulled in only when a request is made",
      "import requests" in io.open(
          os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "linkfinder", "netbox.py"), encoding="utf-8"
      ).read().split("def get(")[1][:400], True)


print()
print("-- one vocabulary, and the verdict put where it can be seen --")
check("the two halves of the tool say the same words",
      [netbox.in_words(k) for k in (netbox.MATCH, netbox.MISMATCH,
                                    netbox.MISSING_IN_NETBOX,
                                    netbox.NOT_VERIFIED, netbox.CONFLICT)],
      ["agrees", "differs", "only here", "not checked", "ambiguous"])
check("an unknown status is still readable, not a constant",
      netbox.in_words("SOMETHING_NEW"), "something new")

MAP = {"nodes": [{"id": "a", "name": "one"}, {"id": "b", "name": "two"},
                 {"id": "c", "name": "three"}], "edges": []}
FOUND = [{"device": "a", "status": netbox.MATCH},
         {"device": "b", "status": netbox.MATCH},
         {"device": "b", "status": netbox.MISMATCH},
         {"device": "c", "status": netbox.MISSING_IN_NETBOX}]
check("every device that was judged is stamped",
      netbox.stamp_verdicts(MAP, FOUND), 3)
states = dict((n["name"], n.get("netbox_state")) for n in MAP["nodes"])
check("in the shared words", states["one"], "agrees")
check("and where a device has two verdicts the worse one wins",
      states["two"], "differs")
check("a device NetBox has never heard of says so",
      states["three"], "only here")

quiet = {"nodes": [{"id": "z", "name": "nobody asked"}], "edges": []}
netbox.stamp_verdicts(quiet, [])
check("a device with no verdict is left unmarked, not called agreeing",
      "netbox_state" in quiet["nodes"][0], False)


print()
print("-- an older NetBox is not turned away at the door --")
# Measured against a live 4.5 server: it accepts a v2 token under both
# schemes, and an older server knows only Token. Refusing a classic token
# outright turned a working setup into an error.
check("a classic 40-character token is accepted",
      netbox.token_warning("0123456789abcdef0123456789abcdef01234567"), "")
check("and is sent the way an older server expects",
      netbox.Netbox("https://x", "0123456789abcdef0123456789abcdef01234567"
                    ).scheme(), "Token")
check("a 4.5 token is sent the way 4.5 documents",
      netbox.Netbox("https://x", "nbt_key.secret").scheme(), "Bearer")
check("the secret half alone is still refused, with both forms named",
      "40-character hexadecimal" in netbox.token_warning("y" * 40 + "Z"), True)
check("and a 4.5 token with no secret still says what is missing",
      "no secret half" in netbox.token_warning("nbt_keyonly"), True)

print()
print("-- two serials in one NetBox field is still the serial --")
# From a live record: the serial field held "A450459X4703513 (1X200532408)",
# the chassis and the product. Comparing the whole string matched nothing, so
# a device NetBox knows perfectly well was called a disagreement and matched
# by its address - the weakest key there is.
from linkfinder.netbox import identify, serial_parts

check("the field is split into the serials in it",
      serial_parts("A450459X4703513 (1X200532408)"),
      ["A450459X4703513", "1X200532408"])
check("and a short fragment is not one", serial_parts("ab-cd (12345)"), [])

node = {"id": "ip:10.0.0.9", "serial": "1X200532408", "name": "bench",
        "mgmt_ips": ["10.0.0.9"], "extra": {}}
shelf = {"devices": [{"id": 7, "name": "BBU_BM_Integ",
                      "serial": "A450459X4703513 (1X200532408)"}],
         "addresses": {}}
record, signals, confidence, ask = identify(node, shelf)
check("the record is found", (record or {}).get("id"), 7)
check("by the serial, not by a hint",
      "its serial among the ones NetBox records" in signals, True)
check("so nobody has to confirm it", ask, False)
check("and it is trusted", confidence, "HIGH")

other = {"devices": [{"id": 8, "name": "someone else",
                      "serial": "A450459X4703513"}], "addresses": {}}
record, _signals, _c, _a = identify(node, other)
check("a serial that is simply not there still matches nothing", record, None)

print()
if FAILURES:
    print("FAILURES:")
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all netbox checks passed")

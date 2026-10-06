# LinkFinder

*Idea, design and architecture by Stas Mitin. MIT licensed - see [LICENSE](LICENSE).*

Find which **switch** and which **switch port** every link is plugged into — for any
system in the lab, not just the two cases the old tools covered.

It replaces and merges:

| Old tool | What it did | Where it stopped |
|---|---|---|
| `check_port.py` / `LLDP_Checker.exe` | SSH to a Linux host, `tcpdump` LLDP for 60 s on the interface holding the management IP | needed `tcpdump`; one interface only; LLDP only; text parsing; one host per run |
| `read_ipmi_port.py` | Redfish LLDP from the BMC | Supermicro and HPE only; one host per run |

Full reference, including every GUI field and every flag: **[DOCUMENTATION.md](DOCUMENTATION.md)**.

## What it does differently

1. **A ladder, not one method.** Each target is tried cheapest-first and stops as soon as
   it has an answer. A host running `lldpd` answers in under a second instead of waiting
   60 s for a capture.
2. **It works where `tcpdump` does not.** The last rung ships a small raw-socket sniffer
   over SSH and runs it with the target's own `python3`. Nothing has to be installed.
3. **Every interface, not just the management one.** `bond0.3674` is resolved through
   `/sys/class/net/*/lower_*` down to the real physical legs, so you get one row per
   cable — which is what a bond hides.
4. **CDP as well as LLDP.** Cisco-only switches were previously invisible.
5. **One binary decoder.** Every capture path (tcpdump, tshark, the raw sniffer, pktmon,
   pktcap-uw) returns pcap/pcapng frames that go through a single LLDP/CDP decoder, so
   there is no regex-over-`tcpdump`-text to break.
6. **The switch side.** For gear you cannot log into — RUs, PDUs, UPSs, appliances — it
   asks the switch instead, over SNMP (LLDP-MIB + the forwarding table) or the switch CLI.
7. **Batch + machine-readable.** Many targets in parallel, table / JSON / CSV output.

## Access paths

| Target | How it is interrogated |
|---|---|
| Linux (any distro) | `lldpctl` → `networkctl lldp` → `lldptool` → `tcpdump` → `tshark` → raw-socket sniffer |
| SUSE / PW nodes | the same, plus `toolbox` when `tcpdump` only exists inside it |
| ESXi | `vim-cmd hostsvc/net/query_networkhint` (CDP **and** LLDP), then `pktcap-uw` |
| Windows | `pktmon` → pcapng, locally or over OpenSSH |
| BMC | Redfish: standard `Port.Ethernet.LLDPReceive`, then an OEM table (Supermicro, HPE iLO, Dell iDRAC, Lenovo XCC, Inspur, Ampere), then a generic hunt for any chassis-id/port-id object |
| Anything else | switch side: SNMP LLDP-MIB, CDP-MIB and the bridge forwarding table; or the switch CLI |

## Install

```bash
pip install -r requirements.txt     # paramiko, requests, urllib3
```

SNMP needs nothing — the SNMPv2c client is hand-rolled on the standard library.

## Usage

```bash
# one Linux host, everything auto-detected
python -m linkfinder 10.70.9.93 -u root -p secret

# a BMC
python -m linkfinder 10.70.9.40 --bmc-user ADMIN --bmc-password secret -m bmc

# a whole rack, 8 at a time, into a CSV inventory
python -m linkfinder --targets-file hosts.txt -u root -p secret --csv rack.csv

# something you cannot log into: ask the switches instead
python -m linkfinder 192.168.1.13 -m switch --switch 10.70.244.30 --community public \
    --mac aa:bb:cc:dd:ee:11

# switch side as a cross-check on a host you CAN log into - it reads the MACs itself
python -m linkfinder 10.70.129.27 -m switch -u root -p secret --switch 10.70.244.30

# old LLDP_Checker behaviour: management interface only
python -m linkfinder 10.70.9.93 -u root -p secret --mgmt-only

# the window
python -m linkfinder --gui
```

Useful flags: `-t/--seconds` capture window (default 65, LLDP ticks every 30 s),
`--iface` to restrict interfaces, `-j` parallel targets, `-w` all columns,
`--json`, `--csv -`, `-q` for results only.

Exit code is `0` if at least one link resolved, `1` if none did.

## Pointing it at a switch

Give it a switch instead of a host and it logs in, works out the NOS and prints that
switch's whole neighbour table - one row per switch port and what is on the other end:

```bash
python -m linkfinder 10.70.129.2 -u admin -p secret
```

Auto-detection handles this on its own: if the SSH target is not Linux/ESXi/Windows it is
treated as a network device. `-m switch-cli` forces it.

| NOS | detected from | neighbours read with |
|---|---|---|
| Cisco IOS / IOS-XE | `cisco`, `ios` | `show lldp neighbors detail`, `show cdp neighbors detail` |
| Cisco NX-OS | `nx-os`, `nexus` | `show lldp neighbors detail`, `show cdp neighbors detail` |
| Arista EOS | `arista` | `show lldp neighbors detail` |
| Juniper JunOS | `junos` | `show lldp neighbors` |
| MikroTik RouterOS | `routeros` | `/ip/neighbor/print detail` |
| Aruba OS-CX | `arubaos-cx`, `aruba` | `show lldp neighbor-info detail` |
| HPE/Aruba ProCurve | `procurve` | `show lldp info remote-device detail` |
| Dell OS10 | `os10`, `dell` | `show lldp neighbors detail` |
| Extreme EXOS | `extremexos` | `show lldp neighbors detail` |
| Huawei VRP / H3C Comware | `huawei`, `vrp`, `h3c` | `display lldp neighbor brief` |

An unrecognised NOS still gets every command tried in turn. Output shapes reduce to three
parsers - `Key: value` blocks, whitespace columns under a header, and RouterOS
`key=value` - so a dialect that is close to one of those usually parses without a code
change. The same table drives the MAC lookup (`show mac address-table address ...` and
its equivalents) used when SNMP is off on a switch.

## Port status and health (opt-in)

Entirely optional. Without `--port-health`, or the matching tick in the window, not one
extra SNMP request is made.

```bash
python -m linkfinder 198.51.100.1 --port-health --community <community>
```

It reads the switch port table over the SNMP transport discovery already uses and gives
each port a verdict: `UP`, `DOWN`, `DISABLED`, `DEGRADED` or `UNKNOWN`, plus whether the
port looks `USED` or a `FREE_CANDIDATE`, and how confident that is.

**An absolute counter proves nothing.** `ifInErrors = 5000` may be five years old, so the
counters are read twice a few seconds apart and only a counter that actually moved counts
as evidence. A port with thousands of old errors and none new is reported healthy; a port
whose errors grew by 37 during the sample is DEGRADED, and the reason says so.

## Topology discovery

Give it one seed device and it walks outwards along LLDP/CDP, building a graph:

```bash
python -m linkfinder --topology 10.70.129.27 -u admin -p secret \
  --max-depth 2 --max-devices 25 --scope 10.70.0.0/16 \
  --topology-json site.topology.json --topology-html site.topology.html
```

```
DEPTH  DEVICE       KIND    STATE         MANAGEMENT IP  CHASSIS
0      delta        host    scanned       10.70.129.27   -
1      sw-lab-1     switch  scanned       10.70.244.30   00:05:80:08:3e:bf
1      sw-edge-1    switch  out-of-scope  10.78.94.1     04:f4:1c:eb:06:4d
2      sw-access-1  switch  unscanned     -              ec:50:aa:1f:ec:80
```

**Identity is the chassis id, not the address.** One device wearing ten VLAN addresses is
one node: as soon as a chassis id arrives for a node first seen by IP the node is re-keyed
and the addresses collect in `mgmt_ips`. Two different chassis ids on one address stay two
devices. Merging never destroys what is already known - addresses and sources are unioned,
the first and last sighting widen, the more informative name or model wins, and a later
failure cannot overwrite an earlier success.

**Depth.** The seed is depth 0. At `--max-depth N` the devices at depth N are still
interrogated, but their neighbours are recorded without being queued.

**Limits.** `--max-depth`, `--max-devices` and `--scope` are always in force. Scope only
restricts who we *connect to*: a neighbour outside it is still drawn, marked
`out-of-scope`.

**Confirm by default.** Between levels it asks:

```
Found 6 new network device(s) at depth 1:
   10.70.244.30     sw-lab-1
Add and scan discovered neighbours? [y/N]
```

`--auto-discover` is the explicit opt-out and is never the default.

**Nothing is a dead end.** A neighbour with no management address still becomes a node,
marked `unscanned`; a device that refuses the login becomes `unreachable` with the reason
attached; neither stops the rest of the walk. Only management addresses that a discovery
protocol actually reported are followed - this is not a subnet scanner.

**Links are deduplicated and graded.** A cable reported from both ends is one edge marked
`confirmed`; seen from one end only it is `reported`; derived from a forwarding table it
is `inferred`.

### The JSON, and the picture

Discovery writes the JSON; the renderer only ever reads it, so the same file can feed
another tool:

```bash
python -m linkfinder --render-topology site.topology.json site.topology.html
```

```json
{
  "schema_version": 1,
  "generated_at": "2026-10-05T16:21:17",
  "seed": "10.70.129.27",
  "nodes": [ { "id": "chassis:00:05:80:08:3e:bf", "kind": "switch", "state": "scanned",
               "mgmt_ips": ["10.70.244.30"], "sources": ["snmp"] } ],
  "edges": [ { "a": "...", "a_iface": "1/1/28", "b": "...", "b_iface": "21",
               "confidence": "confirmed", "protos": ["LLDP"] } ]
}
```

The HTML is one self-contained file - the topology and `cytoscape.min.js` are both
embedded, so it opens from disk with no web server and no internet. Zoom, pan, draggable
nodes, four layouts, a filter box, colour and shape per device kind, solid/dashed/dotted
links by confidence, interface names at each end of every link, and a click-through panel
showing every field. The library lives in `linkfinder/assets/` with its MIT licence.

In the window: the Topology row sets the limits and the Auto Discovery tick, then
**Generate Topology** and **View Topology**.

## It can remember switches - if you ask it to

Off by default: without `--learn` the tool writes nothing to disk at all.

With `--learn`, every management address seen in an LLDP/CDP frame, plus the neighbours
any SNMP-reachable switch names (`lldpRemManAddrTable`), is written to
`~/.linkfinder-switches.json`, and later runs re-query everything known without being
told `--switch` again:

```
  remembered a new switch: 10.70.129.2
  plus 2 remembered switch(es): 10.70.244.30, 10.78.94.1
switch side: asking 10.70.244.30, 10.78.94.1
```

```bash
python -m linkfinder ... --learn            # record and reuse (otherwise nothing is kept)
python -m linkfinder --list-switches        # what it knows, and whether SNMP answered
python -m linkfinder --forget-switches      # wipe it
python -m linkfinder --forget-switches IP   # drop one
```

The window has the same two buttons and an unticked "remember switches it finds" box.

A switch that did not answer SNMP is remembered too, marked as such, and skipped for 24 h
so it costs no time - but it still appears in the list and is still used by the switch-CLI
fallback when `--switch-user` is given.

The SNMP community is a credential and is **not** written even with `--learn`; add
`--remember-credentials` if you want the working community stored per switch. The file is
plain JSON, lives in your home directory and is in `.gitignore`; delete it to start over.

Note that one switch can be learned under several addresses: a router-on-a-stick with many
VLAN interfaces advertises whichever address faces the link in question.

## Secrets and what is written to disk

The code contains no addresses and no credentials, and that is deliberate - everything
site-specific is supplied at run time. Three places can hold real values, all of them
outside the repository and all in `.gitignore`:

| file | holds | written when |
|---|---|---|
| `~/.linkfinder.json` | your usernames and passwords | only by you, by hand |
| `~/.linkfinder-switches.json` | switch addresses and names | only with `--learn` |
| the same file | SNMP communities | only with `--learn --remember-credentials` |

Prefer the environment variables over the file if you would rather not have a password
on disk at all. Nothing is ever sent anywhere; the tool only talks to the targets you name.

Before committing: no lab address, no hostname and no password belongs in the tree. Every
address, MAC and device name in the tests and in this file is rewritten or invented on
purpose - none of them point at real equipment.

## Defaults file

`~/.linkfinder.json` so credentials are not retyped (keep it out of any repo):

```json
{
  "user": "root",
  "password": "...",
  "bmc_user": "ADMIN",
  "bmc_password": "...",
  "community": "public",
  "switches": ["10.70.9.1", "10.70.9.2@othercommunity"],
  "switch_user": "admin",
  "switch_password": "..."
}
```

`LINKFINDER_USER`, `LINKFINDER_PASSWORD`, `LINKFINDER_BMC_USER`,
`LINKFINDER_BMC_PASSWORD` and `LINKFINDER_COMMUNITY` are also read. The command line
wins over the environment, which wins over the file.

## Reading the output

```
TARGET         LOCAL PORT  SWITCH  SWITCH PORT            SWITCH IP      VLAN  VIA                   CONF
10.70.129.27  ens3f3np3   sw-lab-1  10GigabitEthernet 1/1  10.70.244.30  -     LLDP/toolbox-tcpdump  high
10.70.71.218  oam         sw-access-3  1/1/30                 -              -     LLDP/networkctl       high
192.168.1.13   -           sw-lab-1  10GigabitEthernet 1/4  10.70.244.30  100   FDB/snmp-fdb:...      low
```

`SWITCH IP` is the switch's own management address, taken from the LLDP Management
Address TLV — so the next step (log in, or query it over SNMP) needs no lookup. Not every
switch advertises it; `networkctl` also does not expose it, so the column can be empty
even when the port is known.

`SWITCH PORT` prefers a readable name. Some switches send a bare index as the Port ID and
the real interface name only in the port description; the raw Port ID is still kept, as
`switch_port_id_raw` in the CSV and `port` in the JSON.

`CONF` is `low` when the answer came from a forwarding table on a port that is carrying
many MACs — that is an uplink, and the device is further downstream, so the port shown
is not the one the cable is in. `VIA` always names the method, so every row can be
traced back to how it was obtained.

## Things worth knowing

- Auto-detection asks `/redfish/v1` **before** trying SSH, because a BMC usually also
  listens on port 22 for its IPMI shell and would otherwise swallow the target. If SSH is
  tried anyway and refuses the credentials, a host that answers Redfish is retried as a BMC.
- A capture waits for the switch to send; LLDP defaults to one frame every 30 s, which is
  why the default window is 65 s. Nothing is sent by us.
- `tcpdump`/`pktcap-uw` and the raw sniffer need root (or sudo) on the target.
- `--no-enable-lldp` stops the tool from PATCHing a BMC whose LLDP receiver is off. By
  default it does enable it — and says so in the log — because otherwise Supermicro and
  iLO return nothing.
- On Windows, `pktmon filter remove` clears **all** pktmon filters on that machine.
- Supermicro needs `SFT-OOB-LIC`/`SFT-DCMS-SINGLE` and HPE needs iLO Advanced for the
  Redfish LLDP data; without the licence the BMC answers 403 and the tool says so.

## Tests

No lab needed — the decoders and every text/JSON parser are checked against synthetic
frames and real-world captures of command output, and the SNMP codec against a fake
agent on loopback:

```bash
python tests/test_decode.py      # LLDP/CDP frames, pcap and pcapng
python tests/test_snmp.py        # the SNMPv2c codec, against a fake agent
python tests/test_parsers.py     # every text and JSON source we read
python tests/test_store.py       # the remembered-switch file
python tests/test_switchcli.py   # one sample of real output per NOS
python tests/test_topology.py    # identity, merging, edge confidence
python tests/test_crawl.py       # loops, depth, limits, scope, failures
python tests/test_render.py      # the offline HTML page
```

## Building the .exe

```powershell
.\build.ps1
```

Produces `dist\linkfinder.exe` (console/CLI) and `dist\LinkFinder-GUI.exe` (window).

The two names have to differ in more than case: Windows treats `linkfinder.exe` and
`LinkFinder.exe` as the same file, so the second build silently overwrites the first.
A running `LinkFinder-GUI.exe` also has to be closed before PyInstaller can replace it.

## Layout

| File | Role |
|---|---|
| `linkfinder/decode.py` | LLDP + CDP frame decoding, pcap/pcapng readers |
| `linkfinder/snmp.py` | SNMPv2c (BER, get/getnext/getbulk walks), stdlib only |
| `linkfinder/sshx.py` | paramiko wrapper, sudo without a pty, `/sys` interface model |
| `linkfinder/probe_*.py` | one module per access path |
| `linkfinder/engine.py` | path selection, fallbacks, parallel batch |
| `linkfinder/cli.py`, `gui.py` | the two front ends over the same engine |

## Using it from Claude Code

A ready-made skill ships with the repository at `.claude/skills/linkfinder/`. Clone this
repo and Claude Code picks it up automatically, so `/linkfinder <host>` or
`/linkfinder --topology <seed>` work straight away. The skill is site-neutral - it knows
how to drive the tool and nothing about anybody's network.

To make it available everywhere rather than only inside a checkout, copy that folder into
`~/.claude/skills/`.

## Licence

Copyright (c) 2026 Stas Mitin, released under the MIT licence.

Use it, change it, ship it, build on it - the only condition is that the copyright
notice travels with the code. The design and the architecture are Stas Mitin's; if this
helped you, say where it came from.

One third-party component is included: `cytoscape.min.js` 3.30.2 by The Cytoscape
Consortium, also MIT, vendored with its licence text in `linkfinder/assets/`.

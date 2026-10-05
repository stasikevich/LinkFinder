# LinkFinder - user guide

*Idea, design and architecture by Stas Mitin. MIT licensed.*

Complete reference for the tool. The [README](README.md) is the short version; this
document covers every field, every flag, the file formats and what to do when an answer
does not come back.

- [1. What the tool answers](#1-what-the-tool-answers)
- [2. Running it](#2-running-it)
- [3. The window, field by field](#3-the-window-field-by-field)
- [4. Command line reference](#4-command-line-reference)
- [5. How a device is interrogated](#5-how-a-device-is-interrogated)
- [6. Topology discovery](#6-topology-discovery)
- [7. File formats](#7-file-formats)
- [8. Credentials and what touches the disk](#8-credentials-and-what-touches-the-disk)
- [9. Reading the output](#9-reading-the-output)
- [10. Troubleshooting](#10-troubleshooting)
- [11. Extending it](#11-extending-it)
- [12. Tests and building](#12-tests-and-building)

---

## 1. What the tool answers

Two questions:

1. **"Which switch and which switch port is this cable in?"** - point it at a host, a BMC
   or a switch and it reports the far end of every link.
2. **"What does the site look like?"** - give it one seed device and it walks outwards
   along LLDP/CDP, builds a graph and draws it.

It gets the answer from whatever the target can actually offer: an LLDP daemon, a packet
capture, a BMC over Redfish, a switch over SNMP or its own CLI. Nothing is installed on
the target.

---

## 2. Running it

### Binaries

| File | Use |
|---|---|
| `dist\linkfinder.exe` | command line, batch, scripting |
| `dist\LinkFinder-GUI.exe` | the window |

Both are self-contained; no Python needed on the machine that runs them.

### From source

```bash
pip install -r requirements.txt    # paramiko, requests, urllib3
python -m linkfinder --help
python -m linkfinder --gui
```

SNMP needs no package - the SNMPv2c client is written against the standard library.

---

## 3. The window, field by field

### Targets

| Field | Meaning |
|---|---|
| **Targets** | One or more addresses, separated by commas or spaces. For topology discovery only the **first** address is used, as the seed. |

### Credentials

| Field | Meaning |
|---|---|
| **SSH user** | Login used for hosts and for switches. |
| **SSH password** | Also used for `sudo`. When a password is given, keys from the agent and `~/.ssh` are **not** tried - network devices allow only a couple of auth attempts and drop the session. |
| **SSH key file** | Private key instead of a password. |
| **BMC user / BMC password** | Redfish only. Left empty, the SSH credentials are reused. |
| **Switches (SNMP)** | Switches that may be asked from the switch side. `10.0.0.1` or `10.0.0.1@community` to override the community for one of them. |
| **SNMP community** | Default community for every switch. |
| **Interfaces (optional)** | Restrict the probe to these interfaces. A bond expands to its physical legs. |
| **MAC (switch-side)** | Look this MAC up in the forwarding tables instead of working it out from the target. |

### Mode row

| Control | Meaning |
|---|---|
| **Mode** | `auto` picks the access path itself. The rest force one: `ssh`, `bmc`, `esxi`, `windows`, `windows-local`, `switch` (ask the switches about this device), `switch-cli` (log into this switch and read its neighbour table). |
| **Capture seconds** | How long to listen when a capture is unavoidable. 65 by default, because LLDP is sent every 30 seconds. |
| **management interface only** | Probe only the interface carrying the address you typed. This is the behaviour of the old LLDP_Checker. |
| **enable LLDP on the BMC if it is off** | Allows a write to the BMC that switches its LLDP receiver on. Untick to leave the BMC untouched. |
| **remember switches it finds (writes to disk)** | Records discovered switches in `~/.linkfinder-switches.json`. Off by default: with it off nothing is written to disk. |

### Topology row

| Control | Meaning |
|---|---|
| **max depth** | Seed is depth 0. At depth N the devices are still interrogated; their neighbours are recorded but not queued. |
| **max devices** | Hard ceiling on how many devices get interrogated. |
| **scope (CIDR)** | Restricts only *connecting*. Neighbours outside it are still drawn, marked `out-of-scope`. Several networks may be given, space separated. |
| **Auto Discovery (do not ask)** | Off: you are asked before each deeper level. On: it runs until a limit stops it. |

### Buttons

| Button | What it does |
|---|---|
| **FIND SWITCH PORTS** | The per-target port lookup. Fills the table. |
| **Generate Topology** | Asks where to save the HTML, writes the JSON beside it, then crawls. |
| **View Topology** | Opens the last generated page, or asks for one. |
| **Stop** | Ends a running crawl. Devices already in flight finish; nothing new is queued, and the reason is recorded in the file. |
| **Merge topologies** | Pick two or more topology JSON files and combine them into one site map. |
| **Export CSV / Export JSON** | Save the port-lookup results. |
| **Known switches / Forget switches** | Show or wipe the remembered switch list. |

The counter next to the buttons shows `scanned N, queued M` while a crawl runs. The lower
pane is the live log; the upper table is the result.

---

## 4. Command line reference

### Targets and access

| Flag | Meaning |
|---|---|
| `targets` | Positional. Addresses, comma or space separated. |
| `--targets-file FILE` | One target per line; `#` comments allowed. |
| `-m, --mode` | `auto`, `ssh`, `linux`, `esxi`, `windows`, `windows-local`, `bmc`, `switch`, `switch-cli`. |
| `-u, --user`, `-p, --password` | SSH credentials. The password is also used for `sudo`. |
| `--sudo-password` | When sudo differs from the login password. |
| `-i, --key FILE` | SSH private key. |
| `--ssh-port N` | Default 22. |
| `--cred USER:PASSWORD` | An extra credential set, repeatable. Only tried when the previous one was actively **rejected**, so attempts are not wasted on a device that is simply not answering. |
| `--bmc-user`, `--bmc-password`, `--bmc-port` | Redfish. User and password default to the SSH ones. |
| `--no-enable-lldp` | Never PATCH a BMC to switch its LLDP receiver on. |

### What to probe

| Flag | Meaning |
|---|---|
| `--iface NAME` | Repeatable. Bonds expand to their physical legs. |
| `--mgmt-only` | Only the interface carrying the target address. |
| `-t, --seconds N` | Capture window, default 65. |

### Switch side

| Flag | Meaning |
|---|---|
| `--switch IP` or `--switch IP@community` | Repeatable. Switches that may be asked. |
| `--community NAME` | Default SNMP v2c community. |
| `--switch-user`, `--switch-password` | Login for the switch CLI fallback. |
| `--mac MAC` | Look this MAC up instead of resolving one. |

### Topology

| Flag | Meaning |
|---|---|
| `--topology SEED` | Start a recursive discovery from this device. |
| `--max-depth N` | Default 2. Seed is depth 0. |
| `--max-devices N` | Default 25. |
| `--scope CIDR` | Repeatable. Limits who we connect to, not who we draw. |
| `--auto-discover` | Do not ask between levels. Confirm mode is the default. |
| `--topology-workers N` | Devices interrogated in parallel per level, default 4. |
| `--topology-resume JSON` | Carry an earlier scan forward and only fill in the gaps. |
| `--topology-json FILE` | Where to write the graph. |
| `--topology-html FILE` | Where to write the page. |
| `--merge-topology A.json B.json ...` | Combine several scans into one site map. |
| `--render-topology JSON HTML` | Render only; touches no device. |

If neither `--topology-json` nor `--topology-html` is given, the result is saved to the
temp directory rather than lost.

### Output and housekeeping

| Flag | Meaning |
|---|---|
| `--json`, `--csv FILE` | Machine-readable results (`-` for stdout). |
| `-w, --wide` | Every column, including the note. |
| `-q, --quiet` | Results only, no progress log. |
| `-j, --workers N` | Parallel targets for the plain lookup, default 8. |
| `--learn` | Remember discovered switches. Off by default. |
| `--remember-credentials` | With `--learn`, also store the SNMP community that worked. |
| `--list-switches`, `--forget-switches [IP]` | Inspect or wipe the remembered list. |
| `--gui` | Open the window. |

Exit code is `0` when at least one link resolved, `1` when none did.

---

## 5. How a device is interrogated

`auto` decides in this order: a Redfish service on 443 means a BMC (checked **before**
SSH, because BMCs usually also listen on 22 for their IPMI shell), otherwise SSH,
otherwise HTTPS, otherwise the switch side.

### Linux, in order, stopping at the first answer

1. `lldpctl` - instant when `lldpd` is running.
2. `networkctl lldp` - systemd-networkd.
3. `lldptool` - open-lldp.
4. `tcpdump` into a pcap, transferred back base64.
5. `tshark`, same way.
6. A raw-socket sniffer pushed over SSH and run with the target's own `python3`. This is
   what makes nodes with no capture tool answerable.

SUSE nodes that only have `tcpdump` inside `toolbox` are handled there.

Bonds and VLANs are resolved through `/sys/class/net/*/lower_*` down to the physical
legs, so each cable gets its own row.

### Other targets

| Target | Method |
|---|---|
| ESXi | `vim-cmd hostsvc/net/query_networkhint` (CDP and LLDP), then `pktcap-uw` |
| Windows | `pktmon` to pcapng, locally or over OpenSSH |
| BMC | Redfish `Port.Ethernet.LLDPReceive`, then a per-vendor OEM table, then a generic hunt for any chassis-id/port-id object |
| Switch | SNMP LLDP-MIB and CDP-MIB, or the switch CLI |
| Anything else | the switch side: ask the switches for the forwarding table entry |

### Switch dialects

Detected from the version banner, then the matching commands are run.

| NOS | Detected on | Neighbours |
|---|---|---|
| Cisco IOS / IOS-XE | `cisco`, `ios` | `show lldp neighbors detail`, `show cdp neighbors detail` |
| Cisco NX-OS | `nx-os`, `nexus` | same |
| Arista EOS | `arista` | `show lldp neighbors detail` |
| Juniper JunOS | `junos` | `show lldp neighbors` |
| MikroTik RouterOS | `routeros` | `/ip/neighbor/print detail` |
| Aruba OS-CX | `arubaos-cx`, `aruba` | `show lldp neighbor-info detail` |
| HPE/Aruba ProCurve | `procurve` | `show lldp info remote-device detail` |
| Dell OS10 | `os10`, `dell` | `show lldp neighbors detail` |
| Extreme EXOS | `extremexos` | `show lldp neighbors detail` |
| Huawei VRP / H3C Comware | `huawei`, `vrp`, `h3c` | `display lldp neighbor brief` |

An unknown NOS still gets every command tried. Three parsers cover the output shapes:
`Key: value` blocks, whitespace columns under a header, and RouterOS `key=value`.

---

## 6. Topology discovery

```
seed -> interrogate -> read neighbours -> add nodes and links
     -> queue the unvisited ones -> repeat -> JSON -> HTML
```

### Identity

A device is identified by its **chassis id** first, its management address second, its
name last. This matters: a router-on-a-stick answers on one address per VLAN, and keying
on the address alone would draw it as a dozen switches. As soon as a chassis id arrives
for a node first seen by address, the node is re-keyed and the addresses collect in
`mgmt_ips`. Two different chassis ids on the same address stay two devices.

Chassis ids are normalised before comparison: MACs written with colons, dashes, dots or
nothing at all, in any case, all fold to one form. A chassis id that is **not** a MAC - a
name, a serial - is kept as text.

Some devices answer with more than one chassis MAC. When they also publish a serial or a
software id, that is used as an extra key and the two halves merge.

### Merging never destroys

| Field | Rule |
|---|---|
| `mgmt_ips`, `sources` | union |
| `first_seen` | earliest |
| `last_seen` | latest |
| `name`, `vendor` | the more informative non-empty value |
| `state` | only ever upgrades; a later failure cannot overwrite an earlier success |
| `error` | cleared as soon as the device is successfully scanned |

### Limits

All three are always in force.

- `max_depth` - seed is 0; at the limit, devices are probed but their neighbours are not queued.
- `max_devices` - a ceiling on interrogations; the device that is refused is recorded as `limit-reached`.
- `scope` - restricts connecting only. Out-of-scope neighbours are still nodes.

### Confirm mode

The default. Between levels:

```
Found 6 new network device(s) at depth 1:
   10.0.0.2         sw-access-1
Add and scan discovered neighbours? [y/N]
```

`--auto-discover` skips the question. It is never the default. In the window the same
question appears as a dialog.

### Nothing is a dead end

- A neighbour with no management address is still a node, marked `unscanned`.
- A device that refuses the login is `unreachable`, with the reason stored on the node.
- Neither stops the rest of the walk.
- Only management addresses that a discovery protocol actually reported are followed.
  **This is not a subnet scanner** and will never probe an address nobody mentioned.

### Link confidence

| Value | Meaning | Drawn as |
|---|---|---|
| `confirmed` | both ends reported the link | solid |
| `reported` | only one end did | dashed |
| `inferred` | derived from a forwarding table | dotted |

A cable is one edge no matter which end reported it, and no matter how many times.

### Resuming and merging

```bash
# carry an earlier scan forward, only fill in the gaps
linkfinder --topology 10.0.0.1 -u admin -p secret --topology-resume site.topology.json

# combine scans taken with different credentials into one site map
linkfinder --merge-topology core.json edge.json hosts.json \
           --topology-json site.topology.json --topology-html site.topology.html
```

Merging is the normal way to map a site where devices need different logins: one crawl
per credential set, then one merge. The merge uses the same identity rules, so a device
that was `unreachable` in one scan and `scanned` in another ends up scanned, with both
addresses and one confirmed link where the two scans met.

---

## 7. File formats

### Topology JSON

```json
{
  "schema_version": 1,
  "generated_at": "2026-10-05T17:11:02",
  "seed": "10.0.0.1",
  "seeds": ["10.0.0.1", "10.0.0.2"],
  "stopped_because": "depth limit reached (2)",
  "nodes": [
    {
      "id": "chassis:00:05:80:08:3e:bf",
      "kind": "switch",
      "name": "sw-lab-1",
      "chassis_id": "00:05:80:08:3e:bf",
      "chassis_id_raw": "00-05-80-08-3E-BF",
      "mgmt_ips": ["10.0.0.30"],
      "vendor": "ExampleNOS 1.2.3",
      "state": "scanned",
      "depth": 1,
      "sources": ["snmp"],
      "serial": "",
      "first_seen": "2026-10-05T17:10:11",
      "last_seen": "2026-10-05T17:10:48",
      "error": "",
      "extra": {}
    }
  ],
  "edges": [
    {
      "id": "chassis:...|1/1/28::chassis:...|21",
      "a": "chassis:...", "a_iface": "1/1/28",
      "b": "chassis:...", "b_iface": "21",
      "protos": ["LLDP"],
      "sources": ["switch-cli"],
      "reported_by": ["chassis:..."],
      "confidence": "reported",
      "vlan": "100", "speed": "10000Mb/s",
      "first_seen": "...", "last_seen": "..."
    }
  ]
}
```

`kind` is one of `switch`, `router`, `host`, `bmc`, `ap`, `phone`, `unknown`.
`state` is one of `scanned`, `unreachable`, `unscanned`, `out-of-scope`, `limit-reached`.

`schema_version` is checked on load; a newer file is refused rather than half-read.

### Topology HTML

One self-contained file. Both the graph data and `cytoscape.min.js` are embedded, because
a page opened from disk may not fetch a sibling file and must not need the network.
Features: zoom, pan, draggable nodes, four layouts, a filter box, colour and shape per
device kind, line style per link confidence, interface names at each end of every link,
and a panel showing every field of whatever you click. If the scan was cut short, the
header says so.

### CSV

One row per resolved link: target, kind, local interface, MAC, speed, bond parent,
switch, switch IP, switch port, the raw port id, port description, VLAN, chassis id,
switch model, protocol, source, confidence, note.

---

## 8. Credentials and what touches the disk

The code contains no addresses and no credentials. Everything site-specific is supplied at
run time. Three files can hold real values, all outside the repository and all in
`.gitignore`:

| File | Holds | Written when |
|---|---|---|
| `~/.linkfinder.json` | your usernames and passwords | only by you, by hand |
| `~/.linkfinder-switches.json` | switch addresses and names | only with `--learn` |
| the same file | SNMP communities | only with `--learn --remember-credentials` |
| any topology JSON/HTML | site addresses and device names | only where you point it |

`~/.linkfinder.json`:

```json
{
  "user": "root",
  "password": "...",
  "bmc_user": "ADMIN",
  "bmc_password": "...",
  "community": "public",
  "switches": ["10.0.0.1", "10.0.0.2@othercommunity"],
  "switch_user": "admin",
  "switch_password": "...",
  "creds": [
    {"user": "admin", "password": "..."},
    {"user": "bbuadmin", "password": "..."}
  ]
}
```

`LINKFINDER_USER`, `LINKFINDER_PASSWORD`, `LINKFINDER_BMC_USER`,
`LINKFINDER_BMC_PASSWORD` and `LINKFINDER_COMMUNITY` are read too. The command line wins
over the environment, which wins over the file.

Nothing is ever sent anywhere. The tool only talks to the targets you name.

---

## 9. Reading the output

### Port lookup

```
TARGET         LOCAL PORT      SWITCH    SWITCH PORT            SWITCH IP     VLAN  VIA                   CONF
10.0.0.27      oam [mgmt-leg]  sw-edge   sfp-sfpplus3           10.0.9.1      3729  LLDP/toolbox-tcpdump  high
10.0.0.27      ens3f3np3       sw-lab-1  10GigabitEthernet 1/1  10.0.0.30     -     LLDP/toolbox-tcpdump  high
10.0.0.27      ens3f0np0       -         -                      -             -     -                     none
```

- The interface carrying the address you asked about is marked `[mgmt]` or `[mgmt-leg]`
  and sorted first. **It always appears**, even when nothing was heard on it, with a note
  explaining why.
- `SWITCH PORT` prefers a readable name. Some switches send a bare index as the Port ID
  and the real interface name only in the description; the raw id is kept in the CSV and
  JSON.
- `SWITCH IP` comes from the LLDP Management Address TLV. Not every switch sends one; some
  send only a MAC, and then the note says so rather than putting a MAC in an IP column.
- `VIA` names the protocol and the method, so every row can be traced back to how it was
  obtained.
- `CONF` is `low` when the answer came from a forwarding table on a port carrying many
  MACs - that is an uplink and the device is further downstream.

### Topology

```
DEPTH  DEVICE       KIND    STATE         MANAGEMENT IP  CHASSIS
0      delta        host    scanned       10.0.0.27      -
1      sw-lab-1     switch  scanned       10.0.0.30      00:05:80:08:3e:bf
1      sw-edge      switch  out-of-scope  10.9.94.1      04:f4:1c:eb:06:4d
2      sw-access-1  switch  unscanned     -              ec:50:aa:1f:ec:80
```

Followed by a one-line summary and, when the walk was cut short, the reason.

---

## 10. Troubleshooting

**"nothing to talk to on X"** - no SSH on 22, no HTTPS on 443 and no `--switch` given.
If the device is a dumb appliance, give the switch it hangs off with `--switch`.

**"SSH did not get us in: the device dropped the session..."** - typically a switch that
allows two or three auth attempts. Supply the right user with `-u`; with a password set,
the tool no longer wastes attempts on agent keys.

**"captured N frames but none were LLDP or CDP"** - the port is carrying something else.
Most often the switch has discovery disabled on that port; ask the switch side instead.

**"no LLDP on this port, and its MAC is not in the forwarding table of ..."** - the
interface faces a different switch from the ones asked. Add that switch with `--switch`.

**A BMC answers in the browser but the tool says authentication failed** - it was taken
down the SSH path. Current builds probe `/redfish/v1` first; if you force `-m ssh` on a
BMC this is expected.

**"this BMC exposes no LLDP over Redfish"** - some generations simply do not publish it
even with the right licence. The MAC addresses it does publish are collected, so give
`--switch` and the port is found from the switch side.

**The topology stops at depth 1 with everything `unscanned`** - the neighbours published
no management address. Check the summary line: it says why the walk stopped. Seed from a
device whose neighbours do advertise addresses, or scan them separately and merge.

**The graph looks small when the page opens** - press **fit**.

**Windows: a console window flashes** - should not happen any more; every child process
is started without one. If you see it, say which action caused it.

---

## 11. Extending it

The layers are deliberately separate:

```
probes (per access path)  ->  neighbors.py (one uniform answer)
                          ->  crawl.py (recursion and limits)
                          ->  topology.py (graph, identity, merging)
                          ->  JSON
                          ->  render_topology.py (HTML)
```

**A new switch dialect**: add one `Dialect(...)` entry in `linkfinder/probe_switchcli.py`
with its detection words, its neighbour commands, its MAC-table command and which of the
three parsers to use. Add one sample of real output to `tests/test_switchcli.py`.

**A new access path**: write a probe that fills a `TargetResult` the same way the others
do - `local_iface` is a port on the device we interrogated, and the `switch`/`port`/
`chassis_id`/`mgmt_ip` fields describe the far end. Everything above it then works.

**A different picture**: the renderer only reads the JSON. Write another one; nothing in
the discovery layers needs to know.

**A new field in the graph**: add it to `Node`/`Edge` in `topology.py`, to `to_dict`, and
give it a merge rule. The rule matters more than the field.

---

## 12. Tests and building

Every test runs offline against synthetic frames, recorded command output and a fake SNMP
agent on loopback. No lab is needed.

```bash
python tests/test_decode.py      # LLDP/CDP frames, pcap and pcapng
python tests/test_snmp.py        # the SNMPv2c codec, against a fake agent
python tests/test_parsers.py     # every text and JSON source we read
python tests/test_store.py       # the remembered-switch file
python tests/test_switchcli.py   # one sample of real output per NOS
python tests/test_topology.py    # identity, merging, edge confidence
python tests/test_crawl.py       # loops, depth, limits, scope, failures, resume
python tests/test_render.py      # the offline HTML page
```

Build both executables, tests first:

```powershell
.\build.ps1
```

The two names must differ in more than case - Windows treats `linkfinder.exe` and
`LinkFinder.exe` as the same file. A running `LinkFinder-GUI.exe` has to be closed before
PyInstaller can replace it. `linkfinder/assets/` is bundled with `--add-data`, which is
what lets the frozen exe render a topology page.

---

## Credits

The idea, the design and the architecture are **Stas Mitin's**, and so is the code.

Released under the MIT licence (see `LICENSE`): use it, change it and redistribute it
freely, as long as the copyright notice stays with it.

It ships one third-party component: `cytoscape.min.js` 3.30.2 by The Cytoscape Consortium,
under the MIT licence, vendored in `linkfinder/assets/` with its licence text so the
generated topology page works with no network access.

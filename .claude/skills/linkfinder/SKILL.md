---
name: linkfinder
description: "Find which switch and which switch port a device is plugged into, and map a whole network topology, using the portable LinkFinder tool. Works against Linux hosts, ESXi, Windows, BMCs (Redfish) and switches (SNMP or their own CLI: Cisco IOS/NX-OS, Arista, Juniper, MikroTik, Aruba, ProCurve, Dell, Extreme, Huawei/H3C), with no agent installed on the target. Also builds a recursive LLDP/CDP topology from one seed and renders it as a self-contained Cytoscape page. Site-neutral: no vendor, lab or company assumptions. Use whenever someone asks which port a cable is in, what a server is connected to, what is plugged into a switch, or wants a network map. Triggers on 'which switch port', 'what is this plugged into', 'find the switch port', 'trace this cable', 'lldp neighbour', 'cdp neighbour', 'switch port for this MAC', 'network topology', 'map the network', 'draw the topology', 'discover neighbours', 'какой порт свитча', 'куда воткнут', 'построй топологию', 'карта сети'. Usage: /linkfinder [target] or /linkfinder --topology <seed>"
user-invocable: true
argument-hint: "[host|bmc|switch address] | --topology <seed ip> [--max-depth N] [--scope CIDR]"
allowed-tools: Bash, PowerShell, Read, Write, Edit, Grep, Glob, AskUserQuestion
---

# LinkFinder

Answers two questions about a network, without installing anything on the targets:

1. **Which switch and which switch port is this link in?**
2. **What does the network look like?** - recursive LLDP/CDP discovery from one seed,
   rendered as an interactive page.

Written by Stas Mitin, MIT licensed. Source of truth:
**https://github.com/stasikevich/LinkFinder**

This skill carries no site, vendor or company knowledge. Everything specific - addresses,
credentials, communities - comes from the user at run time. Keep it that way: never put a
real address, hostname or password into this skill, into the tool, or into a commit.

## Before anything else: find or install the tool

Look for it in this order and use the first that works:

```bash
linkfinder --version                       # on PATH
python -m linkfinder --version             # a checkout on sys.path
ls ./linkfinder/ ../linkfinder/ ~/tools/linkfinder/ ~/linkfinder/
```

Not present? Clone and install the two dependencies:

```bash
git clone https://github.com/stasikevich/LinkFinder.git
cd LinkFinder && pip install -r requirements.txt     # paramiko, requests, urllib3
python -m linkfinder --help
```

SNMP needs nothing extra - the SNMPv2c client is hand-rolled on the standard library.
The GUI needs tkinter. On Windows there are prebuilt binaries if the user has them; the
package itself runs anywhere Python 3.8+ does.

If the user wants the window rather than the command line: `python -m linkfinder --gui`.

## Work out which question is being asked

| The user says | Do this |
|---|---|
| "which port is this server in", "trace this cable" | port lookup against that address |
| "what is plugged into this switch" | point it at the switch - it reads that switch's neighbour table |
| "find this MAC" | `-m switch --switch <sw> --mac <mac>` |
| "map the network", "draw the topology" | `--topology <seed>` |
| "combine what we scanned" | `--merge-topology a.json b.json ...` |

If the target type is not obvious, do not guess - `auto` works that out itself.

## Ask for what you actually need, once

The tool cannot invent credentials. Before the first run, establish:

- the **target address** (and whether it is a host, a BMC or a switch - or let `auto` decide);
- an **SSH user and password or key** for hosts and switches;
- **BMC credentials** if a BMC is in scope;
- an **SNMP community** if switches are to be asked;
- for a topology: the **limits** (`--max-depth`, `--max-devices`, `--scope`).

Use AskUserQuestion when more than one of these is missing and the answers change the
command. Do not ask for credentials the task does not need.

If the user has no credentials for some device, say so plainly and move on: that device
will appear in the results as `unreachable` or `unscanned`, which is a correct answer,
not a failure. Do not keep asking.

## Port lookup

```bash
python -m linkfinder 198.51.100.10 -u admin -p '<password>'
python -m linkfinder --targets-file hosts.txt -u root -p '<password>' --csv rack.csv -j 8
python -m linkfinder 198.51.100.40 --bmc-user ADMIN --bmc-password '<password>' -m bmc
python -m linkfinder 198.51.100.1 -u admin -p '<password>'      # a switch: its whole table
```

A capture takes up to `-t/--seconds` (65 by default, because LLDP ticks every 30). Tell
the user that before starting, so a quiet minute is not mistaken for a hang.

`--mgmt-only` restricts the probe to the interface carrying the address given; by default
every physical interface with carrier is probed, which is usually what people want when
documenting cabling.

## Topology discovery

```bash
python -m linkfinder --topology 198.51.100.1 -u admin -p '<password>' \
  --max-depth 2 --max-devices 25 --scope 198.51.100.0/24 \
  --topology-json site.topology.json --topology-html site.topology.html
```

Rules the tool enforces, and that you must not try to work around:

- **Confirm mode is the default.** It asks before descending to the next level. Only pass
  `--auto-discover` when the user has explicitly asked for an unattended run.
- **It is not a subnet scanner.** Only management addresses that a discovery protocol
  actually reported are ever followed. Never feed it a range, never loop it over a subnet.
- **The three limits are always on.** If a scan looks truncated, read `stopped_because` in
  the JSON or the page header rather than guessing.
- **Nothing is written to disk unless a path is given** (it falls back to the temp dir and
  says so). `--learn` is the only thing that records switches between runs, and it is off
  by default.

One credential set per crawl. A site where devices need different logins is mapped with
one crawl per credential set and then:

```bash
python -m linkfinder --merge-topology core.json edge.json hosts.json \
  --topology-json site.topology.json --topology-html site.topology.html
```

Merging uses the same identity rules, so a device that was `unreachable` in one scan and
`scanned` in another comes out scanned, with both addresses and one confirmed link where
the scans met. `--topology-resume <json>` continues an earlier scan instead of redoing it.

`--render-topology <json> <html>` redraws without touching a single device - use it
whenever the user only wants the picture again.

## Reading the result

**Node state**: `scanned` (we got in), `unreachable` (we could not, reason on the node),
`unscanned` (known from a neighbour, never interrogated - usually no management address),
`out-of-scope`, `limit-reached`.

**Link confidence**: `confirmed` (both ends reported it), `reported` (one end),
`inferred` (from a forwarding table - treat with care, a port carrying many MACs is an
uplink and the device is further downstream).

**Identity** is the chassis id first, the address second. One device answering on many
VLAN addresses is one node. Do not "fix" apparent duplicates by hand; if two nodes really
are one box, they will share a serial and the tool merges them.

When reporting to the user, give the answer in one line per cable - local port, switch,
switch port - and only then the caveats. Say which method produced each row (the `VIA`
column); an answer from a forwarding table is weaker than one from LLDP.

## Things that waste an hour if you do not know them

| Symptom | Cause |
|---|---|
| "nothing to talk to on X" | no SSH, no HTTPS, no `--switch`. Give the switch it hangs off. |
| BMC works in a browser, tool says auth failed | it was taken down the SSH path; BMCs also listen on 22. Current builds probe `/redfish/v1` first - do not force `-m ssh` on a BMC. |
| "the device dropped the session before login finished" | switches allow two or three auth attempts. Supply the right user; with a password set the tool no longer spends attempts on agent keys. |
| "captured N frames but none were LLDP or CDP" | the port carries other traffic; discovery is probably disabled on the switch port. Ask the switch side. |
| Topology stops at depth 1, everything `unscanned` | the neighbours published no management address. Seed somewhere else, or scan them separately and merge. |
| Switch IP column empty | that switch advertises only a MAC as its management address. There is genuinely no IP to show. |
| The graph opens small | press **fit** on the page. |

## Safety and privacy - matters most when this is shared

- A topology JSON or HTML is **a map of someone's network**. Treat it as sensitive:
  write it where the user asked, never into a repository, never into a ticket or chat
  without asking first.
- Never commit credentials, addresses or hostnames. The tool's own repository is scrubbed;
  keep it that way.
- `--learn` and `--remember-credentials` write to the user's home directory. Both are
  opt-in. Do not switch them on to make your own life easier.
- Enabling LLDP on a BMC is a write to someone's hardware. It is on by default because
  otherwise several vendors return nothing, but say that it happened; `--no-enable-lldp`
  turns it off.
- The tool only ever talks to the addresses it was given or that a device reported. Keep
  that property.

## Extending it

A NOS that is not recognised still gets every command tried, and the three output shapes
(`Key: value` blocks, columns under a header, RouterOS `key=value`) cover most dialects.
To add one properly: one `Dialect(...)` entry in `linkfinder/probe_switchcli.py` plus one
sample of real output in `tests/test_switchcli.py`. Never add a sample taken from a real
site without scrubbing the addresses and hostnames.

Full reference, including every GUI field and flag: `DOCUMENTATION.md` in the repository.

## Verify before claiming

Every suite runs offline, no network and no lab:

```bash
python tests/test_decode.py tests/test_snmp.py tests/test_parsers.py
python tests/test_store.py tests/test_switchcli.py
python tests/test_topology.py tests/test_crawl.py tests/test_render.py
```

If you changed the tool, run them and say the result. If a run against real equipment
produced the answer, say which command and which device produced it - a switch port
reported without provenance is worth very little to whoever has to go and unplug it.

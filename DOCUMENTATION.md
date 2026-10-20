# LinkFinder - user guide

*Idea, design and architecture by Stas Mitin. MIT licensed.*

Complete reference for the tool. The [README](README.md) is the short version; this
document covers every field, every flag, the file formats and what to do when an answer
does not come back.

- [1. What the tool answers](#1-what-the-tool-answers)
- [2. Running it](#2-running-it)
- [3. The window, field by field](#3-the-window-field-by-field)
- [4. Command line reference](#4-command-line-reference)
- [4a. Architecture](#4a-architecture)
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

### Running it from Linux

It runs from Linux as well as from Windows; Windows is simply the side that
gets used daily here.

Proven on Ubuntu 22.04 with the distribution Python 3.10 and nothing installed:
every test suite, the command line, an SNMP scan against a live switch, and
port health. That is the whole SNMP half of the tool, and it needs no package
at all.

Two things do want one:

| You want | Install |
|---|---|
| the SSH and BMC paths | `pip install paramiko requests` |
| the window | `apt install python3-tk` - tkinter is not a pip package |

Without paramiko the SSH path does not crash: it reports
`paramiko is not installed (pip install paramiko)` and the SNMP paths carry on.

The host's own neighbour cache is read with `ip` rather than `arp`, because
net-tools is absent from plenty of minimal installs and containers while `ip`
is always there. `arp` stays as the fallback for older boxes, and Windows keeps
using it outright.

---

## 3. The window, field by field

One place to work and one log, over a top bar and a status bar. Everything
site-specific is typed in; the window never stores anything by itself.

```
 Target [10.0.0.5, 10.0.0.6  or  server1]  Login [root]  Password [****]  [ Advanced ]
 ------------------------------------------------------------------------------------------
  [Devices]  Log
 ------------------------------------------------------------------------------------------
  [ Scan ] [ Map Network ] [ Stop ]      NetBox ready  [ SSH ] [ Save... ] [ Open Map ]
  Scan asks the address(es) in Target and nothing else.  Map Network walks outward
  from them and draws it - up to 2 hops or 25 devices, changeable under Advanced.
 ------------------------------------------------------------------------------------------
  DEPTH  DEVICE   KIND   SETUP   STATE     MANAGEMENT IP     WHY
  0      node-a   linux  dio     scanned   198.51.100.10     -
  1      sw-core-1 switch -      scanned   198.51.100.2      -
  Device [ 198.51.100.10 (node-a) v ]
  [ ] Also read switch port counters    sample [3] s   expect [    ] Mbps
 ------------------------------------------------------------------------------------------
  PORT          STATUS     CONNECTED TO                            ISSUES
  eno1 [mgmt]   UP  1G     sw-access-1  1/1/13                     -
  enp23s0f2     UP  10G    sw-core-1  10GigabitEthernet 1/13       -
  enp23s0f0     UP  10G    node-b enp101s0f3  (direct cable, PTP)  -
  enp23s0f1     UP  10G    unknown                                 no LLDP
 ------------------------------------------------------------------------------------------
  enp23s0f0 on 198.51.100.10   (02:00:00:00:00:08, 10000Mb/s, MTU 9000)
  Far end: node-b enp101s0f3
  Evidence: most likely a direct cable - each end hears only the other
  ...
 ------------------------------------------------------------------------------------------
 * Reading SNMP...                                    device 4, queued 9        00:42
```

It used to be five tabs, and that was the wrong shape: a single port's neighbour was
on one tab, its health on another and its timing on a third, so answering one
question meant visiting three places and holding the port name in your head. Then
Device and Map were still two pages for one question - same targets, same logins,
differing only in how far to walk. They are one page now: the devices found, and
under them the ports of whichever is selected.

### Top bar

| Control | Meaning |
|---|---|
| **Target** | Address to scan, or the seed for a map. Several, comma or space separated. A host needing its own login is written `root@10.0.0.5` or `admin:secret@10.0.0.5`. The grey example is a hint, not a value - it is never scanned. Enter runs the scan. |
| **Login / Password** | SSH credentials, reused for a BMC unless the Advanced panel overrides them; the password is also used for `sudo`. With a password set, agent keys are not tried - network devices allow very few auth attempts. |
| **Advanced** | Folds out everything you set once: SSH key, BMC credentials, switches and SNMP community, interface filter, MAC, mode, capture seconds, and the three behaviour ticks. |
| **Theme** | `system`, `light` or `dark`. `system` follows the Windows setting. |

### Advanced panel

| Control | Meaning |
|---|---|
| **SSH key file** | Private key instead of a password. |
| **BMC user / password** | Redfish only. Empty means reuse the SSH credentials. |
| **Switches (SNMP)** | Switches that may be asked. `address` or `address@community`. |
| **SNMP community** | Default community. |
| **Interfaces** | Restrict the probe. A bond expands to its physical legs. |
| **MAC (switch-side)** | Look this MAC up instead of working one out. |
| **Mode** | `auto` decides for itself; the rest force a path. |
| **Capture seconds** | The longest it will listen when a capture is unavoidable. LLDP ticks every 30 seconds, CDP every 60. A host seen before is listened to for less - see below. |
| **management interface only** | Probe only the interface carrying the address typed. |
| **enable LLDP on a BMC if it is off** | Allows one write to the BMC. Untick to leave it untouched. |
| **remember switches it finds** | Writes to `~/.linkfinder-switches.json`. Off by default. |
| **Known switches / Forget** | Show or wipe that list. |
| **Per-host logins** | One row per host that does not share the login in the top bar. Password masked. A host listed here is asked as well - it does not have to be in Target too. |
| **Load list...** | Read a host list file, the format `--targets-file` takes; fills the table and Target. `File` -> `Save an example host list...` writes one with the format in its own comments. |
| **remember these logins** | Writes the table, passwords included, into the defaults file. Off by default. |
| **Forget saved / Clear** | Remove them from the file, or empty the table on screen. |
| **Extra logins to try** | Not tied to a host. Tried in order, and only after a device actually rejected the login before them. |

### Device

One table, one row per cable, four columns and no more. **Port**, **Status**,
**Connected to**, **Issues**.

**Connected to** answers the question the tool exists for, whatever the route: a
switch and its port when LLDP or the forwarding table resolved it, the far machine
and how sure we are when only PTP could name it, or `unknown` when nothing could.
A port with no answer still gets a row - leaving it out would be the one thing worse
than saying nothing.

**Issues** carries only what is not right: a failed comparison, a degraded switch
port, a live port with no LLDP. Everything that is fine shows `-`. This is deliberate
and it is what keeps the table at four columns instead of fifteen as checks are
added - MTU, VLAN, speed, LACP and the rest are not columns, they are reasons for
a row to appear in Issues.

Colour carries the same meaning everywhere in the window: green resolved and clean,
amber resolved with something to look at, red a fault, grey nothing found.

Select a row and the panel underneath explains that one cable - the local MAC, speed
and MTU; the switch and port with how it was learned; the far end with the evidence
behind it in words; the grandmaster and timing state; every comparison with both
sides' values; and, when port counters were read, the counters themselves with how
much each moved during the sample.

**Also read switch port counters** is the opt-in. Until it is ticked the interval and
expected-speed boxes are dead and not one extra SNMP request is made. Ticked, the
switch port behind each row is read as well, and what it says folds into the same
table rather than opening a page of its own.

With more than one target a **Device** picker appears; with one it stays hidden. The
line above it says how many devices were scanned and when, so a page left over from
an earlier run cannot be mistaken for the current one.

### The two actions

**Scan** asks the address(es) in Target and nothing else. **Map Network** walks
outward from them, draws the map, writes the table, the data and the page side by
side, and offers to open it. **Open Map** opens the one already drawn without
walking anything again, and **Stop** finishes the devices already being asked and
then stops.

They were briefly one button with a *how far* choice beside it - These / +
Connected / Whole site. It read as though the map were a setting on a scan, when
the map is the thing people come here to make, so they are two buttons again. How
far to walk lives under **Advanced**, with **Map Network stops after** N hops or N
devices, **stay inside (CIDR)** and a **keep going without asking** tick. The line
under the buttons names the limits in force, so nobody has to press Map Network to
find out how far it goes.

The table lists every device found with its depth, state, management address and -
when it was not scanned - why.

**Compare...** in the top right takes two saved topology files and reports what moved
between them, into the Log.

### Log

The full transcript, plus **Save CSV**, **Save JSON**, **Error details** and the
remembered-switch list. The main window never shows a traceback: the short reason goes
in the status bar and the technical text waits behind that button.

### Status bar

The dot and the phrase say what is happening right now - `Reading LLDP...`,
`Capturing LLDP/CDP...`, `Reading SNMP...`, `Sampling port counters...`,
`Scanning 198.51.100.4...`. During a topology walk the counter shows how many devices have
been done and how many are queued, and the clock on the right shows elapsed time. The
window stays responsive throughout: all network work happens on worker threads.

## 4. Command line reference

### Targets, and hosts that need their own login

Targets are given on the command line or in `--targets-file`, comma or space
separated, and a host that does not share the common password carries its own:

```
10.0.0.5                     uses -u / -p
root@10.0.0.6                its own user, the shared password
admin:secret@10.0.0.7        entirely its own
```

The split is from the right, so a password may contain an `@`; a hostname may
not. Nothing typed here is written anywhere - it lives for the one run.

Order matters when a host has its own login. Network gear drops the session
after two or three failed attempts, so the set most likely to work is tried
first: the host's own, then the shared pair, then anything from `--cred`. A
user-only override borrows the shared password rather than going without one.

`--cred USER:PASSWORD` is still there for the other shape of the problem -
several possible passwords and no knowledge of which host takes which. Those
are tried in turn, and only when the previous one was actively **rejected**; a
device that simply is not answering does not get attempts spent on it.

**A target that cannot be an address is dropped before anything is tried**, with
the reason. `198.51.100.1801` is four dotted numbers with one out of range -
nobody meant to type that, and reporting it as "no SSH, no HTTPS" would send a
person to check a firewall over an extra digit. Anything containing a letter is
treated as a hostname and left alone.

### Hosts that need their own login

Not every box shares one password. Three ways to say so, and they combine:

1. **In the Target field** - `root@10.0.0.5`, or `admin:secret@10.0.0.6`. The split is
   from the right, so a password may itself contain an `@`.
2. **The Per-host logins table** under Advanced - one row per host, password masked.
   `Load list...` reads a host list file, the same format `--targets-file` takes, and
   fills both the table and Target from it. A host in this table is **asked as well**,
   whether or not it is in Target: typing a host, a user and a password in is asking
   for that host. It used to decide only which password to use if a walk happened to
   arrive there, so four hosts entered by hand were never visited and the map came
   back without them, saying nothing.
3. **The shared Login / Password** in the top bar, used by every host not named above.
4. **Extra logins to try**, under the table - pairs with no host against them, used as a
   fallback when you do not know which box wants which. Each one costs a failed attempt
   on a device that refuses it, and network gear drops the session after two or three,
   so they are tried only after a real rejection and the list is meant to stay short.

Order of preference, highest first:

| Where the login came from | Beats |
|---|---|
| typed into Target as `user:pass@host` | everything |
| the Per-host logins table | the shared login |
| the shared Login / Password | - |
| the Extra logins list, or `--cred` | tried only after an actual rejection |

A user given without a password borrows the shared password, so a host that only needs
a different username costs one field.

**Remembering them.** The tick `remember these logins (writes passwords to disk)` is off
by default. With it on, the table is written into the defaults file as

```json
{
  "per_host": {
    "10.0.0.5": {"user": "root", "password": "secret"}
  }
}
```

in the clear, in your home directory, alongside a `creds` list holding the extra logins.
`Forget saved` removes both keys again. The command line reads the same two, so a login
saved once works in both front ends.

### Targets and access

| Flag | Meaning |
|---|---|
| `targets` | Positional. Addresses, comma or space separated. |
| `--targets-file FILE` | One target per line; `#` comments allowed. A line may be a host, `root:secret@host`, or a whole network. A `[name]` line puts everything under it into that setup. `File` -> `Save an example host list...` writes one to start from. |
| `-m, --mode` | `auto`, `ssh`, `linux`, `esxi`, `windows`, `windows-local`, `bmc`, `switch`, `switch-cli`, `pdu`. |
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
| `--no-ptp` | Skip the timing read. On by default; see section 6b. |

### Switch side

| Flag | Meaning |
|---|---|
| `--switch IP` or `--switch IP@community` | Repeatable. Switches that may be asked. |
| `--community NAME` | Default SNMP v2c community. |
| `--switch-user`, `--switch-password` | Login for the switch CLI fallback. |
| `--mac MAC` | Look this MAC up instead of resolving one. |

### Port status and health (opt-in)

| Flag | Meaning |
|---|---|
| `--port-health` | Also read the switch port table over SNMP and judge each port. **Off by default; without it not one extra SNMP read happens.** |
| `--port-health-interval SECONDS` | Gap between the two counter reads, default 3. |
| `--expect-speed MBPS` | Call a port degraded when it negotiates below this. Without an expectation, speed is never judged. |
| `--all-interfaces` | Include logical interfaces, not only physical ports. |

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
| `--forget-logins` | Remove the saved per-host logins and extra credentials from the defaults file. The window's **Forget logins** does the same. |
| `--gui` | Open the window. |

Exit code is `0` when at least one link resolved, `1` when none did.

---

## 4a. Architecture

This section is the map of the three that follow it. Sections 5 and 6 describe each
stage in detail; this one says what the stages are, why they are in that order, and
where each lives in the source.

### The shape

Five stages. Each hands the next **data**, never a call, which is why every one of
them can be tested on its own with no network at all.

```
targets  ->  access path  ->  device readings  ->  link graph  ->  map + disagreements
```

The graph in the middle is the contract. It is written out as JSON, so it can be
saved, merged with another scan, compared against yesterday, or re-rendered without
touching a single device again.

### Two guarantees

**It only reads.** No command the tool issues changes anything on a target. There is
no write path to NetBox and none is planned - the disagreement report is the product.

**It never reaches the internet.** Everything it needs is inside the binary: the IEEE
vendor register, the icons, the graph library, the help you are reading. A tool that
maps a lab must work in a lab with no way out.

### Stage 1 - what counts as a target

`linkfinder/targets.py` turns what you typed into a list: one address, a subnet such
as `192.0.2.0/24`, a range, or a file. A file may carry a login per line
(`root@192.0.2.5`) and a `[setup name]` heading.

**This is not a subnet scanner.** Give it one address and it visits that address, then
only the addresses devices themselves named. It never invents an address and knocks.

The walk is breadth-first (`linkfinder/crawl.py`): the seed is depth 0, its neighbours
depth 1, and so on. At `max_depth` - 2 by default - devices are still interrogated,
but their neighbours are no longer expanded.

### Stage 2 - choosing the access path

Decided per target, before any login, in `linkfinder/engine.py`:

| What answers | Conclusion |
|---|---|
| Redfish on 443 | management controller |
| port 22, and the SSH banner names a crypto library | rack PDU - it has no shell at all |
| port 22 | ordinary host, over SSH |
| no 22, but 443 | management controller |
| `--switch` was given | switch, over SNMP |

The PDU case is worth the paragraph it costs. A rack PDU accepts an SSH login and then
refuses to run a command, so the ordinary probe logged in, was handed nothing, and
reported a machine that said nothing at all. Its SSH server names the library it was
built on, which no general-purpose host does - one connection, no login, and the tool
knows to speak to it as a PDU instead.

### Stage 3 - reading the device

| Class | Access | What it is read with |
|---|---|---|
| Linux host | SSH | `ip`, `bridge fdb`, `ethtool`, `lldpctl` / `lldpcli`, `networkctl`, `lldptool`, `tcpdump`, `sensors`, `ipmitool`, `dmidecode` |
| Switch | SNMP | LLDP-REM, CDP, dot1d / dot1q forwarding tables, ARP, ifName, ifAlias, sysDescr |
| Switch | SSH CLI | `show lldp neighbors detail`, `show cdp neighbors detail`, `show version` |
| Management controller | Redfish over HTTPS | inventory and the controller's own interfaces |
| Rack PDU | SSH, interactive session | `about`, `netstat`, `olStatus all` |
| ESXi | SSH | `pktcap-uw` in place of `tcpdump` |
| Windows | locally | the PnP registry, `pktmon` |
| Handset | USB / ADB | `/sys/bus/usb/devices/*`, `adb devices` |

Nothing is installed on a target, and nothing is left behind.

#### Neighbours, cheapest source first

On a Linux host the sources are tried cheapest first, and **each one runs only for the
ports the previous one did not settle** - so the 125-second listen is reached only for
a port nothing faster could explain:

1. `lldpctl` - a daemon has already been listening, so the answer is immediate
2. `networkctl lldp` - the same, from systemd
3. `lldptool` - one port at a time
4. **the switch's MAC table**, where a switch is known and answers SNMP: it already
   holds which port each of the host's addresses is on, in under a tenth of a second.
   Those ports are then never listened to. See "Asking the switch first" below.
5. **the host's own neighbour cache**, read with no pinging and no waiting: a port with
   a reachable link-local peer is a direct cable the capture would only sit silent on,
   because LLDP was already tried above and found none. Reading the cache costs one
   command; those ports drop out of the listen too.
6. `tcpdump` - listen for the frames ourselves, for the ports still unexplained
7. `tshark` - where there is no `tcpdump`
8. a raw-socket sniffer in Python - where there is neither

On a host running an LLDP daemon there is nothing to listen for. Where a switch answers
or a neighbour is already cached, those ports are resolved in seconds. Only a port that
is genuinely silent - nothing has ever spoken over it - is listened to for the full
window, because confirming silence is the one thing that cannot be hurried.

#### Asking the switch first

The slow part of a scan is listening for LLDP on a host that runs no LLDP daemon. The
switch it plugs into already knows, in its MAC table, which port each of the host's
addresses is on, and answers over SNMP in a fraction of a second - **read-only, no
login, just the switch address and SNMP enabled** (the tool also learns the switch
address from the host's own LLDP, so often it is given for free). Where a switch is
known, its table is read before any listening and those ports are skipped. The MAC
table carries the port's VLAN too, so a VLAN mismatch surfaces at the same time. Ports
the switch cannot explain - a dark cable, a direct server-to-server link - still fall
through to the capture, so nothing is lost where no switch can help.

#### Why the listen is as long as it is

LLDP sends every 30 seconds and CDP every 60 (IEEE 802.1AB; CDP by convention), so the
capture window is **125 seconds**, with **34 seconds** of grace after the last frame.
The grace used to be 8 seconds - shorter than one LLDP interval - and a cable with two
talkers on it reliably lost one of them. The window is only ever reached for a port the
switch and the neighbour cache could not already settle.

#### What else comes off a host

Every port with an address, a speed, an MTU and a carrier; the burned-in hardware
addresses, not the ones a bond substitutes; the bridge forwarding table; the optics,
with wavelength, reach and the rate the link settled at; card temperatures; DHCP
leases; the BMC address read from inside the machine; attached USB devices; serials.

It also lists the **serial ports** and marks the one a radio is most likely on. A
radio's console is usually reached through a USB-to-serial cable, and nobody
remembers which `/dev/ttyUSB*` or `COMx` it became. The host already knows: the
kernel names the adapter, its bridge chip (FTDI, CP210x, CH340, Prolific, …) and
the cable's own serial number. LinkFinder reads that out - over the same SSH
session on Linux, or from the local PnP table when the Windows binary is run on the
machine itself - and flags the USB-serial adapters apart from the motherboard's own
UARTs and any Bluetooth COM port, where a radio never is. Nothing is opened or
written: reading a byte off the line would interrupt whatever is already talking to
the radio, so the identity alone names the port. It shows up under **Serial ports**
on the device panel, radio cable first. (`linkfinder/serialports.py`.)

A fronthaul radio reached inside a pod's network namespace is found by probing -
it answers SSH and NETCONF on its link-local address - which says a radio is
there, not which one. When the radio's management plane is up, the tool reads
the radio's **own serial and model** the standard way: a NETCONF `<get>` of the
`ietf-hardware` model on port 830, run from inside the namespace through the
host's own ssh client, nothing installed. It is a read, never an edit, and the
serial lands on the radio's node like any other device's. The logins it tries,
and - as a fallback, for a radio whose management plane is down - where a host
records a radio's serial in its own files, are site-specific and so are carried
only by the internal build, never the open one or the repository, exactly like
the host logins. (`linkfinder/netconf.py`, `linkfinder/radiolog.py`.)

### Stage 4 - working out what is cabled to what

LLDP and CDP are handled during the probe: a device announced its neighbour, and that
is the strongest evidence there is. Everything else runs **after** the probes, over
the finished graph, in one declared order - `linkfinder/ladder.py`:

| # | Rung | What it does |
|---|---|---|
| 1 | virtual machine to hypervisor | the guest's address is in the host's bridge table, or its hardware address follows a hypervisor's convention - not evidence about a cable, the cable itself |
| 2 | PTP links | a clock hierarchy names its own parent, deliberately |
| 3 | PTP clock roots | the grandmaster each clock is following, placed on the map |
| 4 | IPv6 neighbours | a link-local address belongs to one cable, not to a subnet |
| 5 | IPv4 neighbour cache | the last rung that adds a link, and only for ports nothing above settled |
| 6 | LACP partner | not a cable but a switch, named by its own aggregator |
| 7 | controller cross-check | a management controller already on the map, matched to its host |
| 8 | consistency check | flags the cables whose two ends tell different stories |
| 9 | LAG folding | collapses the members of a bond into the one link they really are |

The last four read the finished map rather than any probe output, which is why they
come after everything that can still add to it.

**The order is the rule: a port settled by stronger evidence is never revisited by
anything weaker.** That order used to live nowhere. Each caller imported the builders
it remembered and ran them as they happened to be written, so a crawl and a plain host
list drew *different maps from identical facts*.

No rung is fatal. One that cannot run says so and the rest carry on: a map missing one
kind of link is worth far more than no map.

### Stage 5 - how far to believe it

The three words on a link count **independent witnesses**, not probability. They are
decided in one place, `linkfinder/topology.py`:

| Word | What happened |
|---|---|
| `confirmed` | both ends named each other, independently |
| `reported` | one end announced it; the other was never asked, or stayed silent |
| `inferred` | neither end said anything - it was worked out from other evidence |

An `inferred` link names the evidence it came from, in words, on its own panel. See
[Link confidence](#link-confidence) for the long version.

### Stage 6 - what is done with the finished graph

`linkfinder/topology.py` is pure data: nothing in it talks to a device. From there:

| Output | Module | What it is |
|---|---|---|
| the map | `linkfinder/render_topology.py` | a page served on `127.0.0.1`, graph library bundled |
| NetBox comparison | `linkfinder/netbox.py` | a record of intent beside a record of fact; read-only |
| three CSV reports | `linkfinder/findings.py` | what NetBox disagrees with, what moved, what does not add up |
| rings | `linkfinder/loops.py` | a redundant path is not a fault; a ring with a leg blocked by spanning tree is a correct network |
| end-to-end checks | `linkfinder/linkcheck.py` | speed, MTU and VLAN - reported only when both ends spoke |

### How the product itself is checked

Sixty suites, and six gates. Two read the source: no name is used that nothing defines,
and no site name appears anywhere in the prose or the examples. Four read **the built
product**: every module is really inside the binary, no settings or site names were
bundled with it, both front ends wear the same icon, and the binary starts and does
its work.

That is not caution for its own sake. A dynamically assembled import once dropped three
modules from the binary: PyInstaller reads source to find what a program imports and
cannot see a name built at run time. Maps came out missing every ARP and every PTP link
for hours, while every suite stayed green - because from source it all worked.

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

### How long it listens, and why it is not less

Interrogating one host was measured at 145 seconds, 127 of them spent
listening for discovery frames. The listening stops as soon as every port
being watched has produced one - but a host with four ports two of which
have no neighbour never meets that condition, so the window runs out.

Shortening it was tried and **turned off again**, and the measurement is
worth keeping. Remembering which ports had ever spoken, and waiting only
for those, took one host from 145 seconds to 60 with every neighbour still
found, three runs in a row. Across a site of nineteen it took 661 seconds
to 416 - and the map came back with ten fewer links. Two cold passes of
the same site differ by one link, so that was not the network being moody.

Exactly two hosts accounted for all ten, and they are the two whose radio
units answer over IPv6 neighbour discovery inside a pod's network
namespace. One went from four neighbours to none, the other from six to
three; the other seventeen hosts were identical to the entry.

The reason is the thing being shortened. While a capture runs the
interface is listening, the kernel sees the advertisements going past and
fills its neighbour cache - which is read afterwards. Listening for less
time genuinely hears less. Nothing was dropped by a defect; it was never
heard in the first place.

So the whole window is what runs. `linkfinder/quiet.py` keeps the
machinery and the measurement, switched off, for the day somebody can tell
those two hosts from the other seventeen before deciding how long to
listen.

**What did change** is the grace period after the last expected port
lands. It was eight seconds, which is shorter than the gap between one
device's advertisements: a management port facing both a switch and a
virtual machine reported the switch and lost the machine. It is now one
full LLDP interval. Hearing whoever spoke first is not the same as hearing
what is on the wire.

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

### What is believed, and in what order

Several things can claim that two ports are cabled together, and they are not equally
good. The rule is that the stronger evidence goes first, and a port it has already
settled is never revisited by anything weaker - so a guess cannot overwrite a fact, and
one cable cannot be drawn twice.

| | Evidence | Why it sits there |
|---|---|---|
| 1 | LLDP / CDP | The device says who it is, on purpose, out of that port. |
| 2 | PTP | A clock hierarchy names its own parent. |
| 3 | SNMP forwarding table | The switch itself says which port a MAC is behind. |
| 4 | IPv6 neighbour | A link-local address belongs to one cable, not a subnet. |
| 5 | IPv4 ARP | The last rung, and only for ports nothing above settled. |
| 6 | LACP partner | Not a cable but a switch, named by its aggregator. |

That order lives in one list, in `linkfinder/ladder.py`, and both the crawl and a plain
host list walk it. They used to keep a copy each, and the copies had drifted: one knew
about PTP and the other did not, so the same facts drew two different maps.

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

### It starts from the scan you already did

Pressing Map Network after a Scan does not interrogate those devices again. What
the scan collected is written straight into the map, and the walk begins from
their neighbours. The map page says so before you press anything:

```
Starting from the scan of 3 device(s) taken just now - only what is new gets asked.
```

Two consequences worth knowing:

- Every address in the Target field is a seed, not only the first. Scanning
  three hosts and then generating gives you all three on the map.
- The map is only as fresh as the scan it reuses. The age is printed; press
  Scan again to refresh it.

On the command line the same thing: `--topology` takes a list.

```bash
python -m linkfinder --topology 10.0.0.1,10.0.0.2,10.0.0.3 --max-depth 2
```

A seed may carry its own login, exactly as a scan target may:
`--topology root@10.0.0.1,admin:secret@10.0.0.2`.

### Confirm mode

The default. Between levels:

```
Found 6 new network device(s) at depth 1:
   10.0.0.2         sw-access-1
Add and scan discovered neighbours? [y/N]
```

`--auto-discover` skips the question. It is never the default. In the window the same
question appears as a dialog.

### Cables between two servers

A direct cable from one host to another has nobody to report it. Neither end is a
bridge, so there is no LLDP; no switch sits in the middle, so there is no forwarding
table to look it up in. Every other method in this tool is blind to it.

PTP answers it, because the hosts are already talking. After the walk, the clocks each
port heard are matched against the clocks the other devices announce:

| What was heard | Drawn as |
|---|---|
| two ports hear each other and nothing else | `direct` - most likely a cable |
| a clock heard, but by more than one of our ports, or only one way | `segment` - shared layer-2, not proof of a cable |
| nothing nameable | no edge at all |

The grading is deliberately cautious both ways. One clock audible from two of our ports
rules out a cable on at least one of them; one port hearing two clocks rules out a cable
on that port, whichever the far ends are - a splitter or a hub looks exactly like that.

Two limits worth knowing before reading the map:

- **A master port transmits; it usually hears nothing.** A link is pairable only when at
  least one end is listening, so a pair of master ports facing each other stays undrawn.
- **Both ends have to be in the same run.** A grandmaster that was not scanned is named
  in the port's details but has no node to draw an edge to.

### Nothing is a dead end

- A neighbour with no management address is still a node, marked `unscanned`.
- A device that refuses the login is `unreachable`, with the reason stored on the node.
- Neither stops the rest of the walk.
- Only management addresses that a discovery protocol actually reported are followed.
  **This is not a subnet scanner** and will never probe an address nobody mentioned.

### Link confidence

The three words count **how many ends said so themselves**. They are not a
score of how likely the link is - they say who the witness was.

| Value | What happened | Drawn as |
|---|---|---|
| `confirmed` | both ends named each other, independently | solid |
| `reported` | one end announced it; the other was never asked or stayed silent | dashed |
| `inferred` | neither end said anything - it was worked out from other evidence | dotted |

`confirmed` is the only one with two witnesses. Each end was asked on its own
and neither knows the other was asked, so they cannot agree a wrong answer
between them. That is what separates a cable from a one-way tap: a passive
tap hears both sides and neither side hears it, so it can never be confirmed.

`reported` means a discovery protocol - LLDP, CDP, MNDP/LLDP - or a USB bus
enumeration. The device said it; nobody checked it. Scanning the far end is
what turns it into `confirmed`.

`inferred` is this tool's own conclusion, drawn from what answered on a port
(IPv6 neighbour discovery), a forwarding table, the ARP cache, or timing
traffic. The panel names which of those it was, in words, on every such link.
A high `inferred` count is not an error - it means those far ends have not
been scanned.

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

## 6a. Port status and health

Entirely opt-in. Without `--port-health` the tool does not issue a single extra SNMP
request; with it, the switch port table is read and each port gets a verdict.

```bash
python -m linkfinder 198.51.100.1 --port-health --community <community>
```

**Which device is asked.** The switch discovered during the scan is tried first, then
anything named with `--switch`, then the targets themselves. This matters: point the
tool at a server and the server is not a switch, so asking it was the one thing
guaranteed to return nothing.

**Reading the table by index, not by walking it.** `ifName` is walked once to learn
which interfaces exist, and every other column is then fetched for exactly those
indexes. A blind walk is wrong on a switch with thousands of VLAN interfaces: the
VLANs take the low indexes, the physical ports sit above them, and a walk that stops
early hands back a table with every real port missing and no sign anything was cut.

**A missing `ifType` is not a "no".** Some switches publish `ifType` for their VLAN
interfaces and nothing at all for the physical ports. A port is only rejected when
the device positively says it is not a physical one; when the type is absent,
appearing in `ifName` is enough.

**Joining LLDP to a port.** `lldpRemLocalPortNum` is an LLDP port number, not an
`ifIndex` - gear that numbers its ports 1000001 upwards calls them 1..21 over LLDP.
The exact routes are tried first (the interface name, then the local port id, then
the port number as an index); only if all fail is the port number matched against
the trailing number of an interface name, and only when that identifies exactly one
port. A match made that way is labelled as such in the port detail, so an inference
never reads as a reading.

**A dead port reports no speed.** `ifSpeed` still returns a number when the link is
down, and it is not a negotiated one.

**What is read**, all standard MIBs, through the SNMP transport discovery already uses:
IF-MIB (`ifName`, `ifDescr`, `ifType`, `ifAdminStatus`, `ifOperStatus`, `ifSpeed` /
`ifHighSpeed`, `ifLastChange`, `ifPhysAddress`, `ifInErrors`, `ifOutErrors`,
`ifInDiscards`, `ifOutDiscards`, `ifHCInOctets`, `ifHCOutOctets`), LLDP-MIB for the
neighbour on each port, and BRIDGE / Q-BRIDGE for learned MACs and the port VLAN.

**The rule that matters.** An absolute counter proves nothing - `ifInErrors = 5000` may
have accumulated over years. The counters are therefore read twice, a few seconds apart,
and only a counter that actually **moved** counts as evidence. A port with five thousand
old errors and no new ones is reported as healthy.

**Status**: `UP`, `DOWN`, `DISABLED`, `DEGRADED`, `UNKNOWN`.
**Usage**: `USED`, `FREE_CANDIDATE`, `DISABLED`, `UNKNOWN`.

| Observed | Status | Usage | Confidence |
|---|---|---|---|
| admin down | DISABLED | DISABLED | high |
| admin up, link up, LLDP neighbour | UP | USED | high |
| admin up, link up, MACs learned | UP | USED | high (medium for a single MAC) |
| admin up, link up, nothing identifies the far end | UP | USED | low |
| admin up, link down | DOWN | FREE_CANDIDATE | medium |
| admin or link not reported | UNKNOWN | UNKNOWN | low |

A port becomes **DEGRADED** only on real evidence: errors or discards that grew during
the sampling window, a link that changed state in the last five minutes, or a speed below
one you declared with `--expect-speed`. Vendor transceiver alarms are an extension point,
not a promise - nothing is invented when the MIB is absent.

```
Port: Eth1/17
Admin: UP
Link: UP
Speed: 1G
Usage: USED
Health: DEGRADED
Confidence: HIGH

LLDP:
  Neighbor: server01
  Remote port: eno1

Counters:
  rx_errors:   1240 (+37)
  tx_errors:   0 (+0)
  rx_discards: 12 (+3)

Reason:
  rx_errors increased by 37 during sampling
```

If the health read fails while ordinary discovery succeeded, the discovery result is
still printed and the health section says `UNKNOWN` with the reason. A health failure
never aborts a scan.

---

## 6a2. Comparing the two ends of a link

`linkfinder/linkcheck.py`, with `optics.py`, `lag.py` and `stp.py` feeding it.
Runs as part of an ordinary scan; reading the far end costs a handful of SNMP
GETs for the ports a link actually landed on, not a table walk, so it is not
opt-in.

Each check is a small pure function over facts already gathered, and writes a
named layer onto the link rather than a column onto the table:

```
edge.note("mtu", a=9000, b=1500, verdict="mismatch", confidence="high",
          evidence=["local MTU 9000", "remote MTU 1500"])
```

**A verdict needs both sides.** One side known and the other not is UNKNOWN,
never a match and never a fault. Most of what a tool like this costs is paid in
false alarms, and a link whose far end could not be reached is the commonest
case of all.

| Layer | Compared | Flagged when |
|---|---|---|
| `mtu` | `/sys/class/net/<if>/mtu` against `ifMtu` | see below |
| `vlan` | the untagged id each side reports | they differ |
| `speed` | negotiated rate at each end | they differ |
| `lacp` | local bond against the switch aggregator | one end bundled, the other not |
| `stp` | `dot1dStpPortState` for the far port | the port is not forwarding |
| `optics` | the two transceivers | fibre, speed or wavelength disagree |

### MTU: the two numbers do not mean the same thing

A switch port's MTU is a ceiling - the largest frame it will accept. A host's is
what it sends. 1500 facing a port set to 10240 is not a fault, it is the normal
state of almost every network, and reporting it would put a warning on nearly
every link in the result. The fault is the other way round: a host sending
frames the switch will not carry.

Between two endpoints there is no ceiling, both sides send, and any difference
counts. The check takes which case it is in.

This was got wrong first time round, flagged both fronthaul links on the bench,
and the numbers were right while the verdict was wrong. The test that pins it
says so in its own name.

### VLAN

Only the untagged id is judged. Whether a port also carries a VLAN as a trunk
member needs the allowed-VLAN list, which not every device publishes, so the
host's tagged subinterfaces (`eno1.84` and friends) are recorded as context
rather than turned into a verdict that cannot be stood behind.

### LACP, and why a bond is one link

`lag.py`. Members are read from the `master` links in `/sys`; the far end's
port-channel comes from `ifStackTable`, which is plain IF-MIB rather than a
vendor MIB per platform.

On the map the members are folded into the one logical link they form -
`bond0 - Po12`, with the individual cables listed in the overlay. Four members
drawn as four edges says a server has four separate connections to the same
switch, which is both wrong and the kind of wrong that stops people trusting
the picture.

Folding happens only on evidence: members of the same bond, landing on the same
device. Two members on two different switches stay two links, because that is a
fact worth seeing. A bond with one live member stays one link. The per-device
tables are untouched and still show one row per cable - that is what you need in
order to go and unplug something.

### Optics

`optics.py`. `ethtool -m` already decodes the module EEPROM into readable
fields, so that is parsed rather than SFF-8472 re-implemented. One command per
host covers every lit port.

Comparison is ordered by how little room there is to argue:

1. **fibre type** - single-mode glass into a multimode module cannot work, and
   nothing in link state will tell you;
2. **speed**;
3. **wavelength**, with 50 nm of slack for vendor rounding;
4. **form factor** on the same fibre - a warning, not a fault.

Reach is read and shown but never judged. A 10 km optic on a 2 m patch lead is a
normal thing to find in a lab, and flagging it would train people to ignore the
column.

The far end has to be a port this run actually looked at, which in practice
means the direct cables - exactly the links where a mismatch is both likely and
invisible. A switch nobody can log into keeps its module to itself and the
verdict stays UNKNOWN.

Digital diagnostics - RX and TX power, temperature - are reported when the
module has them and said to be absent when it does not. Every optic on the
bench this was written against is silent about its own power, which is normal
and must not read as a missing measurement on a failing link.

### Spanning tree

`stp.py`, standard BRIDGE-MIB only. Forwarding is the quiet case. Blocking,
listening and learning are surfaced - not as faults, but because they answer
"the cable is fine, so where is the traffic". A redundant topology with STP
working is not an error.

`dot1dStpPortTable` is indexed by bridge port, not ifIndex;
`dot1dBasePortIfIndex` bridges the two.

Topology-change counters are recorded and deliberately not judged. Calling a
tree unstable needs observations over time, which is what the snapshot diff is
for; guessing from one reading is how a tool earns a reputation for crying wolf.

**Unverified on hardware.** The switch this was written beside runs no spanning
tree and publishes none of these objects. The decoding is covered by tests
against a fake bridge and has never been read off a live one.

---

## 6b. Timing, and the cable nothing announces

`linkfinder/ptp.py` and `linkfinder/ptp_snmp.py`. Runs by default on Linux
targets; `--no-ptp` turns the whole thing off.

Four rungs, ordered by how little cooperation each needs from the device being
asked. Every one reports which rung answered, and silence on one rung is never
read as an answer from another: a switch that ignores management messages is
still emitting Announce, and the passive read has already proved it is there.

| Rung | How | Needs |
|---|---|---|
| 0 | Announce frames read off the wire | nothing |
| 1 | the protocol's own management messages, via `pmc` | nothing |
| 2 | SNMP: RFC 8173 PTPBASE-MIB, then vendor trees | the community |
| 3 | per-NOS CLI `show ptp ...` | switch login (not built) |

**Why.** A fronthaul port is normally cabled straight into another server or a radio.
There is no switch in the path, so no forwarding table knows the MAC, and neither end
runs lldpd, so there is no LLDP to read. Every rung of the ladder comes back empty
and the port is reported unresolved. PTP is running on that cable regardless.

**Daemon state, for every port.** `ps` gives the running `ptp4l` command lines; the
interface, domain, transport and role come straight off them. Each instance is then
asked with `pmc -u -b 0 -d <domain> -s <uds_address>` for `PORT_DATA_SET` and
`TIME_STATUS_NP`, which give the port state, our own clock identity, the grandmaster
and the offset. The domain and socket are not optional decoration: a bare `pmc` talks
to domain 0 on the default socket and returns nothing on a host running two instances
on domain 24, which looks exactly like PTP not being configured.

**The wire, for ports LLDP could not resolve.** A separate short capture, filter
`ether proto 0x88f7 or udp port 319 or udp port 320`, 25 frames or four seconds. It is
deliberately a second capture rather than an addition to the LLDP filter: PTP at
`logSyncInterval -4` is sixteen messages a second, and folding it into a five-frame
LLDP capture would flush out the one frame that capture exists to catch.

**From a clock back to a machine.** The PTPv2 header carries `sourcePortIdentity`, and
linuxptp builds that identity from the interface MAC by inserting `ff:fe` in the
middle. Undo that and the identity becomes a MAC; match the MAC against the ports we
interrogated in the same run and the far end has a name. An identity not built from a
MAC is left alone rather than turned into a device that does not exist.

**How far the evidence goes.** A PTP event frame is multicast. Hearing one proves a
shared layer-2 segment, not a cable. The giveaway is arithmetic: if one clock is
audible from two of our ports, at least one of those is not a direct link.

| What was seen | What is reported |
|---|---|
| mutual, one to one in both directions | most likely a direct cable |
| the clock is heard from several of our ports | same layer-2 segment |
| the far end hears us but we did not hear it | shared segment, from its side |
| a clock belonging to no machine we scanned | the identity we heard, no more |

Hearing is recorded in both directions on purpose. A four-second capture samples, so
one end often catches the other while the reverse comes back empty; without the
reciprocal entry the same cable would appear and disappear between runs.

### Rung 0: what an Announce actually carries

Only the grandmaster identity was read at first, which threw away most of the
message. The whole body is now decoded: `grandmasterPriority1` and `2`,
`clockClass`, `clockAccuracy`, `stepsRemoved`, `timeSource`, the UTC offset, and
the **PATH_TRACE TLV**.

`clockClass` is the one worth knowing. 6 means the clock is locked to a primary
reference; 7 is holdover still within spec; 165 is a boundary clock running free;
255 is slave-only. A grandmaster that quietly slid from 6 to 165 is the difference
between a healthy fronthaul and one about to drop cells, and nothing in `ip link`
or LLDP will tell you. Values outside the G.8275.1 set are printed as numbers
rather than guessed at.

`stepsRemoved` is how many boundary clocks away the grandmaster is, and PATH_TRACE
names each one it crossed - the timing topology, handed over by the protocol. The
TLV is optional, so an empty path trace means "not enabled", never "no boundary
clocks in the way".

**Naming a switch from its clock.** A boundary clock derives its clock identity
from its chassis MAC, and that is the same MAC it publishes as its LLDP chassis
id. Matching clock identities against the switches already on the map - not only
against our own NICs - is what turns `025566.fffe.000001` into a switch name. On
the bench that identity belonged to the switch the servers were plugged into the
whole time.

### Rung 1: asking the segment, with no credentials

PTP carries its own management protocol - GET, SET and COMMAND messages over the
same transport as the timing itself. `pmc -2 -i <iface> -d <domain> -b 1 'GET
DEFAULT_DATA_SET'` puts the question on the wire and every clock on the segment
that implements the responder answers, boundary clocks included. No SNMP, no
login, nothing configured in advance.

LinkFinder only ever sends GET. There is no code path here that can change a
clock's configuration, which matters because the same mechanism could.

Implementing the responder is optional and plenty of switches skip it. Measured on
the bench: linuxptp answered in full over the wire, while the switch in the middle
produced not one management frame in ten seconds. That is reported as "did not
answer", not as "has no PTP" - rung 0 had already shown it was the grandmaster.

### Rung 2: asking the switch over SNMP

`linkfinder/ptp_snmp.py`, using the community discovery already has. This is the
only rung that can see what a bystander cannot: which of the switch's own ports is
taking time in, how long it has been in holdover, what it would fall back to.

RFC 8173 defines PTPBASE-MIB at `1.3.6.1.2.1.241`; the twelve tables under
`.1.1` are walked and whatever answers is kept. If none of them do, a short probe
looks for a vendor tree worth naming - Cisco `1.3.6.1.4.1.9.9.760`, Juniper,
Arista, Nokia - and reports that one exists without pretending to decode it.

Support is patchy enough that deciding whether there is anything there is the
first job. The bench switch publishes no PTP objects, no vendor tree and no
sysORTable, and is reported as exactly that.

**This rung is tested against a fake agent only.** Nothing in the lab it was
written in implements the MIB, so it has never been run against hardware that
does. Treat its output as unproven until it has been.

### The clock tree

`ptp_tree.py`. Per-port facts answer "what is this port doing"; they do not
answer "is everything on the same clock, and where does that clock come from".
The ports are grouped into domains and the domains into trees.

The distinction that keeps it usable: a server running a master on a
point-to-point segment **is** the grandmaster of that segment, and in a
fronthaul design that is the point. Such a grandmaster is reported as an
island. Two grandmasters that are *not* ours sharing one domain number is a
different matter and is reported as a split domain.

```
PTP, domain 24
  grandmaster 025566.fffe.000001  (sw-core-1, class 6)
      server1 enp23s0f2          SLAVE, offset -3 ns, path delay 9 ns, 1 step(s)
      server4 enp101s0f1         SLAVE, offset -2 ns, path delay 120 ns, 1 step(s)
  grandmaster 02aabb.fffe.000002  (class 6)
      server3 enp23s0f0          SLAVE, offset +3 ns, path delay 21 ns, 1 step(s)
  island: server1 enp23s0f0 is the grandmaster of its own segment
      server2 enp101s0f3         SLAVE, offset +1 ns, path delay 72 ns
  SPLIT DOMAIN: domain 24 follows 2 different grandmasters that are not ours
```

Per port, each against a named IEEE 1588 object:

| Finding | From | Confidence |
|---|---|---|
| grandmaster not locked | `gm.ClockClass` outside the locked set | high |
| offset past the threshold | `offsetFromMaster` | high |
| time not traceable | `timeTraceable = 0` on a slave | medium |

The offset threshold defaults to 1000 ns and is deliberately loose. G.8275.1
allows roughly 1.5 us end to end and a healthy boundary clock sits in the tens
of nanoseconds, so a microsecond is far enough out to be worth a look and far
enough in not to be an alarm.

**A domain-level finding stays at domain level.** The first version stamped the
split onto every port in the domain, which on a six-port fronthaul host filled
the issues column with one piece of news repeated. Only what was measured at a
port goes on that port's row; the split is said once, in the report and on the
line above the device table.

Ports with nothing wrong get a clean verdict rather than none, so "checked and
fine" stays distinguishable from "never looked".

### Where the numbers come from

Nothing in the PTP output is invented, and the values mean the same thing on
anyone's network:

| Shown | Field | Standard |
|---|---|---|
| `dom 24` | `domainNumber` | IEEE 1588; **24** is what ITU-T G.8275.1 specifies, G.8275.2 uses 44, the default profile 0 |
| `SLAVE` / `MASTER` | `portState` | IEEE 1588 (1588-2019 renames these TIME_RECEIVER / TIME_TRANSMITTER; linuxptp and the MIB still print the old names) |
| `offset +0 ns` | `offsetFromMaster` | IEEE 1588 CURRENT_DATA_SET |
| `path delay 9 ns` | `meanPathDelay` | IEEE 1588 CURRENT_DATA_SET |
| `1 step` | `stepsRemoved` | IEEE 1588 |
| `class 6` | `clockClass` | IEEE 1588; the 135-165 band is G.8275.1 |
| `100 ns` | `clockAccuracy 0x21` | IEEE 1588 |
| `GNSS` | `timeSource 0x20` | IEEE 1588 |

The domain number is read from the running daemon and from the frame header -
it is never assumed. A value we do not have a word for is printed as the value
rather than guessed at: `class 99`, `accuracy 0x1b`.

`TIME_STATUS_NP` is read last and only to fill gaps. The NP means
non-portable: it is linuxptp's own object, and a tool that took its headline
number from there would return nothing on most boundary clocks.

### Rung 3: the switch CLI

Not built. `show ptp clock` / `parent` / `port` would slot into the existing
`Dialect` mechanism in `probe_switchcli.py` the same way the LLDP commands do, but
it needs a login on the switch, and writing parsers against output nobody can run
would produce code that only looks finished.

---

## 6c. Comparing two scans

`linkfinder/diff.py`.

```bash
linkfinder --diff monday.topology.json tuesday.topology.json
```

A topology JSON is already a dated, complete record, so this needs no database
and no agent - only the two files. Exit code is 0 when nothing changed and 1
when something did, so it can sit in a loop and only speak up when it matters.

**Matching is the whole problem.** A node id is derived from whatever identity
was known at the time, so the same switch can be `chassis:aa:bb:cc:...` in one
file and `ip:198.51.100.1` in the next. Devices are matched on the identities
they carry, strongest first - chassis, serial, management address, name - so a
device renamed or readdressed between scans is still the same device. Anything
that cannot be matched is reported as added or removed rather than quietly
paired with the wrong one.

**A repatched cable is one event.** A link that moved to the next port is
reported as a move, naming both the old and the new endpoint, rather than as a
removal plus an unrelated arrival. Reading it as two is how a short report turns
into noise.

Beyond devices and links it reports state, name, management addresses, the PTP
grandmaster per interface, and any link whose verdict changed - a link that was
MTU-clean on Monday and is not on Tuesday says so by name.

---

## 6c2. The rack PDU, and which socket feeds what

A network map answers which switch port a machine is in. It has nothing to
say about which socket powers it, and that is the question asked when a rack
has to come down, or when a server is dead and nobody knows whether it has
power at all.

An APC network management card answers both halves:

```
[AP7921B]      pdu 198.51.100.116, scanned
               made by APC by Schneider Electric (MAC prefix (IEEE))
               serial ZZ0000000000
[Power]        AP7921B   serial ZZ0000000000
               up for 201 Days 1 Hour 47 Minutes
               8 outlet(s), 8 on
               1   rack A top shelf                   On
               3   timing source 198.51.100.11        On
```

The outlet names are whatever somebody typed when the rack was built. They
are labels and not hostnames, they are printed exactly as written, and on
most benches they are the only record of the wiring there is.

**It is recognised before anything logs in.** The card answers on port 22 and
then has no shell at all - hand it a command the ordinary way and it returns
nothing and closes the session, which is why a plain SSH probe reported a
device that logged in and said nothing. Its SSH server names the library it
is built on (`SSH-2.0-cryptlib`), which no general-purpose machine does, so
one connection and no login is enough to know to speak to it differently.

**Nothing is switched.** The commands that could turn an outlet on or off are
not sent, and are not in the tool at all. It reads `about`, `netstat` and
`olStatus`.

The model, serial and hardware address make it a device like any other: it
goes on the map, it is held against NetBox, and the switch that learned its
address can place it in a port.

## 6d. Handsets, and what else is plugged in

A lab machine has a second fan-out that no network protocol will ever tell you
about: the phones, console cables and adapters on its USB ports. When a test
fails the question is rarely "is the NUC up" - it is "is UE 3 still attached,
and to which port". So those are drawn on the map too.

### Two layers, and why only one of them is opt-in

Reading the USB tree is free and changes nothing, so it always runs:

- **Linux** - the files under `/sys/bus/usb/devices`. Each device publishes its
  maker, product, serial, the USB version it declares and the rate it actually
  negotiated. Hubs and interface entries are skipped: a hub is wiring, and
  listing a phone's four interfaces would turn one handset into four nodes.
- **Windows** - the PnP registry over SSH. Windows publishes no speed for a USB
  device, but it does publish the port: a location path ending `ACPI(SSnn)` is a
  SuperSpeed port and `ACPI(HSnn)` a high-speed one, which is the generation of
  the connection and therefore the thing being asked about.

Asking **adb** is a separate choice - `--adb` on the command line, *name
handsets over adb* in the window. It is off by default because running `adb`
may start an adb server on that host, and a server that was not running before
is a change. When it is on, the tool runs exactly two kinds of command:
`adb devices -l`, and `adb -s <serial> shell getprop` to read the model and the
Android version. It never pushes, installs, reboots or sets anything.

### What ends up on the map

Each thing on the bus becomes its own node, joined to its host by a link marked
`usb`. The link is coloured by generation, and that is the whole message:

| Generation | Colour |
| ---------- | ------ |
| USB 3 | blue |
| USB 2 | black |
| USB 1 | plain grey |

Rates are not printed. The useful question is which generation a device came up
as, not whether it negotiated 480 or 12 Mbit/s. The one warning raised is a
device that *declares* USB 3 and came up as USB 2 - almost always the cable or
the port, and worth a look. A device that declares USB 2 is not warned about:
an FTDI console cable declares 2.00 and is a UART, not a slow phone, and is
classified as a console cable rather than a handset for exactly that reason.

Four identical phones on one NUC is normal, so a model name that repeats on a
host carries its serial - `ONEPLUS A5000 (70b02c75)` - while a name nobody
else shares is left alone.

A USB link is *reported*, not *confirmed*: the host enumerated the device on its
own bus, which is a reading rather than a guess, but only the host said it. See
**Link confidence**.

### Losing the host is not losing the phones

This matters enough to be a rule. If a NUC cannot be polled, its handsets are
recorded as `UNKNOWN` - not as unplugged. "Twenty phones disconnected" and "one
NUC stopped answering" are very different mornings, and only one of them is
true. In the run record (see **What changed since last time**) that becomes a
single line naming the host, with a count of what went out of sight with it.

The reverse case is reported properly: if the host answered and a handset is no
longer on its bus, it really was unplugged, and it is reported as such. A phone
that is still on the bus but that adb may not talk to - `unauthorized`,
`offline` - is degraded, not gone, and the line says which of the two it is.

## 6e0. The management controller, and whether it answers

A server's BMC has its own address, and nobody can see it from outside until
they already know it. The host can: `ipmitool lan print` asks the controller
over the internal channel, with no network in the way and no password to hold.
It is read on every Linux host that has `ipmitool`, and a host without it says
so rather than leaving a blank.

```
IPMI/BMC address
  192.0.2.17     static, channel default
                 the host it lives in CANNOT reach it
                 other-server has it in its neighbour cache, so it is alive
                 hardware address 00:00:5e:00:53:1c
```

Three facts, each worth something different:

| What | Why it matters |
|---|---|
| **the address** | how you reach a machine that will not boot |
| **static or DHCP** | a static address that answers to nobody is a different fault from a DHCP one |
| **whether it answers** | the day you need the controller is the day you find out |

### Who did the asking

A controller usually sits on a management network the host itself is not on,
so the host failing to ping it proves very little. Saying *"nothing is there"*
on that evidence sends somebody to a rack for no reason.

So the sentence names who tried, and then the map is asked: if any other
machine on it holds that address in its neighbour cache, the controller is
alive and the report says so. Measured on a live server - the host could not
reach its own BMC, and a machine already on the map had the address the whole
time.

### Against NetBox

Where the inventory has an IPMI interface for the device, the two are compared:

```
NetBox records the same address
NetBox records 192.0.2.99 on its IPMI interface - these disagree
NetBox holds no IPMI address for this device - 192.0.2.17 is what it would be
```

**Nothing is written to NetBox.** An empty record is offered the address as
text, for a person to paste. Creating and correcting records is somebody's
job, and it is not this tool's.

## 6e. The serial number, and which physical box this is

A map that cannot say which physical unit a node is cannot be compared with an
inventory - and an inventory is keyed by serial, not by hostname or address. So
every access path is asked for it, each in its own language, and the answers
land in one field.

| Device | Where the serial comes from |
| ------ | --------------------------- |
| Linux  | SMBIOS - `/sys/class/dmi/id/product_serial`, falling back to the chassis then the board |
| Windows | `Win32_BIOS.SerialNumber` over the SSH session already open |
| ESXi   | `esxcli hardware platform get` |
| BMC    | Redfish `SerialNumber` on the Chassis, then the System |
| Switch | SNMP ENTITY-MIB `entPhysicalSerialNum`, taking the chassis entry |

Measured on real machines rather than assumed: a Dell desktop, two Supermicro
servers, a Windows NUC, a FibroLAN-class switch over SNMP and a Supermicro BMC
over Redfish all answered with a real serial.

### A placeholder is not a serial

Boards ship with `To Be Filled By O.E.M.`, `Default string`, `Not Specified`,
`System Serial Number`, `0123456789`. Accepting those would be worse than
having nothing: every machine of that model carries the same text, so they
would all merge into one device on the map. Anything on that list is refused,
and the device is reported as publishing no serial rather than given a fake
one. The model and the maker are still kept - those are worth knowing even
where the serial is missing.

The order is deliberate. `product_serial` is the unit as sold. On a whitebox it
is often a placeholder while the **board** serial is real, so the chassis and
the board are read in the same breath and used in that order. `product_serial`
is root-only on many kernels, which is the other reason all three are read.

### What it is used for

- **Identity.** Two machines of one model with one hostname stay two devices,
  and one box that answers under two chassis MACs is folded into one. The
  serial is a stronger key than either.
- **Finding a device.** It is on the **go to** line and in the filter, so a
  number read off a label or pasted out of an inventory lands on the device.
  The filter folds case, so it does not matter how the label is printed.
- **Comparing with a record of what should be there.** This is what makes a
  NetBox comparison possible later; that is not built yet.

Nothing extra is sent to reach any of this: these are reads over connections
the probes have already opened.

## 6f. NetBox: what should be there, next to what is

An inventory is a record of intent - somebody wrote down which box sits where
and what it is for. A scan is a record of fact. Neither is authoritative alone;
the useful output is where the two disagree.

```bash
linkfinder --topology 198.51.100.1 -u root -p secret --netbox \
    --setup-tag dio --topology-json site.topology.json
```

The address and token come from `--netbox-url` / `--netbox-token`, else
`LINKFINDER_NETBOX_URL` / `LINKFINDER_NETBOX_TOKEN`, else the defaults file.
Nothing is hard-coded. The window has a **NetBox** tick that does the same.

**It reads. It does not write.** There is no code path in LinkFinder that
POSTs, PATCHes or DELETEs anything in NetBox, and the tests check that. The
corrections it finds are written to a file for a person to apply.

### The token, which catches everyone once

NetBox 4.5 issues **version 2** tokens, and they are two halves: a **key** that
stays visible on the token page and a **secret** shown only once when it is
created. The header wants both:

```
Authorization: Bearer nbt_<key>.<secret>
```

An older NetBox issues a version 1 token - 40 hexadecimal characters, no key -
and that one wants `Authorization: Token <token>` instead. The tool picks the
header from the shape of what you gave it, so either kind works without being
told which it is. Checked against the NetBox documentation on 11 Oct 2026: v1
tokens are deprecated as of NetBox 4.6 and are removed in 5.0, so a site still
using one has an upgrade to plan rather than a setting to change.

Sending the secret alone answers `403 {"detail":"Invalid v1 token"}`, which
reads like a wrong token and is really half of a right one. The tool says so in
those words before it tries. Note `/api/status/` answers with no token at all,
so a 200 from it proves nothing.

### What identifies a device

In the order it is trusted. Measured on a real estate of 10 415 devices, where
10 345 carry a serial and only 2 610 have a management address on record:

| Signal | Settles it? |
| ------ | ----------- |
| a link you confirmed earlier | yes |
| its **serial** | yes |
| its serial inside the NetBox device name (`bench-b-SN0000000X...`) | yes |
| a MAC | helps, but asks for confirmation on its own |
| an address | **a hint only** |
| a hostname | **a hint only** |

An address says what answers there today - which is exactly why a swapped box
is detectable, and exactly why it cannot prove identity. A device found only by
hints is reported as `NOT_VERIFIED` and flagged for a person to confirm; it is
never moved into somebody's Setup on that basis. Two records answering to one
device is a `CONFLICT`, not a coin toss.

MAC is used when it happens to be there and is never required: in this estate
about one NetBox interface in a hundred carries one.

### The five verdicts

| It says | When |
| ------- | ---- |
| `CONFLICT` | more than one NetBox record answers to this device |
| `MISMATCH` | they disagree about something checkable - a swapped serial, an address, a status |
| `MISSING_IN_NETBOX` | we read a serial NetBox has never heard of |
| `NOT_VERIFIED` | not enough to go on, or only hints matched |
| `MATCH` | everything checkable agrees |

Worst first, each with its evidence and a confidence. A device nobody could
reach is left out entirely: failing to get in says nothing about whether the
documentation is right. Things an inventory would not hold - a BMC's virtual
keyboard, a console lead - are not judged at all.

Interface and cable records are **not** compared, and that is a measurement
rather than a shortcut. A server's own ports cannot be lined up with NetBox:
barely one interface in a hundred carries a MAC, and about one name in a
thousand looks like an OS name - NetBox holds human labels like
`interface13.1`. Switch ports are closer but still not equal: the switch is
often in NetBox under the same name, yet LLDP reports `1/1/13` where NetBox
says `Port13`. The relationship is regular enough to guess at, which is exactly
why it is left to a person to confirm instead.

### An address NetBox never recorded is a gap, not a contradiction

A server here commonly has one address on record and it is the BMC's, on an
interface called `IPMI`. Comparing what answered against NetBox's primary
address therefore accused the documentation of being wrong when it was merely
incomplete - it fired on real devices before it was fixed. Every address NetBox
holds for a device is read, and a device is only reported when the address it
answered on is among **none** of them, as `MISSING_IN_NETBOX`, saying what
NetBox does hold and on which port.

### NetBox is optional

Everything above is opt-in and nothing else depends on it. Without `--netbox`
no NetBox code runs at all; the HTTP library is imported only when a request is
actually made, so a machine without it still runs the whole tool. With
`--netbox` on and nothing configured, or with NetBox unreachable, the run says
so in one line and carries on - the map is identical either way. Setups keep
working without NetBox, because they are remembered locally as well.

Proven by running the same scan three ways - no NetBox, NetBox asked for but
not configured, and NetBox pointed at a dead address - and comparing the maps:
6 devices and 5 links in all three, with the same setups.

### A Setup is a tag somebody named

Benches live in NetBox as tags. But this estate has 985 tags and 952 of them
are in use - teams, programmes, projects and benches all mixed together, with
nothing in the data telling them apart. So **nothing is a Setup until you say
it is**: `--setup-tag dio`, repeatable, or `netbox.setup_tags` in the defaults
file, or the **Setup tags...** button in the window.

`--netbox-tags` lists the tags carried by the devices this scan matched - a
handful rather than the estate's hundreds - so there is a short list to choose
from. A name you marked by hand on the map is kept beside the ones that came
from NetBox.

### Shared kit joins the bench it serves

Pick a Setup on the map and you get its devices **and** the switch they hang
off, even though that switch belongs to no Setup - a picture of a bench without
its uplink is not a picture of how anything is connected. The rule is one hop
out, and only to devices that are in no Setup at all: a neighbour that belongs
to a different bench is somebody else's and stays out. Borrowed kit is drawn
with a dashed outline.

## 6f0. The map in your browser

This is where the work happens. The window scans; the picture is where you
look at what came back and tell the tool what it is looking at.

### What you are looking at

Every box is a device and every line is a link that was actually found. A
line carries the port it uses on both ends, which is the question the tool
exists to answer. Colour says what kind of thing it is; a dashed line means
the link was inferred rather than reported by both ends.

Click a device and the panel on the right says what is worth knowing about
it: what it is, its serial, its timing, the temperature of its cards and
which ports they are on, its optics, and the address it answers on. Nothing
that merely explains an absence is printed - if there is no "How to reach
it", there is no way in, and a paragraph saying so would help nobody.

### Live or saved

The map is **live** while the window that opened it is open. Live means it
can change things: a setup you type is written the moment you type it.

A page you saved to a file is a **viewer**. It draws, filters, searches and
prints, and it cannot mark anything - there is nothing listening. The
marking controls are not shown rather than shown and broken, and the page
reaches nothing on the network, so it opens from a memory stick on a machine
with no connection at all.

### The answers are files, not a panel

There used to be a strip of buttons along the top - what NetBox does not
know, what moved since last time, what does not add up. Each opened a list
over the map. The panel sat between the reader and the picture, the answers
were gone the moment the tab closed, and nothing was there at all unless
somebody thought to press a button.

A table is the right shape for all three. It sorts, it filters, it goes in a
ticket, and it is still there tomorrow. So they are worked out every time a
map is drawn and written beside it:

| File | What is in it |
|---|---|
| `<map>.csv` | one row per link: device, serial, setups, local port, far end |
| `<map>.netbox.csv` | every disagreement between the scan and the inventory |
| `<map>.moved.csv` | what changed against the previous map |
| `<map>.odd.csv` | what does not add up on the map's own terms |

A question that was not asked gets no file. An empty `netbox.csv` beside the
map would say NetBox agreed about everything, when nobody asked it anything.

**`<map>.netbox.csv` is the one people are sent to find**, so it is laid out
to be read across rather than down:

| verdict | device | field | on the network | in NetBox |
|---|---|---|---|---|
| MISMATCH | core-a | serial | 230317243800564 | 23081601 |
| MISSING_IN_NETBOX | iper-a | ip_address | 192.0.2.14 | 192.0.2.114 |
| NOT_VERIFIED | vm-a | | | nothing to look it up by |
| MATCH | sw-one | | | agrees |

The two sides used to be one sentence - *"it holds serial X, but NetBox
records Y there"* - which reads well and compares badly. Both values were
always kept separately, so putting them in their own columns costs nothing
and a roomful of people can scan it.

`NOT_VERIFIED` matters as much as the disagreements: it says the device
published nothing to look it up by, which is not the same as agreeing.

**Finding them.** `File` → `Show the files it wrote` opens the folder with
the NetBox table already selected. The path has always been announced in the
log, and the log is a tab nobody has open - during a demonstration the
question "where is the comparison with NetBox?" was asked about a file that
had been sitting there, correct, for an hour.

**Handing it to somebody.** `File` → `Save what NetBox does not agree with...`
writes that table wherever you want it. If the run did not ask NetBox, this
asks it now rather than refusing, and with nothing scanned at all it says so
instead of writing an empty table - a file reporting that NetBox disagreed
about nothing, when NetBox was never asked, is worse than no file.

What stayed on the picture is what only works there: marking setups, which
is done by selecting devices and typing a name, and searching. Choosing
which NetBox tags name a Setup is back in the window, under
**Advanced -> NetBox -> Setup tags...**

### Finding things

The search box matches what a device **is** - its name, address, serial or
kind - and not everything it has ever heard of, so searching for a switch
finds the switch rather than every host that mentioned it.

The setup filter shows one bench at a time. A device that serves several
benches - a shared uplink - stays visible in each of them, because hiding it
would be a lie about the picture.

### Marking a setup

Click a device, type the bench name in the panel, press Add. That is the
whole thing, and it is written immediately - there is no save step and no
file to import afterwards.

For more than two or three devices, use **Mark a whole bench** instead: a
bench usually arrives as a list of addresses in a ticket, and pasting a list
is one action where clicking twelve devices is twelve chances to miss one.

### Printing it

The browser's own print works. The navigation and the strips are hidden, the
picture and the panel are not.

## 6g. Marking a setup on the map

The map is where the benches are visible, so it is where they are marked - and
the window writes the change the moment you make it.

Press **Open map** in the window. It serves the map to your browser and answers
it for as long as the window is open. Click a device and its panel has a
**Setup** section: the names it is already in, as chips you can take off, and a
box to add another. For several at once, tick **mark setups**, drag a box round
them, and the strip adds or removes a name for the lot.

There is nothing to save. No download, no file to find, no import, and no
question when you close the tab. The line under the strip says `saved` when the
change has been written, and says so plainly if it has not - a failure is never
dressed up as a success.

A setup is remembered against the device's **serial** first, then its chassis
id, then its address, so a machine that changed address keeps its bench.

### What the server is, and what it is not

It binds to `127.0.0.1` on a port the operating system picks, carries a
one-time token in its own address, and refuses anything that does not have it -
a wrong token looks exactly like a wrong path. It answers four things: the map,
the current setups, a setup change, and the list of what NetBox does not know.
It reads no files, takes no names from a request, sets no cookies and sends no
CORS headers, so no other page on your machine can talk to it. It starts when
you press **Open map** and dies with the window.

### A saved copy is a viewer

Open a `.html` file you saved earlier and it works exactly as it always did -
offline, with no server and nothing to install - but it cannot mark anything,
and the marking controls are not shown at all. That is the honest state of
affairs: a page opened from disk cannot write to disk. Pretending otherwise is
what produced files nobody wanted.

### Opening a shell from the map

Clicking a device offers `ssh://<address>`. A page cannot start a session
itself; it hands the address to whatever the machine registered for that
scheme. When nothing is registered the click would normally do nothing at all,
so the page notices and says so, with the command to run and what to install.
The window has its own SSH button that opens a session in-process and uses
everything in Advanced - per-host login, key file, spare logins and the port.

### What to add in NetBox

**What to add in NetBox** in the window collects the devices and setups NetBox
does not know about, shows them, and offers to write them out for whoever has
the rights to apply them. It is produced when you ask for it rather than
appearing after every change, and nothing is sent anywhere.

## 6j2. One answer for the team, not one per laptop

By default everything the tool remembers - which bench a device is on, the
maps it keeps - lives in your home folder. That is right for one person and
wrong for a team: two engineers scan the same bench and get two different
answers, with nothing to say why.

Point it at a folder everybody can reach:

```
linkfinder --shared //fileserver/lab/linkfinder ...
```

or set it once, in the defaults file, and everything agrees from then on:

```json
{ "shared": "//fileserver/lab/linkfinder" }
```

A UNC path works too; in JSON its backslashes have to be doubled, which is
why the forward-slash form above is the easier one to type.

### How it avoids the usual trouble

| Worry | What is actually done |
|---|---|
| two people writing at once | **each person writes only their own file** and reads everybody's, so there is no lock and no last-writer-wins |
| the share blinks mid-write | written through a temporary file and renamed, so the old one survives whole |
| the share is down | falls back to this machine and **says so** - a scan is never blocked |
| whose mark is this? | `--where` names everyone marking in that folder |
| marks made before sharing | carried into your file on the first run, and the private copy is kept, renamed, not deleted |

The layout is plain files, so it can be read, backed up or put in version
control by anyone:

```
<shared>/setups/<user>@<host>.json
<shared>/history/<when>-<user>.json
```

A device two people put in different setups ends up in both. That is not a
conflict to resolve: a shared uplink really does serve several benches, and
the model has always allowed a list. `who_says` on the panel names who put it
there, so a genuine disagreement is visible rather than merged away silently.

### Say where things are

```
linkfinder --where
```

```
settings and logins   C:\Users\you\.linkfinder.json
passwords             Windows DPAPI, keyed to you
setups                //fileserver/lab/linkfinder/setups/you@your-pc.json
maps it remembers     //fileserver/lab/linkfinder/history (6 kept)
switches it learned   C:\Users\you\.linkfinder-switches.json

setups and kept maps are shared, in //fileserver/lab/linkfinder, as you@your-pc
marking here as well: olga@bench2
```

The window writes the same line into its log when it opens, and the map shows
it under the NetBox strip.

> Silence about this was the actual fault. A mark private to one laptop and a
> mark the whole team shares looked identical, so nobody could tell which they
> had.

## 6m. A range of addresses

A bench is held as "the .74 network" or ".200 to .210", not as a list. All
three ways a person writes that are taken, anywhere a target is taken:

```
linkfinder --targets 10.0.0.0/24
linkfinder --targets 10.0.0.5-10.0.0.40
linkfinder --targets 10.0.0.5-40            the same, written short
```

Nothing is swept silently. A range larger than 1024 addresses is refused
with the number, because a typo that starts sixty-five thousand login
attempts is a tool nobody is allowed to run twice:

```
10.0.0.0/16 holds 65536 addresses - more than the 1024 this will sweep. Narrow it
10.0.0.8-10.0.0.5 runs backwards - put the lower address first
```

Ranges open out before anything else looks at them, so they work the same
for one device, for a map, and together with the NetBox lookup below.

## 6n. Looking something up in NetBox

You hold a bench name or a number off a label, not an address.

```
linkfinder --netbox-find bench-a
linkfinder --netbox-find SN0000000X5304276
```

It tries the serial, the asset tag, the device name and the tag, and says
which one landed. On the map the same thing is the **look one up** drawer
behind the NetBox door.

### Letting NetBox say where to scan

```
linkfinder --from-netbox bench-a --topology-json bench.json
```

The addresses NetBox holds for that bench become the scan's targets, and
the map is drawn from what is actually there. Where the inventory is thin
it says so rather than looking thorough:

```
from netbox: 10 record(s) carry 'bench-a', but an address is recorded for
only 1 of them - the rest cannot be scanned until somebody adds one
```

That sentence is the useful half of the answer: it names a gap somebody
can close, instead of reporting a bench of one device.

## 6o. What does not add up

Four things a network does that no single device will admit to:

| What | How it is seen |
|---|---|
| **one address, two owners** | two machines on the segment learned different hardware addresses for it |
| **one MAC in two places** | the same hardware address turns up on two ports |
| **a ring in the cabling** | a cycle in the discovered topology, with whether spanning tree is holding it open |
| **a cable whose ends disagree** | MTU, VLAN, speed or LACP read differently at each end |

```
linkfinder --map bench.json --suspicious
```

On the map this is written to `<map>.odd.csv`, and each finding
can point at the devices it concerns.

Nothing here decides anything. Every one of these can be innocent - a
gateway answering for a subnet looks exactly like a stolen address until
somebody who knows the bench looks at it - so each finding carries what was
seen and how sure it is.

> A check that breaks never reports a clean network. If one of the four
> cannot run, it says so as a finding marked `UNCLEAR`, because the one
> thing worse than a missed fault is a false all-clear.

## 6k. Marking a whole bench at once

Nothing here starts from a blank file. Ask for the sheet and it comes back
filled in - every device the map knows, with its setup already in the first
column:

```
linkfinder --setup-sheet setups.csv
```

```
setup,device,serial,address,kind,state,key (do not edit)
bench 1,server-a,S907083X6310614,10.0.0.5,host,scanned,ip:10.0.0.5
,radio-1,,169.254.15.16,radio,unscanned,chassis:2c:a5:39:00:14:01
```

Open it in Excel, type a setup next to the rows that need one, save, and hand
it back:

```
linkfinder --mark-list setups.csv
```

**What is in the sheet is what you get.** A name typed in puts the device in
that setup; a cell cleared takes it out; a row left alone stays as it was.
Several setups on one device are separated by commas. The last column is how
a device is recognised even after it is renamed - leave it alone.

In the window it is the **Setups...** button: it asks whether you are saving
a sheet or loading one, and does the rest. On the map it is **Mark a whole
bench**, which offers the same sheet and takes it pasted straight back.

### Or just a list

When the bench is only a few devices and they are all going in one setup, a
plain list is enough:

```
linkfinder --mark-setup "bench 7" --mark-list bench.txt
```

```
# one per line; anything after # is ignored
10.0.0.5
S907083X6310614
2c:a5:39:00:14:01
some-device-name
```

Any of four things identifies a device: a **management address**, a **serial
number**, a **chassis MAC** or the **name it announces**. They can be mixed in
one file, and two lines naming the same device mark it once.

A line that matches nothing in the map is **reported back, never guessed at**:

```
marked 4 device(s) in bench 7 (from the newest map kept)
   server-a
   radio-1
not recognised in that map, so not marked:
   10.9.9.9
```

With no `--map` it works on the newest map the tool kept, so there is nothing
to find on disk. `--mark-setup ""` takes the listed devices out of every setup.

On the map the same thing is **Mark a whole bench**: paste the list, name the
setup, apply.

The marks are remembered by serial, MAC and address, so they survive the next
scan - including a device that changed address.

## 6l. One bench, both ways

The ordinary NetBox comparison runs outwards from the scan, so it can only
report on devices that answered. The question it cannot answer is the one
people actually ask: *the inventory says this bench has ten devices - does
it?*

```
linkfinder --setup-diff bench-a --setup-diff-csv bench-a.csv
```

```
bench-a: 2 in both, 8 only in netbox, 3 only here, 1 to confirm
  IN BOTH         5G AMD SERVER (Multi)      server-a        the serial number
  IN BOTH         MM-0-n78LC-00              Integrity-MM..  one name ends with the other
  ONLY IN NETBOX  Vaunix attenuator                          nothing answering to this was found
  ONLY HERE                                  radio-4         not recorded under this setup
```

Three verdicts, and the useful ones are the last two. A bench of three devices
gives a dozen rows, not a page of search results.

How a record and a device are paired, strongest first:

| Signal | Settles it? |
|---|---|
| the serial numbers are equal | yes |
| the serial appears inside the other's name | yes |
| the names are equal | only with something else agreeing |
| an address is held by both | only with something else agreeing |
| one name ends with the other (`MM-0-n78LC-00` inside `Integrity-MM-0-n78LC-00`) | no - marked **needs a look** |

Strong evidence is always matched first, so a name that merely rhymes can
never take a device away from the record whose serial proves it.

`--setup-diff` is repeatable, and `--setup-diff-csv` writes every bench asked
about into one spreadsheet. Excel opens it directly.

On the map it is **Compare a bench with NetBox**.

## 6h. Which switch port is which, against NetBox

LLDP says `1/1/49`. NetBox says `Port49`. They are the same hole in the same
box, but nothing in either name proves it, and guessing is how somebody ends
up unplugging the wrong cable.

So the tool proposes and shows its working, and **nothing uses the proposal
until a person has confirmed it**:

```
linkfinder <hosts> --netbox --switch-ports
```

```
Switch ports, scan against NetBox:
  rack-sw-7          1/1/12 = port12, 1/1/13 = port13
      every port the scan saw lands on exactly one record, covers 51 port(s)
```

The rule is **per switch**, not per port: one confirmation settles every port
on it, which is why being certain costs a single yes rather than fifty-two.

```
linkfinder <hosts> --netbox --confirm-switch-ports rack-sw-7
linkfinder <hosts> --netbox --confirm-switch-ports "*"     # every certain one
```

On the map the same thing is a button, **Which switch port is which**, with a
Yes beside each switch. Confirmations are kept in the defaults file under
`switch_ports` and apply to every later scan.

A switch that is not in NetBox under the name it announces is reported as
exactly that. It is a finding about the documentation, not a failure.

## 6i. What moved since last time

A cable that moved is invisible in any single scan: the map looks perfectly
reasonable, it is simply not last week's. Answering that needs an earlier map,
so the tool keeps its own.

Every map it draws is kept under `~/.linkfinder-history`, newest twenty, and
an empty or interrupted map is never kept - it must not become the thing a
later scan is judged against.

```
linkfinder --topology 10.0.0.1 -u root -p secret --since-last
```

```
Since the last map (1 device added, 1 device changed, 1 link added):
  device added     rack-sw-9    switch
  link added       core-sw 21 - rack-sw-9 1/1/35
```

Beside the map it is `<map>.moved.csv`. The first scan
says plainly that there is nothing to compare against yet, rather than
pretending nothing changed.

Set `LINKFINDER_HISTORY` to keep the maps somewhere else.

## 6j. The map as a table

The picture answers "how is this connected". A table answers "how many are
there and where", and it is what a spreadsheet, a report or a colleague
outside the team can actually use.

```
linkfinder --topology 10.0.0.1 -u root -p secret --topology-csv site.csv
linkfinder <hosts> --csv scan.csv
```

One row per link, carrying the device, its serial, the setups it belongs to,
the local port, what is on the other end and which port that is. In the window
it is the **Export...** button; on the map, **Download this map as a table**.

You do not have to ask for it. Every walk writes the table, the data and the
page side by side under the same name and says where all three went. The page
is for looking at; the other two are what the work is actually done with, and
needing a dialog to reach them was a step with no purpose. **Save...** still
puts a copy wherever you want, and offers the table first for the same reason.

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
      "id": "chassis:aa:bb:cc:dd:ee:01",
      "kind": "switch",
      "name": "sw-lab-1",
      "chassis_id": "aa:bb:cc:dd:ee:01",
      "chassis_id_raw": "AA-BB-CC-DD-EE-01",
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

One self-contained file. The graph data, the device drawings and `cytoscape.min.js` are
all embedded, because a page opened from disk may not fetch a sibling file and must not
need the network.

Features: zoom, pan, draggable nodes, four layouts, a filter box, line style per link
confidence, interface names at each end of every link, and a panel showing every field of
whatever you click. If the scan was cut short, the header says so.

Each node is drawn as the equipment it is - a rack switch with a port grid, a stack of
server units, a router, a management board, an access point, a desk phone, or a dashed
box with a question mark for an unidentified device. That leaves the border free to carry
state alone: black scanned, red unreachable, dashed and faded for anything never opened.
Unticking **device icons** reverts to the plain shapes, which redraw faster on a very
large map; the data behind both is identical.

The drawings live in `linkfinder/assets/icons/`, one SVG per kind, each cropped by its
`viewBox` to its own content and carrying explicit `width`/`height`. The size attributes
are not decoration - without an intrinsic size the browser reports a zero-sized image and
Cytoscape silently drops the icon at any zoom other than 1.

`icon_uris()` in `render_topology.py` reads each file, substitutes the kind colour for the
`__ACCENT__` placeholder and base64-encodes the result into a data URI. Base64 rather than
inline markup keeps the SVG namespace URL out of the page, so the "nothing here touches
the network" test stays meaningful. The map is kept to one icon per kind rather than per
node, so the cost is about 10 KB however many devices are found.

`tools/make_icons.py` draws the set. It is the source of truth: edit the geometry there
and re-run it rather than hand-editing the SVGs. `KIND_SIZE` in `render_topology.py`
gives each kind its node box, matched to the aspect of its drawing - a switch is wide and
flat because a switch is.

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
    {"user": "siteadmin", "password": "..."}
  ]
}
```

`LINKFINDER_USER`, `LINKFINDER_PASSWORD`, `LINKFINDER_BMC_USER`,
`LINKFINDER_BMC_PASSWORD` and `LINKFINDER_COMMUNITY` are read too. The command line wins
over the environment, which wins over the file.

Nothing is ever sent anywhere. The tool only talks to the targets you name.

### A remembered password is not kept in the file

A per-host password the tool saves for you goes to the store the operating
system already runs, and the file keeps only a handle to it:

| System | Where the password actually lives |
|---|---|
| Windows | DPAPI, keyed to your account. Another account cannot read it, and a tampered blob is refused rather than returned as rubbish |
| macOS | the login keychain |
| Linux with a session keyring | the Secret Service, through `secret-tool` |
| anything else, a server typically | **nowhere** - it is not saved, and the tool says so |

That last row is deliberate. Encrypting a file with a key that ships beside it
is obfuscation: it would read as protection in an audit and stop nobody. Where
there is no store, pass the password each run or use an SSH key.

A file written before this existed still works. The first run moves any
plaintext password into the store and takes the clear copy out of the file,
and says which hosts it moved.

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
1      sw-lab-1     switch  scanned       10.0.0.30      aa:bb:cc:dd:ee:01
1      sw-edge      switch  out-of-scope  10.9.94.1      aa:bb:cc:dd:ee:02
2      sw-access-1  switch  unscanned     -              aa:bb:cc:dd:ee:03
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
If the port is a fronthaul leg there may be no switch at all; check the PTP and Peer
columns, which answer that case without LLDP.

**A neighbour shows an address like `fe80::7a9a:18ff:fe19:1234` and is never scanned**
- that is an IPv6 link-local, the only address that device publishes over LLDP. It is
valid but not routable: without a zone index there is nowhere to connect to, so the
crawl does not waste an attempt on it and the row says so instead of reporting the
device as unreachable. Give the device's real address as a seed, or scan it directly.

**Port health prints "no switch to ask"** - nothing found during the scan published a
usable management address. Name the switch with `--switch`.

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

**A new device kind**: add it to `KIND_STYLE` and `KIND_SIZE` in `render_topology.py`,
draw it in `tools/make_icons.py` and re-run that script. The renderer picks the icon up by
name; an unrecognised kind falls back to the unknown drawing rather than vanishing.

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

The device icons are deliberately not third-party. They are drawn by
`tools/make_icons.py` and covered by the same MIT licence as the rest, so no vendor
stencil set has to be redistributed with the tool.

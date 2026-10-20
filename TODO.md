# Asked for, not yet built

Things raised while using the tool that have not been done. Newest at the top of
each section. When one is finished it moves out of here and into the docs.

## Ports inside a container - done, and what it taught us

Built in `linkfinder/netns.py`. Every Linux host is now checked for network
namespaces holding a port of their own; on the base station this was built
against, 1 of 33 namespaces did, and that one held the whole fronthaul.

Three things were measured first, and each one changed the design:

- **/sys/class/net does not follow `nsenter -n`.** sysfs is filtered by the
  namespace its mount belongs to, so reading it inside a namespace returns
  the root's ports with no error at all. The whole module goes through
  netlink (`ip`) instead. Entering the mount namespace as well is not an
  escape: the container had no `/bin/sh`.

- **The same interface name is a different port in each namespace.** The
  root and the pod both had `ens3f0np0`. The root's was the physical
  function; the pod's was one of its virtual functions - different MAC,
  different ifindex, different PCI address. Ports are therefore keyed
  `<port> in <pod>`, and the bare name is never the identity.

- **A pod's UTS hostname is its pod name.** `nsenter -t <pid> -u hostname`
  turns an inode nobody can read into `bbu-...`, which is what the operator
  already calls it.

The dark port now explains itself rather than being reported as unplugged:
sysfs lists the virtual functions each physical port owns, `ip -d` names the
parent device of every port inside a namespace, and joining those by PCI
address is proof. A name match would have proved nothing, by construction.

Cost on a busy worker: one listing pass, then addresses and neighbour caches
for only the namespaces that hold hardware, then one sysfs read. Three
commands, and a ceiling on how many namespaces are entered.

What is left here:
- `nsenter` needs root. Without it we say ports may be hidden; we do not
  pretend the host had none. A sudo path would widen this.
- The ladder is not run *inside* a namespace yet: a port in there with an
  empty neighbour cache stays unanswered, where an IPv6 query would settle
  it. The one such port found so far had no carrier, so nothing was lost.
- LLDP inside a namespace is untried. `lldpctl` talks to a daemon in the
  root namespace, so it would need a capture, not a query.

## Map

- (nothing outstanding here)

## Getting more out of a port that is UP

A link with a carrier is a device that exists. Today we ask it almost nothing.
What follows is graded by what it costs and what it risks, because some of
these must never be sent.

### Costs nothing, sends nothing - do these first

- **Stop discarding frames on dark ports.** The capture window already runs;
  its filter is LLDP/CDP only, by an old and good decision about BPDUs. On a
  port where LLDP found nothing, recording *every* source MAC seen would name
  the far end for free. Measured caveat: a truly dark fronthaul port sent
  nothing at all in six seconds, so this helps elsewhere, not there.
- **Read the OUI.** The first half of a MAC is a vendor, assigned publicly by
  the IEEE. The lab runs we did today turned up server boards, radio units and
  switches that the tool could only print as hex. Saying "a board by <vendor>"
  or "a radio unit" instead of twelve hex digits costs one lookup table and no
  packets at all. The biggest readability win available, and it works on data
  already collected.
- **Decode what already arrives.** Frames we currently filter out carry more
  than a MAC:
  - **STP/RSTP BPDU** - the far end is a bridge, with its bridge id, priority
    and the port id it sent from.
  - **LACP** - the partner's system MAC and port, even where no bond is set up.
  - **UDLD, CFM/802.1ag CCM, ESMC/SyncE** - device id, port id, maintenance
    domain, clock quality level. ESMC matters in telco: it says what the clock
    on the other end thinks of itself.
  - **EDP, FDP, SONMP/NDP, MNDP, LLTD** - the discovery protocols of vendors
    who are not Cisco.
  - **DHCP, mDNS, NetBIOS, RA** - hostnames, offered by anything chatty.

### One packet, low risk

- **`ff02::2`, all routers** - the same trick as all-nodes, narrowed. A reply
  means the far end routes.
- **mDNS / LLMNR query** - names and services from printers, cameras, Macs,
  BMCs. Nothing telco about it, and it answers "what is this thing".
- **ICMPv6 Node Information Query (RFC 4620)** - literally asks a node its
  name. Rarely implemented; free to try once.
- **802.3ah Link OAM Information PDU** - a peer with OAM enabled answers with
  its OUI and vendor state. Common on carrier gear and on some radio units.

### Must never be sent

Worth writing down so nobody adds them later thinking they are harmless:

- **Any BPDU.** Sending a bridge PDU can win a root election and re-converge
  somebody's spanning tree. Listening is free; speaking is an outage.
- **802.3ah OAM loopback** - puts the far end into loopback and takes the link
  out of service. The Information PDU is fine; the loopback command is not.
- **eCPRI Remote Reset and Remote Memory Access** - exactly what they sound
  like, on a live radio.
- **LACP that could form a bond**, and **LLDP with a shutdown TLV**.

### The shape this should take

One ladder, not a pile of options: what is already known, then what can be
heard, then at most one question per port, with the loudest protocols behind
a flag. Every rung says how it knows, as the IPv6 one already does.

## Joining a guest to its hypervisor

Measured on a KVM host and its two guests, plus an OpenStack instance:

- A guest hears **no LLDP**. A full 35 second capture inside one returned
  nothing, on both of its ports. The OpenStack instance heard nothing either.
  A vSwitch does not pass it and does not generate it.
- The guest is trivially identifiable: `systemd-detect-virt` and
  `/sys/class/dmi/id/sys_vendor` say `kvm` / `QEMU` / `OpenStack Compute`.
  That part is done - a guest is now told what it is instead of being sent to
  look for a switch port it cannot have.
- **The hypervisor holds the answer, and it is exact.** Each guest-facing
  interface carries the guest's own MAC and names its physical parent:

      macvtap0@eno1   52:54:00:d7:97:b0   lower = eno1
      iperf1          52:66:ca:7d:86:65   lower = ens1f1np1, vnet0

  The MAC on the host side *equals* the MAC inside the guest, so the two can
  be joined with no guessing, and `lower_*` walks the rest of the way to the
  physical port the switch reports.

**There are two attachment styles and they join differently.** Measured on
both:

- **macvtap**, as on the KVM host: the host-side interface carries the
  guest's own MAC, so `macvtap0@eno1` matches the guest directly and
  `lower_*` walks to the physical port.
- **a bridge**, as on Proxmox: the tap has a MAC of its own and it is **not**
  the guest's. The guest's MAC is learned by the bridge, so the join is
  `bridge fdb show` -> the port it was learned on -> the bridge -> its
  physical member. Proxmox also encodes the guest id in the name:
  `tap120i0` is VM 120, interface 0, which is free corroboration.

Seen on Proxmox 9.1: `vmbr0` holds `nic8` and `tap120i0`, so a guest on
`vmbr0` leaves through `nic8`, and `nic8` is a real port a switch reports.
Note the interfaces there are renamed `nic0`/`nic8`/`nic13` - any matching
on interface names rather than on `/sys/class/net/*/device` would miss them.

What is left to build: when a guest and its hypervisor are both in one run,
join them and answer the whole question in one line - guest port, host port,
switch, switch port. The machinery for walking `lower_*` already exists for
bonds and VLANs; what is missing is the join and the `bridge fdb` read.

Worth noting for the design: a guest should be drawn on the map behind its
host, not floating. And when only the guest was scanned, the honest output is
the one it now gives - say whose cable it is and stop.

## Watch for

- **`unreachable` on a device we clearly read.** Fixed on 7 Oct: `reachable` was
  derived from whether LLDP resolved, so a host whose optics and sensors we had
  just read still showed as unreachable. Any map generated before that fix still
  shows the old verdict - regenerate before believing it.

## Known gaps, from earlier

- **Cross-device checks do not run on the map.** MTU/VLAN/speed comparison,
  optics matching, duplicate addresses, the PTP clock tree and card temperature
  all live in `_finish_results()`, which only a scan calls. A crawl collects the
  data but never compares across devices.
- **Switch CLI dialects for STP and PTP** are unimplemented: they need logins to
  switches nobody has.
- **The window has never been run from Linux.** Its logic is covered by
  `tests/test_gui.py`, but every run of that suite so far has been on Windows.
- **The open build has no icon of its own.** With no `--icon`, PyInstaller
  gives the console build and the windowed build two different defaults, so
  the two front ends ship looking like unrelated programs. `tools/same_icon.py`
  proves it; it is only wired into the branded build, because the company
  logo can never go in the public repo. Needs its own artwork before release.
- **The map's ssh:// link cannot carry a login.** The window's SSH button now
  uses everything Advanced holds - per-host row, shared login, key file, spare
  logins and the port - but a page can only hand an address to whatever is
  registered for `ssh://`, so a device on a non-standard port or needing a key
  has to be opened from the window. Teaching the page to emit
  `ssh://user@host:port` would cover the port; the key never travels that way.
- **Switch ports could be compared, with one confirmation per switch.**
  Measured 9 Oct 2026 across his merged map: of 21 discovered links landing on a
  switch port, the switch itself is in NetBox under the same name in 7 cases
  but the port NAMES differ - LLDP reports `1/1/13`,
  `Te0/9`, `sfp-sfpplus5`, while NetBox calls them `Port1`..`Port52`. The
  relationship is regular (`1/1/13` <-> `Port13`, `Te0/9` <-> `Port9`,
  `1/1/49` <-> `Port49`) but it is a convention, not a guarantee, and guessing
  is exactly what he ruled out. What makes this worth building: the mapping is
  **per switch, not per port** - one confirmed rule covers all 52 ports - so the
  human cost is tiny and it unlocks "which switch port is this server in,
  really" against the documentation. The server's OWN ports stay unmatchable
  (1.1 % carry a MAC, 1 in 1250 has an OS-style name) and should not be
  attempted. Two of the switches are not in NetBox under the name they announce.
- **A radio unit's serial is still out of reach.** Serial reading is built
  (`linkfinder/hardware.py`) and answers on everything the tool can log into or
  query: Linux over SMBIOS, Windows over `Win32_BIOS`, ESXi over `esxcli`, a BMC
  over Redfish, a switch over ENTITY-MIB - all six proven against real machines.
  What it cannot reach is anything it never talks to. An **RRH/RU** is the clear
  case: it is not an SSH target here, it has no BMC and no SNMP agent we may use,
  and on this bench it is only reachable from inside the BBU pod's network
  namespace. Its serial lives in the BBU's own inventory (`rrhmgr`, the O-RAN
  M-plane `ietf-hardware` tree), so the honest route is to ask the BBU for its
  radios rather than to pretend the tool can ask the radio. Not built; do not
  claim RU serials until it is.
- **Nothing compares the map with an inventory yet.** Now that devices carry a
  serial, the NetBox read becomes worth building: `GET /api/dcim/devices/`,
  `/interfaces/`, `/cables/`, joined on serial, and the only interesting output
  is where the two disagree. Read-only - what the tool measured must never
  overwrite what people recorded.
- **A SuperSpeed port on Windows is still unproven.** Reading handsets is built
  (`linkfinder/handsets.py`), and both halves were measured on real machines: a
  Linux NUC with two phones, an FTDI console cable and a USB 3 LAN adapter, and
  a Windows NUC with four phones. On Linux `/sys/bus/usb/devices/*/serial` holds
  exactly the string `adb devices` prints, with `speed` and `version` beside it.
  On Windows there is no speed property at all, so the generation comes from
  `DEVPKEY_Device_LocationPaths`: `ACPI(HSxx)` is high speed, `ACPI(SSxx)` is
  SuperSpeed. Only the HS half is proven - every port on the Windows bench was
  high speed both times it was measured, so the SS branch has never once been
  taken against real hardware. It is covered by a fixture, not by a machine.

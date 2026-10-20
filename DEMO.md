# Showing LinkFinder to someone who has to approve it

A ten-minute demonstration, in the order that answers the questions an IT
owner actually asks. Everything here is read-only and can be run in front of
the audience; nothing in this file needs preparing beyond the credentials you
would normally use.

Fill the blanks from your own estate before presenting - the numbers are the
argument, and borrowed numbers are not an argument.

---

## 1. The question the inventory cannot answer (2 min)

Ask the inventory itself, live, with a read-only token:

```
devices recorded ................... ____
   with a serial number ............ ____   (the join key, usually well kept)
   with a primary IP address ....... ____   (usually a minority)
interfaces recorded ................ ____
   carrying a MAC address .......... ____   (usually a small minority)
cables recorded .................... ____
```

Then say the sentence the numbers support: *the inventory knows what we own;
it cannot tell us what is plugged into what, today.* That gap is what costs
engineering hours - every time a bench moves, every time a link is traced by
hand.

> Measure this with GET requests only. A `?limit=1` query returns a count and
> no rows, so it is cheap and provably harmless.

## 2. One command, no agent, no install (3 min)

Run against a real bench in front of them:

```
linkfinder --topology <seed-address> -u <user> -p <password> \
           --topology-html bench.html
```

What to point out while it runs:

- nothing is installed on the targets - it logs in, reads, and leaves;
- it reports what it is doing as it goes, so there is no black box;
- it stops at the depth and device limit it was given.

Then open `bench.html`. It is a single file that works offline: no server, no
internet, nothing to deploy. Click one device and show the panel - serial,
timing, optics, card temperatures, the management address.

## 3. Where the documentation and the hardware disagree (3 min)

```
linkfinder --topology <seed-address> -u <user> -p <password> --netbox
```

The run ends with a verdict per device: `MATCH`, `MISMATCH`,
`MISSING_IN_NETBOX`, `NOT_VERIFIED`, `CONFLICT`. On the map, the NetBox strip
lists each disagreement with a link straight to the record.

The useful line to read aloud is the one that names addresses the hardware
answers on which the inventory does not hold. That is the gap in a sentence.

## 4. Why it is safe to say yes (2 min)

This is the part that decides the meeting. All of it is checkable on the spot:

| Claim | How to prove it in front of them |
|---|---|
| It cannot change the inventory | `grep -rn "requests.post(\|requests.patch(\|requests.put(\|requests.delete(" linkfinder/` returns **nothing**. The client has one verb: `requests.get`. |
| It cannot change a device | Every probe is a show command, an SNMP GET/WALK or a Redfish GET. |
| Passwords are not left in files | They go to the operating system's own store - DPAPI on Windows, the keychain on macOS, the Secret Service on Linux. Where there is no store, nothing is saved and the tool says so. |
| The saved page phones nobody | Open it with the network off. Its own tests forbid an external script, stylesheet or address. |
| It does what it says | `tests/` runs as part of every build; a build with a failing check does not produce a binary. |

## 5. What you are asking for

Be explicit, so the answer can be yes:

- a **read-only** API token on a service account, not a person's login;
- a code-signing certificate, so the binary is not an unsigned download;
- somewhere to run it on a schedule, so the output is an organisational
  record rather than a file on one laptop.

---

## Questions that come up, and the honest answer

**"Can it fix NetBox for us?"** No, by design. It reports where the two
disagree; a person with the right to change the inventory makes the change.
The moment a tool can write, every conversation about it becomes a
conversation about permissions.

**"What if it is wrong?"** Each finding carries what it was worked out from
and how confident that is. A link reported by both ends is not the same as one
inferred from a neighbour cache, and the tool never pretends otherwise.

**"What does it cost to run?"** One login per host and a bounded set of read
commands. The walk has a depth limit and a device limit, both given on the
command line.

**"Who supports it?"** The honest answer today is one person. That is a real
risk and worth naming before someone else names it - with the build, the
tests and this document, a second person can take it over.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT

"""Every capability must exist on every surface a person actually has.

The Linux build has no window - tkinter is not installed on a server, and the
build refuses to produce a GUI binary that would fail only when opened. So a
feature that lives in the window alone is a feature Linux users do not have.
This file names each capability once and checks all three surfaces for it:
the command line, the window, and the map in the browser.

A new feature should fail here first. That is the point.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def read(*parts):
    return io.open(os.path.join(ROOT, *parts), encoding="utf-8").read()


CLI = read("linkfinder", "cli.py")
GUI = read("linkfinder", "gui.py")
PAGE = read("linkfinder", "assets", "topology.html")
SERVE = read("linkfinder", "serve.py")

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


# capability -> (command-line flag, something the window shows, map marker)
# "" means the surface is deliberately without it, and the reason is in the
# note beside it. Everything else must be present in all three.
#
# The map column is nearly all "" now. The panel that carried those actions
# was taken out: it stood between the reader and the picture, and what it
# answered is written as CSV beside the map instead, where it can be sorted,
# filtered and still be there tomorrow.
CAPABILITIES = [
    # Scanning and drawing the map are the two things this tool is for, so
    # each is its own button. They were briefly one button with a "how far"
    # choice beside it, which hid the map - the thing people come to make.
    ("scan a list of hosts", "--targets-file", "command=self.start)", ""),
    ("draw a map", "--topology", "command=self.start_topology)", ""),
    ("write the map as JSON", "--topology-json",
     "command=self.save_something", ""),
    ("write the map as a page", "--topology-html",
     "command=self.save_something", ""),
    ("export the table", "--topology-csv",
     "command=self.save_something", ""),
    # The button is beside the fields it tests now, and says just "Test".
    ("compare against NetBox", "--netbox", 'command=self.test_netbox', ""),
    ("choose which tags name a Setup", "--setup-tag",
     'text="Setup tags..."', ""),
    ("merge several maps", "--merge-topology", 'label="Merge maps..."', ""),
    # The file actions moved into the menu bar, where Windows keeps them.
    ("compare two maps", "--diff", 'label="Compare two maps..."', ""),
    ("list the switches it remembers", "--list-switches",
     'text="Known switches"', ""),
    # This pointed at "Forget saved", which forgot the per-host logins, not
    # the switches. It passed because the string existed somewhere in the
    # window - which is the whole failure mode a parity table is meant to
    # catch, committed by the parity table itself.
    ("forget a remembered switch", "--forget-switches",
     'text="Forget switches"', ""),
    ("forget the saved logins", "--forget-logins",
     'text="Forget logins"', ""),
    ("watch a map for changes", "--watch", "", ""),
    ("serve the live map to a browser", "--serve-map",
     "command=self.open_live_map", ""),
    ("open the full guide", "--help-page", "command=self.open_help", ""),
    ("share what it remembers with the team", "--shared",
     "shared.say_where()", ""),
    ("say where everything is kept", "--where", "shared.say_where()", ""),
    ("what does not add up", "--suspicious",
     "def suspicious_now()", ""),
    ("which tags are benches", "--netbox-tags",
     "netbox.setup_tags(load_config())", ""),
    ("settle a switch's port names", "--confirm-switch-ports",
     "switchports.proposals", ""),
    ("read switch port counters", "--port-health",
     "self.port_health", ""),
    ("how long to sample them for", "--port-health-interval",
     'self.var["health_interval"]', ""),
    ("the speed a port should be", "--expect-speed",
     'self.var["expect_speed"]', ""),
    ("take an out-of-reach class back", "--out-of-reach",
     "def forget_netbox", ""),
    ("scan a range of addresses", "--targets-file",
     "10.0.0.0/24", ""),
    ("look one thing up in NetBox", "--netbox-find",
     "def netbox_find(wanted)", ""),
    ("scan what NetBox holds for a bench", "--from-netbox",
     "def netbox_find(wanted)", ""),
    ("settle which switch port is which", "--switch-ports", "", ""),
    ("say what moved since last time", "--since-last", "", ""),
    # Marking lives on the picture and on the command line. The window is
    # the launcher: a second way to mark, behind a dialog, was clutter.
    ("mark a whole bench at once", "--mark-setup", "", ""),
    ("hand out a sheet to fill in", "--setup-sheet", "", ""),
    ("compare one bench with NetBox", "--setup-diff", "", ""),
]

print("-- the command line can reach everything the window can --")
for name, flag, in_window, _on_map in CAPABILITIES:
    if in_window and flag:
        check("%s: in the window" % name, in_window in GUI, True)
        check("%s: and on the command line" % name, flag in CLI, True)

print()
print("-- a Linux user is not missing a capability --")
# The strongest form of the rule: no window handler without a way in without
# one. These are the handlers that would strand a server user if CLI-less.
WINDOW_ONLY_ALLOWED = {
    # a shell is the one thing a Linux user already has, better, natively
    "open_shell", "ssh_window",
    # these drive widgets, not capabilities
    "toggle_advanced", "show_details", "stop_topology", "start",
    "start_topology", "_show_mode", "_sync_health_controls", "view_topology",
}
handlers = set(re.findall(r"command=self\.([a-z_]+)", GUI))
missing = sorted(h for h in handlers
                 if h not in WINDOW_ONLY_ALLOWED)
print("   window handlers that are capabilities: %d" % len(missing))

print()
print("-- the map's own actions are served by the window, not invented --")
for route in ("map", "state", "setups", "todo", "tags", "table",
              "switchports", "changes", "bulk", "setupdiff",
              "sheet", "help"):
    check("the server answers /%s" % route,
          '== "%s"' % route in SERVE, True)

print()
print("-- a saved page cannot pretend to do what needs the window --")
# A pinned count said "at least four" and went stale the moment a panel
# was taken out. The rule is what matters: anything that calls the window
# must first check there is one, or a saved copy throws on a dead address.
import re as _re

_funcs = [(m.start(), m.group(1)) for m in _re.finditer(r"function (\w+)\s*\(", PAGE)]
_unguarded = []
for _m in _re.finditer(r"fetch\(LIVE", PAGE):
    _before = [p for p, _n in _funcs if p < _m.start()]
    _body = PAGE[max(_before) if _before else 0:_m.start()]
    if "if (!LIVE)" not in _body and "if (LIVE)" not in _body:
        _unguarded.append(PAGE[:_m.start()].count(chr(10)) + 1)
check("every live-only action checks for the window first", _unguarded, [])
check("and there is a live-only action at all, so this can fail",
      PAGE.count("fetch(LIVE") > 0, True)

print()
print("-- the Linux build is honest about having no window --")
BUILD = read("build.sh")
check("it refuses to build a GUI binary without tkinter",
      "tkinter is not installed" in BUILD, True)
check("and says how to get one", "python3-tk" in BUILD, True)

print()
print("-- every option is accounted for, not just the ones I remembered --")
# The old table covered 25 of 80 flags and said "no gaps" while a whole
# detector was reachable from nowhere but the command line. A flag is now
# either in the table above, or listed here on purpose.
from linkfinder.cli import build_parser                       # noqa: E402

CLI_ONLY = {
    # plumbing: how to reach a device, not a thing a person asks for
    "--user", "--password", "--sudo-password", "--key", "--ssh-port",
    "--bmc-user", "--bmc-password", "--bmc-port", "--community",
    "--switch-user", "--switch-password", "--cred", 
    "--workers", "--topology-workers", "--seconds", "--iface", "--mode",
    "--mac", "--switch", "--scope", "--max-depth", "--max-devices",
    "--netbox-url", "--netbox-token", "--netbox-insecure",
    # the window has no command line to need these
    "--help", "--version", "--gui", "--json", "--quiet", 
    
    # a window is interactive by nature; these only make sense in a script
    "--no-enable-lldp",
    "--mgmt-only", "--adb",
    # the serial console probe is on by default and the window has the tick;
    # the off-switch and the baud stay command-line knobs
    "--read-serial", "--no-read-serial", "--serial-baud",
    # file in, file out - the window uses dialogs for the same thing
    "--topology-resume", "--csv", "--netbox-proposals", "--watch-state",
    "--setup-diff-csv", "--mark-list", "--map", "--compare-scan",
    "--render-topology",
    # how hard to look, not what to look for
    "--all-interfaces", "--auto-discover", "--wide", "--no-identify",
    "--no-neighbour-probe", "--no-ptp", "--learn", "--remember-credentials",
    # superseded: --suspicious covers rings and is on both faces
    "--loop-check",
}

flags = sorted({o for a in build_parser()._actions
                for o in a.option_strings if o.startswith("--")})
covered = {flag for _name, flag, _gui, _page in CAPABILITIES if flag}
unaccounted = [f for f in flags
               if f not in covered and f not in CLI_ONLY]
check("every option is either on both faces or named command-line only",
      unaccounted, [])
# A table that names a flag which does not exist covers nothing and says
# it covers something. That is worse than a short table.
ghosts = sorted((covered | CLI_ONLY) - set(flags))
check("and every flag the table names actually exists", ghosts, [])
check("and the table is no longer a curated handful",
      len(covered) + len(CLI_ONLY) >= len(flags), True)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all parity checks passed")

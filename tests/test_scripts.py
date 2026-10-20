"""Checks that the shell this tool sends to a host is shell a host can run.

Every probe is a small script typed into a Python string, wrapped in quotes
and handed to a login shell. The parsers that read the output are tested
against saved text, which is worth a lot and cannot catch the one thing
that actually broke twice in a day: the command line itself being
malformed. A canned sample parses perfectly while the script meant to
produce it dies with

    sh: Syntax error: end of file unexpected

and every test stays green.

So this works on the command line as sent, quoting included, in three
passes, and each catches a different kind of damage. A real shell reads
every line without running it, which finds a broken quote or a stray
bracket. Each quoting helper is made to survive a round trip through that
shell, which is where the one that shipped was. Three scripts are then run
against a made-up /sys, which is the only pass that finds a line that
parses perfectly and does the wrong thing.

What none of it covers is real hardware: a kernel that names a file
differently, a tool that prints another way. That still needs a host.

No lab, no passwords, no network. A build that needs a server to be up
goes red for reasons that are not the code.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import handsets, hardware, identify, ipmi, netns, sshx

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-66s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


SH = shutil.which("sh") or shutil.which("bash")
if not SH:
    print("no sh on this machine - cannot check the scripts")
    sys.exit(1)


def shell(args, env=None):
    done = subprocess.run([SH] + args, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, env=env)
    return done.returncode, done.stdout.decode("utf-8", "replace")


def reads(line):
    """Let a real shell parse the command line and run none of it."""
    return shell(["-n", "-c", line])


def runs(line, env=None):
    """Run it for real and give back what it printed."""
    return shell(["-c", line], env=env)[1]


def write(path, text):
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with open(path, "w", newline="\n") as out:
        out.write(text)


# Git Bash on Windows loses the output of a pipeline inside $( ) whenever the
# shell is a child process - `x=$(echo hi | cat)` comes back empty, while the
# same line typed at a prompt is fine. Real hosts do this correctly, so a
# script that uses it is not at fault and must not be rewritten to suit a
# broken local shell. What must not happen is the run going green while the
# check quietly did nothing, so it is measured and announced instead.
PIPES_IN_SUBSTITUTION = runs('printf %s "$(echo hi | cat)"') == "hi"
SKIPPED = []


def skip(name, why):
    print("%-66s skipped" % name)
    SKIPPED.append("%s - %s" % (name, why))


# Every command line this tool sends, built the way the code builds it.
# netns templates two of its scripts, so they are filled in first: the
# unrendered text is not what any host ever sees.
PAIRS = "4026531993:1 4026532518:3417"
LINES = [
    ("sshx.IFACE_SCRIPT", "sh -c %s" % shlex.quote(sshx.IFACE_SCRIPT)),
    ("handsets.USB_SCRIPT",
     "sh -c " + handsets._quote_posix(handsets.USB_SCRIPT)),
    ("hardware.DMI_SCRIPT", "sh -c " + hardware._quote(hardware.DMI_SCRIPT)),
    ("ipmi.SCRIPT", "sh -c " + ipmi._quote(ipmi.SCRIPT)),
    ("netns.LIST_SCRIPT",
     "sh -c " + netns._quote(netns.LIST_SCRIPT % {"cap": netns.MAX_SPACES})),
    ("netns.DETAIL_SCRIPT",
     "sh -c " + netns._quote(netns.DETAIL_SCRIPT % {"pairs": PAIRS})),
    ("netns.VF_SCRIPT", "sh -c " + netns._quote(netns.VF_SCRIPT)),
]

print("-- a real shell can read every command line we send --")
for name, line in LINES:
    code, said = reads(line)
    check("%s parses" % name, code, 0)
    if code:
        FAILURES.append("%s: %s" % (name, said.strip()))

# identify sends a Python program to `python3 -c`, so the shell only has to
# carry it intact; whether it is a program is Python's question to answer.
body = identify.SCRIPT % {"ports": repr(list(identify.PORTS)),
                          "targets": repr(["192.0.2.1"]),
                          "wait": identify.WAIT}
code, said = reads("python3 -c %s" % shlex.quote(body))
check("identify.SCRIPT survives the shell", code, 0)
try:
    compile(body, "identify.SCRIPT", "exec")
    check("identify.SCRIPT is a Python program", True, True)
except SyntaxError as problem:
    check("identify.SCRIPT is a Python program", str(problem), "")

print()
print("-- the quoting itself, there and back through a real shell --")
# This is where it broke. A quoter that mangles an embedded apostrophe
# ships a script no host can read, and nothing downstream notices.
NASTY = "echo 'one' \"two\" $three `four` \\five\n[ -r x ] && echo 'done'\n"
QUOTERS = [
    ("handsets._quote_posix", handsets._quote_posix),
    ("hardware._quote", hardware._quote),
    ("ipmi._quote", ipmi._quote),
    ("netns._quote", netns._quote),
]
for name, quoter in QUOTERS:
    back = runs("printf %%s %s" % quoter(NASTY))
    check("%s hands the text over untouched" % name, back, NASTY)

# A guard rail that cannot fail is not a guard rail. This is the exact
# damage a heredoc does to '\'' - it collapses to ''' - and the test for it
# is the round trip, not the parse: sometimes the quotes still balance and
# the shell reads the line happily while the text arrives mangled.
broken = lambda text: "'%s'" % text.replace("'", "'''")
mangled = runs("printf %%s %s" % broken(NASTY))
check("and a quoter that collapses '\\'' is caught", mangled != NASTY, True)

root = tempfile.mkdtemp(prefix="linkfinder-sys-")
try:
    print()
    print("-- interfaces, read out of a made-up /sys --")
    net = os.path.join(root, "class", "net")
    write(os.path.join(net, "eno1", "address"), "00:00:5e:00:53:01\n")
    write(os.path.join(net, "eno1", "operstate"), "up\n")
    write(os.path.join(net, "eno1", "speed"), "1000\n")
    write(os.path.join(net, "eno1", "carrier"), "1\n")
    write(os.path.join(net, "eno1", "mtu"), "1500\n")
    write(os.path.join(net, "eno1", "device", "uevent"), "x\n")
    write(os.path.join(net, "ens1f0", "address"), "00:00:5e:00:53:0b\n")
    write(os.path.join(net, "ens1f0", "operstate"), "up\n")
    write(os.path.join(net, "ens1f0", "carrier"), "1\n")
    write(os.path.join(net, "ens1f0", "mtu"), "9000\n")
    write(os.path.join(net, "ens1f0", "device", "uevent"), "x\n")
    # A bond leg answers on the bond's address and announces its own.
    write(os.path.join(net, "ens1f0", "bonding_slave", "perm_hwaddr"),
          "00:00:5e:00:53:cc\n")

    here = net.replace(os.sep, "/")
    text = runs("sh -c %s"
                % shlex.quote(sshx.IFACE_SCRIPT.replace("/sys/class/net",
                                                        here)))
    found = sshx.parse_ifaces(text)
    check("both interfaces come back", sorted(found), ["eno1", "ens1f0"])
    check("with the address each answers on",
          found["eno1"].mac, "00:00:5e:00:53:01")
    check("the carrier is read", found["eno1"].carrier, "1")
    check("so is the mtu", str(found["ens1f0"].mtu), "9000")
    check("and a leg's burned-in address survives the round trip",
          found["ens1f0"].perm_mac, "00:00:5e:00:53:cc")

    print()
    print("-- usb, the same way --")
    # Windows cannot hold a colon in a filename, and sysfs names every USB
    # interface `1-2:1.0`. Only that one glob is retargeted; the loop, the
    # reads and the printf run exactly as written.
    usb = os.path.join(root, "bus", "usb", "devices")
    one = os.path.join(usb, "1-2")
    write(os.path.join(one, "idVendor"), "0b05\n")
    write(os.path.join(one, "idProduct"), "19af\n")
    write(os.path.join(one, "manufacturer"), "AsusTek Computer Inc.\n")
    write(os.path.join(one, "product"), "AURA LED Controller\n")
    write(os.path.join(one, "serial"), "9876543210\n")
    write(os.path.join(one, "speed"), "12\n")
    write(os.path.join(one, "version"), " 2.00\n")
    write(os.path.join(one, "bDeviceClass"), "00\n")
    face = os.path.join(one, "1-2@1.0")
    write(os.path.join(face, "bInterfaceClass"), "03\n")
    write(os.path.join(face, "bInterfaceSubClass"), "00\n")
    write(os.path.join(face, "bInterfaceProtocol"), "00\n")

    script = (handsets.USB_SCRIPT
              .replace("/sys/bus/usb/devices", usb.replace(os.sep, "/"))
              .replace('"$d"/*:*', '"$d"/*@*'))
    seen = handsets.parse_usb(runs("sh -c " + handsets._quote_posix(script)))
    check("the device comes back", sorted(seen), ["1-2"])
    check("its maker is read", seen["1-2"].get("maker"),
          "AsusTek Computer Inc.")
    check("its interfaces were actually read",
          seen["1-2"].get("interfaces"), "030000")
    check("so a lighting controller is not called a handset",
          handsets.classify(seen["1-2"]), "peripheral")

    # A deleted `cat` is the shape of damage only a real run finds: the
    # script still parses, the device still appears, and the field it was
    # meant to fill comes back empty.
    hurt = script.replace('$(cat "$i/bInterfaceClass")', "")
    spoilt = handsets.parse_usb(runs("sh -c " + handsets._quote_posix(hurt)))
    check("and a read that quietly stops reading is caught by the output",
          spoilt.get("1-2", {}).get("interfaces") != "030000", True)

    print()
    print("-- the controller, with ipmitool answered by a stand-in --")
    fake = os.path.join(root, "bin")
    os.makedirs(fake)
    write(os.path.join(fake, "ipmitool"),
          "#!/bin/sh\n"
          "cat <<'OUT'\n"
          "Set in Progress         : Set Complete\n"
          "IP Address Source       : Static Address\n"
          "IP Address              : 192.0.2.17\n"
          "Subnet Mask             : 255.255.255.0\n"
          "MAC Address             : 00:00:5e:00:53:1c\n"
          "Default Gateway IP      : 192.0.2.1\n"
          "OUT\n")
    os.chmod(os.path.join(fake, "ipmitool"), 0o755)
    env = os.environ.copy()
    env["PATH"] = fake + os.pathsep + env.get("PATH", "")

    line = "sh -c " + ipmi._quote(ipmi.SCRIPT)
    found = ipmi.parse(runs(line, env=env))
    check("the controller's address survives the whole round trip",
          found.get("address"), "192.0.2.17")
    check("and is not confused with how it was configured",
          found.get("source"), "static")
    check("its hardware address too", found.get("mac"),
          "00:00:5e:00:53:1c")
    check("the channel is named", bool(found.get("channel")), True)
    # Deciding whether to ping means pulling the address back out of the
    # captured text, which is a pipeline inside $( ).
    if PIPES_IN_SUBSTITUTION:
        check("and whether it answered is decided one way or the other",
              "answers" in found, True)
    else:
        skip("and whether it answered is decided one way or the other",
             "this shell loses a pipeline inside $( ) in a child process")

    print()
    print("-- and with no ipmitool it says so, rather than failing --")
    bare = os.environ.copy()
    bare["PATH"] = os.path.dirname(SH).replace(os.sep, "/")
    check("the absent case is reported",
          "IPMI|absent" in runs(line, env=bare), True)
finally:
    shutil.rmtree(root, ignore_errors=True)

print()
if SKIPPED:
    print("%d check(s) this shell cannot carry out:" % len(SKIPPED))
    for line in SKIPPED:
        print("  " + line)
    print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
# The build keeps only the last line of a suite, so a skip that is only
# visible further up is a skip nobody ever sees.
print("all shell-script checks passed"
      + (" (%d skipped, this shell cannot run them)" % len(SKIPPED)
         if SKIPPED else ""))

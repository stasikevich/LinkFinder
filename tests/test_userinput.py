"""What the tool does when an engineer mistypes a setting.
Run: python tests/test_userinput.py

Three outcomes are possible for a field nobody can parse, and two of them
are wrong. Crashing hides the cause, because a windowed build has no
console to crash into. Quietly substituting a default is worse: the run
goes ahead under settings nobody chose. These pin the third - name the
field, quote what was in it, refuse to start.

The fence cases matter most. A control that limits where the tool is
allowed to go must fail closed; one mistyped entry has to stop the run,
never silently become permission to walk anywhere. Two good entries and
one typo must leave no fence standing and no run started, because an
empty fence means no fence at all.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.userinput import (check_walk, cost_note, decimal_number,
                                  networks, settled, whole_number)

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-62s %s" % (label, "ok" if got == want else "FAIL"))


def value(pair):
    return pair[0]


def why(pair):
    return pair[1]


print("-- a field left alone means the default --")
check("empty", whole_number("", "max depth", 2), (2, ""))
check("spaces only", whole_number("   ", "max depth", 2), (2, ""))
check("never filled in at all", whole_number(None, "max depth", 2), (2, ""))
check("and with no default there is simply nothing",
      whole_number("", "max depth"), (None, ""))

print()
print("-- a number that reads as a number --")
check("the plain case", whole_number("4", "max depth"), (4, ""))
check("spaces around it are not the engineer's problem",
      whole_number("  25  ", "max devices"), (25, ""))
check("a signed one", whole_number("+3", "max depth"), (3, ""))
check("both ends of the range are inside it",
      (value(whole_number("1", "max devices")),
       value(whole_number("500", "max devices"))), (1, 500))

print()
print("-- the ways a hurried person types a number wrong --")
check("a letter O instead of a zero",
      whole_number("1O", "max devices"),
      (None, "max devices: '1O' is not a number"))
check("a decimal where a count is wanted",
      whole_number("2.5", "max depth"),
      (None, "max depth: '2.5' is not a whole number"))
check("a comma decimal, as half of Europe writes it",
      whole_number("3,5", "max depth"),
      (None, "max depth: '3,5' is not a whole number"))
check("something that is not a number at all",
      whole_number("soon", "max depth"),
      (None, "max depth: 'soon' is not a number"))
check("hex, which int() will not take either",
      why(whole_number("0x10", "max devices")),
      "max devices: '0x10' is not a number")

print()
print("-- the numbers that would make the run pointless or endless --")
check("zero hops is valid now - scan only the hosts you named",
      whole_number("0", "max depth"), (0, ""))
check("a negative count", why(whole_number("-5", "max devices")),
      "max devices: -5 is outside 1 to 500")
check("one digit too many on the depth, the dangerous typo",
      why(whole_number("88", "max depth")), "max depth: 88 is outside 0 to 8")
check("one past the device cap",
      why(whole_number("501", "max devices")),
      "max devices: 501 is outside 1 to 500")
check("a number far beyond anything meant seriously",
      value(whole_number("1" * 20, "max devices")), None)
check("and the long one still gets an explanation",
      "outside 1 to 500" in why(whole_number("1" * 20, "max devices")), True)

print()
print("-- the one setting that is allowed a fraction --")
check("a fraction of a second", decimal_number("0.5", "reading interval"),
      (0.5, ""))
check("written with a comma", decimal_number("3,5", "reading interval"),
      (3.5, ""))
check("a whole number is a fraction too",
      decimal_number("3", "reading interval"), (3.0, ""))
check("infinity is not an interval",
      why(decimal_number("inf", "reading interval")),
      "reading interval: 'inf' is not a number")
check("neither is nan", value(decimal_number("nan", "reading interval")), None)
check("nor a negative one",
      why(decimal_number("-1", "reading interval")),
      "reading interval: -1 is outside 0.1 to 60.0")
check("zero would spin a loop with no pause",
      value(decimal_number("0", "reading interval")), None)

print()
print("-- the address fence, which must fail closed --")
check("a network", networks("10.0.0.0/24"), (["10.0.0.0/24"], []))
check("several, however they were separated",
      value(networks("10.0.0.0/24, 192.0.2.0/25  198.51.100.0/24")),
      ["10.0.0.0/24", "192.0.2.0/25", "198.51.100.0/24"])
check("a list rather than a line",
      value(networks(["10.0.0.0/24", "192.0.2.0/25"])),
      ["10.0.0.0/24", "192.0.2.0/25"])
check("host bits set is what somebody means, not an error",
      value(networks("203.0.113.5/24")), ["203.0.113.0/24"])
check("IPv6 is a network too", value(networks("2001:db8::/32")),
      ["2001:db8::/32"])
check("nothing typed is not a fence and not an error",
      networks(""), ([], []))
check("nor is a field never filled in", networks(None), ([], []))

print()
print("-- and every way it gets typed wrong --")
check("three octets, the commonest slip",
      networks("203.0.113"),
      ([], ["stay inside: '203.0.113' is not a network"
            " - write a network such as 10.0.0.0/24"]))
check("a backslash, from typing it like a path",
      why(networks("10.0.0.0" + chr(92) + "24"))[0].startswith(
          "stay inside: "), True)
check("a prefix that cannot exist",
      networks("10.0.0.0/33"),
      ([], ["stay inside: '10.0.0.0/33' is not a network"]))
check("a hostname where a network goes",
      value(networks("switch-one.example.com")), [])
check("an address range written with a dash",
      value(networks("10.0.0.1-10.0.0.50")), [])

print()
print("-- one bad entry stops the run; it never widens the fence --")
good_and_bad = networks("10.0.0.0/24 203.0.113 192.0.2.0/25")
check("no fence is handed back", value(good_and_bad), [])
check("only the bad one is complained about", len(why(good_and_bad)), 1)
check("and it names which", "'203.0.113'" in why(good_and_bad)[0], True)

print()
print("-- the map page, checked in one go --")
settings, problems = check_walk("2", "25", "10.0.0.0/24")
check("good settings pass", problems, [])
check("and come back ready to use", settings,
      {"max_depth": 2, "max_devices": 25, "scope": ["10.0.0.0/24"]})
check("the defaults apply when the boxes are empty",
      check_walk("", "", "")[0], {"max_depth": 0, "max_devices": 25,
                                  "scope": []})

two_wrong = check_walk("1O", "0", "203.0.113")
check("every problem is reported at once, not one per attempt",
      len(two_wrong[1]), 3)
check("the depth one is named", "max depth" in two_wrong[1][0], True)
check("the device one too", "max devices" in two_wrong[1][1], True)
check("and the fence", "stay inside" in two_wrong[1][2], True)

print()
print("-- what the settings allow, said before anybody is contacted --")
check("with no fence, that is the loud part",
      cost_note(8, 500, []),
      "up to 500 device(s), 8 hop(s) out from the seed, "
      "with no address fence")
check("with one, it is named",
      cost_note(2, 25, ["10.0.0.0/24"]),
      "up to 25 device(s), 2 hop(s) out from the seed, inside 10.0.0.0/24")

print()
print("-- a value that could not be read never becomes a silent default --")
check("the fallback is the caller's decision, taken in the open",
      settled(whole_number("1O", "max devices"), 25), 25)
check("and a good value is never touched",
      settled(whole_number("30", "max devices"), 25), 30)
check("zero is a value, not a missing one",
      settled(whole_number("0", "expected speed"), 7), 0)

print()
if FAILURES:
    print("FAILURES:")
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all input checks passed")

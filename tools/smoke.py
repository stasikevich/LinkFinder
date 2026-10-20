"""One real scan against one real device, before a release.

Everything else in tests/ runs against stubs and saved maps. That catches
a great deal and cannot catch the thing that matters most: whether the
tool still gets into a machine and comes back with ports.

This is deliberately NOT wired into the build. A build that fails because
a lab host is rebooting teaches people to ignore failures.

    python tools/smoke.py root@10.0.0.5 --password secret

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-58s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


def at_least(name, got, floor):
    ok = got >= floor
    print("%-58s %s (%s)" % (name, "ok" if ok else "FAIL", got))
    if not ok:
        FAILURES.append("%s: got %r, wanted at least %r" % (name, got, floor))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("target", help="an address, or user@address")
    p.add_argument("--password", default="")
    p.add_argument("--seconds", type=int, default=30,
                   help="capture window; short, this is a smoke test")
    args = p.parse_args(argv)

    from linkfinder.engine import (Options, discover_many,
                               targets_with_logins)
    from linkfinder.sshx import Creds

    targets, per_host = targets_with_logins([args.target], "",
                                            log=lambda m: print("   " + m))
    check("the target was understood", len(targets), 1)
    if not targets:
        return 1

    user = ""
    if "@" in args.target:
        user = args.target.split("@", 1)[0]
    opts = Options(ssh=Creds(user=user, password=args.password),
                   per_host=per_host, seconds=args.seconds)

    print()
    print("-- scanning %s for real --" % targets[0])
    start = time.time()
    results = discover_many(targets, opts, log=lambda m: None)
    took = time.time() - start
    print("   took %.1f s" % took)

    check("one result came back", len(results), 1)
    found = results[0] if results else None
    if found is None:
        return 1
    check("it did not error", found.error or "", "")
    if found.error:
        print("   %s" % found.error)
        return 1

    at_least("it found neighbours", len(found.neighbors or []), 1)
    named = [n for n in (found.neighbors or [])
             if getattr(n, "local_iface", "") and getattr(n, "port", "")]
    at_least("and both ends of a link are named", len(named), 1)
    for n in (found.neighbors or [])[:4]:
        print("   %-14s -> %-22s %s"
              % (getattr(n, "local_iface", "?") or "?",
                 getattr(n, "switch", "?") or "-",
                 getattr(n, "port", "?") or "-"))

    print()
    if FAILURES:
        print("%d FAILURES" % len(FAILURES))
        for line in FAILURES:
            print("  " + line)
        return 1
    print("the tool got into a real device and came back with ports")
    return 0


if __name__ == "__main__":
    sys.exit(main())

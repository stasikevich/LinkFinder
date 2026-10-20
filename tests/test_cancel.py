"""Stop must stop: the cancel token wakes a sleep and closes a connection.

The crawl checks its stop_event between devices, but the time went into a
device already being read - a capture window, a blocking SSH read. These check
the token: our own sleeps wake at once, and a registered connection is closed so
the read in flight unblocks. Without a token everything behaves as before.

Run: python tests/test_cancel.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import cancel
from linkfinder import serialports as sp
from linkfinder.sshx import Ssh, Creds

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


print("-- the token: cancelled() and an interruptible sleep --")
check("no token is never cancelled", cancel.cancelled(None), False)
ev = threading.Event()
check("an unset token is not cancelled", cancel.cancelled(ev), False)
ev.set()
check("a set token is cancelled", cancel.cancelled(ev), True)

# A sleep with no token runs to the end; a sleep whose token is already set
# returns at once and says it was cut short.
t = time.time()
cut = cancel.sleep(0.2, None)
check("a plain sleep is not cut short", cut, False)
check("and it actually waited", time.time() - t >= 0.18, True)

ev2 = threading.Event(); ev2.set()
t = time.time()
cut = cancel.sleep(30, ev2)
check("a sleep wakes at once when already cancelled", cut, True)
check("and it did not wait the 30s", time.time() - t < 1.0, True)

# A sleep cancelled from another thread mid-wait wakes within a tick.
ev3 = threading.Event()
threading.Timer(0.3, ev3.set).start()
t = time.time()
cut = cancel.sleep(10, ev3)
check("a sleep wakes when cancelled mid-wait", cut, True)
check("and woke soon after, not at 10s", 0.2 < time.time() - t < 2.0, True)


print()
print("-- close_on_cancel: the connection is closed when Stop fires --")
class FakeConn:
    def __init__(self): self.closed = False
    def close(self): self.closed = True

# Fires: the watcher closes the connection, unblocking a read stuck on it.
conn = FakeConn()
ev4 = threading.Event()
stop_watch = cancel.close_on_cancel(ev4, conn)
check("the connection is open while nothing is cancelled", conn.closed, False)
ev4.set()
time.sleep(0.4)
check("the connection is closed once Stop is pressed", conn.closed, True)
stop_watch()

# Does not fire: finish the work, stop the watcher, the connection is untouched.
conn2 = FakeConn()
ev5 = threading.Event()
stop_watch2 = cancel.close_on_cancel(ev5, conn2)
stop_watch2()
time.sleep(0.2)
check("a connection is left alone when Stop is never pressed", conn2.closed, False)

# No token, nothing to close: a harmless no-op that still hands back a stopper.
check("no token yields a callable no-op", callable(cancel.close_on_cancel(None, conn)),
      True)


print()
print("-- the serial probe stops between ports when cancelled --")
ports = {
    "COM4": {"port": "COM4", "kind": "usb-serial", "likely_radio": True},
    "COM5": {"port": "COM5", "kind": "usb-serial", "likely_radio": True},
}
calls = []
ev6 = threading.Event(); ev6.set()
sp.capture(lambda c: calls.append(c) or "", ports, platform="linux",
           baud=115200, cancel=ev6)
check("an already-cancelled capture opens no port", len(calls), 0)


print()
print("-- a connection closed by Stop fails cleanly, not with AttributeError --")
# When Stop closes the SSH mid-probe, paramiko's transport becomes None and the
# next call raises AttributeError ('NoneType' has no attribute 'open_session').
# That used to surface as the device's error and read like a real fault; the ssh
# methods now catch it and return a plain failure instead.
class _DeadClient:
    def exec_command(self, *a, **k):
        raise AttributeError("'NoneType' object has no attribute 'open_session'")
    def get_transport(self):
        return None

dead = Ssh("10.0.0.1", Creds(user="x", password="y"))
dead.client = _DeadClient()
rc, out, err = dead.run("echo hi")
check("run on a closed connection returns a failure, does not raise", rc, 255)
check("and names it rather than crashing", "open_session" in err, True)
rcb, outb, errb = dead.run_bytes("echo hi")
check("run_bytes is just as safe", rcb, 255)


print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all cancel checks passed")

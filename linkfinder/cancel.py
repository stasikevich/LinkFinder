"""Stop that actually stops: a cancel token threaded through the probes.

Pressing Stop used to only keep the crawl from starting the next device - a
device already being read ran to the end, and a capture window (pktmon 65s, the
LLDP sniffer up to its whole window) had to finish first, so Stop could take a
minute or two. This gives the long waits and the blocking SSH reads a way to be
cut short at once: our own sleeps poll the token, and the SSH connection is
closed when the token fires, which unblocks whatever read is in flight.

The token is an ordinary threading.Event (the crawler's stop_event). Everything
here treats `cancel=None` as "no cancellation", so a probe called without one
behaves exactly as before.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

Cancel = Optional[threading.Event]


def cancelled(cancel: Cancel) -> bool:
    """Whether Stop has been pressed."""
    return cancel is not None and cancel.is_set()


def sleep(seconds: float, cancel: Cancel = None) -> bool:
    """Sleep, but wake at once if cancelled. Returns True when it was cut short."""
    if cancel is None:
        time.sleep(seconds)
        return False
    end = time.time() + seconds
    while True:
        if cancel.is_set():
            return True
        left = end - time.time()
        if left <= 0:
            return False
        time.sleep(min(0.25, left))


def close_on_cancel(cancel: Cancel, closable: Any) -> Callable[[], None]:
    """Close `closable` the moment Stop is pressed, from a watcher thread.

    Closing the SSH client unblocks a read that is stuck waiting on a capture
    window, so the probe returns instead of holding the whole crawl up. Returns a
    function to call when the work is done, which stops the watcher."""
    if cancel is None or closable is None:
        return lambda: None
    done = threading.Event()

    def watch() -> None:
        while not done.is_set():
            if cancel.is_set():
                try:
                    closable.close()
                except Exception:
                    pass
                return
            if done.wait(0.25):
                return

    threading.Thread(target=watch, daemon=True).start()
    return done.set

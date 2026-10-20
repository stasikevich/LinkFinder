"""Run helper processes without flashing a console window.

A PyInstaller --windowed build has no console, so every subprocess Windows
starts gets a brand new one that pops up and disappears. CREATE_NO_WINDOW plus
a hidden STARTUPINFO keeps ping/arp/pktmon invisible.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import subprocess
import sys
from typing import Tuple

CREATE_NO_WINDOW = 0x08000000


def _hidden_kwargs() -> dict:
    if not sys.platform.startswith("win"):
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": info}


def run_quiet(command, timeout: int = 10, shell: bool = False) -> Tuple[int, str]:
    """Return (exit code, stdout+stderr). Never raises, never shows a window."""
    try:
        proc = subprocess.run(command, shell=shell, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, **_hidden_kwargs())
    except Exception as exc:
        return 255, str(exc)
    return proc.returncode, (proc.stdout or b"").decode("utf-8", "replace")

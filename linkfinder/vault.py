"""Where a remembered password is kept, and the refusal when there is nowhere.

A tool that asks for root passwords must not leave them lying in a JSON file,
which is exactly what this one did until now. There are only two honest
answers: hand the secret to a store the operating system already runs and
keys to the logged-in account, or do not keep it at all.

What is deliberately NOT here is a home-made cipher. Encrypting a file with a
key that ships beside it is obfuscation; it would read as protection in an
audit and stop nobody. If this machine has no store, `protect` returns None
and the caller says so out loud.

- Windows: DPAPI (`CryptProtectData`), keyed by the user account, tamper
  evident - a modified blob is refused rather than returned as rubbish.
- macOS: the login keychain through `security`.
- Linux: the Secret Service through `secret-tool`, when a session runs one.
  A headless server usually has none, and that is reported, not faked.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import base64
import os
import platform
import secrets
import subprocess
import sys
from typing import Optional

SERVICE = "linkfinder"
TAG_DPAPI = "dpapi:"
TAG_KEYRING = "keyring:"
TAGS = (TAG_DPAPI, TAG_KEYRING)

# A keychain prompt that waits for a human would hang a scan; every call here
# is non-interactive and a refusal is treated as "no store".
_RUN = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def looks_protected(value: str) -> bool:
    return isinstance(value, str) and value.startswith(TAGS)


# --------------------------------------------------------------- Windows
def _dpapi(seal: bool, data: bytes) -> Optional[bytes]:
    import ctypes
    import ctypes.wintypes as wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    call = (ctypes.windll.crypt32.CryptProtectData if seal
            else ctypes.windll.crypt32.CryptUnprotectData)
    args = ([ctypes.byref(src), SERVICE, None, None, None, 0x01,
             ctypes.byref(out)] if seal else
            [ctypes.byref(src), None, None, None, None, 0x01,
             ctypes.byref(out)])
    if not call(*args):
        return None
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


# ------------------------------------------------------- macOS and Linux
def _tool() -> Optional[str]:
    if sys.platform == "darwin":
        return "security"
    from shutil import which

    return "secret-tool" if which("secret-tool") else None


def _keyring_put(handle: str, secret: str) -> bool:
    tool = _tool()
    if tool == "security":
        done = subprocess.run(
            ["security", "add-generic-password", "-U", "-a", handle,
             "-s", SERVICE, "-w", secret], **_RUN)
        return done.returncode == 0
    if tool == "secret-tool":
        done = subprocess.run(
            ["secret-tool", "store", "--label=LinkFinder",
             "service", SERVICE, "handle", handle],
            input=secret.encode("utf-8"), **_RUN)
        return done.returncode == 0
    return False


def _keyring_get(handle: str) -> Optional[str]:
    tool = _tool()
    if tool == "security":
        done = subprocess.run(
            ["security", "find-generic-password", "-a", handle,
             "-s", SERVICE, "-w"], **_RUN)
        return done.stdout.decode("utf-8").strip() if done.returncode == 0 else None
    if tool == "secret-tool":
        done = subprocess.run(
            ["secret-tool", "lookup", "service", SERVICE, "handle", handle],
            **_RUN)
        if done.returncode == 0 and done.stdout:
            return done.stdout.decode("utf-8")
    return None


# ------------------------------------------------------------- the seam
def available() -> bool:
    """Is there somewhere on this machine a secret may honestly be put."""
    if os.name == "nt":
        try:
            return _dpapi(True, b"probe") is not None
        except Exception:
            return False
    return _tool() is not None


def why_not() -> str:
    """One line naming what is missing, for a person who has to act on it."""
    if available():
        return ""
    if os.name == "nt":
        return "Windows did not answer DPAPI, so nothing was saved"
    if sys.platform == "darwin":
        return "the login keychain did not answer, so nothing was saved"
    return ("this machine runs no secret store, so the password was not saved"
            " - pass it each run, use an SSH key, or install secret-tool"
            " (apt install libsecret-tools)")


def protect(secret: str) -> Optional[str]:
    """Hand the secret to the system store; None when there is no store."""
    if not secret:
        return ""
    try:
        if os.name == "nt":
            sealed = _dpapi(True, secret.encode("utf-8"))
            if sealed is None:
                return None
            return TAG_DPAPI + base64.b64encode(sealed).decode("ascii")
        handle = secrets.token_urlsafe(12)
        if _keyring_put(handle, secret):
            return TAG_KEYRING + handle
    except Exception:
        return None
    return None


def unprotect(value: str) -> str:
    """Read a stored secret back. A value with no tag is from an older file.

    Returning the untagged value unchanged keeps a setup made before this
    existed working; the caller re-saves it protected, which is what finally
    removes the plaintext from disk.
    """
    if not isinstance(value, str) or not value:
        return ""
    try:
        if value.startswith(TAG_DPAPI):
            raw = base64.b64decode(value[len(TAG_DPAPI):].encode("ascii"))
            clear = _dpapi(False, raw)
            return clear.decode("utf-8") if clear else ""
        if value.startswith(TAG_KEYRING):
            return _keyring_get(value[len(TAG_KEYRING):]) or ""
    except Exception:
        return ""
    return value


def forget(value: str) -> None:
    """Drop a keyring entry whose file row is going away. DPAPI holds nothing."""
    if not (isinstance(value, str) and value.startswith(TAG_KEYRING)):
        return
    handle = value[len(TAG_KEYRING):]
    try:
        if _tool() == "security":
            subprocess.run(["security", "delete-generic-password", "-a", handle,
                            "-s", SERVICE], **_RUN)
        elif _tool() == "secret-tool":
            subprocess.run(["secret-tool", "clear", "service", SERVICE,
                            "handle", handle], **_RUN)
    except Exception:
        pass


def describe() -> str:
    """What is holding the passwords, for the log and the window."""
    if not available():
        return "no secret store on this machine"
    if os.name == "nt":
        return "Windows DPAPI, keyed to %s" % (os.environ.get("USERNAME") or "this account")
    if sys.platform == "darwin":
        return "the macOS login keychain"
    return "the system secret store (%s)" % platform.system()

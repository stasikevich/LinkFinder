"""A shell on a device, opened from the map rather than hunted for.

The tool reads; a person fixes. When a map says a port is dark or a clock has
drifted, the next thing anybody does is open a session to that machine - and
then has to find its address again, and its login, in another program. Both are
already here, so the window is here too.

It carries no client of its own: a Windows box has no ssh that can take a
password and PuTTY is not installed everywhere, while paramiko is already
inside this binary on every platform. The password therefore never reaches a
command line, where any other process could read it, and never reaches disk.

What happens in this window is the user acting, not the tool. LinkFinder still
only reads; the line at the top says so, because somebody will ask later.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import queue
import re
import threading
try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:              # a server has no window, and does not need
    tk = ttk = None              # one to use the text handling below

# Colour and cursor moves a plain text widget cannot honour. Stripped rather
# than rendered: this is for reading show commands, not for running an editor.
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[=>]|\r")

READ_SIZE = 65536
POLL_MS = 60


class SshWindow:
    """One shell session in its own window."""

    def __init__(self, parent, host: str, user: str, password: str = "",
                 port: int = 22, title: str = "", note: str = "",
                 key_file: str = "", spares=None) -> None:
        self.host = host
        self.user = user or ""
        self.password = password or ""
        self.key_file = key_file or ""
        # The same logins the scan would fall back on, tried in the same order
        # and only after a real rejection. A window that refuses where a scan
        # succeeds is the sort of difference people stop trusting a tool over.
        self.spares = list(spares or [])
        self.port = port or 22
        self.client = None
        self.channel = None
        self.inbox: "queue.Queue[str]" = queue.Queue()
        self.closing = False

        self.top = tk.Toplevel(parent)
        self.top.title(title or ("%s@%s" % (self.user, host) if self.user else host))
        self.top.geometry("900x560")
        self.top.protocol("WM_DELETE_WINDOW", self.close)

        head = ttk.Frame(self.top, padding=(10, 8, 10, 0))
        head.pack(fill=tk.X)
        self.status = ttk.Label(head, text="connecting to %s..." % host)
        self.status.pack(side=tk.LEFT)
        ttk.Label(head, text="what you type here is you, not the tool",
                  foreground="#718096").pack(side=tk.RIGHT)

        body = ttk.Frame(self.top, padding=10)
        body.pack(fill=tk.BOTH, expand=True)
        self.text = tk.Text(body, wrap=tk.CHAR, font=("Consolas", 10),
                            background="#101418", foreground="#e6edf3",
                            insertbackground="#e6edf3", height=20)
        bar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self.text.yview)
        self.text.configure(yscrollcommand=bar.set, state=tk.DISABLED)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        bar.pack(side=tk.RIGHT, fill=tk.Y)

        foot = ttk.Frame(self.top, padding=(10, 0, 10, 10))
        foot.pack(fill=tk.X)
        self.entry = ttk.Entry(foot)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entry.bind("<Return>", self._send)
        self.entry.bind("<Up>", self._earlier)
        self.entry.bind("<Down>", self._later)
        self.entry.bind("<Control-c>", self._interrupt)
        ttk.Button(foot, text="Send", command=self._send).pack(side=tk.LEFT, padx=(8, 0))
        self.history: list = []
        self.at = 0

        if note:
            self._show(note.rstrip() + "\n\n")
        threading.Thread(target=self._connect, daemon=True).start()
        self.top.after(POLL_MS, self._drain)
        self.entry.focus_set()

    def _logins(self):
        """What to try, best first: what was typed, then the spare logins."""
        out = [(self.user, self.password, self.key_file)]
        for spare in self.spares:
            user = getattr(spare, "user", "") or self.user
            password = getattr(spare, "password", "")
            key = getattr(spare, "key_file", "") or ""
            if (user, password, key) not in out:
                out.append((user, password, key))
        return out

    def _session(self, user, password, key_file):
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        # A key given explicitly is used; without one, fall back to the agent's
        # keys only when there is no password to try.
        client.connect(self.host, port=self.port, username=user or None,
                       password=password or None,
                       key_filename=key_file or None, timeout=20,
                       allow_agent=False,
                       look_for_keys=bool(key_file) or not password)
        return client

    def _connect(self) -> None:
        import paramiko

        client = None
        refused = None
        for user, password, key_file in self._logins():
            try:
                client = self._session(user, password, key_file)
                self.user = user
                break
            except paramiko.AuthenticationException as exc:
                refused = exc
                continue        # a rejection is worth trying the next login on
            except Exception as exc:
                self.inbox.put("\n[could not open a session: %s: %s]\n"
                               % (type(exc).__name__, exc))
                self.inbox.put("\x00failed")
                return
        if client is None:
            tried = len(self._logins())
            self.inbox.put("\n[%d login(s) were refused by %s: %s]\n"
                           % (tried, self.host, refused))
            self.inbox.put("\x00failed")
            return
        try:
            channel = client.invoke_shell(term="vt100", width=110, height=30)
            channel.settimeout(0.2)
        except Exception as exc:
            self.inbox.put("\n[could not open a session: %s: %s]\n"
                           % (type(exc).__name__, exc))
            self.inbox.put("\x00failed")
            return
        self.client, self.channel = client, channel
        self.inbox.put("\x00ready")
        while not self.closing:
            try:
                data = channel.recv(READ_SIZE)
            except Exception:
                if channel.closed or channel.exit_status_ready():
                    break
                continue
            if not data:
                break
            self.inbox.put(data.decode("utf-8", "replace"))
        self.inbox.put("\n[the session ended]\n")
        self.inbox.put("\x00gone")

    def _drain(self) -> None:
        while True:
            try:
                piece = self.inbox.get_nowait()
            except queue.Empty:
                break
            if piece.startswith("\x00"):
                mark = piece[1:]
                self.status.configure(
                    text={"ready": "connected to %s" % self.host,
                          "failed": "not connected",
                          "gone": "the session ended"}.get(mark, mark))
                continue
            self._show(piece)
        if not self.closing:
            self.top.after(POLL_MS, self._drain)

    def _show(self, piece: str) -> None:
        self.text.configure(state=tk.NORMAL)
        self.text.insert(tk.END, ANSI.sub("", piece))
        self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)

    def _send(self, _event=None) -> str:
        line = self.entry.get()
        if self.channel is None:
            return "break"
        try:
            self.channel.send(line + "\n")
        except Exception as exc:
            self._show("\n[could not send: %s]\n" % exc)
            return "break"
        if line.strip():
            self.history.append(line)
        self.at = len(self.history)
        self.entry.delete(0, tk.END)
        return "break"

    def _interrupt(self, _event=None) -> str:
        if self.channel is not None:
            try:
                self.channel.send("\x03")
            except Exception:
                pass
        return "break"

    def _earlier(self, _event=None) -> str:
        if self.history and self.at > 0:
            self.at -= 1
            self.entry.delete(0, tk.END)
            self.entry.insert(0, self.history[self.at])
        return "break"

    def _later(self, _event=None) -> str:
        if self.at < len(self.history) - 1:
            self.at += 1
            self.entry.delete(0, tk.END)
            self.entry.insert(0, self.history[self.at])
        else:
            self.at = len(self.history)
            self.entry.delete(0, tk.END)
        return "break"

    def close(self) -> None:
        self.closing = True
        for shut in (self.channel, self.client):
            try:
                if shut is not None:
                    shut.close()
            except Exception:
                pass
        try:
            self.top.destroy()
        except tk.TclError:
            pass

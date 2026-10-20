"""Becoming root through `su`, for an account that cannot sudo.

An appliance operating account reaches root only with `su` and a different
password. su reads that password from a terminal, not stdin, so the one probe
path that needs a pty - and a pty turns newlines into CRLF and echoes the
prompt. The output is fenced between two markers so what comes back is as
clean as the ordinary no-pty path. Every case here is driven through a fake
channel replaying a real pty transcript, so it needs no bench.

Run: python tests/test_su.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.sshx import Creds, Ssh

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


class FakeChan:
    def __init__(self, transcript):
        self.data = transcript.encode("utf-8")
        self.sent = b""
    def get_pty(self):
        pass
    def settimeout(self, _t):
        pass
    def exec_command(self, cmd):
        self.cmd = cmd
    def sendall(self, b):
        self.sent += b if isinstance(b, bytes) else b.encode("utf-8")
    def recv(self, n):
        out, self.data = self.data[:n], self.data[n:]
        return out


class FakeClient:
    def __init__(self, chan):
        self.chan = chan
    def get_transport(self):
        parent = self
        class T:
            def open_session(self, timeout=None):
                return parent.chan
        return T()


def su(transcript, cmd="ip -o link"):
    s = Ssh.__new__(Ssh)
    s.creds = Creds(user="svc-op", password="secret-login",
                    become_password="secret-root")
    chan = FakeChan(transcript)
    s.client = FakeClient(chan)
    return s._su(cmd), chan


print("-- a clean run through a pty --")
GOOD = ("Password: \r\n__LF_SU_8f3a__\r\n1: lo: <LOOPBACK>\r\n"
        "2: eno1: <UP>\r\n__LF_SU_END_8f3a__0\r\n")
(code, body, err), chan = su(GOOD)
check("the exit code is the command's own", code, 0)
check("nothing is reported on stderr", err, "")
check("the body is only what the command printed, CR stripped",
      body, "1: lo: <LOOPBACK>\n2: eno1: <UP>\n")
check("the prompt and the password echo are not in it",
      "Password" in body or "__LF_SU" in body, False)
check("the password was actually sent", b"secret-root" in chan.sent, True)
check("su was invoked, not sudo", chan.cmd.startswith("su - -c"), True)

print()
print("-- a command that failed under su keeps its exit code --")
(code, body, _e), _c = su(
    "Password: \r\n__LF_SU_8f3a__\r\nbad\r\n__LF_SU_END_8f3a__7\r\n")
check("the non-zero status is carried out", code, 7)
check("with the output it did produce", body, "bad\n")

print()
print("-- a refused su password is a failure, not empty success --")
(code, body, err), _c = su("Password: \r\nsu: Authentication failure\r\n")
check("it is reported as a failure", code != 0, True)
check("and says the password was refused",
      "did not accept the password" in err, True)
check("not as an empty success", (code, body), (code, ""))

print()
print("-- the dispatch: su only for an account that needs it --")
# is_root short-circuits; sudo is for accounts that can; su for the rest.
plain = Ssh.__new__(Ssh)
plain.creds = Creds(user="root", password="x")
plain._is_root = True
check("root runs straight, no escalation at all",
      hasattr(plain, "creds") and plain.creds.become_password, "")
sudoer = Creds(user="admin", password="x")
check("an account with no become password does not go through su",
      sudoer.become_password, "")

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all su-escalation checks passed")

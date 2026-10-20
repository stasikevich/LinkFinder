"""Checks for the built-in shell window - no network, no real host."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


from linkfinder.sshwindow import ANSI

print("-- the window shows output, not the escape codes around it --")
check("colour is stripped", ANSI.sub("", "\x1b[32mUP\x1b[0m"), "UP")
check("a cursor move is stripped", ANSI.sub("", "a\x1b[2Kb"), "ab")
check("a title sequence is stripped",
      ANSI.sub("", "\x1b]0;root@nuc\x07ready"), "ready")
check("a carriage return does not double the lines",
      ANSI.sub("", "line\r\n"), "line\n")
check("ordinary text is untouched",
      ANSI.sub("", "eno1 [mgmt] 1000Mb/s"), "eno1 [mgmt] 1000Mb/s")

print()
print("-- a password is never put anywhere a process could read it --")
import inspect
from linkfinder import sshwindow
SRC = inspect.getsource(sshwindow)
check("no command line is built", "subprocess" in SRC, False)
check("nothing is written to disk", "open(" in SRC, False)
check("the session says whose actions these are",
      "you, not the tool" in SRC, True)

print()
print("-- the window opens from the map, with the login already known --")
try:
    import tkinter as tk
    _probe = tk.Tk(); _probe.destroy()
except Exception as why:
    print("skipped: no window available here (%s)" % type(why).__name__)
else:
    from tkinter import messagebox
    from linkfinder import gui as guimod
    from linkfinder.topology import Node, Topology

    topo = Topology(seed="198.51.100.10")
    topo.nodes["ip:198.51.100.10"] = Node(
        id="ip:198.51.100.10", kind="host", name="node-a",
        mgmt_ips=["198.51.100.10"], state="scanned", depth=0)
    topo.nodes["chassis:aa:bb:cc:00:00:01"] = Node(
        id="chassis:aa:bb:cc:00:00:01", kind="switch", name="sw1",
        chassis_id="aa:bb:cc:00:00:01", state="unscanned", depth=1)

    # Nothing here may stop and wait for a person: a build has nobody to click.

    # The whole family is answered, not the three that happened to come up.

    def _no_dialogs():

        from tkinter import filedialog, messagebox


        for name in ("showinfo", "showwarning", "showerror", "askyesno",

                     "askquestion", "askokcancel", "askretrycancel"):

            if hasattr(messagebox, name):

                setattr(messagebox, name, lambda *_a, **_k: None)

        messagebox.askyesnocancel = lambda *_a, **_k: None

        for name in ("askopenfilename", "asksaveasfilename", "askdirectory"):

            if hasattr(filedialog, name):

                setattr(filedialog, name, lambda *_a, **_k: "")

        filedialog.askopenfilenames = lambda *_a, **_k: ()



    _no_dialogs()

    root = tk.Tk()

    root.withdraw()           # a test needs no window on screen
    root.report_callback_exception = lambda *_a: None
    app = guimod.App(root)
    root.update()
    app._topology_done(topo, "")
    root.update()

    app.var["user"].set("root")
    app.var["password"].set("secret")
    app.var["key"].set("")
    user, password, key_file, spares, port = app._login_for("198.51.100.10")
    check("a host with no login of its own takes the one typed",
          (user, password), ("root", "secret"))
    check("and the ordinary port when nothing says otherwise", port, 22)

    # Everything Advanced holds has to reach the shell, or the window refuses
    # where a scan of the same host succeeds.
    app.var["key"].set("C:/keys/id_ed25519")
    check("the key file from Advanced is handed over",
          app._login_for("198.51.100.10")[2], "C:/keys/id_ed25519")
    app.var["ssh_port"].set("2222")
    check("so is the SSH port", app._login_for("198.51.100.10")[4], 2222)
    app.var["ssh_port"].set("22")

    app.add_extra_row("spare", "spare-pw")
    check("and the spare logins, to try after a rejection",
          [(c.user, c.password) for c in app._login_for("198.51.100.10")[3]],
          [("spare", "spare-pw")])

    app.add_cred_row("198.51.100.10", "admin", "other")
    user, password, _key, _spares, _port = app._login_for("198.51.100.10")
    check("and a host that carries its own login keeps it",
          (user, password), ("admin", "other"))

    print()
    print("-- one appliance on another port does not move everything else --")
    app.add_cred_row("198.51.100.44", "root", "pw", "2222")
    table = app.per_host_table()
    check("the row's own port is used", table["198.51.100.44"].port, 2222)
    check("while the rest stay on 22", table["198.51.100.10"].port, 22)
    check("and the shell is told which",
          app._login_for("198.51.100.44")[4], 2222)

    from linkfinder.engine import per_host_from_config, per_host_to_config
    kept = per_host_to_config(table)
    check("a port is written down", kept["198.51.100.44"].get("port"), 2222)
    check("and an ordinary one is not, so the file stays plain",
          "port" in kept["198.51.100.10"], False)
    check("and it comes back",
          per_host_from_config({"per_host": kept})["198.51.100.44"].port, 2222)

    print()
    print("-- the session itself --")
    from linkfinder.sshwindow import SshWindow
    import inspect as _inspect
    args = _inspect.signature(SshWindow.__init__).parameters
    check("the window takes a port", "port" in args, True)
    check("a key file", "key_file" in args, True)
    check("and the spare logins", "spares" in args, True)
    src = _inspect.getsource(SshWindow)
    check("a rejection moves on to the next login",
          "AuthenticationException" in src, True)
    check("anything else is reported rather than retried for ever",
          src.count("could not open a session") >= 2, True)
    check("the key really is passed to paramiko",
          "key_filename=key_file or None" in src, True)

    told = []
    messagebox.showinfo = lambda title, text, **_k: told.append(text)
    app.topo_tree.selection_remove(app.topo_tree.get_children())
    app.open_ssh()
    check("with nothing picked it asks rather than guessing",
          "Select a device" in (told[-1] if told else ""), True)

    rows = app.topo_tree.get_children()
    switch_row = [r for r in rows
                  if app.topo_rows[r].id.startswith("chassis:")][0]
    app.topo_tree.selection_set(switch_row)
    app.open_ssh()
    check("a device that never published an address is not dialled",
          "did not publish" in (told[-1] if told else "")
          or "publish" in (told[-1] if told else ""), True)
    try:
        root.destroy()
    except tk.TclError:
        pass

print()
if FAILURES:
    print("FAILURES:")
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all shell window checks passed")

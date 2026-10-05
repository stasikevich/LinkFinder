"""Tk window over the same engine the CLI uses.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk, scrolledtext
from typing import List

from . import __author__, __title__, __version__, store
from .cli import _local_label, render_csv
from .engine import Options, discover_many, expand_targets, load_config
from .model import TargetResult, results_to_json
from .sshx import Creds

MODES = ["auto", "ssh", "bmc", "esxi", "windows", "windows-local", "switch", "switch-cli"]
COLUMNS = [("target", "Target", 115), ("iface", "Local port", 105), ("switch", "Switch", 135),
           ("switchip", "Switch IP", 110), ("port", "Switch port", 140),
           ("desc", "Port description", 150), ("vlan", "VLAN", 55), ("via", "Via", 140),
           ("conf", "Confidence", 80)]


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.results: List[TargetResult] = []
        self.messages: "queue.Queue[str]" = queue.Queue()
        root.title("%s %s - which switch port is this plugged into - by %s"
                   % (__title__, __version__, __author__))
        root.geometry("1120x760")

        form = ttk.Frame(root, padding=10)
        form.pack(fill=tk.X)
        for col in range(6):
            form.columnconfigure(col, weight=1 if col in (1, 3, 5) else 0)

        self.var = {}

        def row(label, key, r, c, show=None, default="", width=24):
            ttk.Label(form, text=label).grid(row=r, column=c, sticky=tk.W, padx=(0, 6), pady=3)
            var = tk.StringVar(value=str(self.cfg.get(default, "")) if default else "")
            entry = ttk.Entry(form, textvariable=var, width=width, show=show)
            entry.grid(row=r, column=c + 1, sticky=tk.EW, padx=(0, 14), pady=3)
            self.var[key] = var
            return entry

        ttk.Label(form, text="Targets (IP / name, comma or space separated)",
                  font=("Segoe UI", 9, "bold")).grid(row=0, column=0, columnspan=6, sticky=tk.W)
        self.var["targets"] = tk.StringVar()
        ttk.Entry(form, textvariable=self.var["targets"]).grid(
            row=1, column=0, columnspan=6, sticky=tk.EW, pady=(0, 8))

        row("SSH user", "user", 2, 0, default="user")
        row("SSH password", "password", 2, 2, show="*", default="password")
        row("SSH key file", "key", 2, 4, default="key")
        row("BMC user", "bmc_user", 3, 0, default="bmc_user")
        row("BMC password", "bmc_password", 3, 2, show="*", default="bmc_password")
        row("Switches (SNMP)", "switches", 3, 4)
        self.var["switches"].set(", ".join(self.cfg.get("switches", []) or []))
        row("SNMP community", "community", 4, 0)
        self.var["community"].set(self.cfg.get("community", "public"))
        row("Interfaces (optional)", "ifaces", 4, 2)
        row("MAC (switch-side)", "mac", 4, 4)

        controls = ttk.Frame(root, padding=(10, 0))
        controls.pack(fill=tk.X)
        ttk.Label(controls, text="Mode").pack(side=tk.LEFT)
        self.var["mode"] = tk.StringVar(value="auto")
        ttk.Combobox(controls, textvariable=self.var["mode"], values=MODES, width=14,
                     state="readonly").pack(side=tk.LEFT, padx=(6, 16))
        ttk.Label(controls, text="Capture seconds").pack(side=tk.LEFT)
        self.var["seconds"] = tk.StringVar(value="65")
        ttk.Spinbox(controls, from_=10, to=600, increment=5, width=6,
                    textvariable=self.var["seconds"]).pack(side=tk.LEFT, padx=(6, 16))
        self.mgmt_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="management interface only",
                        variable=self.mgmt_only).pack(side=tk.LEFT, padx=(0, 16))
        self.enable_lldp = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="enable LLDP on the BMC if it is off",
                        variable=self.enable_lldp).pack(side=tk.LEFT, padx=(0, 16))
        self.learn = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="remember switches it finds (writes to disk)",
                        variable=self.learn).pack(side=tk.LEFT)

        topo = ttk.Frame(root, padding=(10, 6, 10, 0))
        topo.pack(fill=tk.X)
        ttk.Label(topo, text="Topology:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
        ttk.Label(topo, text="max depth").pack(side=tk.LEFT, padx=(10, 4))
        self.max_depth = tk.StringVar(value="2")
        ttk.Spinbox(topo, from_=0, to=10, width=4,
                    textvariable=self.max_depth).pack(side=tk.LEFT)
        ttk.Label(topo, text="max devices").pack(side=tk.LEFT, padx=(12, 4))
        self.max_devices = tk.StringVar(value="25")
        ttk.Spinbox(topo, from_=1, to=500, width=5,
                    textvariable=self.max_devices).pack(side=tk.LEFT)
        ttk.Label(topo, text="scope (CIDR)").pack(side=tk.LEFT, padx=(12, 4))
        self.scope = tk.StringVar()
        ttk.Entry(topo, textvariable=self.scope, width=22).pack(side=tk.LEFT)
        self.auto_discover = tk.BooleanVar(value=False)
        ttk.Checkbutton(topo, text="Auto Discovery (do not ask)",
                        variable=self.auto_discover).pack(side=tk.LEFT, padx=(14, 0))

        buttons = ttk.Frame(root, padding=10)
        buttons.pack(fill=tk.X)
        self.btn_run = ttk.Button(buttons, text="FIND SWITCH PORTS", command=self.start)
        self.btn_run.pack(side=tk.LEFT)
        self.btn_topo = ttk.Button(buttons, text="Generate Topology",
                                   command=self.start_topology)
        self.btn_topo.pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="View Topology",
                   command=self.view_topology).pack(side=tk.LEFT)
        self.btn_stop = ttk.Button(buttons, text="Stop", command=self.stop_topology,
                                   state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="Merge topologies",
                   command=self.merge_topologies).pack(side=tk.LEFT, padx=(16, 0))
        self.counter = ttk.Label(buttons, text="")
        self.counter.pack(side=tk.LEFT, padx=12)
        ttk.Button(buttons, text="Export CSV", command=self.export_csv).pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="Export JSON", command=self.export_json).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Known switches",
                   command=self.show_switches).pack(side=tk.LEFT, padx=(16, 0))
        ttk.Button(buttons, text="Forget switches",
                   command=self.forget_switches).pack(side=tk.LEFT, padx=6)
        self.progress = ttk.Progressbar(buttons, mode="indeterminate", length=220)
        self.progress.pack(side=tk.RIGHT)
        ttk.Label(buttons, text="by %s" % __author__,
                  foreground="#718096").pack(side=tk.RIGHT, padx=(0, 12))

        pane = ttk.PanedWindow(root, orient=tk.VERTICAL)
        pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        table_frame = ttk.Frame(pane)
        self.tree = ttk.Treeview(table_frame, columns=[c[0] for c in COLUMNS], show="headings")
        for key, title, width in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=tk.W)
        bar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        pane.add(table_frame, weight=3)

        log_frame = ttk.Frame(pane)
        self.log = scrolledtext.ScrolledText(log_frame, height=12, font=("Consolas", 9),
                                             background="#f6f8fa")
        self.log.pack(fill=tk.BOTH, expand=True)
        pane.add(log_frame, weight=2)

        self.root.after(120, self._drain)

    def say(self, message: str) -> None:
        self.messages.put(message)

    def _drain(self) -> None:
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            self.log.insert(tk.END, message + "\n")
            self.log.see(tk.END)
        self.root.after(120, self._drain)

    def build_options(self) -> Options:
        get = lambda k: self.var[k].get().strip()
        ifaces = [x for x in get("ifaces").replace(",", " ").split() if x] or None
        switches = [x for x in get("switches").replace(",", " ").split() if x]
        return Options(
            mode=get("mode"),
            ssh=Creds(user=get("user"), password=get("password"), key_file=get("key")),
            bmc_user=get("bmc_user"), bmc_password=get("bmc_password"),
            enable_lldp=self.enable_lldp.get(),
            community=get("community") or "public", switches=switches,
            seconds=int(get("seconds") or 65), ifaces=ifaces,
            mgmt_only=self.mgmt_only.get(), mac=get("mac"), learn=self.learn.get())

    def start(self) -> None:
        targets = expand_targets([self.var["targets"].get()])
        if not targets:
            messagebox.showwarning("Nothing to do", "Enter at least one target.")
            return
        self.log.delete(1.0, tk.END)
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.btn_run.configure(state=tk.DISABLED, text="Running…")
        self.progress.start(12)
        opts = self.build_options()
        thread = threading.Thread(target=self._work, args=(targets, opts))
        thread.daemon = True
        thread.start()

    def _work(self, targets, opts) -> None:
        try:
            results = discover_many(targets, opts, log=self.say)
        except Exception as exc:
            self.say("fatal: %s: %s" % (type(exc).__name__, exc))
            results = []
        self.root.after(0, lambda: self._finish(results))

    def _finish(self, results: List[TargetResult]) -> None:
        self.results = results
        rows = 0
        for result in results:
            for nb in result.neighbors:
                self.tree.insert("", tk.END, values=(
                    nb.host, _local_label(nb), nb.switch or "-", nb.mgmt_ip or "-",
                    nb.best_port or "-", nb.port_desc or "-", nb.vlan or "-",
                    ("%s/%s" % (nb.proto, nb.source)) if nb.source else "-", nb.confidence))
                rows += 1 if nb.resolved else 0
        for result in results:
            if not result.resolved:
                self.say("not resolved %s: %s" % (result.target, result.error or "no neighbour"))
                continue
            for nb in result.neighbors:
                if not nb.resolved:
                    self.say("not resolved %s %s: %s"
                             % (result.target, _local_label(nb), nb.note or "no neighbour"))
        self.say("done - %d link(s) resolved" % rows)
        self.progress.stop()
        self.btn_run.configure(state=tk.NORMAL, text="FIND SWITCH PORTS")

    def show_switches(self) -> None:
        for line in store.describe().splitlines():
            self.say(line)

    def forget_switches(self) -> None:
        count = len(store.load())
        if not count:
            messagebox.showinfo("Nothing to forget", "No switches are remembered yet.")
            return
        if messagebox.askyesno("Forget switches",
                               "Forget all %d remembered switch(es)?" % count):
            self.say("forgot %d remembered switch(es)" % store.forget())

    def _confirm_on_main_thread(self, depth, candidates) -> bool:
        """Tk may only be touched from its own thread, so hand the dialog over."""
        answer = {"yes": False}
        done = threading.Event()

        def ask():
            shown = "\n".join("   %-16s %s" % (ip, label) for ip, label in candidates[:15])
            if len(candidates) > 15:
                shown += "\n   ... and %d more" % (len(candidates) - 15)
            answer["yes"] = messagebox.askyesno(
                "Topology discovery",
                "Found %d new network device(s) at depth %d:\n\n%s\n\n"
                "Add and scan discovered neighbours?" % (len(candidates), depth, shown))
            done.set()

        self.root.after(0, ask)
        done.wait()
        return answer["yes"]

    def stop_topology(self) -> None:
        crawler = getattr(self, "crawler", None)
        if crawler is not None:
            crawler.stop()
            self.say("stop requested - finishing the devices already in flight")
            self.btn_stop.configure(state=tk.DISABLED, text="Stopping")

    def merge_topologies(self) -> None:
        from .render_topology import write_html
        from .topology import merge_files

        paths = filedialog.askopenfilenames(title="Pick the topology JSON files to merge",
                                            filetypes=[("JSON", "*.json")])
        if not paths or len(paths) < 2:
            if paths:
                messagebox.showinfo("Merge", "Pick at least two files.")
            return
        out = filedialog.asksaveasfilename(title="Save the merged map as",
                                           defaultextension=".html",
                                           initialfile="site.topology.html",
                                           filetypes=[("HTML", "*.html")])
        if not out:
            return
        try:
            combined = merge_files(list(paths))
            json_path = os.path.splitext(out)[0] + ".json"
            with open(json_path, "w", encoding="utf-8") as handle:
                handle.write(combined.to_json())
            write_html(combined.to_dict(), out)
        except Exception as exc:
            messagebox.showerror("Merge failed", "%s: %s" % (type(exc).__name__, exc))
            return
        self.last_topology_html = out
        self.say("merged %d file(s): %s" % (len(paths), combined.summary()))
        self.say("written to %s and %s" % (json_path, out))
        if messagebox.askyesno("Merged", "Open the combined map?"):
            webbrowser.open("file:///" + out.replace("\\", "/"))

    def _topology_progress(self, done: int, queued: int) -> None:
        self.root.after(0, lambda: self.counter.configure(
            text="scanned %d, queued %d" % (done, queued)))

    def start_topology(self) -> None:
        seeds = expand_targets([self.var["targets"].get()])
        if not seeds:
            messagebox.showwarning("No seed", "Put the seed device address in Targets.")
            return
        path = filedialog.asksaveasfilename(
            title="Save the topology page as", defaultextension=".html",
            initialfile="site.topology.html", filetypes=[("HTML", "*.html")])
        if not path:
            return
        self.log.delete(1.0, tk.END)
        self.btn_topo.configure(state=tk.DISABLED, text="Discovering…")
        self.btn_run.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL, text="Stop")
        self.counter.configure(text="scanned 0")
        self.progress.start(12)
        opts = self.build_options()
        thread = threading.Thread(target=self._topology_work, args=(seeds[0], opts, path))
        thread.daemon = True
        thread.start()

    def _topology_work(self, seed, opts, html_path) -> None:
        from .crawl import Crawler, Limits
        from .render_topology import write_html
        try:
            limits = Limits(max_depth=int(self.max_depth.get() or 2),
                            max_devices=int(self.max_devices.get() or 25),
                            scope=[s for s in self.scope.get().replace(",", " ").split() if s])
            confirm = (lambda _d, _c: True) if self.auto_discover.get() \
                else self._confirm_on_main_thread
            self.crawler = Crawler(opts, limits=limits, confirm=confirm,
                                   log=self.say, progress=self._topology_progress,
                                   workers=4)
            topology = self.crawler.run(seed)
            json_path = os.path.splitext(html_path)[0] + ".json"
            with open(json_path, "w", encoding="utf-8") as handle:
                handle.write(topology.to_json())
            write_html(topology.to_dict(), html_path)
            self.say(topology.summary())
            if topology.stopped_because:
                self.say("stopped because: %s" % topology.stopped_because)
            self.say("topology JSON written to %s" % json_path)
            self.say("topology page written to %s" % html_path)
            self.last_topology_html = html_path
        except Exception as exc:
            self.say("topology failed: %s: %s" % (type(exc).__name__, exc))
            html_path = ""
        self.root.after(0, lambda: self._topology_done(html_path))

    def _topology_done(self, html_path) -> None:
        self.progress.stop()
        self.btn_topo.configure(state=tk.NORMAL, text="Generate Topology")
        self.btn_run.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED, text="Stop")
        self.crawler = None
        if html_path and messagebox.askyesno("Topology ready", "Open it in the browser?"):
            webbrowser.open("file:///" + html_path.replace("\\", "/"))

    def view_topology(self) -> None:
        path = getattr(self, "last_topology_html", "")
        if not path or not os.path.exists(path):
            path = filedialog.askopenfilename(title="Open a topology page",
                                              filetypes=[("HTML", "*.html")])
        if path:
            self.last_topology_html = path
            webbrowser.open("file:///" + path.replace("\\", "/"))

    def export_csv(self) -> None:
        if not self.results:
            messagebox.showinfo("Nothing yet", "Run a discovery first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")])
        if path:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(render_csv(self.results))
            self.say("CSV written to %s" % path)

    def export_json(self) -> None:
        if not self.results:
            messagebox.showinfo("Nothing yet", "Run a discovery first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                            filetypes=[("JSON", "*.json")])
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(results_to_json(self.results))
            self.say("JSON written to %s" % path)


def run_gui() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()

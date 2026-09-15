#!/usr/bin/env python3
import csv
import os
import plistlib
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

APP_NAME = "WTF is system data"

KNOWN_LOCATIONS = [
    ("User caches", "~/Library/Caches", "Usually safe to clear through the owning app; macOS/apps recreate many caches."),
    ("User Application Support", "~/Library/Application Support", "App databases, assets, SDKs, downloaded content. Inspect before deleting."),
    ("User Containers", "~/Library/Containers", "Sandboxed app data. Deleting can reset or break apps."),
    ("Group Containers", "~/Library/Group Containers", "Shared app data. Inspect before deleting."),
    ("User logs", "~/Library/Logs", "Usually low risk, but useful for troubleshooting."),
    ("Saved Application State", "~/Library/Saved Application State", "Usually small; apps recreate it."),
    ("iPhone/iPad backups", "~/Library/Application Support/MobileSync/Backup", "Old local device backups can be very large."),
    ("Mail data", "~/Library/Mail", "Mail messages and attachments. Manage from Mail when possible."),
    ("Messages attachments", "~/Library/Messages/Attachments", "Attachments from Messages. Deleting removes local copies."),
    ("Xcode DerivedData", "~/Library/Developer/Xcode/DerivedData", "Build artifacts. Generally safe to regenerate."),
    ("Xcode Archives", "~/Library/Developer/Xcode/Archives", "Archived builds; keep anything you still need."),
    ("Xcode DeviceSupport", "~/Library/Developer/Xcode/iOS DeviceSupport", "Developer support files for iOS versions/devices."),
    ("CoreSimulator", "~/Library/Developer/CoreSimulator", "Simulator devices and data. Can be very large."),
    ("Docker", "~/Library/Containers/com.docker.docker", "Docker images, volumes, and VM data. Prefer Docker cleanup tools."),
    ("Homebrew cache", "~/Library/Caches/Homebrew", "Downloaded package archives. Usually safe to prune with Homebrew."),
    ("System-wide Application Support", "/Library/Application Support", "Shared app assets and databases. Inspect carefully."),
    ("System-wide caches", "/Library/Caches", "Some entries require admin access. Prefer app/macOS cleanup."),
    ("System-wide logs", "/Library/Logs", "Logs used for diagnostics."),
    ("Private var folders", "/private/var", "Caches, databases, sleep/VM data, updates, and service data. Do not blindly delete."),
    ("Virtual memory", "/private/var/vm", "Swap/sleep data managed by macOS. Do not delete manually."),
    ("Temporary data", "/private/tmp", "Temporary files. macOS normally manages this."),
    ("Shared users", "/Users/Shared", "Installers, app data, and files shared by users."),
]

BROAD_ROOTS = [
    ("Home Library", "~/Library"),
    ("System Library", "/Library"),
    ("Private var", "/private/var"),
    ("Users Shared", "/Users/Shared"),
]

def human_bytes(n):
    if n is None:
        return "Unknown"
    n = float(n)
    units = ["B","KB","MB","GB","TB","PB"]
    i = 0
    while n >= 1024 and i < len(units)-1:
        n /= 1024.0
        i += 1
    if i == 0:
        return f"{int(n)} {units[i]}"
    return f"{n:.2f} {units[i]}"

def expand(path):
    return os.path.abspath(os.path.expanduser(path))

def allocated_size(st):
    blocks = getattr(st, "st_blocks", 0)
    if blocks:
        return blocks * 512
    return st.st_size

def scan_tree(path, cancel_event=None, large_file_threshold=1024**3):
    """Return allocated bytes, inaccessible count, large files.
    Symlinks are not followed. Hard-linked files are counted once per scan.
    """
    total = 0
    denied = 0
    large_files = []
    seen = set()
    stack = [path]

    while stack:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Scan cancelled")
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        st = entry.stat(follow_symlinks=False)
                        key = (st.st_dev, st.st_ino)
                        if key in seen:
                            continue
                        seen.add(key)
                        if entry.is_dir(follow_symlinks=False):
                            # Directory blocks are tiny but real; include them.
                            total += allocated_size(st)
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            sz = allocated_size(st)
                            total += sz
                            if sz >= large_file_threshold:
                                large_files.append((sz, entry.path))
                    except (PermissionError, FileNotFoundError, OSError):
                        denied += 1
        except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
            denied += 1
    large_files.sort(reverse=True)
    return total, denied, large_files

def top_children(path, cancel_event=None):
    results = []
    try:
        with os.scandir(path) as it:
            children = [e for e in it if not e.is_symlink() and e.is_dir(follow_symlinks=False)]
    except Exception:
        return results
    for e in children:
        if cancel_event and cancel_event.is_set():
            break
        try:
            size, denied, _ = scan_tree(e.path, cancel_event, large_file_threshold=10**30)
            results.append((size, denied, e.path))
        except RuntimeError:
            break
        except Exception:
            pass
    results.sort(reverse=True)
    return results

def snapshot_info():
    snapshots = []
    try:
        p = subprocess.run(
            ["/usr/sbin/diskutil", "apfs", "listSnapshots", "/", "-plist"],
            capture_output=True, timeout=20
        )
        if p.returncode == 0 and p.stdout:
            obj = plistlib.loads(p.stdout)
            items = obj.get("Snapshots") or obj.get("APFSSnapshots") or []
            for s in items:
                name = s.get("Name") or s.get("SnapshotName") or s.get("UUID") or "Snapshot"
                size = s.get("Size") or s.get("SnapshotSize")
                purgeable = s.get("Purgeable") or s.get("SnapshotPurgeable")
                snapshots.append((name, size, purgeable))
    except Exception:
        pass

    if not snapshots:
        try:
            p = subprocess.run(
                ["/usr/bin/tmutil", "listlocalsnapshots", "/"],
                capture_output=True, text=True, timeout=20
            )
            if p.returncode == 0:
                for line in p.stdout.splitlines():
                    if "com.apple.TimeMachine" in line:
                        snapshots.append((line.strip(), None, None))
        except Exception:
            pass
    return snapshots

class InspectorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1220x780")
        self.minsize(960, 650)
        self.q = queue.Queue()
        self.cancel_event = threading.Event()
        self.rows = []
        self.large_files = []
        self.scanning = False
        self.deep_scanning = False
        self.deep_cancel_event = threading.Event()
        self.deep_current_path = None
        self._build()
        self.after(150, self._poll)

    def _build(self):
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)

        title = ttk.Label(outer, text=APP_NAME, font=("Helvetica", 22, "bold"))
        title.pack(anchor="w")
        ttk.Label(
            outer,
            text="Itemize storage macOS may lump into “System Data”. Files are never deleted or written to by this app.",
        ).pack(anchor="w", pady=(2, 10))

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=(0, 10))

        self.scan_btn = ttk.Button(toolbar, text="Scan System Data", command=self.start_scan)
        self.scan_btn.pack(side="left")
        self.cancel_btn = ttk.Button(toolbar, text="Cancel", command=self.cancel_scan, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="Export CSV", command=self.export_csv).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="Reveal Selected", command=self.reveal_selected).pack(side="left", padx=(8, 0))

        self.status = ttk.Label(toolbar, text="Ready")
        self.status.pack(side="right")

        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 10))

        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True)

        # Locations tab
        loc_frame = ttk.Frame(self.nb)
        self.nb.add(loc_frame, text="System Data Locations")
        cols = ("size", "category", "path", "access", "guidance")
        self.tree = ttk.Treeview(loc_frame, columns=cols, show="headings")
        headings = {
            "size": "Allocated Size",
            "category": "Category",
            "path": "Path",
            "access": "Access",
            "guidance": "What it is / cleanup guidance",
        }
        widths = {"size": 115, "category": 190, "path": 360, "access": 90, "guidance": 450}
        for c in cols:
            self.tree.heading(c, text=headings[c], command=lambda cc=c: self.sort_tree(self.tree, cc, False))
            self.tree.column(c, width=widths[c], anchor="w")
        ys = ttk.Scrollbar(loc_frame, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(loc_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        loc_frame.rowconfigure(0, weight=1)
        loc_frame.columnconfigure(0, weight=1)

        # Large folders
        folder_frame = ttk.Frame(self.nb)
        self.nb.add(folder_frame, text="Largest Folders")
        self.folder_tree = ttk.Treeview(folder_frame, columns=("size","path","access"), show="headings")
        for c, text, w in [("size","Allocated Size",120),("path","Folder",780),("access","Access",100)]:
            self.folder_tree.heading(c, text=text, command=lambda cc=c: self.sort_tree(self.folder_tree, cc, False))
            self.folder_tree.column(c, width=w, anchor="w")
        fys = ttk.Scrollbar(folder_frame, orient="vertical", command=self.folder_tree.yview)
        self.folder_tree.configure(yscrollcommand=fys.set)
        self.folder_tree.grid(row=0, column=0, sticky="nsew")
        fys.grid(row=0, column=1, sticky="ns")
        folder_frame.rowconfigure(0, weight=1)
        folder_frame.columnconfigure(0, weight=1)

        # Large files
        files_frame = ttk.Frame(self.nb)
        self.nb.add(files_frame, text="Files ≥ 1 GB")
        self.file_tree = ttk.Treeview(files_frame, columns=("size","path"), show="headings")
        self.file_tree.heading("size", text="Allocated Size", command=lambda: self.sort_tree(self.file_tree,"size",False))
        self.file_tree.heading("path", text="File", command=lambda: self.sort_tree(self.file_tree,"path",False))
        self.file_tree.column("size", width=120)
        self.file_tree.column("path", width=900)
        ffys = ttk.Scrollbar(files_frame, orient="vertical", command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=ffys.set)
        self.file_tree.grid(row=0, column=0, sticky="nsew")
        ffys.grid(row=0, column=1, sticky="ns")
        files_frame.rowconfigure(0, weight=1)
        files_frame.columnconfigure(0, weight=1)

        # Snapshots
        snap_frame = ttk.Frame(self.nb, padding=10)
        self.nb.add(snap_frame, text="APFS / Time Machine Snapshots")
        self.snap_text = tk.Text(snap_frame, wrap="word", height=20)
        self.snap_text.pack(fill="both", expand=True)
        self.snap_text.insert("1.0", "Run a scan to inspect local snapshots.\n")
        self.snap_text.configure(state="disabled")

        # Deep Dive tab
        deep_frame = ttk.Frame(self.nb, padding=10)
        self.nb.add(deep_frame, text="Deep Dive")

        # Path controls
        deep_controls = ttk.Frame(deep_frame)
        deep_controls.pack(fill="x", pady=(0, 10))

        ttk.Label(deep_controls, text="Directory:").pack(side="left")

        self.deep_path_var = tk.StringVar()
        self.deep_path_entry = ttk.Entry(
            deep_controls,
            textvariable=self.deep_path_var,
            width=80
        )
        self.deep_path_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))

        ttk.Button(
            deep_controls,
            text="Browse",
            command=self.browse_deep_directory
        ).pack(side="left")

        ttk.Button(
            deep_controls,
            text="Scan",
            command=self.start_deep_scan
        ).pack(side="left", padx=(8, 0))

        ttk.Button(
            deep_controls,
            text="Up",
            command=self.deep_go_up
        ).pack(side="left", padx=(8, 0))

        # Results tree
        deep_cols = ("size", "name", "type", "path", "access")

        self.deep_tree = ttk.Treeview(
            deep_frame,
            columns=deep_cols,
            show="headings"
        )

        deep_headings = {
            "size": "Allocated Size",
            "name": "Name",
            "type": "Type",
            "path": "Path",
            "access": "Access",
        }

        deep_widths = {
            "size": 120,
            "name": 250,
            "type": 80,
            "path": 600,
            "access": 100,
        }

        for c in deep_cols:
            self.deep_tree.heading(
                c,
                text=deep_headings[c],
                command=lambda cc=c: self.sort_tree(
                    self.deep_tree,
                    cc,
                    False
                )
            )

            self.deep_tree.column(
                c,
                width=deep_widths[c],
                anchor="w"
            )

        deep_ys = ttk.Scrollbar(
            deep_frame,
            orient="vertical",
            command=self.deep_tree.yview
        )

        deep_xs = ttk.Scrollbar(
            deep_frame,
            orient="horizontal",
            command=self.deep_tree.xview
        )

        self.deep_tree.configure(
            yscrollcommand=deep_ys.set,
            xscrollcommand=deep_xs.set
        )

        self.deep_tree.pack(
            side="left",
            fill="both",
            expand=True
        )

        deep_ys.pack(
            side="right",
            fill="y"
        )

        # Double-click a directory to scan inside it
        self.deep_tree.bind(
            "<Double-1>",
            self.deep_open_selected
        )

        # Press Return in the path box to scan
        self.deep_path_entry.bind(
            "<Return>",
            lambda event: self.start_deep_scan()
        )
        info = ttk.Label(
            outer,
            text="For the most complete result, grant this app full disk access System Settings → Privacy & Security → Full Disk Access. This app does not write anything to disks.",
            foreground="#555555",
        )
        info.pack(anchor="w", pady=(8, 0))

    def start_scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.cancel_event.clear()
        self.rows.clear()
        self.large_files.clear()
        for t in (self.tree, self.folder_tree, self.file_tree):
            for item in t.get_children():
                t.delete(item)
        self.scan_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress.start(10)
        self.status.config(text="Scanning…")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def cancel_scan(self):
        self.cancel_event.set()
        self.status.config(text="Cancelling…")

    def _scan_worker(self):
        try:
            # Known hotspots
            seen_paths = set()
            total_known = 0
            for i, (cat, raw_path, guidance) in enumerate(KNOWN_LOCATIONS, 1):
                if self.cancel_event.is_set():
                    raise RuntimeError("Scan cancelled")
                path = expand(raw_path)
                if path in seen_paths or not os.path.exists(path):
                    continue
                seen_paths.add(path)
                self.q.put(("status", f"Scanning {path}"))
                size, denied, large = scan_tree(path, self.cancel_event)
                total_known += size
                self.q.put(("known", cat, path, size, denied, guidance))
                for sz, fp in large:
                    self.q.put(("large_file", sz, fp))

            # Broad roots: top-level itemization
            for root_name, raw_root in BROAD_ROOTS:
                if self.cancel_event.is_set():
                    raise RuntimeError("Scan cancelled")
                root = expand(raw_root)
                if not os.path.isdir(root):
                    continue
                self.q.put(("status", f"Itemizing {root}"))
                children = top_children(root, self.cancel_event)
                for size, denied, path in children[:80]:
                    self.q.put(("folder", size, path, denied))

            snaps = snapshot_info()
            self.q.put(("snapshots", snaps))
            self.q.put(("done",))
        except RuntimeError as e:
            self.q.put(("cancelled", str(e)))
        except Exception as e:
            self.q.put(("error", repr(e)))

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "status":
                    self.status.config(text=msg[1])
                elif kind == "known":
                    _, cat, path, size, denied, guidance = msg
                    access = "OK" if denied == 0 else f"{denied} skipped"
                    iid = self.tree.insert("", "end", values=(human_bytes(size), cat, path, access, guidance))
                    self.rows.append((size, cat, path, access, guidance))
                elif kind == "folder":
                    _, size, path, denied = msg
                    access = "OK" if denied == 0 else f"{denied} skipped"
                    self.folder_tree.insert("", "end", values=(human_bytes(size), path, access))
                elif kind == "large_file":
                    _, size, path = msg
                    self.large_files.append((size, path))
                elif kind == "snapshots":
                    self._show_snapshots(msg[1])
                elif kind == "done":
                    self._finish("Scan complete")
                    self._populate_large_files()
                    self._sort_by_size_default()
                elif kind == "cancelled":
                    self._finish("Scan cancelled")
                    self._populate_large_files()
                elif kind == "deep_results":
                    _, path, results = msg

                    self._populate_deep_results(
                        path,
                        results
                    )

                elif kind == "deep_error":
                    _, error = msg

                    self.deep_scanning = False
                    self.progress.stop()
                    self.status.config(
                        text="Deep scan failed"
                    )

                    messagebox.showerror(
                        APP_NAME,
                        error
                    )
                elif kind == "error":
                    self._finish("Scan failed")
                    messagebox.showerror(APP_NAME, msg[1])
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _finish(self, status):
        self.scanning = False
        self.progress.stop()
        self.status.config(text=status)
        self.scan_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")

    def _populate_large_files(self):
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        seen = set()
        for size, path in sorted(self.large_files, reverse=True):
            if path in seen:
                continue
            seen.add(path)
            self.file_tree.insert("", "end", values=(human_bytes(size), path))

    def _sort_by_size_default(self):
        def parse_size(s):
            parts = s.split()
            if len(parts) < 2:
                return 0
            try:
                v = float(parts[0])
            except:
                return 0
            mult = {"B":1,"KB":1024,"MB":1024**2,"GB":1024**3,"TB":1024**4,"PB":1024**5}
            return v * mult.get(parts[1], 1)
        for tree in (self.tree, self.folder_tree, self.file_tree):
            items = list(tree.get_children(""))
            items.sort(key=lambda iid: parse_size(tree.set(iid, "size")), reverse=True)
            for idx, iid in enumerate(items):
                tree.move(iid, "", idx)

    def sort_tree(self, tree, col, reverse):
        def key(iid):
            val = tree.set(iid, col)
            if col == "size":
                units = {"B":1,"KB":1024,"MB":1024**2,"GB":1024**3,"TB":1024**4,"PB":1024**5}
                p = val.split()
                try:
                    return float(p[0]) * units.get(p[1], 1)
                except:
                    return 0
            return val.lower()
        items = list(tree.get_children(""))
        items.sort(key=key, reverse=reverse)
        for i, iid in enumerate(items):
            tree.move(iid, "", i)
        tree.heading(col, command=lambda: self.sort_tree(tree, col, not reverse))

    def browse_deep_directory(self):
        path = filedialog.askdirectory(
            title="Choose a directory to inspect"
        )

        if not path:
            return

        self.deep_path_var.set(path)
        self.start_deep_scan()


    def start_deep_scan(self):
        if self.deep_scanning:
            return

        raw_path = self.deep_path_var.get().strip()

        if not raw_path:
            messagebox.showinfo(
                APP_NAME,
                "Enter or choose a directory first."
            )
            return

        path = expand(raw_path)

        if not os.path.isdir(path):
            messagebox.showerror(
                APP_NAME,
                "That directory does not exist."
            )
            return

        self.deep_current_path = path
        self.deep_path_var.set(path)

        self.deep_scanning = True
        self.deep_cancel_event.clear()

        # Clear old results
        for item in self.deep_tree.get_children():
            self.deep_tree.delete(item)

        self.status.config(
            text=f"Inspecting {path}"
        )

        self.progress.start(10)

        threading.Thread(
            target=self._deep_scan_worker,
            args=(path,),
            daemon=True
        ).start()


    def _deep_scan_worker(self, path):
        try:
            results = []

            with os.scandir(path) as entries:
                for entry in entries:

                    if self.deep_cancel_event.is_set():
                        raise RuntimeError(
                            "Deep scan cancelled"
                        )

                    # Do not follow symbolic links
                    if entry.is_symlink():
                        continue

                    try:
                        if entry.is_dir(
                                follow_symlinks=False
                        ):
                            size, denied, _ = scan_tree(
                                entry.path,
                                self.deep_cancel_event,
                                large_file_threshold=10**30
                            )

                            item_type = "Folder"

                        elif entry.is_file(
                                follow_symlinks=False
                        ):
                            st = entry.stat(
                                follow_symlinks=False
                            )

                            size = allocated_size(st)
                            denied = 0
                            item_type = "File"

                        else:
                            continue

                        results.append(
                            (
                                size,
                                entry.name,
                                item_type,
                                entry.path,
                                denied
                            )
                        )

                    except (
                            PermissionError,
                            FileNotFoundError,
                            OSError
                    ):
                        results.append(
                            (
                                0,
                                entry.name,
                                "Unknown",
                                entry.path,
                                1
                            )
                        )

            # Biggest first
            results.sort(
                key=lambda x: x[0],
                reverse=True
            )

            self.q.put(
                (
                    "deep_results",
                    path,
                    results
                )
            )

        except RuntimeError as e:
            self.q.put(
                (
                    "deep_error",
                    str(e)
                )
            )

        except Exception as e:
            self.q.put(
                (
                    "deep_error",
                    repr(e)
                )
            )


    def _populate_deep_results(
            self,
            path,
            results
    ):
        for item in self.deep_tree.get_children():
            self.deep_tree.delete(item)

        for (
                size,
                name,
                item_type,
                item_path,
                denied
        ) in results:

            access = (
                "OK"
                if denied == 0
                else f"{denied} skipped"
            )

            self.deep_tree.insert(
                "",
                "end",
                values=(
                    human_bytes(size),
                    name,
                    item_type,
                    item_path,
                    access
                )
            )

        self.deep_current_path = path
        self.deep_path_var.set(path)

        self.deep_scanning = False
        self.progress.stop()

        self.status.config(
            text=f"Deep scan complete: {path}"
        )


    def deep_open_selected(self, event=None):
        sel = self.deep_tree.selection()

        if not sel:
            return

        item = sel[0]

        path = self.deep_tree.set(
            item,
            "path"
        )

        item_type = self.deep_tree.set(
            item,
            "type"
        )

        if item_type == "Folder":
            self.deep_path_var.set(path)
            self.start_deep_scan()

        else:
            subprocess.Popen(
                [
                    "/usr/bin/open",
                    "-R",
                    path
                ]
            )


    def deep_go_up(self):
        if not self.deep_current_path:
            return

        parent = os.path.dirname(
            self.deep_current_path
        )

        # Already at filesystem root
        if parent == self.deep_current_path:
            return

        self.deep_path_var.set(parent)
        self.start_deep_scan()

    def reveal_selected(self):
        current_tab = self.nb.index(self.nb.select())

        if current_tab == 0:
            tree = self.tree
            path_col = "path"
        elif current_tab == 1:
            tree = self.folder_tree
            path_col = "path"
        elif current_tab == 2:
            tree = self.file_tree
            path_col = "path"
        elif current_tab == 4:
            tree = self.deep_tree
            path_col = "path"
        else:
            messagebox.showinfo(APP_NAME, "There is nothing to reveal on this tab.")
            return

        sel = tree.selection()

        if not sel:
            messagebox.showinfo(APP_NAME, "Select a path first.")
            return

        path = tree.set(sel[0], path_col)

        if not os.path.exists(path):
            messagebox.showwarning(APP_NAME, "That path no longer exists.")
            return

        subprocess.Popen(["/usr/bin/open", "-R", path])

    def export_csv(self):
        if not self.rows:
            messagebox.showinfo(APP_NAME, "Run a scan first.")
            return
        target = filedialog.asksaveasfilename(
            title="Export scan",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile="system-data-scan.csv",
        )
        if not target:
            return
        with open(target, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Allocated bytes","Allocated size","Category","Path","Access","Guidance"])
            for size, cat, path, access, guidance in sorted(self.rows, reverse=True):
                w.writerow([size, human_bytes(size), cat, path, access, guidance])
        self.status.config(text=f"Exported {target}")

    def _show_snapshots(self, snaps):
        lines = []
        if not snaps:
            lines.append("No local APFS / Time Machine snapshots were returned, or macOS denied access.")
        else:
            lines.append(f"Found {len(snaps)} local snapshot(s).\n")
            for name, size, purgeable in snaps:
                line = f"• {name}"
                if size is not None:
                    line += f" — {human_bytes(size)}"
                if purgeable is not None:
                    line += f" — purgeable: {purgeable}"
                lines.append(line)
            lines.append(
                "\nSnapshot sizes are not exposed consistently across macOS versions. "
                "Snapshots can contribute to storage that Finder categorizes as System Data."
            )
            lines.append(
                "\nDo not delete snapshots blindly. Time Machine/macOS normally manages them; "
                "this app intentionally only inventories them."
            )
        self.snap_text.configure(state="normal")
        self.snap_text.delete("1.0", "end")
        self.snap_text.insert("1.0", "\n".join(lines))
        self.snap_text.configure(state="disabled")

if __name__ == "__main__":
    InspectorApp().mainloop()

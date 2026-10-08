"""A progress window for ``fetch-fulltext``, like Zotero's own "Find Full Text".

Each paper is a row with its status (searching, attached, no file found ...).
As soon as a paper is not found, a button offers the browser step for the
papers not found so far, and the window stays open at the end with a summary,
the same button and the run's report. Double-clicking a paper selects it in
Zotero. The terminal keeps the detailed log.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
from collections.abc import Callable

STATUS_TEXT = {
    "waiting": "Waiting",
    "searching": "Searching…",
    "attached": "✓ Attached",
    "found": "✓ Found",
    "not found": "✗ No file found",
    "waiting for browser": "… Not found yet; the browser step follows",
    "browser": "Searching with the browser…",
    "error": "✗ Error",
    "skipped": "– Skipped",
}
FINISHED = {"attached", "found", "not found", "error", "skipped"}


class Progress:
    """The window's bookkeeping, separate from tkinter so it can be tested."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.labels: dict[str, str] = {}
        self.status: dict[str, str] = {}
        self.detail: dict[str, str] = {}
        self.browser_tried: set[str] = set()
        self.active_runs = 0
        self.browser_busy = False
        self.reports: list[str] = []

    def apply(self, event: dict) -> str | None:
        """Take one event from the fetcher; returns the paper's key when its row changed."""
        key = event.get("key") or ""
        if not key:
            if event.get("status") == "done" and event.get("detail"):
                self.reports.append(event["detail"])
            return None
        if key not in self.status:
            self.order.append(key)
        self.labels[key] = event.get("label") or self.labels.get(key, key)
        self.status[key] = event.get("status", "")
        self.detail[key] = event.get("detail", "")
        if self.status[key] == "browser":
            self.browser_tried.add(key)     # searched with the browser: not offered again
        return key

    def not_found_for_browser(self) -> list[str]:
        return [k for k in self.order if self.status.get(k) == "not found" and k not in self.browser_tried]

    def browser_button(self) -> tuple[bool, str]:
        keys = self.not_found_for_browser()
        n = len(keys)
        text = f"Search {n} not found with the browser" if n else "Search not-found papers with the browser"
        return bool(n) and not self.browser_busy, text

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for k in self.order:
            s = self.status.get(k, "")
            c[s] = c.get(s, 0) + 1
        return c

    def summary(self) -> str:
        c = self.counts()
        total = len(self.order) - c.get("skipped", 0)
        finished = sum(c.get(s, 0) for s in FINISHED) - c.get("skipped", 0)
        attached = c.get("attached", 0) + c.get("found", 0)
        parts = [f"{attached} attached", f"{c.get('not found', 0)} not found"]
        if c.get("error"):
            parts.append(f"{c['error']} error(s)")
        if c.get("skipped"):
            parts.append(f"{c['skipped']} skipped (already a PDF, or no title)")
        head = "Done" if self.active_runs == 0 else f"Searching ({finished} of {total} done)"
        return f"{head}: " + ", ".join(parts) + "."

    def fraction(self) -> float:
        c = self.counts()
        total = len(self.order) - c.get("skipped", 0)
        finished = sum(c.get(s, 0) for s in FINISHED) - c.get("skipped", 0)
        return finished / total if total else 1.0


def run_window(run_kwargs: dict, run: Callable[..., object] | None = None) -> None:
    """Run the fetch in a background thread and show its progress until the window is closed."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    if run is None:
        from zotero_mcp.fulltext_fetch import run as run

    prog = Progress()
    events: queue.Queue = queue.Queue()
    main_uses_browser = "browser" in (run_kwargs.get("steps") or [])

    root = tk.Tk()
    root.title("Find Full Text — zotero-mcp")
    root.geometry("900x440")
    root.minsize(520, 300)
    frame = ttk.Frame(root, padding=10)
    frame.pack(fill="both", expand=True)
    head = ttk.Label(frame, text="Searching…")
    head.pack(anchor="w")
    bar = ttk.Progressbar(frame, mode="determinate", maximum=1000)
    bar.pack(fill="x", pady=(4, 8))
    holder = ttk.Frame(frame)
    holder.pack(fill="both", expand=True)
    tree = ttk.Treeview(holder, columns=("item", "status"), show="headings", height=12)
    tree.heading("item", text="Item")
    tree.heading("status", text="Full Text")
    tree.column("item", width=480, anchor="w", stretch=True)
    tree.column("status", width=370, anchor="w", stretch=True)
    tree.tag_configure("ok", foreground="#1a7f37")
    tree.tag_configure("bad", foreground="#b42318")
    tree.tag_configure("busy", foreground="#555555")
    scroll = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(8, 0))
    hint = ttk.Label(frame, text="Double-click a paper to show it in Zotero. The terminal has the details.",
                     foreground="#666666")
    hint.pack(anchor="w", pady=(6, 0))

    def start(kwargs: dict, uses_browser: bool) -> None:
        prog.active_runs += 1
        if uses_browser:
            prog.browser_busy = True

        def work() -> None:
            try:
                run(progress=events.put, **kwargs)
            except Exception as e:  # shown in the window and the terminal
                print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
                events.put({"key": "", "status": "failed", "detail": f"{type(e).__name__}: {e}"})
            finally:
                events.put({"key": "", "status": "run finished", "browser": uses_browser})

        threading.Thread(target=work, daemon=True).start()

    def on_browser() -> None:
        keys = prog.not_found_for_browser()
        if not keys:
            return
        prog.browser_tried.update(keys)
        for k in keys:
            prog.apply({"key": k, "label": prog.labels.get(k, k), "status": "waiting for browser", "detail": ""})
            redraw(k)
        kwargs = {k: v for k, v in run_kwargs.items() if k not in ("collection", "limit")}
        kwargs.update(keys=keys, steps=["browser"], retry=True, workers=1)
        start(kwargs, True)
        refresh()

    def on_report() -> None:
        if prog.reports:
            try:
                os.startfile(prog.reports[-1])  # type: ignore[attr-defined]  # Windows
            except Exception:
                messagebox.showinfo("Report", prog.reports[-1])

    def on_close() -> None:
        if prog.active_runs and not messagebox.askyesno(
                "Still searching", "A search is still running. Stop it and close? "
                                   "Papers already attached stay attached."):
            return
        root.destroy()

    def on_open(_event=None) -> None:
        sel = tree.selection()
        if sel:
            try:
                os.startfile(f"zotero://select/library/items/{sel[0]}")  # type: ignore[attr-defined]
            except Exception:
                pass

    browser_btn = ttk.Button(buttons, text="Search not-found papers with the browser", command=on_browser)
    browser_btn.pack(side="left")
    ttk.Button(buttons, text="Close", command=on_close).pack(side="right")
    report_btn = ttk.Button(buttons, text="Show report", command=on_report)
    report_btn.pack(side="right", padx=(0, 6))
    tree.bind("<Double-1>", on_open)
    root.protocol("WM_DELETE_WINDOW", on_close)

    def redraw(key: str) -> None:
        status = prog.status.get(key, "")
        text = STATUS_TEXT.get(status, status)
        if prog.detail.get(key) and status in ("attached", "found"):
            text += f" ({prog.detail[key]})"
        tag = "ok" if status in ("attached", "found") else "bad" if status in ("not found", "error") else "busy"
        values = (prog.labels.get(key, key), text)
        if tree.exists(key):
            tree.item(key, values=values, tags=(tag,))
        else:
            tree.insert("", "end", iid=key, values=values, tags=(tag,))
        if status in ("searching", "browser"):
            tree.see(key)

    def refresh() -> None:
        head.configure(text=prog.summary())
        bar.configure(value=int(prog.fraction() * 1000))
        enabled, text = prog.browser_button()
        browser_btn.configure(text=text, state="normal" if enabled else "disabled")
        report_btn.configure(state="normal" if prog.reports else "disabled")

    def poll() -> None:
        finished_now = False
        try:
            while True:
                event = events.get_nowait()
                if event.get("status") == "run finished":
                    prog.active_runs -= 1
                    if event.get("browser"):
                        prog.browser_busy = False
                    finished_now = prog.active_runs == 0
                    continue
                if event.get("status") == "failed":
                    messagebox.showerror("Find full text", event.get("detail", ""))
                    continue
                key = prog.apply(event)
                if key:
                    redraw(key)
        except queue.Empty:
            pass
        refresh()
        if finished_now:
            # The end: bring the window forward with the summary and the browser button.
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            root.after(800, lambda: root.attributes("-topmost", False))
            root.bell()
        root.after(200, poll)

    start(dict(run_kwargs), main_uses_browser)
    root.after(200, poll)
    root.mainloop()

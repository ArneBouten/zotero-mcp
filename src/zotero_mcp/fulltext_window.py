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
    "needs your browser": "⚠ Bot check: open it in your own browser",
    "waiting for your download": "… Waiting for your download (save the PDF to Downloads)",
    "no download": "✗ No download found",
}
FINISHED = {"attached", "found", "not found", "error", "skipped", "needs your browser", "no download"}


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
        self.own_links: dict[str, str] = {}

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
        if self.status[key] == "needs your browser":
            self.own_links[key] = self.detail[key]
        return key

    def for_own_browser(self) -> list[str]:
        return [k for k in self.order if self.status.get(k) == "needs your browser" and k in self.own_links]

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
        parts = [f"{attached} attached", f"{c.get('not found', 0) + c.get('no download', 0)} not found"]
        if c.get("needs your browser"):
            parts.append(f"{c['needs your browser']} for your own browser")
        if c.get("error"):
            parts.append(f"{c['error']} error(s)")
        if c.get("skipped"):
            parts.append(f"{c['skipped']} skipped (already a PDF, or no title)")
        head = "Done" if self.active_runs == 0 else f"Searching ({finished} of {total} done)"
        return f"{head}: " + ", ".join(parts) + "."

    def headline(self) -> str:
        """The line under the window's title; the counts are in the coloured labels beside it."""
        c = self.counts()
        total = len(self.order) - c.get("skipped", 0)
        finished = sum(c.get(s, 0) for s in FINISHED) - c.get("skipped", 0)
        if self.active_runs:
            text = f"Searching… {finished} of {total} paper{'s' if total != 1 else ''} done"
        else:
            text = f"Done: {total} paper{'s' if total != 1 else ''}"
        if c.get("error"):
            text += f", {c['error']} with an error"
        if c.get("skipped"):
            text += f" ({c['skipped']} skipped: already a PDF, or no title)"
        return text + ("" if self.active_runs else ".")

    def fraction(self) -> float:
        c = self.counts()
        total = len(self.order) - c.get("skipped", 0)
        finished = sum(c.get(s, 0) for s in FINISHED) - c.get("skipped", 0)
        return finished / total if total else 1.0


def _style(root, ttk) -> dict:
    """A light, flat look: the clam theme (which takes colours on every platform), the system's UI font."""
    from tkinter import font as tkfont

    ui = {"bg": "#f6f7f9", "card": "#ffffff", "text": "#1f2937", "muted": "#6b7280", "accent": "#2563eb",
          "accent_dark": "#1d4ed8", "ok": "#15803d", "bad": "#b91c1c", "warn": "#b45309", "border": "#e5e7eb",
          "stripe": "#f9fafb", "select": "#dbeafe", "button": "#ffffff", "button_hover": "#f3f4f6"}
    families = set(tkfont.families(root))
    family = next((f for f in ("Segoe UI", "Inter", "Helvetica Neue", "DejaVu Sans") if f in families), None)
    base = tkfont.nametofont("TkDefaultFont")
    if family:
        base.configure(family=family)
    base.configure(size=10)
    for name in ("TkTextFont", "TkHeadingFont", "TkMenuFont"):
        try:
            tkfont.nametofont(name).configure(family=base.cget("family"), size=10)
        except Exception:
            pass
    fam = base.cget("family")
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(".", background=ui["bg"], foreground=ui["text"], font=(fam, 10))
    style.configure("App.TFrame", background=ui["bg"])
    style.configure("Card.TFrame", background=ui["border"])
    style.configure("Title.TLabel", background=ui["bg"], foreground=ui["text"], font=(fam, 15, "bold"))
    style.configure("Muted.TLabel", background=ui["bg"], foreground=ui["muted"])
    style.configure("Hint.TLabel", background=ui["bg"], foreground=ui["muted"], font=(fam, 9))
    for name, colour, tint in (("ChipOk", ui["ok"], "#dcfce7"), ("ChipWarn", ui["warn"], "#fef3c7"),
                               ("ChipBad", ui["bad"], "#fee2e2")):
        style.configure(f"{name}.TLabel", background=tint, foreground=colour, padding=(10, 3),
                        font=(fam, 9, "bold"))
    style.configure("Papers.Treeview", background=ui["card"], fieldbackground=ui["card"], foreground=ui["text"],
                    rowheight=28, borderwidth=0, relief="flat")
    style.map("Papers.Treeview", background=[("selected", ui["select"])], foreground=[("selected", ui["text"])])
    style.configure("Papers.Treeview.Heading", background=ui["bg"], foreground=ui["muted"], relief="flat",
                    borderwidth=0, padding=(6, 6), font=(fam, 9, "bold"))
    style.map("Papers.Treeview.Heading", background=[("active", ui["bg"])])
    style.layout("Papers.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Thin.Horizontal.TProgressbar", troughcolor=ui["border"], background=ui["accent"],
                    bordercolor=ui["border"], lightcolor=ui["accent"], darkcolor=ui["accent"], thickness=6)
    style.configure("Done.Thin.Horizontal.TProgressbar", troughcolor=ui["border"], background=ui["ok"],
                    bordercolor=ui["border"], lightcolor=ui["ok"], darkcolor=ui["ok"], thickness=6)
    style.configure("TButton", background=ui["button"], foreground=ui["text"], bordercolor=ui["border"],
                    lightcolor=ui["button"], darkcolor=ui["button"], focuscolor=ui["button"], padding=(12, 5),
                    relief="flat")
    style.map("TButton", background=[("disabled", ui["bg"]), ("active", ui["button_hover"])],
              foreground=[("disabled", "#9ca3af")], lightcolor=[("active", ui["button_hover"])],
              darkcolor=[("active", ui["button_hover"])])
    style.configure("Accent.TButton", background=ui["accent"], foreground="#ffffff", bordercolor=ui["accent"],
                    lightcolor=ui["accent"], darkcolor=ui["accent"], focuscolor=ui["accent"],
                    font=(fam, 10, "bold"))
    style.map("Accent.TButton", background=[("active", ui["accent_dark"])],
              lightcolor=[("active", ui["accent_dark"])], darkcolor=[("active", ui["accent_dark"])])
    style.configure("Vertical.TScrollbar", background="#e5e7eb", troughcolor=ui["card"], bordercolor=ui["card"],
                    arrowcolor=ui["muted"], lightcolor="#e5e7eb", darkcolor="#e5e7eb", gripcount=0)
    return ui


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
    root.geometry("940x500")
    root.minsize(560, 320)
    ui = _style(root, ttk)
    root.configure(background=ui["bg"])

    frame = ttk.Frame(root, padding=(18, 14, 18, 12), style="App.TFrame")
    frame.pack(fill="both", expand=True)
    top = ttk.Frame(frame, style="App.TFrame")
    top.pack(fill="x")
    titles = ttk.Frame(top, style="App.TFrame")
    titles.pack(side="left", fill="x", expand=True)
    ttk.Label(titles, text="Find Full Text", style="Title.TLabel").pack(anchor="w")
    head = ttk.Label(titles, text="Searching…", style="Muted.TLabel")
    head.pack(anchor="w", pady=(2, 0))
    chips_box = ttk.Frame(top, style="App.TFrame")
    chips_box.pack(side="right", anchor="s")
    chips = {}
    for col, (name, style) in enumerate((("attached", "ChipOk"), ("own", "ChipWarn"), ("not found", "ChipBad"))):
        chips[name] = ttk.Label(chips_box, text="", style=f"{style}.TLabel")
        chips[name].grid(row=0, column=col, padx=(6, 0))
        chips[name].grid_remove()
    bar = ttk.Progressbar(frame, mode="determinate", maximum=1000, style="Thin.Horizontal.TProgressbar")
    bar.pack(fill="x", pady=(10, 12))

    # The buttons and the hint first, at the bottom: a small window shrinks the list, never hides them.
    buttons = ttk.Frame(frame, style="App.TFrame")
    hint = ttk.Label(frame, text="Double-click a paper to show it in Zotero. Papers behind a bot check open in "
                                 "your own browser: save the PDF to Downloads and it is attached.",
                     style="Hint.TLabel", wraplength=860, justify="left")
    hint.pack(side="bottom", anchor="w", fill="x", pady=(10, 0))
    buttons.pack(side="bottom", fill="x", pady=(12, 0))
    hint.bind("<Configure>", lambda e: hint.configure(wraplength=max(300, e.width - 10)))
    holder = ttk.Frame(frame, style="Card.TFrame", padding=1)
    holder.pack(fill="both", expand=True)
    tree = ttk.Treeview(holder, columns=("item", "status"), show="headings", height=8, style="Papers.Treeview")
    tree.heading("item", text="Paper", anchor="w")
    tree.heading("status", text="Full text", anchor="w")
    tree.column("item", width=500, anchor="w", stretch=True)
    tree.column("status", width=380, anchor="w", stretch=True)
    tree.tag_configure("ok", foreground=ui["ok"])
    tree.tag_configure("bad", foreground=ui["bad"])
    tree.tag_configure("warn", foreground=ui["warn"])
    tree.tag_configure("busy", foreground=ui["muted"])
    tree.tag_configure("stripe", background=ui["stripe"])
    scroll = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)

    def on_scroll(first: str, last: str) -> None:
        # Only shown when the list is longer than the window.
        if float(first) <= 0 and float(last) >= 1:
            scroll.pack_forget()
        elif not scroll.winfo_ismapped():
            scroll.pack(side="right", fill="y", before=tree)
        scroll.set(first, last)

    tree.configure(yscrollcommand=on_scroll)
    tree.pack(side="left", fill="both", expand=True)

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

    def on_own_browser() -> None:
        keys = prog.for_own_browser()[:5]
        if not keys:
            return
        import webbrowser

        started = __import__("time").time() - 5
        for k in keys:
            webbrowser.open(prog.own_links[k])
            prog.apply({"key": k, "label": prog.labels.get(k, k), "status": "waiting for your download",
                        "detail": prog.own_links[k]})
            redraw(k)
        prog.active_runs += 1

        def watch() -> None:
            from zotero_mcp import fulltext_fetch as ff

            try:
                got = ff.watch_downloads(
                    keys, since=started, on_found=lambda key, detail: events.put(
                        {"key": key, "label": prog.labels.get(key, key), "status": "attached", "detail": detail}))
                for k in keys:
                    if k not in got:
                        events.put({"key": k, "label": prog.labels.get(k, k), "status": "no download",
                                    "detail": "nothing matching appeared in Downloads"})
            except Exception as e:
                events.put({"key": "", "status": "failed", "detail": f"{type(e).__name__}: {e}"})
            finally:
                events.put({"key": "", "status": "run finished", "browser": False})

        threading.Thread(target=watch, daemon=True).start()
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

    own_btn = ttk.Button(buttons, text="Open in my own browser", command=on_own_browser, style="Accent.TButton")
    own_btn.pack(side="left")
    browser_btn = ttk.Button(buttons, text="Search not-found papers with the browser", command=on_browser)
    browser_btn.pack(side="left", padx=(8, 0))
    ttk.Button(buttons, text="Close", command=on_close).pack(side="right")
    report_btn = ttk.Button(buttons, text="Show report", command=on_report)
    report_btn.pack(side="right", padx=(0, 8))
    tree.bind("<Double-1>", on_open)
    root.protocol("WM_DELETE_WINDOW", on_close)

    def redraw(key: str) -> None:
        status = prog.status.get(key, "")
        text = STATUS_TEXT.get(status, status)
        if prog.detail.get(key) and status in ("attached", "found"):
            text += f" ({prog.detail[key]})"
        tag = "ok" if status in ("attached", "found") else \
            "bad" if status in ("not found", "error", "no download") else \
            "warn" if status in ("needs your browser", "waiting for your download") else "busy"
        values = (prog.labels.get(key, key), text)
        if tree.exists(key):
            stripe = "stripe" in tree.item(key, "tags")
            tree.item(key, values=values, tags=(tag, "stripe") if stripe else (tag,))
        else:
            stripe = len(tree.get_children()) % 2 == 1
            tree.insert("", "end", iid=key, values=values, tags=(tag, "stripe") if stripe else (tag,))
        if status in ("searching", "browser"):
            tree.see(key)

    def refresh() -> None:
        head.configure(text=prog.headline())
        bar.configure(value=int(prog.fraction() * 1000))
        enabled, text = prog.browser_button()
        browser_btn.configure(text=text, state="normal" if enabled else "disabled")
        report_btn.configure(state="normal" if prog.reports else "disabled")
        own = prog.for_own_browser()
        own_btn.configure(text=f"Open {min(len(own), 5)} in my own browser" if own else "Open in my own browser",
                          state="normal" if own else "disabled")
        if own:
            own_btn.pack(side="left", before=browser_btn, padx=(0, 8))
        else:
            own_btn.pack_forget()
        c = prog.counts()
        for name, n, word in (("attached", c.get("attached", 0) + c.get("found", 0), "✓ {} attached"),
                              ("own", c.get("needs your browser", 0) + c.get("waiting for your download", 0),
                               "⚠ {} for your browser"),
                              ("not found", c.get("not found", 0) + c.get("no download", 0), "✗ {} not found")):
            if n:
                chips[name].configure(text=word.format(n))
                chips[name].grid()
            else:
                chips[name].grid_remove()
        done = prog.active_runs == 0
        bar.configure(style="Done.Thin.Horizontal.TProgressbar" if done else "Thin.Horizontal.TProgressbar")

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

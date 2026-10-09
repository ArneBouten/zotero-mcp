"""A progress window for ``fetch-fulltext`` and ``maintain``, like Zotero's own "Find Full Text".

Each paper is a row. For ``fetch-fulltext`` the row shows its full-text status
(searching, attached, no file found ...); for ``maintain`` also its metadata
(filled, corrected, to review, wrong PDF ...), with the steps (metadata, PDFs,
metadata again) above the list. As soon as a paper is not found, a button
offers the browser step for the papers not found so far; papers behind a bot
check get a button that opens them in the user's own browser. The window stays
open at the end with a summary, those buttons and the run's reports.
Double-clicking a paper selects it in Zotero. The terminal keeps the detailed log.
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
    "not found": "✗ Not found",
    "waiting for browser": "… Browser next",
    "browser": "Searching (browser)…",
    "error": "✗ Error",
    "skipped": "– Skipped",
    "needs your browser": "⚠ Bot check",
    "waiting for your download": "… Waiting for your download",
    "no download": "✗ No download",
}
FINISHED = {"attached", "found", "not found", "error", "skipped", "needs your browser", "no download"}

META_TEXT = {"waiting": "Waiting", "checking": "Checking…"}
META_ICON = {"ok": "✓", "updated": "✎", "review": "⚑", "wrong pdf": "⚠", "no record": "?", "not checked": "–",
             "retracted": "⚠", "error": "✗", "pdf replaced": "✓", "other version": "◐", "notice": "ℹ",
             "unchanged": "–"}
META_PENDING = {"waiting", "checking"}

#: Row colour: the most pressing of the row's columns wins.
_SEVERITY = {"bad": 4, "warn": 3, "busy": 2, "ok": 1}
_META_TONE = {"error": "bad", "retracted": "bad", "wrong pdf": "warn", "review": "warn", "no record": "warn",
              "updated": "ok", "ok": "ok", "pdf replaced": "ok", "other version": "ok", "notice": "warn",
              "unchanged": "busy"}
_FETCH_TONE = {"not found": "bad", "error": "bad", "no download": "bad", "needs your browser": "warn",
               "waiting for your download": "warn", "attached": "ok", "found": "ok"}

TITLES = {"fetch": "Find Full Text", "maintain": "Check & complete", "metadata": "Check metadata"}


class Progress:
    """The window's bookkeeping, separate from tkinter so it can be tested."""

    def __init__(self, stages: list[str] | None = None) -> None:
        self.order: list[str] = []
        self.labels: dict[str, str] = {}
        self.status: dict[str, str] = {}
        self.detail: dict[str, str] = {}
        self.meta: dict[str, str] = {}
        self.meta_detail: dict[str, str] = {}
        self.meta_changed: set[str] = set()
        self.browser_tried: set[str] = set()
        self.active_runs = 0
        self.browser_busy = False
        self.reports: list[str] = []
        self.own_links: dict[str, str] = {}
        self.stages = list(stages or [])
        self.stage_index = -1
        self.stage_keys: set[str] = set()
        self.main_done = not self.stages
        self.count: tuple[int, int] | None = None     # a step without rows (retractions): done, total
        self.count_what = ""                           # and what it is

    def apply(self, event: dict) -> str | None:
        """Take one event from the run; returns the paper's key when its row changed."""
        key = event.get("key") or ""
        status = event.get("status", "")
        if not key:
            if status == "done" and event.get("detail") and event["detail"] not in self.reports:
                self.reports.append(event["detail"])
            elif status == "stage":
                self.stage_index = int(event.get("index", self.stage_index + 1))
                self.stage_keys = set()
                self.count = None
            elif status == "count":
                self.count = (int(event.get("done", 0)), int(event.get("total", 0)))
                self.count_what = str(event.get("what") or "")
            return None
        if key not in self.labels:
            self.order.append(key)
        self.labels[key] = event.get("label") or self.labels.get(key, key)
        if status == "waiting":
            self.stage_keys.add(key)
        if event.get("phase") == "metadata":
            self.meta[key] = status
            self.meta_detail[key] = event.get("detail", "")
            if event.get("changed"):
                self.meta_changed.add(key)
            return key
        self.status[key] = status
        self.detail[key] = event.get("detail", "")
        if status in ("attached", "found") and self.meta.get(key) in ("wrong pdf", "other version"):
            # The right (or published) PDF was found: it replaced the old one (in Zotero's trash).
            self.meta_detail[key] = ("wrong PDF replaced" if self.meta[key] == "wrong pdf"
                                     else "published version attached")
            self.meta[key] = "pdf replaced"
        if status == "browser":
            self.browser_tried.add(key)     # searched with the browser: not offered again
        if status == "needs your browser":
            self.own_links[key] = self.detail[key]
        return key

    # -- the buttons ---------------------------------------------------------

    def for_own_browser(self) -> list[str]:
        return [k for k in self.order if self.status.get(k) == "needs your browser" and k in self.own_links]

    def not_found_for_browser(self) -> list[str]:
        return [k for k in self.order if self.status.get(k) == "not found" and k not in self.browser_tried]

    def browser_button(self) -> tuple[bool, str]:
        keys = self.not_found_for_browser()
        n = len(keys)
        text = f"Search {n} not found with the browser" if n else "Search not-found papers with the browser"
        return bool(n) and not self.browser_busy, text

    # -- counting --------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        """Full-text statuses (papers the fetcher has not seen yet are not counted)."""
        c: dict[str, int] = {}
        for k in self.order:
            if k in self.status:
                s = self.status[k]
                c[s] = c.get(s, 0) + 1
        return c

    def meta_counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for s in self.meta.values():
            c[s] = c.get(s, 0) + 1
        return c

    def _fetch_progress(self) -> tuple[int, int]:
        c = self.counts()
        total = sum(c.values()) - c.get("skipped", 0)
        finished = sum(c.get(s, 0) for s in FINISHED) - c.get("skipped", 0)
        return finished, total

    def _stage_progress(self) -> tuple[int, int]:
        if self.count is not None:
            return self.count
        keys = self.stage_keys
        if self._stage_is_metadata():
            done = sum(1 for k in keys if self.meta.get(k) not in META_PENDING)
        else:
            done = sum(1 for k in keys if self.status.get(k) in FINISHED)
        return done, len(keys)

    def _stage_is_metadata(self) -> bool:
        return 0 <= self.stage_index < len(self.stages) and self.stages[self.stage_index].startswith("Metadata")

    def summary(self) -> str:
        c = self.counts()
        finished, total = self._fetch_progress()
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
        """The short line under the title: what is running and how far, or "Done"."""
        if self.stages and not self.main_done and self.stage_index >= 0:
            done, total = self._stage_progress()
            if self.stages[self.stage_index] == "Search index":
                return "Updating the search index…"
            if self.stages[self.stage_index] == "Retractions":
                return f"Checking for retractions and corrections · {done} of {total}"
            if self.count is not None and self.count_what:
                return f"{self.count_what} · {done} of {total}"
            what = "Checking metadata" if self._stage_is_metadata() else "Fetching PDFs"
            return f"{what} · {done} of {total}"
        finished, total = self._fetch_progress()
        n = len(self.order) if self.stages else total
        if self.active_runs:
            return f"Searching · {finished} of {total}"
        return f"Done · {n} paper{'s' if n != 1 else ''}"

    def fraction(self) -> float:
        if self.stages and not self.main_done and self.stage_index >= 0:
            done, total = self._stage_progress()
            within = done / total if total else 0.0
            return min(1.0, (self.stage_index + within) / len(self.stages))
        if self.stages and not self.active_runs:
            return 1.0
        finished, total = self._fetch_progress()
        return finished / total if total else 1.0

    def _keys(self, test: Callable[[str], bool]) -> list[str]:
        return [k for k in self.order if test(k)]

    def chip_groups(self) -> list[tuple[str, list[tuple[str, str, list[str], str]]]]:
        """The coloured counts at the top, as two groups (metadata, PDFs): (style, text, the papers
        it counts, what it means, for the tooltip); only counts above zero."""
        meta = self.meta.get
        status = self.status.get
        wrong = self._keys(lambda k: meta(k) == "wrong pdf")
        other = self._keys(lambda k: meta(k) == "other version")
        other_chip = ("ChipNeutral", "◐ {} other form", other,
                      "The right paper as an accepted manuscript, preprint or proof, or the whole book around a "
                      "chapter. Tagged; nothing to check. A fetch swaps in the published version when it finds it.")
        groups = []
        if self.stages:
            chips = [
                ("ChipInfo", "✎ {} fixed", self._keys(lambda k: k in self.meta_changed),
                 "Empty fields filled in, or errors corrected that two sources agree on. A note on the paper "
                 "lists the changes."),
                ("ChipReview", "⚑ {} to review", self._keys(lambda k: meta(k) == "review"),
                 "Changes only one source suggests: not made. 'Review' in 'To do' shows them, to accept or "
                 "reject."),
                ("ChipBad", "⚠ {} retracted", self._keys(lambda k: meta(k) == "retracted"),
                 "Retracted by the journal (tag: retracted)."),
                ("ChipNeutral", "ℹ {} correction", self._keys(lambda k: meta(k) == "notice"),
                 "A correction, erratum or expression of concern was published this year. A note on the paper "
                 "says which."),
            ]
            groups.append(("Metadata", chips))
        pdf_chips = [("ChipWarn", "⚠ {} wrong PDF", wrong,
                      "Another paper is attached. Tag: fulltext/check-pdf, with a note."), other_chip]
        if self.stages != ["Metadata"]:
            pdf_chips = [
                ("ChipOk", "✓ {} attached", self._keys(lambda k: status(k) in ("attached", "found")),
                 "PDFs found and attached."),
            ] + pdf_chips + [
                ("ChipWarn", "⚠ {} bot check", self._keys(lambda k: status(k) in ("needs your browser",
                                                                                   "waiting for your download")),
                 "Only your own browser gets past this site's check. See 'To do'."),
                ("ChipBad", "✗ {} not found", self._keys(lambda k: status(k) in ("not found", "no download")),
                 "No copy found (tag: fulltext/not-found)."),
            ]
        groups.append(("PDFs", pdf_chips))
        return [(name, [(st, text.format(len(keys)), keys, tip) for st, text, keys, tip in chips if keys])
                for name, chips in groups if any(c[2] for c in chips)]

    def chips(self) -> list[tuple[str, str]]:
        """(style, text) of every count shown, in order."""
        return [(style, text) for _name, chips in self.chip_groups() for style, text, _keys, _tip in chips]

    def todo(self) -> list[tuple[str, str, str | None, str]]:
        """What is left for the user: (tone, short text, the button's action or None, what to do,
        for the tooltip). Empty when there is nothing."""
        meta = self.meta.get
        status = self.status.get
        out: list[tuple[str, str, str | None, str]] = []
        own = self._keys(lambda k: status(k) == "needs your browser")
        if own:
            out.append(("warn", f"⚠ {len(own)} behind a bot check", "own",
                        "Opens them in your own browser. Download the PDF there; it is attached automatically."))
        waiting = self._keys(lambda k: status(k) == "waiting for your download")
        if waiting:
            out.append(("busy", f"… Waiting for your download ({len(waiting)})", None,
                        "Save the PDF to your Downloads folder and keep this window open."))
        browser = self.not_found_for_browser()
        if browser:
            out.append(("bad", f"✗ {len(browser)} not found", "browser",
                        "Tries ResearchGate, Academia.edu and your publisher logins in the fetcher's Chrome window."))
        review = self._keys(lambda k: meta(k) == "review")
        if review:
            out.append(("review", f"⚑ {len(review)} with suggested changes", "review",
                        "Accept or reject each suggestion. Later also in Zotero: right-click › Review suggested "
                        "metadata."))
        wrong = self._keys(lambda k: meta(k) == "wrong pdf")
        if wrong and not self.active_runs:
            text = (f"⚠ {len(wrong)} with another paper attached, the right one not found  ⓘ"
                    if self.stages != ["Metadata"] else f"⚠ {len(wrong)} with another paper attached  ⓘ")
            out.append(("warn", text, None,
                        "A note on the paper says what is wrong (tag: fulltext/check-pdf). "
                        + ("'Check & complete' looks for the right one." if self.stages == ["Metadata"]
                           else "It stays attached until the right one is found.")))
        errors = self._keys(lambda k: meta(k) == "error" or status(k) == "error")
        if errors:
            out.append(("bad", f"✗ {len(errors)} error{'s' if len(errors) != 1 else ''}", "report",
                        "The report has the details."))
        return out

    def sort_key(self, key: str, column: str) -> tuple:
        """For sorting by a column: the paper's label; for metadata and full text, the most pressing
        first (bad, warn, busy, ok), then the text."""
        if column == "item":
            return (self.labels.get(key, key).lower(),)
        if column == "meta":
            tone, text = _META_TONE.get(self.meta.get(key, ""), "busy"), self.meta_text(key)
        else:
            tone, text = _FETCH_TONE.get(self.status.get(key, ""), "busy"), self.fetch_text(key)
        return (-_SEVERITY[tone] if text else 1, text.lower())

    # -- a row -------------------------------------------------------------------

    def meta_text(self, key: str) -> str:
        status = self.meta.get(key)
        if status is None:
            return ""
        if status in META_TEXT:
            return META_TEXT[status]
        detail = self.meta_detail.get(key, "") or status
        return f"{META_ICON.get(status, '')} {detail[:1].upper()}{detail[1:]}".strip()

    def fetch_text(self, key: str) -> str:
        status = self.status.get(key)
        if status is None:
            return ""
        detail = self.detail.get(key, "")
        if status == "skipped":
            return "– Has a PDF" if "already" in detail else f"– {detail[:1].upper()}{detail[1:]}"
        text = STATUS_TEXT.get(status, status)
        if detail and status in ("attached", "found"):
            text += " · " + detail.replace(", published version", "").replace("from your download", "your download")
        return text

    def tone(self, key: str) -> str:
        tones = []
        if key in self.meta:
            tones.append(_META_TONE.get(self.meta[key], "busy"))
        status = self.status.get(key)
        if status is not None and status != "skipped":
            tones.append(_FETCH_TONE.get(status, "busy"))
        if not tones:
            return "ok" if status == "skipped" else "busy"
        return max(tones, key=lambda t: _SEVERITY[t])


class _Tooltip:
    """A small explanation that appears while the mouse is over a widget."""

    def __init__(self, widget, text: str) -> None:
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<Destroy>", self.hide, add="+")

    def show(self, _event=None) -> None:
        import tkinter as tk

        if self.tip is not None:
            return
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, background="#1f2937", foreground="#ffffff", justify="left",
                 wraplength=320, padx=10, pady=6).pack()

    def hide(self, _event=None) -> None:
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


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
    style.configure("Link.TLabel", background=ui["bg"], foreground=ui["accent"], font=(fam, 9, "underline"))
    style.configure("StepDone.TLabel", background=ui["bg"], foreground=ui["ok"], font=(fam, 10, "bold"))
    style.configure("StepNow.TLabel", background=ui["bg"], foreground=ui["accent"], font=(fam, 10, "bold"))
    style.configure("StepTodo.TLabel", background=ui["bg"], foreground="#9ca3af", font=(fam, 10))
    for name, colour, tint in (("ChipOk", ui["ok"], "#dcfce7"), ("ChipWarn", ui["warn"], "#fef3c7"),
                               ("ChipBad", ui["bad"], "#fee2e2"), ("ChipInfo", ui["accent_dark"], "#dbeafe"),
                               ("ChipReview", "#7e22ce", "#f3e8ff"), ("ChipNeutral", "#4b5563", "#f3f4f6")):
        style.configure(f"{name}.TLabel", background=tint, foreground=colour, padding=(10, 3),
                        font=(fam, 9, "bold"))
        # The chip whose papers the list is showing: filled.
        style.configure(f"{name}Active.TLabel", background=colour, foreground="#ffffff", padding=(10, 3),
                        font=(fam, 9, "bold"))
    style.configure("Panel.TFrame", background=ui["card"])
    style.configure("PanelTitle.TLabel", background=ui["card"], foreground=ui["text"], font=(fam, 10, "bold"))
    for tone, colour in (("Review", "#7e22ce"), ("Warn", ui["warn"]), ("Bad", ui["bad"]), ("Busy", ui["muted"]),
                         ("Ok", ui["ok"])):
        style.configure(f"Todo{tone}.TLabel", background=ui["card"], foreground=colour)
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
    for name in ("Soft", "SmallAccent"):
        style.configure(f"{name}.TButton", padding=(10, 2))
    style.configure("Soft.TButton", background="#f3f4f6", lightcolor="#f3f4f6", darkcolor="#f3f4f6",
                    bordercolor="#e5e7eb", focuscolor="#f3f4f6")
    style.map("Soft.TButton", background=[("disabled", ui["card"]), ("active", "#e5e7eb")],
              lightcolor=[("active", "#e5e7eb")], darkcolor=[("active", "#e5e7eb")],
              foreground=[("disabled", "#9ca3af")])
    style.configure("SmallAccent.TButton", background=ui["accent"], foreground="#ffffff", bordercolor=ui["accent"],
                    lightcolor=ui["accent"], darkcolor=ui["accent"], focuscolor=ui["accent"], font=(fam, 10, "bold"))
    style.map("SmallAccent.TButton", background=[("active", ui["accent_dark"])],
              lightcolor=[("active", ui["accent_dark"])], darkcolor=[("active", ui["accent_dark"])])
    style.configure("Vertical.TScrollbar", background="#e5e7eb", troughcolor=ui["card"], bordercolor=ui["card"],
                    arrowcolor=ui["muted"], lightcolor="#e5e7eb", darkcolor="#e5e7eb", gripcount=0)
    return ui


def ask_check_again(count: int, when: str, title: str = "Check & complete") -> bool:
    """The papers chosen were all checked before and nothing changed since: ask before checking
    them again. True to check again."""
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(f"{title} — zotero-mcp")
    root.resizable(False, False)
    ui = _style(root, ttk)
    root.configure(background=ui["bg"])
    frame = ttk.Frame(root, padding=(20, 16, 20, 14), style="App.TFrame")
    frame.pack(fill="both", expand=True)
    head = "Nothing changed since the last check" if count == 1 else f"These {count} papers are unchanged"
    text = (f"This paper was last checked on {when}, and nothing changed in Zotero since." if count == 1
            else f"All {count} papers were checked before (the last on {when}), and nothing changed in Zotero since.")
    ttk.Label(frame, text=head, style="Title.TLabel").pack(anchor="w")
    ttk.Label(frame, text=text, style="Muted.TLabel", wraplength=420,
              justify="left").pack(anchor="w", pady=(6, 14))
    answer = {"again": False}

    def again() -> None:
        answer["again"] = True
        root.destroy()

    buttons = ttk.Frame(frame, style="App.TFrame")
    buttons.pack(fill="x")
    ttk.Button(buttons, text="Cancel", command=root.destroy).pack(side="right")
    ttk.Button(buttons, text="Check again", command=again, style="Accent.TButton").pack(side="right", padx=(0, 8))
    root.bind("<Return>", lambda e: again())
    root.bind("<Escape>", lambda e: root.destroy())
    root.lift()
    root.attributes("-topmost", True)
    root.after(600, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return answer["again"]


_NAME_FIELDS = ("creators", "editors")


def _names(text: str) -> list[str]:
    return [a.strip() for a in str(text or "").split(";") if a.strip()]


def _middle(old: str, new: str) -> tuple[int, int, int]:
    """Where two texts differ: (start, end in old, end in new), widened to whole words when
    the difference starts or ends inside a word."""
    n = min(len(old), len(new))
    i = 0
    while i < n and old[i] == new[i]:
        i += 1
    j = 0
    while j < n - i and old[-1 - j] == new[-1 - j]:
        j += 1
    a_end, b_end = len(old) - j, len(new) - j

    def inside(text: str, k: int) -> bool:
        return 0 < k < len(text) and text[k - 1].isalnum() and text[k].isalnum()

    while i > 0 and (inside(old, i) or inside(new, i)):
        i -= 1
    while (a_end < len(old) and inside(old, a_end)) or (b_end < len(new) and inside(new, b_end)):
        a_end += 1
        b_end += 1
    return i, a_end, b_end


def describe_change(field: str, old: str, new: str) -> str:
    """Only what changes, for the list: "2023 → 2025", "Keer, Hilde → Van Keer, Hilde",
    "+ ": A Systematic Review""."""
    from difflib import SequenceMatcher

    old, new = str(old or ""), str(new or "")
    if not old.strip():
        return f"add {new}"
    if field in _NAME_FIELDS:
        a, b = _names(old), _names(new)
        parts = []
        for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            before, after = "; ".join(a[i1:i2]), "; ".join(b[j1:j2])
            if op == "replace":
                parts.append(f"{before} → {after}")
            elif op == "insert":
                parts.append(f"+ {after}")
            elif op == "delete":
                parts.append(f"− {before}")
        return "   ·   ".join(parts) or "spacing or punctuation only"
    if len(old) <= 24 and len(new) <= 24:
        return f"{old} → {new}"
    i, a_end, b_end = _middle(old, new)
    gone, added = old[i:a_end].strip(), new[i:b_end].strip()
    if not gone and not added:
        return "spacing only"
    if not gone:
        return f'+ "{added}"'
    if not added:
        return f'− "{gone}"'
    return f'"{gone}" → "{added}"'


def diff_segments(field: str, old: str, new: str) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    """Both values in pieces, each marked True where it differs: for highlighting."""
    from difflib import SequenceMatcher

    old, new = str(old or ""), str(new or "")
    if field not in _NAME_FIELDS:
        i, a_end, b_end = _middle(old, new)
        left = [(old[:i], False), (old[i:a_end], True), (old[a_end:], False)]
        right = [(new[:i], False), (new[i:b_end], True), (new[b_end:], False)]
        return [p for p in left if p[0]], [p for p in right if p[0]]
    a, b = _names(old), _names(new)
    left: list[tuple[str, bool]] = []
    right: list[tuple[str, bool]] = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        for names, side in ((a[i1:i2], left), (b[j1:j2], right)):
            for name in names:
                if side:
                    side.append(("; ", False))
                side.append((name, op != "equal"))
    return left, right


def review_window(keys: list[str] | None = None, *, collection: str | None = None, master=None,
                  on_decided: Callable[[str, int, int, int], None] | None = None, session=None) -> None:
    """The suggested metadata changes waiting for review (of ``keys``, a collection, or all), one
    row each under its paper: Accept changes the field in Zotero, Reject keeps the user's value (and
    it is not suggested again). ``master``: opened from the progress window, which ``on_decided``
    (key, applied, rejected, still left) keeps up to date."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    from zotero_mcp import metadata_audit as ma

    session = session or ma.ReviewSession()
    root = tk.Toplevel(master) if master is not None else tk.Tk()
    root.title("Review suggested metadata — zotero-mcp")
    root.geometry("1120x580")
    root.minsize(640, 360)
    ui = _style(root, ttk) if master is None else {"bg": "#f6f7f9", "muted": "#6b7280", "card": "#ffffff",
                                                    "stripe": "#f9fafb", "text": "#1f2937"}
    root.configure(background=ui["bg"])
    frame = ttk.Frame(root, padding=(18, 14, 18, 12), style="App.TFrame")
    frame.pack(fill="both", expand=True)
    top = ttk.Frame(frame, style="App.TFrame")
    top.pack(fill="x")
    ttk.Label(top, text="Suggested changes", style="Title.TLabel").pack(side="left")
    head = ttk.Label(top, text="Loading…", style="Muted.TLabel")
    head.pack(side="right", anchor="s", pady=(0, 3))
    ttk.Label(frame, text="Accept changes the field in Zotero. Reject keeps yours, and it is not suggested again. "
                          "Select a paper to decide all its suggestions at once.",
              style="Muted.TLabel", wraplength=900, justify="left").pack(anchor="w", pady=(4, 10))

    buttons = ttk.Frame(frame, style="App.TFrame")
    buttons.pack(side="bottom", fill="x", pady=(12, 0))
    detail = tk.Text(frame, height=4, wrap="word", relief="flat", background=ui["card"], foreground=ui["text"],
                     font="TkDefaultFont",
                     padx=10, pady=8, borderwidth=0, highlightthickness=1, highlightbackground="#e5e7eb")
    detail.pack(side="bottom", fill="x", pady=(10, 0))
    detail.configure(state="disabled")
    holder = ttk.Frame(frame, style="Card.TFrame", padding=1)
    holder.pack(fill="both", expand=True)
    tree = ttk.Treeview(holder, columns=["change", "why"], show="tree headings", style="Papers.Treeview")
    tree.heading("#0", text="Paper / field", anchor="w")
    tree.column("#0", width=380, anchor="w", stretch=True)
    for col, text, width in (("change", "Change", 430), ("why", "Why", 250)):
        tree.heading(col, text=text, anchor="w")
        tree.column(col, width=width, anchor="w", stretch=True)
    tree.tag_configure("paper", font=("TkDefaultFont", 10, "bold"))
    tree.tag_configure("stripe", background=ui["stripe"])
    scroll = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)

    changes: dict[str, object] = {}         # row id -> Change
    results: queue.Queue = queue.Queue()
    busy = {"n": 0}

    def one_line(text: str, n: int = 90) -> str:
        text = " ".join(str(text or "").split())
        return text if len(text) <= n else text[: n - 1] + "…"

    def show(rows) -> None:
        for key, label, suggestions in rows:
            tree.insert("", "end", iid=key, text=label, open=True, tags=("paper",))
            for i, c in enumerate(suggestions):
                rid = f"{key}|{c.field}"
                changes[rid] = c
                tree.insert(key, "end", iid=rid, text=ma.FIELD_LABELS.get(c.field, c.field),
                            values=(one_line(describe_change(c.field, c.old, c.new), 110),
                                    one_line(f"{c.why or ''}{' (' + ', '.join(c.sources) + ')' if c.sources else ''}")),
                            tags=("stripe",) if i % 2 else ())
        counted()

    def counted() -> None:
        n = len(changes)
        papers = len(tree.get_children())
        head.configure(text=(f"{n} suggestion{'s' if n != 1 else ''} on {papers} paper{'s' if papers != 1 else ''}"
                             if n else "Nothing left to review"))
        state = "normal" if n and not busy["n"] else "disabled"
        for b in (accept_btn, reject_btn):
            b.configure(state=state)

    def chosen() -> dict[str, list[str]]:
        """The selected suggestions, by paper (a paper row stands for all its suggestions)."""
        out: dict[str, list[str]] = {}
        for rid in tree.selection():
            if "|" in rid:
                key, field = rid.split("|", 1)
                out.setdefault(key, []).append(field)
            else:
                out.setdefault(rid, []).extend(r.split("|", 1)[1] for r in tree.get_children(rid))
        return {k: sorted(set(v)) for k, v in out.items()}

    def decide(accept: bool) -> None:
        picked = chosen()
        if not picked:
            return
        busy["n"] += len(picked)
        counted()
        head.configure(text="Saving in Zotero…")

        def work() -> None:
            for key, fields in picked.items():
                try:
                    done = session.decide(key, fields if accept else [], [] if accept else fields)
                    results.put((key, fields, done, None))
                except Exception as e:
                    results.put((key, fields, None, f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def poll() -> None:
        try:
            while True:
                item = results.get_nowait()
                if item[0] == "__loaded__":
                    if item[2] is not None:
                        messagebox.showerror("Review suggested metadata", item[2], parent=root)
                    else:
                        show(item[1])
                        if not item[1]:
                            head.configure(text="Nothing waiting for review")
                    continue
                key, fields, done, error = item
                busy["n"] -= 1
                if error:
                    messagebox.showerror("Review suggested metadata", f"{key}: {error}", parent=root)
                else:
                    for f in fields:
                        rid = f"{key}|{f}"
                        changes.pop(rid, None)
                        if tree.exists(rid):
                            tree.delete(rid)
                    if tree.exists(key) and not tree.get_children(key):
                        tree.delete(key)
                    if not tree.selection() and changes:
                        # On to the next suggestion, so they can be decided one after the other.
                        first = next(iter(changes))
                        tree.selection_set(first)
                        tree.focus(first)
                        tree.see(first)
                    if on_decided is not None:
                        on_decided(key, *done)
                counted()
        except queue.Empty:
            pass
        if root.winfo_exists():
            root.after(150, poll)

    detail.tag_configure("label", foreground=ui["muted"])
    detail.tag_configure("gone", foreground="#b91c1c", background="#fee2e2", overstrike=True)
    detail.tag_configure("new", foreground="#15803d", background="#dcfce7")

    def on_select(_event=None) -> None:
        sel = tree.selection()
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        if len(sel) == 1 and sel[0] in changes:
            c = changes[sel[0]]
            left, right = diff_segments(c.field, c.old, c.new)
            detail.insert("end", tree.item(tree.parent(sel[0]), "text") + "\n", "label")
            detail.insert("end", "Yours:  ", "label")
            for text, differs in left or [("(empty)", False)]:
                detail.insert("end", text, "gone" if differs else ())
            detail.insert("end", "\nSuggested:  ", "label")
            for text, differs in right:
                detail.insert("end", text, "new" if differs else ())
            detail.insert("end", f"\nWhy:  {c.why or ''} ({', '.join(c.sources)})", "label")
        elif len(sel) == 1:
            detail.insert("end", f"{tree.item(sel[0], 'text')}: {len(tree.get_children(sel[0]))} suggestion(s).")
        detail.configure(state="disabled")

    def on_open(_event=None) -> None:
        sel = tree.selection()
        if sel:
            try:
                os.startfile(f"zotero://select/library/items/{sel[0].split('|')[0]}")  # type: ignore[attr-defined]
            except Exception:
                pass

    accept_btn = ttk.Button(buttons, text="Accept", command=lambda: decide(True), style="Accent.TButton")
    accept_btn.pack(side="left")
    reject_btn = ttk.Button(buttons, text="Reject", command=lambda: decide(False))
    reject_btn.pack(side="left", padx=(8, 0))
    ttk.Button(buttons, text="Close", command=root.destroy).pack(side="right")
    ttk.Button(buttons, text="Show in Zotero", command=on_open).pack(side="right", padx=(0, 8))
    tree.bind("<<TreeviewSelect>>", on_select)
    tree.bind("<Double-1>", on_open)
    menu = tk.Menu(root, tearoff=0)

    def on_menu(event) -> None:
        row = tree.identify_row(event.y)
        if not row:
            return
        if row not in tree.selection():
            tree.selection_set(row)
        menu.delete(0, "end")
        if not busy["n"]:
            menu.add_command(label="Accept", command=lambda: decide(True))
            menu.add_command(label="Reject", command=lambda: decide(False))
            menu.add_separator()
        menu.add_command(label="Show in Zotero", command=on_open)
        menu.tk_popup(event.x_root, event.y_root)

    tree.bind("<Button-3>", on_menu)
    counted()

    def load() -> None:
        try:
            results.put(("__loaded__", session.load(keys, collection), None))
        except Exception as e:
            results.put(("__loaded__", [], f"The suggestions could not be read: {type(e).__name__}: {e}"))

    threading.Thread(target=load, daemon=True).start()
    root.after(150, poll)
    root.lift()
    if master is None:
        root.attributes("-topmost", True)
        root.after(600, lambda: root.attributes("-topmost", False))
        root.mainloop()


def run_window(run_kwargs: dict, run: Callable[..., object] | None = None, *, mode: str = "fetch",
               fetch: Callable[..., object] | None = None, quiet: bool = False) -> None:
    """Run in a background thread and show the progress until the window is closed.

    ``mode``: "fetch" (``fulltext_fetch.run``), "maintain" (metadata, PDFs, metadata
    again: ``maintenance.run``) or "metadata" (``maintenance.run`` without fetching).
    ``fetch`` is what the browser button runs (``fulltext_fetch.run``). ``quiet`` (the import
    trigger): the window starts minimised, comes forward at the end only when something is left
    for the user, and otherwise closes by itself.
    """
    import tkinter as tk
    from tkinter import messagebox, ttk

    from zotero_mcp import maintenance

    if run is None:
        if mode == "fetch":
            from zotero_mcp.fulltext_fetch import run as run
        else:
            run = maintenance.run
    if fetch is None:
        if mode == "fetch":
            fetch = run
        else:
            from zotero_mcp.fulltext_fetch import run as fetch
    if mode in ("maintain", "metadata"):
        run_kwargs = dict(run_kwargs, fetch=mode == "maintain")
    with_pdfs = mode != "metadata"
    with_meta = mode != "fetch"

    prog = Progress(maintenance.stages(mode == "maintain", bool(run_kwargs.get("index"))) if with_meta else None)
    events: queue.Queue = queue.Queue()
    main_uses_browser = mode == "fetch" and "browser" in (run_kwargs.get("steps") or [])
    dry_run = bool(run_kwargs.get("dry_run", not run_kwargs.get("apply", True)))

    root = tk.Tk()
    title = TITLES.get(mode, TITLES["fetch"])
    root.title(f"{title} — zotero-mcp")
    root.geometry("1040x600" if with_meta and with_pdfs else "940x560")
    root.minsize(600, 340)
    ui = _style(root, ttk)
    root.configure(background=ui["bg"])

    frame = ttk.Frame(root, padding=(18, 14, 18, 12), style="App.TFrame")
    frame.pack(fill="both", expand=True)
    top = ttk.Frame(frame, style="App.TFrame")
    top.pack(fill="x")
    ttk.Label(top, text=title, style="Title.TLabel").pack(side="left")
    head = ttk.Label(top, text="Starting…", style="Muted.TLabel")
    head.pack(side="right", anchor="s", pady=(0, 3))
    steps_box = ttk.Frame(frame, style="App.TFrame")
    step_labels: list = []
    if len(prog.stages) > 1:
        steps_box.pack(anchor="w", pady=(4, 0))
        for i, name in enumerate(prog.stages):
            if i:
                ttk.Label(steps_box, text="›", style="StepTodo.TLabel").pack(side="left", padx=8)
            lab = ttk.Label(steps_box, text=f"{i + 1}  {name}", style="StepTodo.TLabel")
            lab.pack(side="left")
            step_labels.append(lab)
    # The counts; hover for what one means, click to show only its papers (again: all).
    chips_box = ttk.Frame(frame, style="App.TFrame")
    chips_box.pack(fill="x", pady=(10, 0))
    chip_widgets: list = []
    shown_chips: list = [None]
    show_all = ttk.Label(chips_box, text="", style="Link.TLabel", cursor="hand2")
    #: What the list shows: only one chip's papers ("filter", the chip's text without its count),
    #: sorted by a column ("sort", "desc"); "dirty" when the rows need placing again.
    view: dict = {"filter": None, "sort": None, "desc": False, "dirty": False}
    bar = ttk.Progressbar(frame, mode="determinate", maximum=1000, style="Thin.Horizontal.TProgressbar")
    bar.pack(fill="x", pady=(10, 12))

    # At the bottom first, so a small window shrinks the list, never these: the buttons, and
    # "To do" (what is left for you, each with its button), shown only when there is something.
    buttons = ttk.Frame(frame, style="App.TFrame")
    buttons.pack(side="bottom", fill="x", pady=(12, 0))
    todo_card = ttk.Frame(frame, style="Card.TFrame", padding=1)
    todo_inner = ttk.Frame(todo_card, style="Panel.TFrame", padding=(12, 8, 10, 8))
    todo_inner.pack(fill="both", expand=True)
    ttk.Label(todo_inner, text="To do", style="PanelTitle.TLabel").pack(anchor="w", pady=(0, 2))
    todo_rows: list = []
    shown_todo: list = [None]

    holder = ttk.Frame(frame, style="Card.TFrame", padding=1)
    holder.pack(fill="both", expand=True)
    columns = ["item"] + (["meta"] if with_meta else []) + (["status"] if with_pdfs else [])
    tree = ttk.Treeview(holder, columns=columns, show="headings", height=8, style="Papers.Treeview")
    widths = {"item": 420 if len(columns) == 3 else 520, "meta": 250 if with_pdfs else 360,
              "status": 300 if with_meta else 360}
    headings = {"item": "Paper", "meta": "Metadata", "status": "Full text"}
    for col, text in headings.items():
        if col in columns:
            tree.heading(col, text=text, anchor="w", command=lambda c=col: on_sort(c))
            tree.column(col, width=widths[col], anchor="w", stretch=True)
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

    def start(target: Callable[..., object], kwargs: dict, uses_browser: bool, main: bool = False) -> None:
        prog.active_runs += 1
        if uses_browser:
            prog.browser_busy = True

        def work() -> None:
            try:
                target(progress=events.put, **kwargs)
            except Exception as e:  # shown in the window and the terminal
                print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
                events.put({"key": "", "status": "failed", "detail": f"{type(e).__name__}: {e}"})
            finally:
                events.put({"key": "", "status": "run finished", "browser": uses_browser, "main": main})

        threading.Thread(target=work, daemon=True).start()

    def on_browser() -> None:
        keys = prog.not_found_for_browser()
        if not keys:
            return
        prog.browser_tried.update(keys)
        for k in keys:
            prog.apply({"key": k, "label": prog.labels.get(k, k), "status": "waiting for browser", "detail": ""})
            redraw(k)
        kwargs = dict(keys=keys, steps=["browser"], retry=True, workers=1, dry_run=dry_run)
        if run_kwargs.get("save_dir"):
            kwargs["save_dir"] = run_kwargs["save_dir"]
        start(fetch, kwargs, True)
        refresh()

    def on_own_browser() -> None:
        keys = prog.for_own_browser()[:5]
        if not keys:
            return
        import time
        import webbrowser

        started = time.time() - 5
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

    def on_recheck(keys: list[str] | None = None) -> None:
        keys = keys or [k for k in prog.order if prog.meta.get(k) == "unchanged"]
        if not keys:
            return
        for k in keys:
            prog.apply({"key": k, "label": prog.labels.get(k, k), "phase": "metadata", "status": "waiting",
                        "detail": ""})
            redraw(k)
        start(run, dict(run_kwargs, keys=keys, collection=None, new=False, since=None, every=True), False)
        refresh()

    def on_review(keys: list[str] | None = None) -> None:
        keys = keys or [k for k in prog.order if prog.meta.get(k) == "review"]
        if keys:
            review_window(keys, master=root, on_decided=decided)

    def decided(key: str, applied: int, rejected: int, left: int) -> None:
        if left:
            prog.meta_detail[key] = f"{left} to review"
            if applied:
                prog.meta_changed.add(key)
        elif applied:
            prog.meta[key] = "updated"
            prog.meta_detail[key] = "suggestion accepted" if applied == 1 else f"{applied} suggestions accepted"
            prog.meta_changed.add(key)
        else:
            prog.meta[key], prog.meta_detail[key] = "ok", "suggestion rejected, yours kept"
        redraw(key)
        refresh()

    def on_sort(column: str) -> None:
        if view["sort"] == column:
            view["desc"] = not view["desc"]
        else:
            view["sort"], view["desc"] = column, False
        for col, text in headings.items():
            if col in columns:
                arrow = (" ▼" if view["desc"] else " ▲") if col == column else ""
                tree.heading(col, text=text + arrow)
        view["dirty"] = True
        relayout()

    def set_filter(chip: str | None) -> None:
        view["filter"] = None if view["filter"] == chip else chip
        shown_chips[0] = None           # redraw the chips (the active one filled)
        view["dirty"] = True
        refresh()

    def on_report() -> None:
        for path in prog.reports[-2:]:      # the metadata report and the fetch report
            try:
                os.startfile(path)  # type: ignore[attr-defined]  # Windows
            except Exception:
                messagebox.showinfo("Report", path)

    def on_close() -> None:
        if prog.active_runs and not messagebox.askyesno(
                "Still running", "A check or search is still running. Stop it and close? "
                                 "What is done so far stays done."):
            return
        root.destroy()

    def on_open(_event=None) -> None:
        sel = tree.selection()
        if sel:
            try:
                os.startfile(f"zotero://select/library/items/{sel[0]}")  # type: ignore[attr-defined]
            except Exception:
                pass

    # Papers passed over because nothing changed since their last check: check them again on request
    # (all of them here, or one by right-clicking its row).
    recheck_btn = ttk.Button(buttons, text="Check all unchanged anyway", command=on_recheck)
    _Tooltip(recheck_btn, "Nothing changed in Zotero since their last check, so they were only checked for "
                          "retractions. Right-click a paper to check only that one (or the selected ones).")
    ttk.Button(buttons, text="Close", command=on_close).pack(side="right")
    report_btn = ttk.Button(buttons, text="Show report", command=on_report)
    report_btn.pack(side="right", padx=(0, 8))
    tree.bind("<Double-1>", on_open)

    if with_meta:
        # Right-click a paper that was passed over (unchanged): check it again on its own.
        menu = tk.Menu(root, tearoff=0)

        def on_menu(event) -> None:
            row = tree.identify_row(event.y)
            if not row:
                return
            if row not in tree.selection():
                tree.selection_set(row)     # right-click outside the selection: just that paper
            menu.delete(0, "end")
            chosen = [k for k in tree.selection() if prog.meta.get(k) == "unchanged"]
            if chosen and not prog.active_runs:
                menu.add_command(label="Check anyway" if len(chosen) == 1 else f"Check these {len(chosen)} unchanged anyway",
                                 command=lambda: on_recheck(chosen))
            to_review = [k for k in tree.selection() if prog.meta.get(k) == "review"]
            if to_review and not dry_run:
                menu.add_command(label="Review suggestions…", command=lambda: on_review(to_review))
            menu.add_command(label="Show in Zotero", command=on_open)
            menu.tk_popup(event.x_root, event.y_root)

        tree.bind("<Button-3>", on_menu)
    root.protocol("WM_DELETE_WINDOW", on_close)

    def chip_keys() -> dict[str, list[str]]:
        import re

        return {re.sub(r"\d+ ", "", text, count=1): keys
                for _name, chips in prog.chip_groups() for _style, text, keys, _tip in chips}

    def relayout() -> None:
        """Place the rows: only the chosen chip's papers, in the chosen order, striped anew."""
        view["dirty"] = False
        keys = list(prog.order)
        if view["filter"] is not None:
            wanted = set(chip_keys().get(view["filter"], []))
            keys = [k for k in keys if k in wanted]
        if view["sort"]:
            keys.sort(key=lambda k: prog.sort_key(k, view["sort"]), reverse=view["desc"])
        keep = set(keys)
        for k in tree.get_children():
            if k not in keep:
                tree.detach(k)
        for i, k in enumerate(keys):
            if not tree.exists(k):
                continue
            tree.move(k, "", i)
            tone = [t for t in tree.item(k, "tags") if t != "stripe"]
            tree.item(k, tags=tuple(tone) + (("stripe",) if i % 2 else ()))
        if view["filter"] is not None:
            show_all.configure(text=f"Showing {len(keys)} of {len(prog.order)} · Show all")
            show_all.pack(side="right")
        else:
            show_all.pack_forget()

    show_all.bind("<Button-1>", lambda _e: set_filter(None))

    def redraw(key: str) -> None:
        values = [prog.labels.get(key, key)]
        if with_meta:
            values.append(prog.meta_text(key))
        if with_pdfs:
            values.append(prog.fetch_text(key))
        tone = prog.tone(key)
        if tree.exists(key):
            stripe = "stripe" in tree.item(key, "tags")
            tree.item(key, values=values, tags=(tone, "stripe") if stripe else (tone,))
        else:
            stripe = len(tree.get_children()) % 2 == 1
            tree.insert("", "end", iid=key, values=values, tags=(tone, "stripe") if stripe else (tone,))
        if view["filter"] is not None or view["sort"]:
            view["dirty"] = True            # placed again at the next refresh

    def refresh() -> None:
        head.configure(text=prog.headline())
        bar.configure(value=int(prog.fraction() * 1000))
        finished = prog.active_runs == 0
        bar.configure(style="Done.Thin.Horizontal.TProgressbar" if finished else "Thin.Horizontal.TProgressbar")
        for i, lab in enumerate(step_labels):
            if i < prog.stage_index or (prog.main_done and i <= prog.stage_index):
                lab.configure(text=f"✓  {prog.stages[i]}", style="StepDone.TLabel")
            elif i == prog.stage_index:
                lab.configure(text=f"{i + 1}  {prog.stages[i]}", style="StepNow.TLabel")
            else:
                lab.configure(text=f"{i + 1}  {prog.stages[i]}", style="StepTodo.TLabel")
        groups = prog.chip_groups()
        if groups != shown_chips[0]:
            shown_chips[0] = groups
            for w in chip_widgets:
                w.destroy()
            chip_widgets.clear()
            import re

            if view["filter"] is not None and view["filter"] not in chip_keys():
                view["filter"], view["dirty"] = None, True      # its papers are gone (all reviewed, say)
            for g, (_name, chips) in enumerate(groups):
                for c, (style, text, _keys, explanation) in enumerate(chips):
                    cid = re.sub(r"\d+ ", "", text, count=1)
                    active = view["filter"] == cid
                    chip = ttk.Label(chips_box, text=text, style=f"{style}{'Active' if active else ''}.TLabel",
                                     cursor="hand2")
                    chip.pack(side="left", padx=(16 if g and not c else 0, 6))   # a gap between metadata and PDFs
                    chip.bind("<Button-1>", lambda _e, cid=cid: set_filter(cid))
                    _Tooltip(chip, explanation + (" Click to show all papers again." if active
                                                  else " Click to show only these."))
                    chip_widgets.append(chip)
        unchanged = [k for k in prog.order if prog.meta.get(k) == "unchanged"]
        if unchanged and not prog.active_runs:
            recheck_btn.configure(text="Check it anyway" if len(unchanged) == 1 else "Check all unchanged anyway")
            recheck_btn.pack(side="left", padx=(0, 8))
        else:
            recheck_btn.pack_forget()
        items = prog.todo()
        state = (items, prog.browser_busy)
        if state != shown_todo[0]:
            shown_todo[0] = state
            for w in todo_rows:
                w.destroy()
            todo_rows.clear()
            for tone, text, action, explanation in items:
                row = ttk.Frame(todo_inner, style="Panel.TFrame")
                row.pack(fill="x", pady=(3, 0), ipady=1)
                lab = ttk.Label(row, text=text, style=f"Todo{tone.capitalize()}.TLabel")
                lab.pack(side="left")
                _Tooltip(lab, explanation)
                if action == "own":
                    n = min(len(prog.for_own_browser()), 5)
                    ttk.Button(row, text=f"Open {n} in my browser", command=on_own_browser,
                               style="SmallAccent.TButton").pack(side="right")
                elif action == "browser":
                    ttk.Button(row, text="Search with browser", command=on_browser, style="Soft.TButton",
                               state="disabled" if prog.browser_busy else "normal").pack(side="right")
                elif action == "report":
                    ttk.Button(row, text="Show report", command=on_report, style="Soft.TButton").pack(side="right")
                elif action == "review" and not dry_run:
                    ttk.Button(row, text="Review", command=on_review, style="SmallAccent.TButton").pack(side="right")
                todo_rows.append(row)
            if items:
                todo_card.pack(side="bottom", fill="x", pady=(12, 0), before=holder)
            else:
                todo_card.pack_forget()
        if view["dirty"]:
            relayout()

    def poll() -> None:
        finished_now = False
        try:
            while True:
                event = events.get_nowait()
                if event.get("status") == "run finished":
                    prog.active_runs -= 1
                    if event.get("browser"):
                        prog.browser_busy = False
                    if event.get("main"):
                        prog.main_done = True
                    finished_now = prog.active_runs == 0
                    continue
                if event.get("status") == "failed":
                    messagebox.showerror(title, event.get("detail", ""))
                    continue
                key = prog.apply(event)
                if key:
                    redraw(key)
        except queue.Empty:
            pass
        refresh()
        if finished_now and quiet and not prog.todo():
            root.after(5000, root.destroy)     # nothing for the user: done without a word
            finished_now = False
        if finished_now:
            # The end: bring the window forward with the summary and the buttons.
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            root.after(800, lambda: root.attributes("-topmost", False))
            root.bell()
        root.after(200, poll)

    if quiet:
        root.iconify()
    start(run, dict(run_kwargs), main_uses_browser, main=True)
    root.after(200, poll)
    root.mainloop()

"""Library maintenance in the right order: metadata, then PDFs, then metadata again.

1. **Metadata audit** with ``--apply`` (fills empty fields, makes corrections two
   sources agree on, sends the rest to the review list) and the attachment check
   (a wrong PDF, a manuscript or a proof is replaced when the right one is found).
   A filled-in DOI is what the fetcher finds PDFs by.
2. **Fetch full text** for the items still without a PDF (the normal steps; the
   browser step stays a button).
3. **Audit again** the items that no registry knew and now have a PDF: its first
   pages (a DOI printed there, or Gemini's reading) are a source now.

The search index and the passage labels follow at the next index update.

``index=True`` adds the search index's incremental update as a last step (the import trigger).

``fetch=False`` keeps it to step 1 (metadata only: no PDFs downloaded, wrong PDFs
are tagged but not replaced).

``new=True`` takes the items added since the last such run (the first run only
remembers the time); this is what runs before the index update when Claude
Desktop starts, if ``"maintenance": {"new_items": true}`` is in config.json
(``"fetch": false`` there for metadata only).
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Callable
from pathlib import Path


def _state_path() -> Path:
    from zotero_mcp.fulltext_fetch import config_dir

    return config_dir() / "maintenance.json"


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(state: dict) -> None:
    try:
        _state_path().parent.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError:
        pass


def _added(raw: dict) -> str:
    return str(raw.get("data", raw).get("dateAdded") or "")


def new_item_keys(backend, since: str | None) -> list[str]:
    """Regular items added after ``since`` (ISO time), oldest first."""
    from zotero_mcp.metadata_audit import AUDITED_TYPES

    items = backend.list_items("-attachment", limit=100000) or []
    out = []
    for raw in items:
        data = raw.get("data", raw)
        if data.get("itemType") not in AUDITED_TYPES:
            continue
        added = _added(raw).replace("Z", "")
        if since and added and added > since.replace("Z", ""):
            out.append((added, raw.get("key") or data.get("key")))
    return [k for _a, k in sorted(out)]


def _config(config_path: str | Path | None = None) -> dict:
    try:
        path = Path(config_path) if config_path else Path.home() / ".config" / "zotero-mcp" / "config.json"
        return json.loads(path.read_text(encoding="utf-8")).get("maintenance") or {}
    except Exception:
        return {}


def config_new_items(config_path: str | Path | None = None) -> bool:
    """Whether config.json asks for new items to be maintained at startup."""
    return bool(_config(config_path).get("new_items"))


def config_fetch(config_path: str | Path | None = None) -> bool:
    """Whether the startup run fetches PDFs too (``"fetch": false`` keeps it to metadata)."""
    return _config(config_path).get("fetch", True) is not False


INDEX_STAGE = "Search index"


def stages(fetch: bool, index: bool = False) -> list[str]:
    return (["Metadata", "PDFs", "Metadata again"] if fetch else ["Metadata"]) + ([INDEX_STAGE] if index else [])


def _not_recently_missed(backend, keys: list[str]) -> list[str]:
    """The papers not searched in vain within the fetcher's retry period (30 days): neither for a
    PDF (tag fulltext/not-found) nor for the published version of a manuscript or proof. After
    that they are searched again, since authors and publishers put PDFs online later."""
    from zotero_mcp import fulltext_fetch as ff

    if not keys:
        return []
    state = ff._load_state()
    cutoff = _dt.datetime.now() - _dt.timedelta(days=ff.Settings.load().retry_days)
    out = []
    for key in keys:
        entry = state.get(key) or {}
        last = entry.get("last_attempt")
        try:
            missed = entry.get("status") == "not found" and bool(last) and _dt.datetime.fromisoformat(last) > cutoff
        except ValueError:
            missed = False
        if not missed:
            out.append(key)
    return out


def update_index(log: Callable[[str], None] = print) -> bool:
    """The search index's incremental update (new and changed items, then their passage labels),
    in its own process at low priority, as at Claude Desktop's start. It takes the index's update
    lock, so it waits for nobody and skips when another update (Claude Desktop's) is running."""
    import subprocess
    import sys

    kwargs: dict = {"stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                                   | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000))
    try:
        done = subprocess.run([sys.executable, "-m", "zotero_mcp.background_update"], timeout=3600, **kwargs)
    except Exception as e:
        log(f"The search index could not be updated: {type(e).__name__}: {e}")
        return False
    log("Search index updated." if done.returncode == 0 else
        f"The search index update ended with code {done.returncode}; the next start of Claude Desktop catches up.")
    return done.returncode == 0


def run(*, keys: list[str] | None = None, collection: str | None = None, new: bool = False, since: str | None = None,
        apply: bool = True, fetch: bool = True, index: bool = False, log: Callable[[str], None] = print,
        progress: Callable[[dict], None] | None = None, backend=None, audit_run=None, fetch_run=None,
        index_run: Callable[..., bool] | None = None, every: bool = False, retraction_run=None,
        writer_factory=None) -> dict:
    """Audit, fetch, audit again. Returns a summary. ``progress`` receives the audit's and the
    fetcher's events and {"status": "stage", "detail": name, "index": i} at each step.

    Papers unchanged since their last check (and its rules) are not checked again unless
    ``every``: they only get a retraction check (at most monthly), and a PDF search only when they
    have none and were not searched in vain within the fetcher's retry period."""
    from zotero_mcp import fulltext_fetch as ff
    from zotero_mcp import metadata_audit as ma

    if backend is None:
        from zotero_mcp import library

        backend = library.get_library_backend()
    audit_run = audit_run or ma.run
    fetch_run = fetch_run or ff.run
    notify = progress or (lambda event: None)
    names = stages(fetch, index)
    n = len(names)

    def stage(i: int) -> None:
        notify({"key": "", "status": "stage", "detail": names[i], "index": i})

    state = _load()
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    if new or since:
        start = since or state.get("last_new_items")
        if not start:
            state["last_new_items"] = now
            _save(state)
            log("First run: from now on, items added to Zotero are checked"
                f"{' and fetched' if fetch else ''} here. (For earlier items: zotero-mcp maintain --since YYYY-MM-DD.)")
            return {"items": 0}
        keys = new_item_keys(backend, start)
        if not keys:
            state["last_new_items"] = now
            _save(state)
            log("No new items since the last run.")
            return {"items": 0}
        log(f"{len(keys)} item(s) added since {start[:16].replace('T', ' ')}.")
    summary: dict = {}
    if collection and not keys:
        from zotero_mcp.metadata_audit import AUDITED_TYPES

        keys = [i.get("key") or i.get("data", {}).get("key") for i in backend.collection_items(collection) or []
                if i.get("data", {}).get("itemType") in AUDITED_TYPES]
    keys = list(keys or [])

    log(f"1/{n} Metadata ...")
    stage(0)
    every = every or len(keys) <= SMALL_SELECTION
    changed, unchanged = (keys, []) if every or not apply else split_unchanged(backend, keys)
    if unchanged:
        log(f"{len(unchanged)} paper(s) unchanged since their last check: only a retraction check "
            f"(zotero-mcp maintain --all checks them again too).")
        from zotero_mcp.fulltext_fetch import ItemInfo

        known = backend.get_items(unchanged) or {}
        seen = _load().get("checked_modified") or {}
        for key in unchanged:
            label = ItemInfo.from_zotero(known[key]).label if key in known else key
            when = (seen.get(key) or {}).get("date", "")
            when = f", checked {_dt.date.fromisoformat(when).strftime('%d-%m-%Y')}" if when else ""
            notify({"key": key, "label": label, "phase": "metadata", "status": "unchanged",
                    "detail": f"unchanged{when}"})
        if writer_factory is None:
            from zotero_mcp.metadata_audit import MetadataWriter

            writer_factory = MetadataWriter
        summary["retractions"] = (retraction_run or check_retractions)(
            unchanged, backend=backend, log=log, progress=progress, writer=writer_factory())
    audits: list = []
    if changed:
        # The replacements for wrong PDFs are fetched in step 2, with the rest.
        report = audit_run(keys=changed, apply=apply, log=log, fetch_replacements=False, progress=progress)
        audits = getattr(report, "audits", []) or []
        summary["audit"] = report.totals() if hasattr(report, "totals") else {}
        if getattr(report, "report_path", ""):
            notify({"key": "", "status": "done", "detail": report.report_path})
    unknown = [a.key for a in audits if any("no registry record" in f for f in a.flags)]
    fetch_keys = changed + _not_recently_missed(backend, unchanged)

    attached: set[str] = set()
    if fetch and keys:
        log(f"2/{n} Full text for the items without a PDF ...")
        stage(1)
        wanted = [k for k in keys if k in set(fetch_keys)]
        fetched = fetch_run(keys=wanted, log=log, dry_run=not apply, progress=progress) if wanted else None
        attached = {r.key for r in getattr(fetched, "results", []) if r.status == "attached"}
        summary["fetched"] = len(attached)

    if fetch:
        again = [k for k in unknown if k in attached]
        stage(2)
        if again:
            log(f"3/{n} Metadata again for {len(again)} item(s) no registry knew, now with their PDF ...")
            report2 = audit_run(keys=again, apply=apply, log=log, progress=progress)
            summary["audit_again"] = report2.totals() if hasattr(report2, "totals") else {}
            if getattr(report2, "report_path", ""):
                notify({"key": "", "status": "done", "detail": report2.report_path})
        else:
            log(f"3/{n} Nothing to check again.")
    if apply:
        remember_checked(backend, keys or [])
    if index and apply:
        log(f"{n}/{n} Search index ...")
        stage(n - 1)
        summary["indexed"] = (index_run or update_index)(log=log)
    if new or since:
        state["last_new_items"] = now
        _save(state)
    summary["items"] = len(keys or [])
    return summary


# ---------------------------------------------------------------------------
# Papers unchanged since their last check
# ---------------------------------------------------------------------------

#: The checking rules' version. A paper checked under older rules counts as changed, so
#: the next run checks it again; raised when the rules improve enough to be worth that.
RULES = 1
#: A selection this small is always checked fully: clicking a few papers means "check these".
SMALL_SELECTION = 5
#: Days between retraction checks of an unchanged paper.
RETRACTION_DAYS = 30
#: Days after which a recent article still without volume or pages (online first) is checked
#: again: the registries fill those in once the article is in an issue.
OPEN_RECHECK_DAYS = 30


def _still_open(raw: dict) -> bool:
    """A recent journal article without volume or pages: its metadata is likely to change online."""
    data = raw.get("data", raw)
    if data.get("itemType") != "journalArticle":
        return False
    year = re.search(r"\d{4}", str(data.get("date") or ""))
    recent = bool(year) and int(year.group()) >= _dt.date.today().year - 2
    return recent and not (str(data.get("volume") or "").strip() and str(data.get("pages") or "").strip())


def _modified(raw: dict) -> str:
    return str(raw.get("data", raw).get("dateModified") or "")


def _stamps(backend, items: dict) -> dict[str, str]:
    """Each paper's latest change: its own, or one of its attachments' (a PDF added or replaced
    does not change the paper's own modified time in Zotero)."""
    try:
        children = backend.get_children(list(items)) or {}
    except Exception:
        children = {}
    out = {}
    for key, raw in items.items():
        times = [_modified(raw)] + [_modified(c) for c in children.get(key) or []
                                    if (c.get("data", c).get("itemType") == "attachment")]
        out[key] = max((t.replace("T", " ").replace("Z", "") for t in times if t), default="")
    return out


def remember_checked(backend, keys: list[str]) -> None:
    """Note each paper's latest change (and the rules' version) after a check, the check's own
    changes included, so a later run knows which papers changed since."""
    if not keys:
        return
    try:
        found = backend.get_items(list(keys)) or {}
    except Exception:
        return
    items = {k: v for k, v in found.items() if k in set(keys)}
    stamps = _stamps(backend, items)
    state = _load()
    seen = state.setdefault("checked_modified", {})
    today = _dt.date.today().isoformat()
    for key, stamp in stamps.items():
        if stamp:
            seen[key] = {"stamp": stamp, "rules": RULES, "date": today, "open": _still_open(items[key])}
    _save(state)


def split_unchanged(backend, keys: list[str]) -> tuple[list[str], list[str]]:
    """(to check, unchanged). To check: never checked; changed in Zotero since (the paper or an
    attachment, e.g. a PDF added later); checked under older rules; or a recent article that still
    lacked volume or pages, a month after its last check (online-first metadata gets completed)."""
    seen = _load().get("checked_modified") or {}
    try:
        found = backend.get_items(list(keys)) or {}
    except Exception:
        return list(keys), []
    stamps = _stamps(backend, found)
    today = _dt.date.today()
    changed, unchanged = [], []
    for key in keys:
        entry = seen.get(key)
        same = (isinstance(entry, dict) and stamps.get(key) and entry.get("stamp") == stamps[key]
                and entry.get("rules") == RULES)
        if same and entry.get("open"):
            try:
                same = (today - _dt.date.fromisoformat(entry.get("date", ""))).days < OPEN_RECHECK_DAYS
            except ValueError:
                same = False
        (unchanged if same else changed).append(key)
    return changed, unchanged


def check_retractions(keys: list[str], *, backend, log: Callable[[str], None] = print,
                      progress: Callable[[dict], None] | None = None, http=None, settings=None,
                      writer=None, sleep=None, every_days: int = RETRACTION_DAYS) -> dict:
    """Crossref's notices (Retraction Watch data) for papers unchanged since their last check, at
    most every ``every_days``: a new retraction is tagged ``retracted`` with a note; a correction,
    erratum or expression of concern from the last year gets a note. Older notices are noted silently."""
    import time

    from zotero_mcp import fulltext_fetch as ff
    from zotero_mcp import metadata_audit as ma

    state = ma._load_state()
    today = _dt.date.today()

    def due(key: str) -> bool:
        last = (state.get(key) or {}).get("notices_checked")
        try:
            return not last or (today - _dt.date.fromisoformat(last)).days >= every_days
        except ValueError:
            return True

    keys = [k for k in keys if due(k)]
    totals = {"checked": 0, "retracted": 0, "notices": 0, "not_checked": 0}
    if not keys:
        return totals
    settings = settings or ff.Settings.load()
    http = http or ff.Http(settings)
    sleep = sleep or time.sleep
    notify = progress or (lambda event: None)
    found = backend.get_items(list(keys)) or {}
    year_ago = (today - _dt.timedelta(days=365)).isoformat()
    for key in keys:
        raw = found.get(key)
        if not raw:
            continue
        info = ff.ItemInfo.from_zotero(raw)
        if not info.doi:
            continue
        rec = ma.crossref(info.doi, http, settings)
        sleep(0.1)                          # Crossref's polite pool: a few requests a second at most
        if rec is None:
            totals["not_checked"] += 1
            continue
        totals["checked"] += 1
        entry = state.setdefault(key, {})
        entry["notices_checked"] = today.isoformat()
        known = set(entry.get("notices") or [])
        new = [u for u in rec.updates if (u[2] or u[0] + u[1]) not in known]
        entry["notices"] = sorted(known | {u[2] or u[0] + u[1] for u in new})
        # A correction from years ago is old news: only recent ones (and every retraction) are shown.
        new = [u for u in new if (u[1] or "9999") >= year_ago
               or (u[0] or "").lower().replace("-", "_") in ("retraction", "withdrawal", "removal",
                                                              "partial_retraction", "expression_of_concern")]
        if not new:
            continue
        audit = ma.ItemAudit(key, info.label, info.item_type)
        rec.updates = new
        ma._note_updates(audit, rec)
        detail = "; ".join(audit.flags)
        totals["retracted" if audit.retracted else "notices"] += 1
        log(f"  {info.label} [{key}]: {detail}")
        notify({"key": key, "label": info.label, "phase": "metadata",
                "status": "retracted" if audit.retracted else "notice", "detail": detail})
        if writer is not None:
            try:
                if audit.retracted:
                    writer.apply(audit, [], tags_add=[ma.TAG_RETRACTED])
                writer.add_note(key, f"<p><b>{'Retracted' if audit.retracted else 'Notice'} "
                                     f"({ma._today()})</b>: {ma.html.escape(detail)}.</p>")
            except Exception as e:
                log(f"    -> could not write: {type(e).__name__}: {e}")
    ma._save_state(state)
    return totals

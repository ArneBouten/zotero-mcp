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
        index_run: Callable[..., bool] | None = None, fetch_keys: list[str] | None = None,
        _stages: list[str] | None = None, _offset: int = 0) -> dict:
    """Audit, fetch, audit again. Returns a summary. ``progress`` receives the audit's and the
    fetcher's events and {"status": "stage", "detail": name, "index": i} at each step."""
    from zotero_mcp import fulltext_fetch as ff
    from zotero_mcp import metadata_audit as ma

    if backend is None:
        from zotero_mcp import library

        backend = library.get_library_backend()
    audit_run = audit_run or ma.run
    fetch_run = fetch_run or ff.run
    notify = progress or (lambda event: None)
    names = _stages or stages(fetch, index)
    n = len(names) - _offset

    def stage(i: int) -> None:
        notify({"key": "", "status": "stage", "detail": names[i + _offset], "index": i + _offset})

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

    log(f"1/{n} Metadata ...")
    stage(0)
    # The replacements for wrong PDFs are fetched in step 2, with the rest.
    report = audit_run(keys=keys, collection=collection, apply=apply, log=log, fetch_replacements=False,
                       progress=progress)
    audits = getattr(report, "audits", []) or []
    summary["audit"] = report.totals() if hasattr(report, "totals") else {}
    if getattr(report, "report_path", ""):
        notify({"key": "", "status": "done", "detail": report.report_path})
    keys = keys or [a.key for a in audits]
    unknown = [a.key for a in audits if any("no registry record" in f for f in a.flags)]

    attached: set[str] = set()
    if fetch and keys:
        log(f"2/{n} Full text for the items without a PDF ...")
        stage(1)
        wanted = [k for k in keys if fetch_keys is None or k in set(fetch_keys)]
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
# The monthly check
# ---------------------------------------------------------------------------

#: Days between monthly checks; a check that could not finish its backlog comes back the next day.
MONTHLY_DAYS = 30
#: Papers fully checked per monthly run (registries' daily allowances); the rest the next day.
MONTHLY_MAX_FULL = 400
RETRACTION_STAGE = "Retractions"


def monthly_stages() -> list[str]:
    return [RETRACTION_STAGE] + stages(True, True)


def _modified(raw: dict) -> str:
    return str(raw.get("data", raw).get("dateModified") or "")


def remember_checked(backend, keys: list[str]) -> None:
    """Note each paper's Zotero "modified" time after a check (and after the check's own writes),
    so the monthly check knows which papers changed since."""
    if not keys:
        return
    try:
        found = backend.get_items(list(keys)) or {}
    except Exception:
        return
    state = _load()
    seen = state.setdefault("checked_modified", {})
    for key in keys:
        raw = found.get(key)
        if raw and _modified(raw):
            seen[key] = _modified(raw)
    _save(state)


def monthly_plan(backend) -> tuple[list[str], list[str]]:
    """(papers to check fully, papers to check for retractions only). Fully: never checked, or
    changed in Zotero since their last check. The rest, when they have a DOI: retractions only."""
    from zotero_mcp.metadata_audit import AUDITED_TYPES, norm_doi

    seen = _load().get("checked_modified") or {}
    full, recheck = [], []
    for raw in backend.list_items("-attachment", limit=100000) or []:
        data = raw.get("data", raw)
        if data.get("itemType") not in AUDITED_TYPES:
            continue
        key = raw.get("key") or data.get("key")
        if key not in seen or (_modified(raw) and _modified(raw) != seen[key]):
            full.append((_modified(raw), key))
        elif norm_doi(data.get("DOI") or ""):
            recheck.append(key)
    return [k for _m, k in sorted(full)], recheck


def monthly_due() -> tuple[bool, str]:
    state = _load()
    last = state.get("last_monthly")
    if not last:
        return True, "first monthly check"
    try:
        days = (_dt.datetime.now() - _dt.datetime.fromisoformat(last)).days
    except ValueError:
        return True, "last date unreadable"
    if state.get("monthly_backlog") and days >= 1:
        return True, f"{state['monthly_backlog']} papers left from the last run"
    return days >= MONTHLY_DAYS, f"last check {days} day(s) ago"


def check_retractions(keys: list[str], *, backend, log: Callable[[str], None] = print,
                      progress: Callable[[dict], None] | None = None, http=None, settings=None,
                      writer=None, sleep=None) -> dict:
    """Crossref's notices (Retraction Watch data) for papers checked before: a new retraction is
    tagged ``retracted`` with a note; a new correction, erratum or expression of concern gets a note."""
    import time

    from zotero_mcp import fulltext_fetch as ff
    from zotero_mcp import metadata_audit as ma

    settings = settings or ff.Settings.load()
    http = http or ff.Http(settings)
    sleep = sleep or time.sleep
    notify = progress or (lambda event: None)
    found = backend.get_items(list(keys)) or {} if keys else {}
    state = ma._load_state()
    totals = {"checked": 0, "retracted": 0, "notices": 0, "not_checked": 0}
    for i, key in enumerate(keys, 1):
        notify({"key": "", "status": "count", "done": i - 1, "total": len(keys)})
        raw = found.get(key)
        if not raw:
            continue
        info = ff.ItemInfo.from_zotero(raw)
        rec = ma.crossref(info.doi, http, settings) if info.doi else None
        sleep(0.1)                          # Crossref's polite pool: a few requests a second at most
        if rec is None:
            totals["not_checked"] += 1
            continue
        totals["checked"] += 1
        entry = state.setdefault(key, {})
        known = set(entry.get("notices") or [])
        new = [u for u in rec.updates if (u[2] or u[0] + u[1]) not in known]
        entry["notices"] = sorted(known | {u[2] or u[0] + u[1] for u in new})
        # A correction from years ago is old news: only recent ones (and every retraction) are shown.
        year_ago = (_dt.date.today() - _dt.timedelta(days=365)).isoformat()
        new = [u for u in new if (u[1] or "9999") >= year_ago
               or (u[0] or "").lower().replace("-", "_") in ("retraction", "withdrawal", "removal",
                                                              "partial_retraction", "expression_of_concern")]
        if not new:
            continue
        audit = ma.ItemAudit(key, info.label, info.item_type)
        rec.updates = new
        ma._note_updates(audit, rec)
        detail = "; ".join(audit.flags)
        if audit.retracted:
            totals["retracted"] += 1
            status = "retracted"
        else:
            totals["notices"] += 1
            status = "notice"
        log(f"  {info.label} [{key}]: {detail}")
        notify({"key": key, "label": info.label, "phase": "metadata", "status": status, "detail": detail})
        if writer is not None:
            try:
                if audit.retracted:
                    writer.apply(audit, [], tags_add=[ma.TAG_RETRACTED])
                writer.add_note(key, f"<p><b>{'Retracted' if audit.retracted else 'Notice'} "
                                     f"({ma._today()})</b>: {ma.html.escape(detail)}.</p>")
            except Exception as e:
                log(f"    -> could not write: {type(e).__name__}: {e}")
    notify({"key": "", "status": "count", "done": len(keys), "total": len(keys)})
    ma._save_state(state)
    return totals


def monthly(*, force: bool = False, log: Callable[[str], None] = print,
            progress: Callable[[dict], None] | None = None, backend=None, audit_run=None, fetch_run=None,
            index_run=None, retraction_run=None, writer_factory=None, max_full: int = MONTHLY_MAX_FULL) -> dict:
    """The monthly check, without Claude: papers changed (or never checked) are checked fully
    (metadata, PDFs for those without one and not recently searched in vain, metadata again,
    search index); every other paper with a DOI only for new retractions and corrections."""
    from zotero_mcp import fulltext_fetch as ff

    due, why = monthly_due()
    if not due and not force:
        log(f"No monthly check due ({why}).")
        return {"due": False}
    if backend is None:
        from zotero_mcp import library

        backend = library.get_library_backend()
    notify = progress or (lambda event: None)
    names = monthly_stages()
    full, recheck = monthly_plan(backend)
    backlog = full[max_full:]
    full = full[:max_full]
    log(f"Monthly check ({why}): {len(full)} paper(s) to check fully"
        f"{f' ({len(backlog)} more the next day)' if backlog else ''}, {len(recheck)} for retractions.")

    notify({"key": "", "status": "stage", "detail": names[0], "index": 0})
    log(f"1/{len(names)} Retractions and corrections ...")
    if writer_factory is None:
        from zotero_mcp.metadata_audit import MetadataWriter

        writer_factory = MetadataWriter
    summary: dict = {"retractions": (retraction_run or check_retractions)(
        recheck, backend=backend, log=log, progress=progress, writer=writer_factory())}
    remember_checked(backend, [k for k in recheck])

    if full:
        # Papers searched in vain within the fetcher's retry period are not searched again.
        found = backend.get_items(full) or {}
        cutoff = _dt.datetime.now() - _dt.timedelta(days=ff.Settings.load().retry_days)
        fstate = ff._load_state()

        def recent_miss(key: str) -> bool:
            tags = {t.get("tag") for t in (found.get(key) or {}).get("data", {}).get("tags") or []}
            last = (fstate.get(key) or {}).get("last_attempt")
            try:
                return ff.TAG_NOT_FOUND in tags and bool(last) and _dt.datetime.fromisoformat(last) > cutoff
            except ValueError:
                return False

        summary.update(run(keys=full, fetch=True, index=True, log=log, progress=progress, backend=backend,
                           audit_run=audit_run, fetch_run=fetch_run, index_run=index_run,
                           fetch_keys=[k for k in full if not recent_miss(k)], _stages=names, _offset=1))
    else:
        log("Nothing changed since the last check.")
    state = _load()
    state["last_monthly"] = _dt.datetime.now().isoformat(timespec="seconds")
    state["monthly_backlog"] = len(backlog)
    _save(state)
    return summary

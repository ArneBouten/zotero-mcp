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

``new=True`` takes the items added since the last such run (the first run only
remembers the time); this is what runs before the index update when Claude
Desktop starts, if ``"maintenance": {"new_items": true}`` is in config.json.
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


def config_new_items(config_path: str | Path | None = None) -> bool:
    """Whether config.json asks for new items to be maintained at startup."""
    try:
        path = Path(config_path) if config_path else Path.home() / ".config" / "zotero-mcp" / "config.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        return bool((cfg.get("maintenance") or {}).get("new_items"))
    except Exception:
        return False


def run(*, keys: list[str] | None = None, collection: str | None = None, new: bool = False, since: str | None = None,
        apply: bool = True, fetch: bool = True, window: bool = False, log: Callable[[str], None] = print,
        backend=None, audit_run=None, fetch_run=None) -> dict:
    """Audit, fetch, audit again. Returns a summary."""
    from zotero_mcp import fulltext_fetch as ff
    from zotero_mcp import metadata_audit as ma

    if backend is None:
        from zotero_mcp import library

        backend = library.get_library_backend()
    audit_run = audit_run or ma.run
    fetch_run = fetch_run or ff.run
    state = _load()
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    if new or since:
        start = since or state.get("last_new_items")
        if not start:
            state["last_new_items"] = now
            _save(state)
            log("First run: from now on, items added to Zotero are checked and fetched here. "
                "(For earlier items: zotero-mcp maintain --since YYYY-MM-DD.)")
            return {"items": 0}
        keys = new_item_keys(backend, start)
        if not keys:
            state["last_new_items"] = now
            _save(state)
            log("No new items since the last run.")
            return {"items": 0}
        log(f"{len(keys)} item(s) added since {start[:16].replace('T', ' ')}.")
    summary: dict = {}

    log("1/3 Metadata ...")
    # The replacements for wrong PDFs are fetched in step 2, with the rest.
    report = audit_run(keys=keys, collection=collection, apply=apply, log=log, fetch_replacements=False)
    audits = getattr(report, "audits", []) or []
    summary["audit"] = report.totals() if hasattr(report, "totals") else {}
    keys = keys or [a.key for a in audits]
    unknown = [a.key for a in audits if any("no registry record" in f for f in a.flags)]

    attached: set[str] = set()
    if fetch and keys:
        log("2/3 Full text for the items without a PDF ...")
        kwargs = dict(keys=keys, log=log, dry_run=not apply)
        if window:
            from zotero_mcp import fulltext_window

            fulltext_window.run_window(kwargs)
            attached = {k for k in keys if not ff.select_items(keys=[k], backend=backend)[0]}
        else:
            fetched = fetch_run(**kwargs)
            attached = {r.key for r in getattr(fetched, "results", []) if r.status == "attached"}
        summary["fetched"] = len(attached)

    again = [k for k in unknown if k in attached]
    if again:
        log(f"3/3 Metadata again for {len(again)} item(s) no registry knew, now with their PDF ...")
        report2 = audit_run(keys=again, apply=apply, log=log)
        summary["audit_again"] = report2.totals() if hasattr(report2, "totals") else {}
    else:
        log("3/3 Nothing to check again.")
    if new or since:
        state["last_new_items"] = now
        _save(state)
    summary["items"] = len(keys or [])
    return summary

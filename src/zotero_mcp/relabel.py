"""Give indexed passages their printed page, heading, chapter and section.

Metadata only: the passages' text and embeddings are not touched, so nothing
is re-embedded and nothing costs embedding money. Items are re-read when their
passages carry no labels of the current ``structure.STRUCTURE_VERSION`` (new
or re-indexed items, or after an upgrade); ``force`` re-reads everything.

Gemini (optional, ``semantic_search.structure.gemini``) judges the heading
candidates of PDFs whose bookmarks do not describe their sections. Its answers
are cached per file, so a second run costs nothing.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from zotero_mcp import structure as st
from zotero_mcp.gemini_util import DEFAULT_MODEL

logger = logging.getLogger(__name__)



def structure_dir() -> Path:
    from zotero_mcp.fulltext_fetch import config_dir

    return config_dir() / "structure"


def structure_config(config_path: str | None) -> dict:
    """``semantic_search.structure`` from the config file."""
    try:
        from zotero_mcp.cli import _semantic_config_path

        path = Path(config_path) if config_path else _semantic_config_path(None)
        with open(path, encoding="utf-8") as f:
            return (json.load(f).get("semantic_search") or {}).get("structure") or {}
    except Exception:
        return {}


class _Cache:
    """Gemini answers per (file, prompt version), on disk."""

    def __init__(self) -> None:
        self.dir = structure_dir() / "gemini-cache"

    def get(self, key: str) -> str | None:
        try:
            return (self.dir / f"{key}.json").read_text(encoding="utf-8")
        except OSError:
            return None

    def put(self, key: str, value: str) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / f"{key}.json").write_text(value, encoding="utf-8")
        except OSError:
            pass


def _item_fields(reader, key: str) -> dict:
    if hasattr(reader, "run"):   # a SerialReader: query on its own thread
        return reader.run(lambda r: _item_fields_direct(r, key))
    return _item_fields_direct(reader, key)


def _item_fields_direct(reader, key: str) -> dict:
    out = {"pages": "", "title": "", "itemType": ""}
    try:
        conn = reader._get_connection()
        rows = conn.execute(
            """SELECT f.fieldName, v.value FROM items i
               JOIN itemData d ON d.itemID = i.itemID
               JOIN fieldsCombined f ON f.fieldID = d.fieldID
               JOIN itemDataValues v ON v.valueID = d.valueID
               WHERE i.key = ? AND f.fieldName IN ('pages', 'title')""", (key,)).fetchall()
        for name, value in rows:
            out[name] = value or ""
        row = conn.execute("""SELECT t.typeName FROM items i JOIN itemTypesCombined t ON t.itemTypeID = i.itemTypeID
                              WHERE i.key = ?""", (key,)).fetchone()
        if row:
            out["itemType"] = row[0]
    except Exception as e:
        logger.debug("item fields for %s: %s", key, e)
    return out


def _pdf_for(reader, key: str, attachment_keys: str = "") -> Path | None:
    """The PDF that was indexed (its key is in the passage metadata), else the item's first PDF."""
    import re

    for att in [a for a in re.split(r"[,;\s]+", attachment_keys or "") if a]:
        try:
            path = reader.resolve_attachment_file(att)
        except Exception:
            path = None
        if path and str(path).lower().endswith(".pdf"):
            return Path(path)
    try:
        atts = reader.get_attachment_paths(key)
    except Exception:
        return None
    pdfs = [a["resolved_path"] for a in atts
            if a.get("exists") and a.get("resolved_path") and str(a["resolved_path"]).lower().endswith(".pdf")]
    return Path(pdfs[0]) if pdfs else None


def _chunks(collection, key: str) -> list[dict]:
    res = collection.get(where={"parent_item_key": key}, include=["metadatas", "documents"])
    from zotero_mcp.semantic_search import _passage_body

    rows = []
    for cid, meta, doc in zip(res.get("ids") or [], res.get("metadatas") or [], res.get("documents") or []):
        meta = meta or {}
        rows.append({"id": cid, "meta": meta, "body": _passage_body(doc or "", meta)})
    rows.sort(key=lambda r: int(r["meta"].get("chunk_index", 0)))
    return rows


def items_to_label(collection, force: bool = False) -> list[str]:
    """Parent keys whose first passage carries no current structure labels."""
    keys = []
    seen = set()
    ids = sorted(collection.get(include=[]).get("ids") or [])
    first = [i for i in ids if i.endswith("#0")]
    for start in range(0, len(first), 2000):
        res = collection.get(ids=first[start:start + 2000], include=["metadatas"])
        for cid, meta in zip(res.get("ids") or [], res.get("metadatas") or []):
            key = cid.split("#", 1)[0]
            if key in seen:
                continue
            seen.add(key)
            if force or int((meta or {}).get("structure_v") or 0) < st.STRUCTURE_VERSION:
                keys.append(key)
    return keys


def run(*, keys: list[str] | None = None, limit: int | None = None, config_path: str | None = None,
        gemini: bool | None = None, dry_run: bool = False, force: bool = False, workers: int = 3,
        log: Callable[[str], None] = print, search=None, reader=None, ask=None) -> dict:
    """Label the passages of ``keys`` (default: every item that needs it)."""
    if search is None:
        from zotero_mcp.semantic_search import create_semantic_search

        search = create_semantic_search(config_path)
    if reader is None:
        from zotero_mcp.local_db import get_serial_reader

        reader = get_serial_reader()
    collection = search.chroma_client.collection
    cfg = structure_config(config_path)
    use_gemini = cfg.get("gemini", False) if gemini is None else gemini
    model = cfg.get("gemini_model") or DEFAULT_MODEL
    cache = _Cache()
    if use_gemini and ask is None:
        try:
            raw_ask = st.gemini_asker(model, search.chroma_client.embedding_config)
        except Exception as e:
            log(f"Gemini unavailable ({type(e).__name__}: {e}); using bookmarks and rules only.")
            raw_ask = None
        ask = raw_ask

    todo = keys or items_to_label(collection, force=force)
    if limit:
        todo = todo[:limit]
    log(f"{len(todo)} item(s) to label{' (dry run: nothing is written)' if dry_run else ''}"
        f"{'; Gemini: ' + model if ask else '; no Gemini'}.")
    totals: Counter = Counter()
    samples: list[dict] = []
    started = time.monotonic()

    def one(key: str) -> dict:
        chunks = _chunks(collection, key)
        if not chunks:
            return {"key": key, "skip": "not indexed"}
        fields = _item_fields(reader, key) if reader else {}
        item_type = fields.get("itemType") or chunks[0]["meta"].get("item_type", "")
        has_pages = any(c["meta"].get("page") is not None for c in chunks)
        structure = None
        pdf = None
        if has_pages and reader is not None:
            pdf = _pdf_for(reader, key, str(chunks[0]["meta"].get("attachment_keys") or ""))
        unreadable = False
        if pdf:
            scan = st.read_pdf(pdf)
            unreadable = scan is None
            if scan:
                cached_ask = None
                if ask is not None:
                    sig = st.file_signature(pdf)

                    def cached_ask(prompt, _sig=sig):
                        # Per file and exact prompt: a changed prompt, or the bookmark check and
                        # the candidate list of the same file, are asked separately.
                        tag = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]
                        ck = f"{_sig}-{st.STRUCTURE_VERSION}-{tag}-{model.replace('/', '_')}"
                        hit = cache.get(ck)
                        if hit is not None:
                            return hit
                        answer = ask(prompt)
                        cache.put(ck, answer)
                        return answer
                structure = st.analyse(scan, item_type, fields.get("pages", ""), fields.get("title", ""),
                                       ask=cached_ask)
        metas, rep = st.label_passages(chunks, structure, item_type)
        changed = [(c["id"], m) for c, m in zip(chunks, metas) if m != c["meta"]]
        return {"key": key, "type": item_type, "pdf": str(pdf) if pdf else "", "structure": structure,
                "unreadable": unreadable,
                "report": rep, "changed": changed, "chunks": chunks, "metas": metas}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for n, res in enumerate(pool.map(one, todo), 1):
            key = res["key"]
            if res.get("skip"):
                totals["skipped"] += 1
                continue
            s = res["structure"]
            rep = res["report"]
            totals["items"] += 1
            why = "PDF unreadable" if res.get("unreadable") else "no PDF"
            totals[f"pages from {s.labels_from if s else why}"] += 1
            totals[f"headings from {s.headings_from if s else why}"] += 1
            totals["reference passages found by shape"] += rep.get("references", 0)
            if s and s.note.startswith("gemini failed"):
                totals["Gemini calls that failed"] += 1
                if totals["Gemini calls that failed"] == 1:
                    log(f"  Gemini failed: {s.note} (model {model}); headings fall back to rules.")
            if rep.get("mismatch"):
                totals["headings not found in the indexed text"] += 1
            if res["changed"] and not dry_run:
                ids = [cid for cid, _m in res["changed"]]
                search.chroma_client.update_metadatas(ids, [m for _c, m in res["changed"]])
            totals["passages updated" if not dry_run else "passages that would change"] += len(res["changed"])
            heads = len(s.headings) if s else 0
            if not s:
                log(f"[{n}/{len(todo)}] {key} ({res['type']}): {why}; "
                    f"{len(res['changed'])} passage(s) changed (reference lists only)")
                continue
            log(f"[{n}/{len(todo)}] {key} ({res['type']}): pages {s.labels_from if s else '-'}, "
                f"{heads} headings from {s.headings_from if s else '-'}"
                f"{', ' + s.note if s and s.note else ''}; {len(res['changed'])} passage(s) changed")
            if len(samples) < 60:
                samples.append(res)
    totals["minutes"] = round((time.monotonic() - started) / 60, 1)
    report_path = _write_report(totals, samples, dry_run)
    log(f"Done. {dict(totals)}")
    if report_path:
        log(f"Report: {report_path}")
    return {"totals": dict(totals), "report": report_path}


def _write_report(totals: Counter, samples: list[dict], dry_run: bool) -> str | None:
    lines = [f"# Passage labels ({_dt.datetime.now():%Y-%m-%d %H:%M}){' — dry run' if dry_run else ''}", ""]
    for k, v in sorted(totals.items()):
        lines.append(f"- {k}: {v}")
    lines += ["", "## Examples to check", ""]
    for res in samples:
        s = res["structure"]
        lines.append(f"### {res['key']} ({res['type']}) — {Path(res['pdf']).name if res['pdf'] else 'no PDF'}")
        if s:
            labels = s.labels or []
            shown = ", ".join(f"PDF {i + 1} = p. {lab}" for i, lab in enumerate(labels[:3]) if lab)
            lines.append(f"- Printed pages: {s.labels_from}{' (' + shown + ')' if shown else ''}")
            lines.append(f"- Headings from {s.headings_from}{' — ' + s.note if s.note else ''}:")
            for h in s.headings[:40]:
                lines.append(f"  - {'  ' * (h.level - 1)}PDF p. {h.page}: {h.text} → {h.section or '-'}")
        seen = set()
        for c, m in zip(res["chunks"], res["metas"]):
            tag = (m.get("section"), m.get("heading"))
            if tag in seen:
                continue
            seen.add(tag)
            lines.append(f"  - passage {m.get('chunk_index')} (PDF p. {m.get('page')}, printed "
                         f"{m.get('page_label', '-')}): {m.get('section') or '-'} | {m.get('heading') or '-'}")
            if len(seen) > 25:
                break
        lines.append("")
    try:
        runs = structure_dir() / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        path = runs / f"{_dt.datetime.now():%Y%m%d-%H%M%S}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)
    except OSError:
        return None

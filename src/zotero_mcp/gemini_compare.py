"""Compare Gemini models (and thinking levels) on the same items, side by side.

Both Gemini tasks are compared: judging headings for the passage labels (the
bookmark check or the candidate lines) and reading a PDF's first pages for the
metadata audit. Nothing is written to Zotero or the search index; the result is
a report in ``~/.config/zotero-mcp/structure/runs/compare-<date-time>.md``.

Answers are cached per file, prompt, model and thinking level, so running the
comparison again (or adding a model) only pays for what is new.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import random
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from zotero_mcp import structure as st

DEFAULT_MODELS = "gemini-3.8-flash:low,gemini-3.6-flash:minimal,gemini-3.5-flash-lite:minimal"
PDF_FIELDS = ("title", "authors", "year", "journal", "volume", "issue", "pages", "doi")
MAIN = ("Introduction", "Methods", "Results", "Discussion")


def parse_models(spec: str) -> list[tuple[str, str]]:
    """"gemini-3.8-flash:low,gemini-3.5-flash-lite:minimal" -> [(model, thinking), ...]."""
    out = []
    for part in (spec or DEFAULT_MODELS).split(","):
        part = part.strip()
        if not part:
            continue
        model, _, thinking = part.partition(":")
        out.append((model.strip(), (thinking or "low").strip().lower()))
    return out


def _label(model: str, thinking: str) -> str:
    return f"{model} ({thinking})"


class _Cache:
    def __init__(self, folder: Path):
        self.dir = folder

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


def _cached(ask: Callable[[str], str], cache: _Cache, sig: str, model: str, thinking: str,
            seconds: Counter, label: str) -> Callable[[str], str]:
    def call(prompt: str) -> str:
        tag = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]
        key = f"{sig}-{tag}-{model.replace('/', '_')}-{thinking}"
        hit = cache.get(key)
        if hit is not None:
            return hit
        started = time.monotonic()
        answer = ask(prompt)
        seconds[label] += time.monotonic() - started
        cache.put(key, answer)
        return answer
    return call


def _zotero_values(data: dict) -> dict[str, str]:
    import re

    authors = [c.get("lastName") or c.get("name") or "" for c in data.get("creators") or []
               if c.get("creatorType", "author") == "author"]
    m = re.search(r"\d{4}", str(data.get("date") or ""))
    return {"title": data.get("title") or "", "authors": "; ".join(a for a in authors if a),
            "year": m.group(0) if m else "", "journal": data.get("publicationTitle") or "",
            "volume": str(data.get("volume") or ""), "issue": str(data.get("issue") or ""),
            "pages": str(data.get("pages") or ""), "doi": data.get("DOI") or ""}


def _reading_values(reading: dict | None) -> dict[str, str]:
    if not isinstance(reading, dict):
        return {}
    out = {f: str(reading.get(f) or "") for f in PDF_FIELDS if f != "authors"}
    out["authors"] = "; ".join(str(a).split()[-1] for a in reading.get("authors") or [] if str(a).split())
    return out


def _agrees(field_name: str, a: str, b: str) -> bool:
    from zotero_mcp import fulltext_fetch as ff
    from zotero_mcp import metadata_audit as ma

    if not a or not b:
        return not a and not b
    if field_name == "title":
        return ma.title_match(a, b) >= 0.9
    if field_name == "authors":
        return [ff._fold(x) for x in a.split("; ")] == [ff._fold(x) for x in b.split("; ")]
    name = {"journal": "publicationTitle", "doi": "DOI"}.get(field_name, field_name)
    return ma.same(name, a, b)


def run(*, models: list[tuple[str, str]], limit: int = 30, seed: int = 1, keys: list[str] | None = None,
        config_path: str | None = None, log: Callable[[str], None] = print, search=None, reader=None,
        backend=None, asker: Callable[[str, dict, str, str], Callable[[str], str]] | None = None,
        workers: int = 3) -> dict:
    """Run every model on the same ``limit`` items and write a side-by-side report."""
    from zotero_mcp import gemini_util, relabel
    from zotero_mcp import metadata_audit as ma

    if search is None:
        from zotero_mcp.semantic_search import create_semantic_search

        search = create_semantic_search(config_path)
    if reader is None:
        from zotero_mcp.local_db import get_serial_reader

        reader = get_serial_reader()
    if backend is None:
        from zotero_mcp import library

        backend = library.get_library_backend()
    collection = search.chroma_client.collection
    embedding_config = getattr(search.chroma_client, "embedding_config", None) or {}

    if asker is None:
        def asker(model: str, schema: dict, thinking: str, label: str) -> Callable[[str], str]:
            return gemini_util.json_asker(model, schema, embedding_config, thinking=thinking, usage_key=label)

    heading_schema = {
        "type": "OBJECT",
        "properties": {"headings": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"id": {"type": "STRING"}, "level": {"type": "INTEGER"},
                           "section": {"type": "STRING", "enum": list(st.SECTIONS) + ["Other"]}},
            "required": ["id", "level", "section"]}}},
        "required": ["headings"],
    }
    labels = [_label(m, t) for m, t in models]
    raw = {lbl: (asker(m, heading_schema, t, lbl), asker(m, ma.PDF_SCHEMA, t, lbl))
           for (m, t), lbl in zip(models, labels)}
    cache = _Cache(relabel.structure_dir() / "compare-cache")
    seconds: Counter = Counter()

    pool = keys or relabel.items_to_label(collection, force=True)
    if not keys:
        random.Random(seed).shuffle(pool)
    log(f"Comparing {', '.join(labels)} on {limit} item(s) with a PDF "
        f"({len(models) * 2} Gemini calls per item, cached answers are reused).")

    def one(key: str) -> dict | None:
        chunks = relabel._chunks(collection, key)
        if not chunks or not any(c["meta"].get("page") is not None for c in chunks):
            return None
        pdf = relabel._pdf_for(reader, key, str(chunks[0]["meta"].get("attachment_keys") or ""))
        if not pdf:
            return None
        scan = st.read_pdf(pdf)
        if not scan:
            return None
        fields = relabel._item_fields(reader, key)
        item_type = fields.get("itemType") or chunks[0]["meta"].get("item_type", "")
        prompt = ma.pdf_prompt(pdf)
        sig = st.file_signature(pdf)
        res = {"key": key, "pdf": Path(pdf).name, "type": item_type, "title": fields.get("title", ""),
               "headings": {}, "reading": {}}
        for lbl, (m, t) in zip(labels, models):
            ask_h, ask_p = raw[lbl]
            s = st.analyse(scan, item_type, fields.get("pages", ""), fields.get("title", ""),
                           ask=_cached(ask_h, cache, sig, m, t, seconds, lbl))
            res["headings"][lbl] = s
            reading = None
            if prompt:
                try:
                    reading = json.loads(_cached(ask_p, cache, sig, m, t, seconds, lbl)(prompt))
                except Exception as e:
                    reading = {"_error": f"{type(e).__name__}: {str(e)[:120]}"}
            res["reading"][lbl] = reading
        return res

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        it = iter(pool)
        while len(results) < limit:
            batch = [k for _, k in zip(range(max(1, workers) * 2), it)]
            if not batch:
                break
            for res in ex.map(one, batch):
                if res and len(results) < limit:
                    results.append(res)
                    log(f"[{len(results)}/{limit}] {res['key']} {res['pdf'][:70]}")

    found = backend.get_items([r["key"] for r in results]) if results else {}
    for r in results:
        item = found.get(r["key"]) or {}
        r["zotero"] = _zotero_values(item.get("data", item))

    summary = _summarise(results, labels, seconds, gemini_util.USAGE)
    path = _write_report(results, labels, summary)
    log("")
    for lbl in labels:
        log(f"{lbl}: {summary[lbl]['line']}")
    if path:
        log(f"Report: {path}")
    return {"summary": summary, "report": path, "items": len(results)}


def _heading_map(s: st.Structure | None) -> dict[str, tuple[int, str]]:
    if s is None:
        return {}
    return {st.fold(h.text): (h.level, h.section or "-") for h in s.headings}


def _summarise(results: list[dict], labels: list[str], seconds: Counter, usage: dict) -> dict:
    ref = labels[0]
    out = {}
    for lbl in labels:
        c: Counter = Counter()
        for r in results:
            s = r["headings"].get(lbl)
            mine, base = _heading_map(s), _heading_map(r["headings"].get(ref))
            c["items"] += 1
            c["headings"] += len(mine)
            c["failed"] += bool(s and s.note.startswith("gemini failed"))
            c["main_sections"] += len({sec for _l, sec in mine.values() if sec in MAIN})
            if lbl != ref:
                union = set(mine) | set(base)
                shared = set(mine) & set(base)
                c["same_headings"] += len(shared)
                c["union_headings"] += len(union)
                c["same_section"] += sum(1 for k in shared if mine[k][1] == base[k][1])
                c["shared"] += len(shared)
            reading = r["reading"].get(lbl) or {}
            got = _reading_values(reading)
            for f in PDF_FIELDS:
                z = r.get("zotero", {}).get(f, "")
                if z:
                    c["zotero_filled"] += 1
                    c["agrees_zotero"] += _agrees(f, got.get(f, ""), z)
            if lbl != ref:
                base_read = _reading_values(r["reading"].get(ref) or {})
                for f in PDF_FIELDS:
                    c["read_fields"] += 1
                    c["agrees_ref"] += _agrees(f, got.get(f, ""), base_read.get(f, ""))
        u = usage.get(lbl) or Counter()
        cost = ""
        base_model = lbl.split(" ")[0]
        from zotero_mcp.gemini_util import PRICES

        if base_model in PRICES and u.get("calls"):
            pin, pout = PRICES[base_model]
            total = u["input"] / 1e6 * pin + (u["output"] + u["thinking"]) / 1e6 * pout
            cost = f"${total:.3f}" + (f" (${total / c['items'] * 1000:.2f} per 1,000 items)" if c["items"] else "")
        parts = [f"{c['headings']} headings on {c['items']} items",
                 f"{c['main_sections']} main sections found"]
        if lbl != ref:
            parts.append(f"same headings as {ref}: {c['same_headings']}/{c['union_headings']}")
            parts.append(f"same section where both found it: {c['same_section']}/{c['shared']}")
            parts.append(f"PDF reading agrees with {ref}: {c['agrees_ref']}/{c['read_fields']} fields")
        parts.append(f"PDF reading agrees with Zotero: {c['agrees_zotero']}/{c['zotero_filled']} filled fields")
        if c["failed"]:
            parts.append(f"{c['failed']} failed")
        parts.append(f"new calls {u.get('calls', 0)}, output {u.get('output', 0) + u.get('thinking', 0)} tokens "
                     f"({u.get('thinking', 0)} thinking), {seconds.get(lbl, 0):.0f} s" + (f", {cost}" if cost else ""))
        out[lbl] = {"counts": dict(c), "line": "; ".join(parts), "cost": cost}
    return out


def _write_report(results: list[dict], labels: list[str], summary: dict) -> str | None:
    from zotero_mcp import relabel

    lines = [f"# Gemini models compared ({_dt.datetime.now():%d-%m-%Y %H:%M})", "",
             f"{len(results)} items. Reference: {labels[0]}. Costs count only new calls (cached answers are free).", ""]
    for lbl in labels:
        lines.append(f"- **{lbl}**: {summary[lbl]['line']}")
    lines += ["", "## Per item", ""]
    for r in results:
        lines.append(f"### {r['key']} ({r['type']}) — {r['pdf']}")
        lines.append("")
        heads = [r["headings"].get(lbl) for lbl in labels]
        froms = " / ".join(f"{s.headings_from if s else '-'}" for s in heads)
        lines.append(f"Headings from: {froms}")
        lines.append("")
        order: list[str] = []
        texts: dict[str, str] = {}
        for s in heads:
            for h in (s.headings if s else []):
                k = st.fold(h.text)
                if k not in texts:
                    order.append(k)
                    texts[k] = h.text[:70]
        maps = [_heading_map(s) for s in heads]
        lines.append("| Heading | " + " | ".join(labels) + " |")
        lines.append("|---|" + "---|" * len(labels))
        for k in order:
            cells = [f"{m[k][0]} {m[k][1]}" if k in m else "—" for m in maps]
            mark = "" if len(set(cells)) == 1 else " ⚠"
            lines.append(f"| {texts[k]}{mark} | " + " | ".join(cells) + " |")
        lines.append("")
        readings = [_reading_values(r["reading"].get(lbl)) for lbl in labels]
        zot = r.get("zotero", {})
        rows = []
        for f in PDF_FIELDS:
            vals = [v.get(f, "") for v in readings]
            if len({v for v in vals}) > 1 or (zot.get(f) and not all(_agrees(f, v, zot[f]) for v in vals)):
                rows.append(f"| {f} | {zot.get(f, '')[:60]} | " + " | ".join(v[:60] or "—" for v in vals) + " |")
        if rows:
            lines.append("| PDF reading | Zotero | " + " | ".join(labels) + " |")
            lines.append("|---|---|" + "---|" * len(labels))
            lines += rows
        else:
            lines.append("PDF reading: all models agree.")
        errors = [f"{lbl}: {(r['reading'].get(lbl) or {}).get('_error')}" for lbl in labels
                  if (r["reading"].get(lbl) or {}).get("_error")]
        if errors:
            lines.append("")
            lines.append("Errors: " + "; ".join(errors))
        lines.append("")
    try:
        runs = relabel.structure_dir() / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        path = runs / f"compare-{_dt.datetime.now():%Y%m%d-%H%M%S}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)
    except OSError:
        return None

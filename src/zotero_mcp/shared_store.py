"""``zotero-mcp shared-store setup|status``: embeddings and section labels shared between computers.

``setup`` turns the shared store on in config.json (``semantic_search.shared_store``), then
copies what this computer already has into the shared folder (``state_dir``, see
``zotero-mcp share-state``): every passage's embedding from the local index, and Gemini's
answers about the PDFs' structure (headings, sections, printed pages). Nothing is sent to an
API; the vectors are read from the index. From then on each computer takes what the other
already paid for, and adds what it embeds or labels itself. The search index itself stays on
each computer.

Run ``setup`` on the computer with the fuller index first, then on the other one (where it
mostly finds everything there already).
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
from collections.abc import Callable
from pathlib import Path

from zotero_mcp import embedding_store as es


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _require_shared_folder() -> Path:
    from zotero_mcp import fulltext_fetch as ff

    shared = ff.shared_dir()
    if shared.resolve() == ff.config_dir().resolve():
        raise RuntimeError(
            "No shared folder yet. Run `zotero-mcp share-state <folder>` first (the same synced folder "
            "on each computer, e.g. OneDrive - UGent\\Academic\\Software\\zotero-mcp\\state)."
        )
    return shared


def enable(config_path: str | Path, log: Callable[[str], None] = print) -> bool:
    """Set ``semantic_search.shared_store.enabled`` in config.json (backup beside it). True if changed."""
    path = Path(config_path)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    sem = data.setdefault("semantic_search", {})
    if (sem.get("shared_store") or {}).get("enabled"):
        return False
    if path.exists():
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))
    sem.setdefault("shared_store", {})["enabled"] = True
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Shared store turned on in {path} (backup beside it).")
    return True


def _open_index(config_path: str):
    from zotero_mcp.chroma_client import create_chroma_client

    return create_chroma_client(config_path)


def export_embeddings(client, store: es.EmbeddingStore, log: Callable[[str], None] = print,
                      batch: int = 1000) -> dict:
    """Copy the local index's embeddings into the store. No API calls."""
    ef = client.embedding_function
    prepare = getattr(ef, "_prepare_document", lambda t: t)
    collection = client.collection
    total = collection.count()
    added = seen = 0
    store.refresh(force=True)
    offset = 0
    while offset < total:
        got = collection.get(limit=batch, offset=offset, include=["documents", "embeddings"])
        docs = got.get("documents") or []
        vecs = got.get("embeddings")
        if vecs is None:
            vecs = []
        if not len(docs):
            break
        pairs = [(prepare(d), v) for d, v in zip(docs, vecs) if d is not None and v is not None]
        added += store.put_many(pairs, path=store.export_file())
        seen += len(docs)
        offset += len(docs)
        if seen % 20000 < batch or offset >= total:
            log(f"  {seen}/{total} passages read, {added} added to the store")
    return {"passages": seen, "added": added}


def copy_structure_cache(log: Callable[[str], None] = print) -> int:
    from zotero_mcp import relabel

    src = relabel.structure_dir() / "gemini-cache"
    dst = relabel.shared_cache_dir()
    if not src.is_dir() or src.resolve() == dst.resolve():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in src.glob("*.json"):
        target = dst / f.name
        if not target.exists():
            shutil.copy2(f, target)
            n += 1
    return n


def setup(config_path: str, export: bool = True, log: Callable[[str], None] = print) -> dict:
    shared = _require_shared_folder()
    enable(config_path, log)
    result: dict = {"shared_folder": str(shared)}
    if not export:
        log("Nothing copied (--no-export). New embeddings and labels go to the shared folder from now on.")
        return result
    client = _open_index(config_path)
    store = getattr(client.embedding_function, "_shared_store", None)
    if store is None:
        raise RuntimeError(
            f"The embedding model '{client.embedding_model}' runs locally and costs nothing; the shared "
            "store only serves API models (Gemini, OpenAI, ...)."
        )
    log(f"Copying this index's embeddings to {store.dir} (no API calls) ...")
    result["embeddings"] = export_embeddings(client, store, log)
    n = copy_structure_cache(log)
    result["structure_answers_copied"] = n
    log(f"Copied {n} of Gemini's structure answers to the shared folder.")
    log(f"Done: {store.count()} embeddings in the store. Both computers now take from it what the "
        "other already paid for.")
    return result


def status(config_path: str, coverage: bool = False, log: Callable[[str], None] = print) -> dict:
    from zotero_mcp import fulltext_fetch as ff
    from zotero_mcp import relabel

    on = es.enabled_in_config_file(config_path)
    log(f"Shared store: {'on' if on else 'off'} (semantic_search.shared_store.enabled in {config_path})")
    log(f"Shared folder: {ff.shared_dir()}")
    client = _open_index(config_path)
    ns = es.namespace_for(client.embedding_function)
    out: dict = {"enabled": on, "namespace": ns}
    if ns:
        store = getattr(client.embedding_function, "_shared_store", None) or es.EmbeddingStore(ns)
        out["embeddings"] = store.count()
        log(f"Embeddings ({ns}): {out['embeddings']} in {store.dir}")
        per: dict[str, list[int]] = {}
        for f in store.files():
            who = f["name"].split("-", 1)[0]
            agg = per.setdefault(who, [0, 0])
            agg[0] += f["records"] or 0
            agg[1] += f["bytes"]
        for who, (n, size) in sorted(per.items()):
            log(f"  {who}: {n} records, {_format_bytes(size)}")
        if coverage:
            prepare = getattr(client.embedding_function, "_prepare_document", lambda t: t)
            have = total = 0
            offset = 0
            count = client.collection.count()
            while offset < count:
                got = client.collection.get(limit=1000, offset=offset, include=["documents"])
                docs = got.get("documents") or []
                if not docs:
                    break
                for d in docs:
                    if d is None:
                        continue
                    total += 1
                    have += store.has(prepare(d))
                offset += len(docs)
            out["coverage"] = {"in_store": have, "passages": total}
            log(f"This index: {have} of {total} passages are in the store")
    shared_cache = relabel.shared_cache_dir()
    local_cache = relabel.structure_dir() / "gemini-cache"
    n_shared = len(list(shared_cache.glob("*.json"))) if shared_cache.is_dir() else 0
    n_local = len(list(local_cache.glob("*.json"))) if local_cache.is_dir() else 0
    out["structure_answers"] = {"shared": n_shared, "local": n_local}
    log(f"Gemini structure answers: {n_shared} shared, {n_local} in this computer's own cache")
    return out

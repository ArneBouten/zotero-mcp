"""One search index on two computers: what one indexes, the other copies.

Each computer keeps its own ChromaDB index (an index must never live in a synced
folder), and the two stay the same copy through a log in the shared folder
(``<state_dir>/index/``):

- **Publications.** After an update, every paper whose index entries changed is
  written to the log whole: its entries' ids, texts and metadata (the
  embeddings are in the shared embedding store, by text). A paper removed from
  the index is published as deleted.
- **Newest wins, both ways.** For each paper the newest publication, from
  whichever computer, is the truth. Before an update, a computer copies the
  papers the other one published more recently than what it has (``pull``):
  no PDF reading, no API calls.
- **One computer per paper.** Before a computer reads a new or changed paper's
  PDF it claims it in the log. The other computer leaves a paper claimed by its
  peer in the last 30 minutes alone and copies it once it is published; a claim
  that was never finished (the computer went to sleep) runs out, and the other
  one does the work.

Files are written by one computer each and only appended to
(``<COMPUTER>-<YYYY-MM>.jsonl``, a full publication in ``<COMPUTER>-pack-<NNNN>.jsonl``),
one line per publication or claim, so a synced folder never has two writers for a
file. A line the sync has only half delivered waits until the rest arrives.
What this computer has read and applied is kept outside the synced folder, in
``~/.config/zotero-mcp/index-sync/``.

Part of the shared store (``semantic_search.shared_store.enabled``); see
``docs/two-computers.md``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from zotero_mcp.embedding_store import _config_dir, _slug, computer_name, locked

logger = logging.getLogger(__name__)

#: A claim older than this is taken to have been abandoned.
CLAIM_TTL_SECONDS = 30 * 60
#: How long a paper whose attachment file has not arrived here is left to the other computer.
MISSING_FILE_WAIT_SECONDS = 24 * 3600
PACK_FILE_BYTES = 50 * 1024 * 1024


def log_root() -> Path:
    from zotero_mcp.fulltext_fetch import shared_dir

    return shared_dir() / "index"


def item_of(doc_id: str) -> str:
    return str(doc_id).split("#", 1)[0]


def canonical_fingerprint(rows: Iterable[dict]) -> str:
    """The same for two sets of entries exactly when ids, texts and metadata are equal."""
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: r["id"]):
        h.update(json.dumps([r["id"], r["doc"], r.get("meta") or {}], sort_keys=True,
                            ensure_ascii=False).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:32]


class IndexSync:
    def __init__(self, client: Any, store: Any, root: Path | None = None, state_dir: Path | None = None,
                 computer: str | None = None) -> None:
        self.client = client          # ChromaClient
        self.store = store            # EmbeddingStore (vectors by text)
        self.dir = Path(root) if root else log_root()
        self.computer = _slug(computer or computer_name()).upper()
        state_dir = state_dir or (_config_dir() / "index-sync")
        state_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = state_dir / "log.lock"
        self._db = sqlite3.connect(str(state_dir / f"{_slug(client.collection_name)}.sqlite"),
                                   check_same_thread=False, timeout=30)
        for ddl in (
            "CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, scanned INTEGER)",
            "CREATE TABLE IF NOT EXISTS pubs (item TEXT PRIMARY KEY, t REAL, who TEXT, file TEXT, "
            "offset INTEGER, length INTEGER, fp TEXT, deleted INTEGER)",
            "CREATE TABLE IF NOT EXISTS claims (item TEXT PRIMARY KEY, t REAL, who TEXT)",
            "CREATE TABLE IF NOT EXISTS applied (item TEXT PRIMARY KEY, t REAL, who TEXT)",
            "CREATE TABLE IF NOT EXISTS missing (item TEXT PRIMARY KEY, since REAL)",
        ):
            self._db.execute(ddl)
        self._db.commit()
        self._lock = threading.RLock()
        self._touched: set[str] = set()
        self._suspended = 0
        self._last_refresh = 0.0

    # -- journal of local writes ------------------------------------------------------

    def touch(self, keys: Iterable[str]) -> None:
        if self._suspended:
            return
        with self._lock:
            self._touched.update(k for k in keys if k)

    @contextlib.contextmanager
    def suspended(self):
        """Writes made while copying from the other computer are not published back."""
        with self._lock:
            self._suspended += 1
        try:
            yield
        finally:
            with self._lock:
                self._suspended -= 1

    @property
    def pending(self) -> int:
        return len(self._touched)

    # -- reading the log ---------------------------------------------------------------

    def has_news(self) -> bool:
        """Cheap: has any log file grown since it was last read?"""
        if not self.dir.is_dir():
            return False
        with self._lock:
            known = dict(self._db.execute("SELECT name, scanned FROM files"))
        for p in self.dir.glob("*.jsonl"):
            try:
                if p.stat().st_size > known.get(p.name, 0) and not p.name.startswith(self.computer + "-"):
                    return True
            except OSError:
                continue
        return False

    def refresh(self, force: bool = False) -> int:
        with self._lock:
            if not force and time.monotonic() - self._last_refresh < 5:
                return 0
            self._last_refresh = time.monotonic()
            if not self.dir.is_dir():
                return 0
            known = dict(self._db.execute("SELECT name, scanned FROM files"))
            n = 0
            for path in sorted(self.dir.glob("*.jsonl")):
                try:
                    n += self._scan(path, known.get(path.name, 0))
                except Exception as e:
                    logger.warning(f"Index log: could not read {path.name}: {e}")
            self._db.commit()
            return n

    def _scan(self, path: Path, scanned: int) -> int:
        size = path.stat().st_size
        if size < scanned:
            scanned = 0
        if size == scanned:
            return 0
        n = 0
        with open(path, "rb") as f:
            f.seek(scanned)
            pos = scanned
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # half delivered: wait for the rest
                try:
                    rec = json.loads(raw)
                    self._index_record(rec, path.name, pos, len(raw) - 1)
                    n += 1
                except Exception:
                    pass
                pos += len(raw)
        self._db.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (path.name, pos))
        return n

    def _index_record(self, rec: dict, file: str, offset: int, length: int) -> None:
        item, t, who = rec["item"], float(rec["t"]), rec.get("by", "")
        if rec.get("claim"):
            self._db.execute(
                "INSERT INTO claims VALUES (?, ?, ?) ON CONFLICT(item) DO UPDATE SET t = excluded.t, "
                "who = excluded.who WHERE excluded.t > claims.t", (item, t, who))
            return
        fp = None if rec.get("deleted") else canonical_fingerprint(rec.get("rows") or [])
        self._db.execute(
            "INSERT INTO pubs VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(item) DO UPDATE SET "
            "t = excluded.t, who = excluded.who, file = excluded.file, offset = excluded.offset, "
            "length = excluded.length, fp = excluded.fp, deleted = excluded.deleted "
            "WHERE excluded.t > pubs.t OR (excluded.t = pubs.t AND excluded.who > pubs.who)",
            (item, t, who, file, offset, length, fp, 1 if rec.get("deleted") else 0))

    def _read(self, file: str, offset: int, length: int) -> dict:
        with open(self.dir / file, "rb") as f:
            f.seek(offset)
            return json.loads(f.read(length))

    # -- writing the log ------------------------------------------------------------------

    def _own_file(self) -> Path:
        return self.dir / f"{self.computer}-{time.strftime('%Y-%m')}.jsonl"

    def _pack_file(self) -> Path:
        existing = sorted(self.dir.glob(f"{self.computer}-pack-*.jsonl")) if self.dir.is_dir() else []
        if existing and existing[-1].stat().st_size < PACK_FILE_BYTES:
            return existing[-1]
        n = int(existing[-1].stem.rsplit("-", 1)[-1]) + 1 if existing else 1
        return self.dir / f"{self.computer}-pack-{n:04d}.jsonl"

    def _append(self, records: list[dict], path: Path) -> None:
        if not records:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        lines = [(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8") for r in records]
        with locked(self._lock_path), open(path, "ab") as f:
            f.seek(0, os.SEEK_END)
            start = f.tell()
            f.write(b"".join(lines))
            f.flush()
            os.fsync(f.fileno())
        pos = start
        with self._lock:
            for rec, line in zip(records, lines):
                self._index_record(rec, path.name, pos, len(line) - 1)
                if not rec.get("claim"):
                    self._db.execute("INSERT OR REPLACE INTO applied VALUES (?, ?, ?)",
                                     (rec["item"], rec["t"], self.computer))
                pos += len(line)
            self._db.commit()

    def claim(self, keys: Iterable[str]) -> None:
        now = time.time()
        self._append([{"item": k, "t": now, "by": self.computer, "claim": True} for k in keys], self._own_file())

    def claimed_elsewhere(self, keys: Iterable[str]) -> set[str]:
        """Papers the other computer started on in the last 30 minutes and has not published yet."""
        self.refresh()
        now = time.time()
        out = set()
        with self._lock:
            for k in keys:
                c = self._db.execute("SELECT t, who FROM claims WHERE item = ?", (k,)).fetchone()
                if not c or c[1] == self.computer or now - c[0] > CLAIM_TTL_SECONDS:
                    continue
                p = self._db.execute("SELECT t FROM pubs WHERE item = ?", (k,)).fetchone()
                if p and p[0] >= c[0]:
                    continue  # published since: pull copies it
                out.add(k)
        return out

    def still_waiting_for_files(self, keys: Iterable[str]) -> set[str]:
        """Of papers whose attachment file is not on this computer yet, those to leave for now.

        Zotero syncs an item before its file; indexing it here then would publish a
        text-less copy that the other computer takes over. Such a paper is left alone (and not
        claimed) for up to a day: the computer that has the file indexes it, or the file
        arrives. After a day it is indexed as it is (a link to a file that never comes).
        """
        now = time.time()
        out = set()
        with self._lock:
            for k in keys:
                row = self._db.execute("SELECT since FROM missing WHERE item = ?", (k,)).fetchone()
                if row is None:
                    self._db.execute("INSERT INTO missing VALUES (?, ?)", (k, now))
                    out.add(k)
                elif now - row[0] < MISSING_FILE_WAIT_SECONDS:
                    out.add(k)
            self._db.commit()
        return out

    def files_arrived(self, keys: Iterable[str]) -> None:
        with self._lock:
            self._db.executemany("DELETE FROM missing WHERE item = ?", [(k,) for k in keys])
            self._db.commit()

    def _rows_for(self, keys: list[str], with_vectors: bool) -> dict[str, list[dict]]:
        include = ["documents", "metadatas"] + (["embeddings"] if with_vectors else [])
        got = self.client.collection.get(where={"item_key": {"$in": keys}}, include=include)
        rows: dict[str, list[dict]] = defaultdict(list)
        vecs = got.get("embeddings") if with_vectors else None
        for i, doc_id in enumerate(got.get("ids") or []):
            meta = (got.get("metadatas") or [None])[i] or {}
            row = {"id": doc_id, "doc": (got.get("documents") or [None])[i], "meta": meta}
            if vecs is not None:
                row["_vec"] = vecs[i]
            rows[meta.get("item_key") or item_of(doc_id)].append(row)
        return rows

    def publish(self, keys: Iterable[str] | None = None, pack: bool = False,
                log: Callable[[str], None] | None = None, batch: int = 50) -> int:
        """Write the current entries of the touched papers (or ``keys``) to the log."""
        with self._lock:
            if keys is None:
                keys, self._touched = sorted(self._touched), set()
            else:
                keys = sorted(set(keys))
        if not keys:
            return 0
        prepare = getattr(self.client.embedding_function, "_prepare_document", lambda t: t)
        done = 0
        for i in range(0, len(keys), batch):
            part = keys[i:i + batch]
            rows = self._rows_for(part, with_vectors=True)
            vectors = [(prepare(r["doc"]), r["_vec"]) for rs in rows.values() for r in rs
                       if r["doc"] is not None and r.get("_vec") is not None]
            if vectors:
                self.store.put_many(vectors)
            now = time.time()
            records = []
            for k in part:
                rs = rows.get(k) or []
                if rs:
                    records.append({"item": k, "t": now, "by": self.computer,
                                    "rows": [{"id": r["id"], "doc": r["doc"], "meta": r["meta"]} for r in rs]})
                else:
                    records.append({"item": k, "t": now, "by": self.computer, "deleted": True})
            self._append(records, self._pack_file() if pack else self._own_file())
            done += len(part)
            if log and (done % 500 < batch or done == len(keys)):
                log(f"  {done}/{len(keys)} papers published")
        return done

    def publish_all(self, log: Callable[[str], None] | None = None) -> int:
        keys = set()
        for ids, metas in self.client.iter_metadatas():
            for doc_id, meta in zip(ids, metas):
                keys.add((meta or {}).get("item_key") or item_of(doc_id))
        return self.publish(keys, pack=True, log=log)

    # -- copying from the log ------------------------------------------------------------

    def pull(self, keys: Iterable[str] | None = None, force: bool = False,
             log: Callable[[str], None] | None = None, batch_rows: int = 2000) -> dict:
        """Copy the papers the other computer published more recently than this index has them.

        With ``keys`` and ``force``, copy those papers' newest publication whoever made it
        (used to repair a difference ``compare`` found).
        """
        self.refresh(force=True)
        self.store.refresh(force=True)
        with self._lock:
            if keys is not None:
                ks = list(set(keys))
                marks = ",".join("?" * len(ks))
                rows = self._db.execute(
                    f"SELECT p.item, p.t, p.who, p.file, p.offset, p.length, p.deleted, a.t FROM pubs p "
                    f"LEFT JOIN applied a ON a.item = p.item WHERE p.item IN ({marks})", ks).fetchall() if ks else []
            else:
                rows = self._db.execute(
                    "SELECT p.item, p.t, p.who, p.file, p.offset, p.length, p.deleted, a.t FROM pubs p "
                    "LEFT JOIN applied a ON a.item = p.item WHERE p.who != ? AND (a.t IS NULL OR p.t > a.t)",
                    (self.computer,)).fetchall()
        todo = [r for r in rows if force or (r[2] != self.computer and (r[7] is None or r[1] > r[7]))]
        result = {"copied": 0, "deleted": 0, "waiting": 0}
        if not todo:
            return result
        prepare = getattr(self.client.embedding_function, "_prepare_document", lambda t: t)
        buf_ids: list[str] = []
        buf_docs: list[str] = []
        buf_metas: list[dict] = []
        buf_vecs: list[list[float]] = []
        done_marks: list[tuple[str, float, str]] = []

        def flush():
            if buf_ids:
                with self.suspended():
                    self.client.upsert_embeddings(list(buf_docs), list(buf_metas), list(buf_ids), list(buf_vecs))
                buf_ids.clear(), buf_docs.clear(), buf_metas.clear(), buf_vecs.clear()
            with self._lock:
                self._db.executemany("INSERT OR REPLACE INTO applied VALUES (?, ?, ?)", done_marks)
                self._db.commit()
            done_marks.clear()

        for n, (item, t, who, file, offset, length, deleted, _applied) in enumerate(todo, 1):
            if deleted:
                with self.suspended():
                    self.client.collection.delete(where={"item_key": item})
                done_marks.append((item, t, who))
                result["deleted"] += 1
                continue
            try:
                rec = self._read(file, offset, length)
            except Exception as e:
                logger.warning(f"Index log: {item} unreadable ({e})")
                result["waiting"] += 1
                continue
            entries = rec.get("rows") or []
            found = self.store.get_many([prepare(r["doc"]) for r in entries])
            if len(found) < len(entries):
                # The embeddings have not arrived through the sync yet: try again later.
                result["waiting"] += 1
                continue
            with self.suspended():
                self.client.collection.delete(where={"item_key": item})
                stale = [r["id"] for r in entries]
                if stale:
                    self.client.collection.delete(ids=stale)
            for i, r in enumerate(entries):
                buf_ids.append(r["id"])
                buf_docs.append(r["doc"])
                buf_metas.append(r["meta"])
                buf_vecs.append(found[i])
            done_marks.append((item, t, who))
            result["copied"] += 1
            if len(buf_ids) >= batch_rows:
                flush()
                if log:
                    log(f"  {n}/{len(todo)} papers copied")
        flush()
        if log:
            log(f"  {result['copied']} copied, {result['deleted']} removed"
                + (f", {result['waiting']} waiting for the sync" if result["waiting"] else ""))
        return result

    # -- comparing ----------------------------------------------------------------------------

    def compare(self, log: Callable[[str], None] | None = None) -> dict:
        """Papers whose entries here differ from their newest publication."""
        self.refresh(force=True)
        local: dict[str, list[dict]] = defaultdict(list)
        offset, total = 0, self.client.collection.count()
        while offset < total:
            got = self.client.collection.get(limit=2000, offset=offset, include=["documents", "metadatas"])
            ids = got.get("ids") or []
            if not ids:
                break
            for i, doc_id in enumerate(ids):
                meta = (got.get("metadatas") or [None])[i] or {}
                local[meta.get("item_key") or item_of(doc_id)].append(
                    {"id": doc_id, "doc": (got.get("documents") or [None])[i], "meta": meta})
            offset += len(ids)
        with self._lock:
            pubs = {item: (fp, deleted, who) for item, fp, deleted, who in
                    self._db.execute("SELECT item, fp, deleted, who FROM pubs")}
        different, only_here, only_log, same = [], [], [], 0
        for item, rows in local.items():
            p = pubs.get(item)
            if p is None or p[1]:
                only_here.append(item)
            elif p[0] != canonical_fingerprint(rows):
                different.append(item)
            else:
                same += 1
        for item, (fp, deleted, who) in pubs.items():
            if not deleted and item not in local:
                only_log.append(item)
        out = {"same": same, "different": sorted(different), "only_here": sorted(only_here),
               "only_in_log": sorted(only_log)}
        if log:
            log(f"Papers the same as their newest publication: {same}")
            log(f"Different: {len(different)}; only in this index: {len(only_here)}; "
                f"published but not in this index: {len(only_log)}")
        return out

    def close(self) -> None:
        with self._lock:
            self._db.close()


def attach(client: Any, semantic_config: dict | None) -> IndexSync | None:
    """Give a ChromaClient the shared index log when the shared store is on."""
    store = getattr(getattr(client, "embedding_function", None), "_shared_store", None)
    if store is None or client is None:
        return None
    sync_cfg = ((semantic_config or {}).get("shared_store") or {}).get("index", True)
    if not sync_cfg:
        return None
    try:
        sync = IndexSync(client, store)
    except Exception as e:
        logger.warning(f"Shared index log unavailable ({e}); this index is kept on its own.")
        return None
    client.index_sync = sync
    return sync

"""A store of embeddings that several computers share through a synced folder.

Each computer keeps its own search index (an index must never live in a synced
folder: OneDrive would corrupt it), but the embeddings that go into it are paid
for once. Before a passage is sent to the embedding API, the store is asked for
it; whatever had to be embedded is added to the store, so the other computer
finds it there.

On disk, in ``<state_dir>/embeddings/<namespace>/`` (``namespace`` names the
provider, the model and, where it matters, the vector size):

- ``<COMPUTER>-<YYYY-MM>.vec``: what that computer embedded that month.
- ``<COMPUTER>-export-<NNNN>.vec``: embeddings copied from an existing index
  (``zotero-mcp shared-store setup``), about 200 MB per file.

Every file is written by one computer only, and only appended to, so a synced
folder never has two writers for one file. A file is a 16-byte header
(``ZMCPVEC1``, the vector size, a reserved word) followed by fixed-size
records: the SHA-256 of the text as it was sent to the API, then the vector as
little-endian float32 (what ChromaDB stores, so nothing is lost). A record the
sync has only half delivered is ignored until the rest arrives.

Which key is in which file and where is kept per computer, outside the synced
folder, in ``~/.config/zotero-mcp/embedding-store/<namespace>.sqlite``; it is
brought up to date from the files' new bytes only.

Turned on with ``"shared_store": {"enabled": true}`` under ``semantic_search``
in config.json. Any failure of the store falls back to the API: the store can
save money, never cost an index update.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import struct
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAGIC = b"ZMCPVEC1"
HEADER = struct.Struct("<8sII")  # magic, dim, reserved
HEADER_SIZE = HEADER.size  # 16
KEY_SIZE = 32
#: Export files are closed at about this size, so a synced folder uploads them in parts.
EXPORT_FILE_BYTES = 200 * 1024 * 1024


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("_") or "x"


def computer_name() -> str:
    return _slug(os.environ.get("COMPUTERNAME") or socket.gethostname() or "computer").upper()


def namespace_for(embedding_function: Any) -> str | None:
    """Provider, model and vector size: embeddings are only reusable within one namespace.

    None for embedding functions the store does not serve (local models cost nothing).
    """
    try:
        provider = embedding_function.name()
    except Exception:
        return None
    model = getattr(embedding_function, "model_name", None)
    if not provider or not model:
        return None
    parts = [provider, model]
    dims = getattr(embedding_function, "dimensions", None)
    if dims:
        parts.append(f"d{dims}")
    return _slug("-".join(str(p) for p in parts))


def text_key(namespace: str, text: str) -> bytes:
    return hashlib.sha256(f"{namespace}\x00doc\x00{text}".encode()).digest()


def _config_dir() -> Path:
    return Path.home() / ".config" / "zotero-mcp"


def store_root() -> Path:
    from zotero_mcp.fulltext_fetch import shared_dir

    return shared_dir() / "embeddings"


def is_enabled(semantic_config: dict | None) -> bool:
    cfg = (semantic_config or {}).get("shared_store") or {}
    if os.environ.get("ZOTERO_MCP_SHARED_STORE", "").strip().lower() in ("0", "false", "off", "no"):
        return False
    return bool(cfg.get("enabled"))


def enabled_in_config_file(config_path: str | Path | None = None) -> bool:
    try:
        path = Path(config_path) if config_path else _config_dir() / "config.json"
        return is_enabled(json.loads(path.read_text(encoding="utf-8")).get("semantic_search") or {})
    except Exception:
        return False


class EmbeddingStore:
    """Embeddings shared between computers; see the module docstring."""

    def __init__(self, namespace: str, root: Path | None = None, index_dir: Path | None = None,
                 computer: str | None = None) -> None:
        self.namespace = namespace
        self.dir = (root or store_root()) / namespace
        self.computer = _slug(computer or computer_name()).upper()
        index_dir = index_dir or (_config_dir() / "embedding-store")
        index_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(index_dir / f"{namespace}.sqlite"), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, scanned INTEGER, dim INTEGER)")
        self._db.execute("CREATE TABLE IF NOT EXISTS keys (key BLOB PRIMARY KEY, file TEXT, offset INTEGER)")
        self._db.commit()
        self._lock = threading.RLock()
        self._last_refresh = 0.0
        self.dim: int | None = None
        # Counters for the current process, for reports ("N from the shared store").
        self.hits = 0
        self.misses = 0
        self.added = 0

    # -- reading ---------------------------------------------------------------

    def _record_size(self, dim: int) -> int:
        return KEY_SIZE + 4 * dim

    def refresh(self, force: bool = False) -> int:
        """Index the records that arrived since the last look. Returns how many were new."""
        with self._lock:
            if not force and time.monotonic() - self._last_refresh < 30:
                return 0
            self._last_refresh = time.monotonic()
            if not self.dir.is_dir():
                return 0
            known = {name: (scanned, dim) for name, scanned, dim in
                     self._db.execute("SELECT name, scanned, dim FROM files")}
            new = 0
            for path in sorted(self.dir.glob("*.vec")):
                try:
                    new += self._scan(path, *known.get(path.name, (0, None)))
                except Exception as e:  # one unreadable file must not stop the rest
                    logger.warning(f"Shared store: could not read {path.name}: {e}")
            self._db.commit()
            return new

    def _scan(self, path: Path, scanned: int, dim: int | None) -> int:
        size = path.stat().st_size
        if size < HEADER_SIZE:
            return 0
        with open(path, "rb") as f:
            magic, file_dim, _ = HEADER.unpack(f.read(HEADER_SIZE))
            if magic != MAGIC or not file_dim:
                return 0
            if dim is not None and dim != file_dim or size < scanned:
                # Replaced or shrunk: start this file over.
                self._db.execute("DELETE FROM keys WHERE file = ?", (path.name,))
                scanned = 0
            self._check_dim(file_dim)
            rec = self._record_size(file_dim)
            start = max(scanned, HEADER_SIZE)
            complete = HEADER_SIZE + (size - HEADER_SIZE) // rec * rec
            if complete <= start:
                self._db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (path.name, start, file_dim))
                return 0
            f.seek(start)
            rows = []
            offset = start
            chunk_records = max(1, (16 * 1024 * 1024) // rec)
            while offset < complete:
                n = min(chunk_records, (complete - offset) // rec)
                data = f.read(n * rec)
                for i in range(n):
                    rows.append((data[i * rec:i * rec + KEY_SIZE], path.name, offset + i * rec))
                offset += n * rec
            before = self._db.total_changes
            self._db.executemany("INSERT OR IGNORE INTO keys VALUES (?, ?, ?)", rows)
            added = self._db.total_changes - before
            self._db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (path.name, complete, file_dim))
            return added

    def _check_dim(self, dim: int) -> None:
        if self.dim is None:
            self.dim = dim
        elif self.dim != dim:
            raise ValueError(f"vector size {dim} differs from {self.dim} in the same namespace")

    def get_many(self, texts: Sequence[str]) -> dict[int, list[float]]:
        """Vectors for the texts the store has, by position in ``texts``."""
        self.refresh()
        out: dict[int, list[float]] = {}
        with self._lock:
            handles: dict[str, Any] = {}
            try:
                for i, text in enumerate(texts):
                    row = self._db.execute("SELECT file, offset FROM keys WHERE key = ?",
                                           (text_key(self.namespace, text),)).fetchone()
                    if not row:
                        continue
                    name, offset = row
                    f = handles.get(name)
                    if f is None:
                        f = handles[name] = open(self.dir / name, "rb")
                    vec = self._read_vector(f, offset)
                    if vec is not None:
                        out[i] = vec
            finally:
                for f in handles.values():
                    f.close()
        self.hits += len(out)
        self.misses += len(texts) - len(out)
        return out

    def _read_vector(self, f, offset: int) -> list[float] | None:
        if not self.dim:
            return None
        f.seek(offset + KEY_SIZE)
        data = f.read(4 * self.dim)
        if len(data) != 4 * self.dim:
            return None
        return list(struct.unpack(f"<{self.dim}f", data))

    def count(self) -> int:
        self.refresh(force=True)
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM keys").fetchone()[0]

    def has(self, text: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM keys WHERE key = ?",
                                    (text_key(self.namespace, text),)).fetchone() is not None

    # -- writing ---------------------------------------------------------------

    def own_file(self) -> Path:
        return self.dir / f"{self.computer}-{time.strftime('%Y-%m')}.vec"

    def put_many(self, items: Iterable[tuple[str, Sequence[float]]], path: Path | None = None) -> int:
        """Add vectors the store does not have yet, to this computer's file. Returns how many."""
        self.refresh()
        with self._lock:
            records = []
            seen: set[bytes] = set()
            for text, vector in items:
                key = text_key(self.namespace, text)
                if key in seen or self._db.execute("SELECT 1 FROM keys WHERE key = ?", (key,)).fetchone():
                    continue
                vec = [float(x) for x in vector]
                self._check_dim(len(vec))
                seen.add(key)
                records.append((key, struct.pack(f"<{len(vec)}f", *vec)))
            if not records:
                return 0
            path = path or self.own_file()
            self.dir.mkdir(parents=True, exist_ok=True)
            rows = []
            with open(path, "ab") as f:
                if f.tell() == 0:
                    f.write(HEADER.pack(MAGIC, self.dim, 0))
                offset = f.tell()
                f.write(b"".join(k + v for k, v in records))
                f.flush()
                os.fsync(f.fileno())
                end = f.tell()
            rec = self._record_size(self.dim)
            for i, (key, _) in enumerate(records):
                rows.append((key, path.name, offset + i * rec))
            self._db.executemany("INSERT OR IGNORE INTO keys VALUES (?, ?, ?)", rows)
            self._db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (path.name, end, self.dim))
            self._db.commit()
            self.added += len(records)
            return len(records)

    def export_file(self) -> Path:
        """The export file to append to: the last one, or a new one once it reaches its size."""
        existing = sorted(self.dir.glob(f"{self.computer}-export-*.vec")) if self.dir.is_dir() else []
        if existing and existing[-1].stat().st_size < EXPORT_FILE_BYTES:
            return existing[-1]
        n = int(existing[-1].stem.rsplit("-", 1)[-1]) + 1 if existing else 1
        return self.dir / f"{self.computer}-export-{n:04d}.vec"

    def files(self) -> list[dict[str, Any]]:
        out = []
        if self.dir.is_dir():
            for p in sorted(self.dir.glob("*.vec")):
                size = p.stat().st_size
                n = (size - HEADER_SIZE) // self._record_size(self.dim) if self.dim and size > HEADER_SIZE else None
                out.append({"name": p.name, "bytes": size, "records": n})
        return out

    def close(self) -> None:
        with self._lock:
            self._db.close()


def attach(embedding_function: Any, semantic_config: dict | None) -> EmbeddingStore | None:
    """Give an embedding function the shared store when config.json turns it on."""
    if embedding_function is None or not is_enabled(semantic_config):
        return None
    namespace = namespace_for(embedding_function)
    if not namespace or not hasattr(embedding_function, "_shared_store"):
        return None
    try:
        store = EmbeddingStore(namespace)
    except Exception as e:
        logger.warning(f"Shared embedding store unavailable ({e}); embedding through the API only.")
        return None
    embedding_function._shared_store = store
    return store

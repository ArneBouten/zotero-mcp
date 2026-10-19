"""Small cached answers (Gemini's reading of a PDF) shared between computers in a few files.

The same idea as :mod:`zotero_mcp.embedding_store`, for text: instead of one small
file per answer (thousands of files, which a synced folder like OneDrive handles
slowly), each computer appends its answers to its own file per month,
``<COMPUTER>-<YYYY-MM>.jsonl``, and existing answers are packed into
``<COMPUTER>-pack-<NNNN>.jsonl``. One line per answer: ``{"k": key, "v": text}``.
A line the sync has only half delivered is ignored until the rest arrives.

Where each key is, is kept per computer outside the synced folder, in
``~/.config/zotero-mcp/answer-store/<name>.sqlite``. Answers kept the old way (one
``<key>.json`` file each, in ``legacy_dirs``) are still read.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from zotero_mcp.embedding_store import _config_dir, _slug, computer_name, locked

logger = logging.getLogger(__name__)

#: Pack files are closed at about this size.
PACK_FILE_BYTES = 50 * 1024 * 1024


class AnswerStore:
    def __init__(self, name: str, root: Path, legacy_dirs: Iterable[Path] = (),
                 index_dir: Path | None = None, computer: str | None = None) -> None:
        self.name = _slug(name)
        self.dir = Path(root)
        self.legacy_dirs = [Path(d) for d in legacy_dirs]
        self.computer = _slug(computer or computer_name()).upper()
        self.index_dir = index_dir or (_config_dir() / "answer-store")
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.index_dir / f"{self.name}.sqlite"),
                                   check_same_thread=False, timeout=30)
        self._db.execute("CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, scanned INTEGER)")
        self._db.execute("CREATE TABLE IF NOT EXISTS keys (key TEXT PRIMARY KEY, file TEXT, offset INTEGER, "
                         "length INTEGER)")
        self._db.commit()
        self._lock = threading.RLock()
        self._last_refresh = 0.0

    def _lock_path(self) -> Path:
        return self.index_dir / f"{self.name}.lock"

    # -- reading -------------------------------------------------------------------

    def refresh(self, force: bool = False) -> int:
        with self._lock:
            if not force and time.monotonic() - self._last_refresh < 30:
                return 0
            self._last_refresh = time.monotonic()
            if not self.dir.is_dir():
                return 0
            known = dict(self._db.execute("SELECT name, scanned FROM files"))
            new = 0
            for path in sorted(self.dir.glob("*.jsonl")):
                try:
                    new += self._scan(path, known.get(path.name, 0))
                except Exception as e:
                    logger.warning(f"Answer store {self.name}: could not read {path.name}: {e}")
            self._db.commit()
            return new

    def _scan(self, path: Path, scanned: int) -> int:
        size = path.stat().st_size
        if size < scanned:  # replaced or shrunk: start over
            self._db.execute("DELETE FROM keys WHERE file = ?", (path.name,))
            scanned = 0
        if size == scanned:
            return 0
        with open(path, "rb") as f:
            f.seek(scanned)
            data = f.read(size - scanned)
        end = data.rfind(b"\n")
        if end < 0:
            return 0
        rows = []
        pos = 0
        while pos <= end:
            nl = data.index(b"\n", pos)
            line = data[pos:nl]
            try:
                key = json.loads(line)["k"]
                rows.append((key, path.name, scanned + pos, nl - pos))
            except Exception:
                pass  # not a whole answer: a damaged line is skipped, not fatal
            pos = nl + 1
        before = self._db.total_changes
        self._db.executemany("INSERT OR IGNORE INTO keys VALUES (?, ?, ?, ?)", rows)
        added = self._db.total_changes - before
        self._db.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (path.name, scanned + end + 1))
        return added

    def get(self, key: str) -> str | None:
        self.refresh()
        with self._lock:
            row = self._db.execute("SELECT file, offset, length FROM keys WHERE key = ?", (key,)).fetchone()
        if row:
            name, offset, length = row
            try:
                with open(self.dir / name, "rb") as f:
                    f.seek(offset)
                    return json.loads(f.read(length))["v"]
            except Exception as e:
                logger.debug(f"Answer store {self.name}: {key} unreadable ({e})")
        for d in self.legacy_dirs:
            try:
                return (d / f"{key}.json").read_text(encoding="utf-8")
            except OSError:
                continue
        return None

    def has(self, key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM keys WHERE key = ?", (key,)).fetchone() is not None

    def count(self) -> int:
        self.refresh(force=True)
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM keys").fetchone()[0]

    # -- writing -------------------------------------------------------------------

    def own_file(self) -> Path:
        return self.dir / f"{self.computer}-{time.strftime('%Y-%m')}.jsonl"

    def pack_file(self) -> Path:
        existing = sorted(self.dir.glob(f"{self.computer}-pack-*.jsonl")) if self.dir.is_dir() else []
        if existing and existing[-1].stat().st_size < PACK_FILE_BYTES:
            return existing[-1]
        n = int(existing[-1].stem.rsplit("-", 1)[-1]) + 1 if existing else 1
        return self.dir / f"{self.computer}-pack-{n:04d}.jsonl"

    def put(self, key: str, value: str) -> bool:
        return self.put_many([(key, value)]) == 1

    def put_many(self, items: Iterable[tuple[str, str]], path: Path | None = None) -> int:
        """Add answers the store does not have yet. Returns how many were added."""
        self.refresh()
        with self._lock:
            lines: list[tuple[str, bytes]] = []
            seen: set[str] = set()
            for key, value in items:
                if key in seen or self.has(key):
                    continue
                seen.add(key)
                lines.append((key, (json.dumps({"k": key, "v": value}, ensure_ascii=False) + "\n").encode("utf-8")))
            if not lines:
                return 0
            path = path or self.own_file()
            self.dir.mkdir(parents=True, exist_ok=True)
            with locked(self._lock_path()), open(path, "ab") as f:
                f.seek(0, os.SEEK_END)
                start = f.tell()
                f.write(b"".join(b for _, b in lines))
                f.flush()
                os.fsync(f.fileno())
                end = f.tell()
            rows, pos = [], start
            for key, b in lines:
                rows.append((key, path.name, pos, len(b) - 1))
                pos += len(b)
            self._db.executemany("INSERT OR IGNORE INTO keys VALUES (?, ?, ?, ?)", rows)
            # Move the scan mark past our own lines only when nothing came before them
            # unscanned (another process on this computer may have appended in between).
            self._db.execute("UPDATE files SET scanned = ? WHERE name = ? AND scanned = ?", (end, path.name, start))
            self._db.execute("INSERT OR IGNORE INTO files VALUES (?, ?)", (path.name, end if start == 0 else 0))
            self._db.commit()
            return len(lines)

    def pack_legacy(self, dirs: Iterable[Path], log=print) -> int:
        """Put the answers kept one file each in ``dirs`` into pack files. Returns how many."""
        added = 0
        batch: list[tuple[str, str]] = []
        for d in dirs:
            if not Path(d).is_dir():
                continue
            for f in sorted(Path(d).glob("*.json")):
                try:
                    batch.append((f.stem, f.read_text(encoding="utf-8")))
                except OSError:
                    continue
                if len(batch) >= 500:
                    added += self.put_many(batch, path=self.pack_file())
                    batch = []
        if batch:
            added += self.put_many(batch, path=self.pack_file())
        return added

    def files(self) -> list[dict]:
        out = []
        if self.dir.is_dir():
            for p in sorted(self.dir.glob("*.jsonl")):
                out.append({"name": p.name, "bytes": p.stat().st_size})
        return out

    def close(self) -> None:
        with self._lock:
            self._db.close()

"""Tell a running server that another process changed the semantic index.

ChromaDB caches one "system" per index directory in each process, and with it
the vector index it loaded from disk. Vectors that another process adds later
(the startup update, which runs in its own process, or a manual
``zotero-mcp update-db``) reach the SQLite store, so counts go up, but they
are not in the cached vector index: searches in the server miss them until it
restarts.

Every process that changes the index therefore records a new *generation* in
a small file beside it: its pid and a timestamp. Before opening the index, a
process compares that file with the generation it saw last. When another
process has written since, it drops ChromaDB's cached system for that
directory, and the next client loads the index afresh. Its own writes never
cause a reload, because its cached index already has them.

Standard library only, so the server can import it without ChromaDB.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

#: Index directory (absolute) -> generation this process last saw.
_seen: dict[str, str] = {}


def generation_path(persist_directory: str | os.PathLike) -> Path:
    """The generation file for an index directory: ``<dir>.generation`` beside it."""
    path = Path(persist_directory)
    return path.with_name(path.name + ".generation")


def _read(persist_directory: str | os.PathLike) -> str:
    try:
        return generation_path(persist_directory).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def mark_index_changed(persist_directory: str | os.PathLike) -> None:
    """Record that this process has just changed the index. Never raises."""
    target = generation_path(persist_directory)
    value = f"{os.getpid()} {time.time_ns()}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        tmp.write_text(value, encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        return
    _seen[os.path.abspath(persist_directory)] = value


def changed_elsewhere(persist_directory: str | os.PathLike) -> bool:
    """Whether another process changed the index since this process last looked.

    The first call for a directory only records its generation: nothing of it
    can be cached in this process yet.
    """
    key = os.path.abspath(persist_directory)
    current = _read(persist_directory)
    previous = _seen.get(key)
    _seen[key] = current
    if previous is None or current == previous:
        return False
    writer = current.split(" ", 1)[0] if current else ""
    return writer != str(os.getpid())


def refresh_if_changed_elsewhere(persist_directory: str | os.PathLike) -> bool:
    """Drop ChromaDB's cached system for the directory if another process wrote.

    The cached system is only removed from ChromaDB's registry, not stopped,
    so a search still running on it in another thread finishes normally; the
    next client builds a new one from disk. Returns True when it dropped one.
    """
    if not changed_elsewhere(persist_directory):
        return False
    try:
        from chromadb.api.shared_system_client import SharedSystemClient
    except Exception:
        return False
    key = os.path.abspath(persist_directory)
    dropped = False
    systems = SharedSystemClient._identifier_to_system
    for identifier in list(systems):
        try:
            same = os.path.abspath(identifier) == key
        except Exception:
            same = False
        if same:
            systems.pop(identifier, None)
            dropped = True
    return dropped

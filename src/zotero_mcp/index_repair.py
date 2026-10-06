"""Check and rebuild the local ChromaDB vector index.

ChromaDB keeps each collection's vectors in an hnswlib index on disk
(``<segment>/data_level0.bin`` with ``header.bin``) and the map from document
id to hnswlib label in ``index_metadata.pickle``. When two processes write the
same persistent index at once — which happened on Windows before index updates
were locked there — both hand out the same labels and each saves its own copy
over the other's: labels end up stored twice, and ids map to labels the index
no longer holds. Nothing notices until a later write touches one of those ids.
Then ChromaDB fails with "Failed to apply logs to the hnsw segment writer",
keeps the change pending, and every process that opens the index afterwards
fails or hangs replaying it.

:func:`check_vector_index` finds such ids by reading the files directly.
:func:`rebuild_vector_index` writes a fresh index holding every passage whose
vector can be trusted, copied as stored — nothing is re-embedded. Items with
an untrusted passage are left out entirely, so the next ``update-db`` sees
them as not indexed and indexes them again from their text.
"""

from __future__ import annotations

import os
import pickle
import shutil
import sqlite3
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

#: hnswlib persistent header: int version, then size_t/int fields in this order.
_HEADER = struct.Struct("<i Q Q Q Q Q Q i I Q Q Q d Q")
_DELETE_MARK = 0x01
_DELETE_OP = 3  # chromadb.db.mixins.embeddings_queue operation code

Progress = Callable[[str], None]


class IndexRepairError(RuntimeError):
    """The index could not be checked or rebuilt."""


@dataclass
class VectorIndexCheck:
    """Result of :func:`check_vector_index`."""

    segment_dir: Path
    elements: int = 0
    ids_mapped: int = 0
    duplicate_labels: int = 0
    missing_labels: int = 0
    #: Document ids whose stored vector cannot be trusted.
    untrusted_ids: set[str] = field(default_factory=set)
    #: Pending (not yet saved) changes in ChromaDB's log, by operation code.
    pending: dict[int, int] = field(default_factory=dict)

    @property
    def untrusted_items(self) -> set[str]:
        return {_item_key(i) for i in self.untrusted_ids}

    @property
    def healthy(self) -> bool:
        return not self.untrusted_ids


def _item_key(doc_id: str) -> str:
    return doc_id.split("#", 1)[0]


def _vector_segment_dir(chroma_dir: Path, db: sqlite3.Connection, collection: str) -> tuple[str, Path]:
    row = db.execute(
        "select s.id from segments s join collections c on c.id = s.collection "
        "where c.name = ? and s.scope = 'VECTOR'",
        (collection,),
    ).fetchone()
    if not row:
        raise IndexRepairError(f"No vector segment for collection {collection!r} in {chroma_dir}")
    return row[0], chroma_dir / row[0]


def _open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def check_vector_index(chroma_dir: str | os.PathLike, collection: str = "zotero_library") -> VectorIndexCheck:
    """Find document ids whose vector the on-disk hnswlib index does not hold reliably.

    Read-only. An id is untrusted when its label is absent from the index or
    stored for more than one element (two writers handed out the same label).
    """
    chroma_dir = Path(chroma_dir)
    db = _open_ro(chroma_dir / "chroma.sqlite3")
    try:
        segment_id, segment_dir = _vector_segment_dir(chroma_dir, db, collection)
        saved = db.execute("select seq_id from max_seq_id where segment_id = ?", (segment_id,)).fetchone()
        saved_seq = int(saved[0]) if saved else 0
        pending = dict(
            db.execute(
                "select operation, count(*) from embeddings_queue where seq_id > ? group by operation",
                (saved_seq,),
            ).fetchall()
        )
    finally:
        db.close()

    result = VectorIndexCheck(segment_dir=segment_dir, pending=pending)
    header_path = segment_dir / "header.bin"
    if not header_path.exists():
        return result  # nothing saved yet: an index too small to have been persisted

    header = header_path.read_bytes()
    if len(header) < _HEADER.size:
        raise IndexRepairError(f"{header_path} is truncated")
    (version, off_level0, _max_elements, count, per_element, label_offset,
     *_rest) = _HEADER.unpack_from(header)
    if version != 1:
        raise IndexRepairError(f"Unsupported hnswlib index version {version} in {header_path}")

    data_path = segment_dir / "data_level0.bin"
    if data_path.stat().st_size < count * per_element:
        raise IndexRepairError(f"{data_path} is shorter than its header says")

    with open(segment_dir / "index_metadata.pickle", "rb") as f:
        id_map = pickle.load(f)
    label_to_id: dict[int, str] = id_map["label_to_id"]
    result.ids_mapped = len(label_to_id)
    result.elements = count

    seen: dict[int, int] = {}
    deleted: set[int] = set()
    chunk = max(1, (64 * 1024 * 1024) // per_element)
    with open(data_path, "rb") as f:
        for start in range(0, count, chunk):
            n = min(chunk, count - start)
            f.seek(start * per_element)
            block = f.read(n * per_element)
            for i in range(n):
                base = i * per_element
                label = struct.unpack_from("<Q", block, base + label_offset)[0]
                seen[label] = seen.get(label, 0) + 1
                if block[base + off_level0 + 2] & _DELETE_MARK:
                    deleted.add(label)

    duplicated = {label for label, n in seen.items() if n > 1}
    missing = {label for label in label_to_id if label not in seen}
    result.duplicate_labels = len(duplicated)
    result.missing_labels = len(missing)
    result.untrusted_ids = {
        label_to_id[label] for label in (duplicated | missing) if label in label_to_id
    }
    return result


def rebuild_vector_index(
    chroma_dir: str | os.PathLike,
    work_dir: str | os.PathLike,
    collection: str = "zotero_library",
    skip_items: set[str] | None = None,
    batch_size: int = 1000,
    progress: Progress = print,
) -> dict:
    """Write a fresh copy of the index into ``work_dir`` from ``chroma_dir``.

    ``chroma_dir`` is only read: it is first copied to ``work_dir/source``,
    whose pending deletes are dropped from the log (the metadata already
    reflects them, and replaying one that touches a damaged label is exactly
    what fails). Every passage is then copied — id, vector, text, metadata —
    into ``work_dir/chroma_db``, except all passages of ``skip_items`` and of
    any item whose vectors cannot be read. Returns counts and the items left
    out; they are indexed again by the next update.
    """
    import chromadb
    from chromadb.config import Settings

    chroma_dir = Path(chroma_dir)
    work_dir = Path(work_dir)
    source = work_dir / "source"
    target = work_dir / "chroma_db"
    if target.exists() or source.exists():
        raise IndexRepairError(f"{work_dir} already holds a rebuild; remove it or pick another folder")
    if not (chroma_dir / "chroma.sqlite3").exists():
        raise IndexRepairError(f"No index at {chroma_dir}")
    work_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    progress(f"Copying the index to {source} ...")
    shutil.copytree(chroma_dir, source)

    db = sqlite3.connect(source / "chroma.sqlite3")
    try:
        segment_id, _ = _vector_segment_dir(source, db, collection)
        saved = db.execute("select seq_id from max_seq_id where segment_id = ?", (segment_id,)).fetchone()
        saved_seq = int(saved[0]) if saved else 0
        dropped = db.execute(
            "delete from embeddings_queue where seq_id > ? and operation = ?",
            (saved_seq, _DELETE_OP),
        ).rowcount
        db.commit()
    finally:
        db.close()
    if dropped:
        progress(f"Dropped {dropped} pending deletes from the copy's log.")

    settings = Settings(anonymized_telemetry=False, allow_reset=False)
    src = chromadb.PersistentClient(path=str(source), settings=settings).get_collection(collection)
    dst_client = chromadb.PersistentClient(path=str(target), settings=settings)
    # No embedding function: the collection's stored configuration stays
    # empty, as in an index zotero-mcp created, so its own client opens it
    # with whatever embedding model is configured.
    dst = dst_client.create_collection(collection, embedding_function=None)

    all_ids: list[str] = []
    offset = 0
    while True:
        page = src.get(include=[], limit=10000, offset=offset)["ids"]
        if not page:
            break
        all_ids.extend(page)
        offset += len(page)
    progress(f"{len(all_ids)} passages in the index.")

    by_item: dict[str, list[str]] = {}
    for doc_id in all_ids:
        by_item.setdefault(_item_key(doc_id), []).append(doc_id)
    skip = set(skip_items or ())
    left_out = sorted(k for k in by_item if k in skip)
    copied = 0
    failed_items: list[str] = []

    def copy_items(keys: list[str]) -> None:
        nonlocal copied
        ids = [i for k in keys for i in by_item[k]]
        try:
            got = src.get(ids=ids, include=["embeddings", "documents", "metadatas"])
        except Exception:
            if len(keys) == 1:
                failed_items.append(keys[0])
                return
            mid = len(keys) // 2
            copy_items(keys[:mid])
            copy_items(keys[mid:])
            return
        if len(got["ids"]) != len(ids):
            # Some ids vanished between listing and reading: copy what came.
            pass
        dst.add(
            ids=got["ids"],
            embeddings=got["embeddings"],
            documents=got["documents"],
            metadatas=got["metadatas"],
        )
        copied += len(got["ids"])

    pending_keys: list[str] = []
    pending_n = 0
    keys = [k for k in sorted(by_item) if k not in skip]
    for n_done, key in enumerate(keys, 1):
        pending_keys.append(key)
        pending_n += len(by_item[key])
        if pending_n >= batch_size or n_done == len(keys):
            copy_items(pending_keys)
            pending_keys, pending_n = [], 0
            progress(f"  {copied} passages copied ({n_done}/{len(keys)} items)")

    count = dst.count()
    if count != copied:
        raise IndexRepairError(f"The new index holds {count} passages, expected {copied}")
    probe = dst.get(limit=1, include=["embeddings"])
    if probe["ids"]:
        dst.query(query_embeddings=[list(probe["embeddings"][0])], n_results=1, include=[])

    left_out = sorted(set(left_out) | set(failed_items))
    elapsed = time.monotonic() - started
    progress(f"Done in {elapsed / 60:.1f} min: {copied} passages copied, {len(left_out)} item(s) left out.")
    return {
        "target": str(target),
        "source_copy": str(source),
        "passages": len(all_ids),
        "copied": copied,
        "left_out_items": left_out,
        "unreadable_items": sorted(failed_items),
        "dropped_pending_deletes": dropped,
    }


def _main(argv: list[str]) -> int:
    """``python -m zotero_mcp.index_repair <request.json>``: run one rebuild.

    Used by ``zotero-mcp db-rebuild-vectors``, which runs the rebuild in a
    child process so every ChromaDB file handle is closed before it swaps the
    folders. Writes ``rebuild-result.json`` next to the request.
    """
    import json

    request_path = Path(argv[0])
    request = json.loads(request_path.read_text())
    try:
        result = rebuild_vector_index(
            request["chroma_dir"],
            request["work_dir"],
            skip_items=set(request.get("skip_items") or ()),
        )
    except IndexRepairError as e:
        print(f"Rebuild failed: {e}")
        return 2
    (request_path.parent / "rebuild-result.json").write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))

"""Tests for the vector-index check and rebuild (zotero_mcp.index_repair).

The damage they model is what two processes writing the same persistent
ChromaDB index leave behind: an hnswlib label stored for two elements, and an
id whose label no element holds any more. It is made here by editing the
saved files directly, which is deterministic, rather than by racing writers.
"""

import random
import struct

import pytest

chromadb = pytest.importorskip("chromadb")

from zotero_mcp.index_repair import (  # noqa: E402
    _HEADER,
    REBUILD_HNSW,
    check_vector_index,
    rebuild_vector_index,
)

COLLECTION = "zotero_library"
DIM = 8


def _build(path, items=300, chunks=4):
    """An index of `items` items with `chunks` passages each, saved to disk."""
    client = chromadb.PersistentClient(path=str(path))
    col = client.create_collection(COLLECTION, embedding_function=None)
    rng = random.Random(7)
    ids = [f"I{i:04d}#{c}" for i in range(items) for c in range(chunks)]
    for start in range(0, len(ids), 200):
        batch = ids[start:start + 200]
        col.add(
            ids=batch,
            embeddings=[[rng.random() for _ in range(DIM)] for _ in batch],
            documents=[f"text of {i}" for i in batch],
            metadatas=[{"item_key": i.split("#")[0]} for i in batch],
        )
    return ids


def _segment(path):
    check = check_vector_index(path)
    return check.segment_dir


def _duplicate_label(segment_dir, src_element, dst_element):
    """Write element src's label over element dst's, as a lost write would."""
    header = (segment_dir / "header.bin").read_bytes()
    fields = _HEADER.unpack_from(header)
    per_element, label_offset = fields[4], fields[5]
    with open(segment_dir / "data_level0.bin", "r+b") as f:
        f.seek(src_element * per_element + label_offset)
        label = f.read(8)
        f.seek(dst_element * per_element + label_offset)
        overwritten = struct.unpack("<Q", f.read(8))[0]
        f.seek(dst_element * per_element + label_offset)
        f.write(label)
    return struct.unpack("<Q", label)[0], overwritten


@pytest.fixture
def index(tmp_path):
    path = tmp_path / "chroma_db"
    ids = _build(path)
    return path, ids


def test_a_healthy_index_checks_clean(index):
    path, ids = index
    check = check_vector_index(path)
    assert check.healthy
    assert check.elements >= 1000  # saved at ChromaDB's 1000-change threshold
    assert check.duplicate_labels == check.missing_labels == 0


def test_check_finds_duplicated_and_missing_labels(index):
    path, ids = index
    seg = _segment(path)
    label, overwritten = _duplicate_label(seg, 10, 11)
    check = check_vector_index(path)
    assert not check.healthy
    assert check.duplicate_labels == 1
    assert check.missing_labels == 1
    # Both the twice-stored label's id and the id whose label vanished.
    assert len(check.untrusted_ids) == 2
    assert all(i.split("#")[0] in check.untrusted_items for i in check.untrusted_ids)


def test_rebuild_leaves_out_damaged_items_and_is_clean(index, tmp_path):
    path, ids = index
    _duplicate_label(_segment(path), 10, 11)
    check = check_vector_index(path)
    bad_items = check.untrusted_items

    result = rebuild_vector_index(
        path, tmp_path / "work", skip_items=bad_items, progress=lambda m: None
    )

    target = result["target"]
    assert check_vector_index(target).healthy
    assert result["left_out_items"] == sorted(bad_items)
    expected = [i for i in ids if i.split("#")[0] not in bad_items]
    assert result["copied"] == len(expected)
    # The source is untouched; the copy is a separate folder.
    assert check_vector_index(path).untrusted_items == bad_items

    from chromadb.config import Settings

    col = chromadb.PersistentClient(
        path=target, settings=Settings(anonymized_telemetry=False, allow_reset=False)
    ).get_collection(COLLECTION)
    got = col.get(ids=expected[:5], include=["documents", "metadatas", "embeddings"])
    docs = dict(zip(got["ids"], got["documents"]))
    metas = dict(zip(got["ids"], got["metadatas"]))
    assert docs == {i: f"text of {i}" for i in expected[:5]}
    assert all(metas[i]["item_key"] == i.split("#")[0] for i in expected[:5])
    # Vectors are copied as stored: each passage still finds itself first.
    src = chromadb.PersistentClient(path=str(path)).get_collection(COLLECTION)
    original = src.get(ids=expected[:5], include=["embeddings"])
    for doc_id, vector in zip(original["ids"], original["embeddings"]):
        hit = col.query(query_embeddings=[list(vector)], n_results=1, include=[])["ids"][0][0]
        assert hit == doc_id
    assert col.configuration["hnsw"]["ef_search"] == REBUILD_HNSW["ef_search"]
    # Removals work again — the operation the damage used to break.
    col.delete(ids=expected[:50])
    assert col.count() == len(expected) - 50


def test_rebuild_refuses_an_occupied_work_dir(index, tmp_path):
    from zotero_mcp.index_repair import IndexRepairError

    path, _ = index
    (tmp_path / "work" / "chroma_db").mkdir(parents=True)
    with pytest.raises(IndexRepairError):
        rebuild_vector_index(path, tmp_path / "work", progress=lambda m: None)


def test_status_reports_integrity_and_caches_it(index, monkeypatch):
    from zotero_mcp.tools import search as search_tools

    path, _ = index
    search_tools._INTEGRITY_CACHE.clear()
    assert search_tools._index_integrity(str(path), COLLECTION).startswith("intact")

    calls = []
    import zotero_mcp.index_repair as repair

    original = repair.check_vector_index

    def counting(*a, **kw):
        calls.append(1)
        return original(*a, **kw)

    monkeypatch.setattr(repair, "check_vector_index", counting)
    search_tools._index_integrity(str(path), COLLECTION)
    assert calls == []  # unchanged files: served from the cache

    _duplicate_label(_segment(path), 10, 11)
    line = search_tools._index_integrity(str(path), COLLECTION)
    assert line.startswith("DAMAGED") and "db-rebuild-vectors" in line


def test_status_integrity_is_silent_without_an_index(tmp_path):
    from zotero_mcp.tools import search as search_tools

    assert search_tools._index_integrity(None) is None
    assert search_tools._index_integrity(str(tmp_path)) is None


def test_rebuild_keeps_clustered_passages_findable(tmp_path):
    """Papers whose passages are near-duplicates must not trap the search.

    Copied paper by paper with ChromaDB's default graph settings, an index
    like this answered some queries with passages from the wrong papers
    only (recall@200 as low as 0). The rebuild shuffles and uses
    REBUILD_HNSW; recall of the exact top 50 must stay high for every query.
    """
    import numpy as np

    rng = np.random.default_rng(0)
    dim, papers, per = 256, 300, 60
    centers = rng.normal(size=(papers, dim))
    vectors = np.vstack([c + 0.25 * rng.normal(size=(per, dim)) for c in centers])
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    ids = [f"P{p:04d}#{i}" for p in range(papers) for i in range(per)]

    path = tmp_path / "chroma_db"
    col = chromadb.PersistentClient(path=str(path)).create_collection(
        COLLECTION, embedding_function=None
    )
    for start in range(0, len(ids), 1000):  # paper by paper, as updates write
        col.add(
            ids=ids[start:start + 1000],
            embeddings=vectors[start:start + 1000].tolist(),
            documents=["x"] * len(ids[start:start + 1000]),
        )

    result = rebuild_vector_index(path, tmp_path / "work", progress=lambda m: None)
    from chromadb.config import Settings

    rebuilt = chromadb.PersistentClient(
        path=result["target"], settings=Settings(anonymized_telemetry=False, allow_reset=False)
    ).get_collection(COLLECTION)

    queries = centers[rng.choice(papers, 20)] + 0.9 * rng.normal(size=(20, dim))
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    recalls = []
    for q in queries:
        exact = {ids[i] for i in np.argsort(((vectors - q) ** 2).sum(1))[:50]}
        got = set(rebuilt.query(query_embeddings=[q.tolist()], n_results=50, include=[])["ids"][0])
        recalls.append(len(exact & got) / 50)
    # Built paper by paper with ChromaDB's defaults, data like this gave
    # min 0.00 over repeated runs (scripts in the PR description).
    assert min(recalls) >= 0.9, recalls

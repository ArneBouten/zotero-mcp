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
    assert got["documents"] == [f"text of {i}" for i in expected[:5]]
    assert got["metadatas"][0]["item_key"] == expected[0].split("#")[0]
    # Removals work again — the operation the damage used to break.
    col.delete(ids=expected[:50])
    assert col.count() == len(expected) - 50


def test_rebuild_refuses_an_occupied_work_dir(index, tmp_path):
    from zotero_mcp.index_repair import IndexRepairError

    path, _ = index
    (tmp_path / "work" / "chroma_db").mkdir(parents=True)
    with pytest.raises(IndexRepairError):
        rebuild_vector_index(path, tmp_path / "work", progress=lambda m: None)

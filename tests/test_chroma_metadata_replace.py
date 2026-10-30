"""Metadata writes replace, not merge (ChromaClient._replacing).

ChromaDB 1.x merges metadata on ``upsert`` and ``update``: a key the new dict
lacks keeps its stored value. The relabeller drops ``structure_gemini`` once
Gemini answers and drops headings that no longer apply, and a re-indexed paper
gets fresh passages; under merge semantics all of those kept their old keys.
With ``structure_gemini: "failed"`` stuck on every first passage, each index
update relabelled the whole library and published all of it to the shared log.
"""

import pytest

pytest.importorskip("chromadb")

import chromadb  # noqa: E402
from chromadb.config import Settings  # noqa: E402

from zotero_mcp import relabel  # noqa: E402
from zotero_mcp import structure as st  # noqa: E402
from zotero_mcp.chroma_client import ChromaClient  # noqa: E402


def _client(tmp_path):
    db = chromadb.PersistentClient(path=str(tmp_path / "chroma"), settings=Settings(anonymized_telemetry=False))
    c = ChromaClient.__new__(ChromaClient)
    c.client = db
    c.collection_name = "zotero_library"
    c.collection = db.get_or_create_collection("zotero_library", embedding_function=None)
    c.index_sync = None
    return c


def _meta(c, doc_id):
    return c.collection.get(ids=[doc_id], include=["metadatas"])["metadatas"][0]


def test_update_metadatas_removes_keys_the_new_dict_lacks(tmp_path):
    c = _client(tmp_path)
    c.upsert_embeddings(["t"], [{"item_key": "A", "heading": "Intro", "structure_gemini": "failed"}],
                        ["A#0"], [[0.1, 0.2]])

    c.update_metadatas(["A#0"], [{"item_key": "A", "structure_v": 3}], replace=True)

    assert _meta(c, "A#0") == {"item_key": "A", "structure_v": 3}


def test_update_metadatas_still_merges_by_default(tmp_path):
    c = _client(tmp_path)
    c.upsert_embeddings(["t"], [{"item_key": "A", "title": "T"}], ["A#0"], [[0.1, 0.2]])

    c.update_metadatas(["A#0"], [{"item_key": "A", "group_id": 0}])

    assert _meta(c, "A#0") == {"item_key": "A", "title": "T", "group_id": 0}


def test_upsert_replaces_a_reindexed_passage_metadata(tmp_path):
    c = _client(tmp_path)
    c.upsert_embeddings(["old"], [{"item_key": "A", "heading": "Old heading", "structure_v": 3}],
                        ["A#0"], [[0.1, 0.2]])

    # The PDF was replaced: the passage is written afresh, without labels.
    c.upsert_embeddings(["new"], [{"item_key": "A", "chunk_index": 0}], ["A#0"], [[0.3, 0.4]])

    assert _meta(c, "A#0") == {"item_key": "A", "chunk_index": 0}


def test_new_ids_are_written_unchanged(tmp_path):
    c = _client(tmp_path)
    c.upsert_embeddings(["a", "b"], [{"item_key": "A"}, {"item_key": "B", "x": 1}], ["A#0", "B#0"],
                        [[0.1, 0.2], [0.2, 0.1]])

    assert _meta(c, "B#0") == {"item_key": "B", "x": 1}


def test_a_gemini_retry_that_succeeds_takes_the_item_off_the_list(tmp_path):
    c = _client(tmp_path)
    failed = {"item_key": "A", "parent_item_key": "A", "chunk_index": 0,
              "structure_v": st.STRUCTURE_VERSION, "structure_gemini": "failed"}
    c.upsert_embeddings(["t"], [failed], ["A#0"], [[0.1, 0.2]])
    assert relabel.items_to_label(c.collection, retry_gemini=True) == ["A"]

    ok = dict(failed)
    ok.pop("structure_gemini")
    c.update_metadatas(["A#0"], [ok], replace=True)

    assert relabel.items_to_label(c.collection, retry_gemini=True) == []

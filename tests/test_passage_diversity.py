"""Chunked semantic search returns `limit` distinct papers, not one paper's passages.

A passage index gives each paper many near-identical hits, and the paper most
about the query fills the top of the list. These tests pin the thinning that
keeps a few passages per paper and the pool sizing that feeds it.
"""

import sys

import pytest

if sys.version_info >= (3, 14):
    pytest.skip(
        "chromadb relies on pydantic v1 paths incompatible with Python 3.14+",
        allow_module_level=True,
    )

from zotero_mcp import semantic_search
from zotero_mcp.semantic_search import _diversify_passages


def _results(ids):
    return {
        "ids": [list(ids)],
        "distances": [[i / 100 for i in range(len(ids))]],
        "documents": [[f"doc {x}" for x in ids]],
        "metadatas": [[{"parent_item_key": x.split("#")[0]} for x in ids]],
    }


# --- _diversify_passages ---------------------------------------------------


def test_caps_passages_per_item_and_keeps_order():
    r = _results(["A#1", "A#2", "A#3", "B#0", "A#4", "B#1", "B#2", "C#0"])
    kept = _diversify_passages(r, max_per_item=2, max_items=None)
    assert kept == 5
    assert r["ids"][0] == ["A#1", "A#2", "B#0", "B#1", "C#0"]
    # Every column is thinned in step.
    assert r["documents"][0] == ["doc A#1", "doc A#2", "doc B#0", "doc B#1", "doc C#0"]
    assert [m["parent_item_key"] for m in r["metadatas"][0]] == ["A", "A", "B", "B", "C"]
    assert r["distances"][0] == [0.0, 0.01, 0.03, 0.05, 0.07]


def test_caps_number_of_items():
    r = _results(["A#0", "B#0", "A#1", "C#0", "D#0", "C#1"])
    _diversify_passages(r, max_per_item=2, max_items=2)
    assert r["ids"][0] == ["A#0", "B#0", "A#1"]


def test_one_passage_per_item():
    r = _results(["A#0", "A#1", "B#0", "B#1"])
    _diversify_passages(r, max_per_item=1, max_items=10)
    assert r["ids"][0] == ["A#0", "B#0"]


def test_empty_results_are_left_alone():
    r = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert _diversify_passages(r, 2, 5) == 0
    assert _diversify_passages({}, 2, 5) == 0


def test_item_level_ids_without_passage_suffix():
    r = _results(["A", "B", "A", "C"])
    _diversify_passages(r, max_per_item=1, max_items=None)
    assert r["ids"][0] == ["A", "B", "C"]


# --- search(): crowding --------------------------------------------------------


class _CrowdedChroma:
    """One long book owns the first 60 hits; four papers follow."""

    embedding_max_tokens = 8000

    def __init__(self):
        self.n_results = None
        ids = [f"BOOK#{i}" for i in range(60)]
        for key in ("P1", "P2", "P3", "P4"):
            ids += [f"{key}#{i}" for i in range(3)]
        self._ids = ids

    def search(self, query_texts=None, n_results=10, where=None, where_document=None):
        self.n_results = n_results
        ids = self._ids[:n_results]
        return {
            "ids": [ids],
            "distances": [[0.1 + i / 1000 for i in range(len(ids))]],
            "documents": [[f"text {x}" for x in ids]],
            "metadatas": [[{"parent_item_key": x.split("#")[0], "chunk_index": 0} for x in ids]],
        }


def _search(monkeypatch, chunking=None, reranker=None, multiplier=5):
    monkeypatch.setattr(semantic_search, "get_zotero_client", lambda: object())
    s = semantic_search.ZoteroSemanticSearch(chroma_client=_CrowdedChroma())
    s._chunking_config = {"enabled": True, **(chunking or {})}
    s._reranker_config = {"enabled": reranker is not None, "candidate_multiplier": multiplier}
    s._reranker = reranker
    s._attach_zotero_items = lambda enriched: None
    return s


def test_crowded_pool_still_returns_limit_items(monkeypatch):
    s = _search(monkeypatch)
    out = s.search("q", limit=5)
    assert [r["item_key"] for r in out["results"]] == ["BOOK", "P1", "P2", "P3", "P4"]
    assert s.chroma_client.n_results == 200  # default pool
    assert out["passages_considered"] == 72


def test_pool_grows_with_limit_and_is_capped(monkeypatch):
    s = _search(monkeypatch)
    assert s._passage_pool_size(5, 2) == 200
    assert s._passage_pool_size(30, 2) == 600
    assert s._passage_pool_size(200, 2) == 1000
    s._chunking_config["search_pool"] = 50
    assert s._passage_pool_size(5, 2) == 100  # limit * 20 floor
    s._chunking_config["search_pool"] = "junk"
    assert s._passage_pool_size(5, 2) == 200


def test_passages_per_item_config(monkeypatch):
    s = _search(monkeypatch, chunking={"max_passages_per_item": 3})
    assert s._passages_per_item() == 3
    s._chunking_config["max_passages_per_item"] = 0
    assert s._passages_per_item() == 1
    s._chunking_config["max_passages_per_item"] = None
    assert s._passages_per_item() == 2


class _ReverseReranker:
    """A plain rerank() object: reverses whatever it is shown."""

    def __init__(self):
        self.seen = None

    def rerank(self, query, documents, top_k):
        self.seen = list(documents)
        return list(reversed(range(len(documents))))[:top_k]


def test_reranker_sees_few_passages_per_item_from_many_items(monkeypatch):
    rr = _ReverseReranker()
    s = _search(monkeypatch, reranker=rr, multiplier=5)
    out = s.search("q", limit=2)
    # 2 x 5 = 10 items allowed, 2 passages each, but only 5 items exist.
    assert len(rr.seen) == 10
    assert sum(1 for d in rr.seen if "BOOK" in d) == 2
    # Reversed order puts P4 first; grouping yields two distinct items.
    assert [r["item_key"] for r in out["results"]] == ["P4", "P3"]
    assert out["rerank"]["applied"] is True


def test_item_level_index_keeps_historical_fetch(monkeypatch):
    monkeypatch.setattr(semantic_search, "get_zotero_client", lambda: object())
    s = semantic_search.ZoteroSemanticSearch(chroma_client=_CrowdedChroma())
    s._chunking_config = {"enabled": False}
    s._reranker_config = {"enabled": False}
    s._attach_zotero_items = lambda enriched: None
    s.search("q", limit=7)
    assert s.chroma_client.n_results == 7

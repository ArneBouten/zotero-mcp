"""Passages that answer a question, instead of a paper's whole text."""

from conftest import DummyContext, FakeZotero

import zotero_mcp.client as zotero_client
import zotero_mcp.local_db as local_db
import zotero_mcp.utils as zotero_utils
from zotero_mcp import excerpts
from zotero_mcp.local_db import FulltextExtraction
from zotero_mcp.read_online import Reading
from zotero_mcp.tools import retrieval

HEAD = "Journal of Testing Studies 12 (2020)"


def _page(n: int, body: str) -> str:
    return f"{HEAD}\n{body}\n{n}"


def _paper() -> str:
    filler = ("The literature on motivation in schools is broad and covers many settings. "
              "Teachers differ in how they structure lessons and give feedback. ") * 3
    pages = [
        _page(1, "Autonomy support and burnout in teachers\nAbstract\n" + filler),
        _page(2, "Method\nParticipants. We recruited 412 secondary school teachers through "
                 "school principals in Flanders. The response rate was 61 percent.\n\n" + filler),
        _page(3, "Measures. Burnout was measured with the Maslach Burnout Inventory, "
                 "educator version, on a seven-point scale.\n\n" + filler),
        _page(4, "Results\nAutonomy support predicted lower emotional exhaustion "
                 "(beta = -.32, p < .001).\n\n" + filler),
        _page(5, "References\nDeci, E. L. (2000). Self-determination. doi:10.1/x (2001). "
                 "Smith (2002). Ryan (2003). Jones (2004). Teachers recruited burnout (2005)."),
    ]
    return "\f".join(pages)


class _Ranker:
    label = "fake/rank"

    def __init__(self, order=None, error=None):
        self.order, self.error, self.seen = order, error, None

    def rerank_detailed(self, query, documents, top_k):
        self.seen = documents
        if self.error:
            return [(i, 0.0) for i in range(top_k)], self.error
        hits = [i for i, d in enumerate(documents) if "Maslach" in d]
        rest = [i for i in range(len(documents)) if i not in hits]
        return [(i, 1.0 - n / 100) for n, i in enumerate(hits + rest)][:top_k], None


def test_passages_keep_pages_and_drop_running_heads_and_references():
    passages, dropped = excerpts.split_passages(_paper())
    assert dropped
    text = " ".join(p.text for p in passages)
    assert HEAD not in text
    assert "Deci" not in text
    recruit = next(p for p in passages if "412" in p.text)
    assert recruit.first_page <= 2 <= recruit.last_page


def test_keyword_ranking_finds_the_answer():
    found = excerpts.find("How many teachers were recruited, and how?", _paper(), passages=1,
                          reranker=None)
    assert len(found.passages) == 1
    assert "412" in found.passages[0].text
    md = found.markdown()
    assert "The passage of" in md
    assert ("**p. 2**" in md or "**pp. 1–2**" in md) and "keyword match" in md and "reference list was left out" in md


def test_a_question_about_references_keeps_them():
    found = excerpts.find("Which references are cited for self-determination?", _paper(),
                          passages=2, reranker=None)
    assert not found.dropped_references


def test_reranker_order_wins_and_is_named():
    ranker = _Ranker()
    found = excerpts.find("What instrument measured exhaustion?", _paper(), passages=1, reranker=ranker)
    assert "Maslach" in found.passages[0].text
    assert "fake/rank" in found.markdown()
    assert ranker.seen  # passages went to the re-ranker


def test_failed_reranker_falls_back_to_keywords_and_says_so():
    found = excerpts.find("How many teachers were recruited?", _paper(), passages=1,
                          reranker=_Ranker(error="HTTP 401"))
    assert "412" in found.passages[0].text
    assert "HTTP 401" in found.markdown()


def test_adjacent_passages_merge_into_one_block():
    ps = [excerpts.Passage(i, f"t{i}", 3, 3) for i in (4, 5, 9)]
    blocks = excerpts._merge_adjacent(ps)
    assert [[p.index for p in b] for b in blocks] == [[4, 5], [9]]


def test_no_match_says_so():
    found = excerpts.find("photosynthesis chloroplast", _paper(), reranker=None)
    assert not found.passages
    assert "No passage" in found.markdown()


def test_text_without_pages_has_no_page_labels():
    text = "We recruited 412 teachers. " * 10 + "Burnout was measured with the MBI. " * 10
    found = excerpts.find("How was burnout measured?", text, passages=1, reranker=None)
    assert found.passages and found.passages[0].first_page == 0
    assert "**·**" in found.markdown()


def test_read_paper_with_a_question_returns_passages():
    reading = Reading(True, "Teacher paper", source="publisher", url="https://x.org/a.pdf",
                      pages=5, text=_paper(), text_path="C:/tmp/a.txt")
    md = reading.markdown(question="How many teachers were recruited?", passages=1)
    assert "412" in md and "Passages for" in md and "C:/tmp/a.txt" in md
    assert "Maslach" not in md


class _Config:
    class semantic_search:  # noqa: N801
        class extraction:
            fulltext_display_max_pages = 10
            attachment_priority = ("pdf", "html", "other")

    def resolve_zotero_db_path(self):
        return "/dev/null"


def test_get_item_fulltext_with_a_question(monkeypatch):
    seen = {}

    class _Reader:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_item_by_key(self, key):
            return type("I", (), {"item_id": 1})()

        def extract_fulltext_detailed(self, item_id):
            return FulltextExtraction(_paper(), "pdf", page_count=5, truncated=False)

    monkeypatch.setattr(zotero_utils, "is_local_mode", lambda: True)
    monkeypatch.setattr(zotero_client, "get_zotero_client", FakeZotero)
    monkeypatch.setattr(retrieval, "load_config", lambda: _Config())
    monkeypatch.setattr(local_db, "LocalZoteroReader", _Reader)
    monkeypatch.setattr(excerpts, "configured_reranker", lambda *a: None)

    out = retrieval.get_item_fulltext(item_key="ABCD1234", question="How many teachers were recruited?",
                                      passages="1", ctx=DummyContext())
    assert "412" in out and "Passages for" in out
    assert "Maslach" not in out
    assert seen["pdf_max_pages"] == retrieval.EXCERPT_MAX_PAGES


def test_a_references_heading_followed_by_more_chapters_is_kept():
    body = "Chapter text about teachers and burnout in schools. " * 40
    text = body + "\nReferences\nSmith (2001). Jones (2002).\n" + body * 3
    passages, dropped = excerpts.split_passages(text)
    assert not dropped
    assert sum(len(p.text) for p in passages) > len(body) * 3


def test_ligatures_match_plain_words():
    text = "The beneﬁts of exercise were large. " * 5 + "Other text about nothing in particular. " * 20
    found = excerpts.find("What are the benefits?", text, passages=1, reranker=None)
    assert found.passages and "benefits" in found.passages[0].text

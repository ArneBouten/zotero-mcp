"""The stored citation graph (OpenAlex), with a fake OpenAlex and a fake library."""

import copy
from pathlib import Path

import pytest

from zotero_mcp import citations


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(citations.time, "sleep", lambda s: None)
    monkeypatch.setattr(citations, "start_background", lambda: True)
    return tmp_path


def _item(key, title, doi="", year="2020", author="Smith", itype="journalArticle"):
    return {"key": key, "data": {"key": key, "itemType": itype, "title": title, "DOI": doi, "date": year,
                                 "creators": [{"creatorType": "author", "lastName": author}]}}


class Library:
    def __init__(self, items):
        self.items = {i["key"]: copy.deepcopy(i) for i in items}

    def list_items(self, item_type=None, limit=100):
        return list(self.items.values())

    def get_items(self, keys):
        return {k: self.items[k] for k in keys if k in self.items}

    def collection_items(self, key, include_subcollections=False):
        return [self.items[k] for k in ("A", "B", "C") if k in self.items]


def _work(wid, title, doi=None, refs=(), year=2020, author="Ann Smith", cited=5):
    return {"id": f"https://openalex.org/{wid}", "doi": f"https://doi.org/{doi}" if doi else None, "title": title,
            "publication_year": year, "cited_by_count": cited, "referenced_works": [f"https://openalex.org/{r}" for r in refs],
            "authorships": [{"author": {"display_name": author}}]}


class OpenAlex:
    """Answers list calls by DOI, single works by DOI or ID, and 'cites' lists."""

    def __init__(self, works):
        self.works = {w["id"].rsplit("/", 1)[-1]: w for w in works}
        self.calls = []

    def api_json(self, url, params=None, **kw):
        params = params or {}
        self.calls.append((url, dict(params)))
        by_doi = {(w["doi"] or "").lower().replace("https://doi.org/", ""): w for w in self.works.values()}
        if url == citations.API:
            f = params.get("filter", "")
            if f.startswith("doi:"):
                wanted = [d.replace("https://doi.org/", "").lower() for d in f[4:].split("|")]
                return 200, {"results": [by_doi[d] for d in wanted if d in by_doi]}
            if f.startswith("cites:"):
                w = f[6:]
                return 200, {"results": [x for x in self.works.values() if f"https://openalex.org/{w}" in x["referenced_works"]]}
            if params.get("search"):
                return 200, {"results": [w for w in self.works.values() if w["title"].lower() == params["search"].lower()]}
            return 400, None
        tail = url.rsplit("/works/", 1)[-1]
        if tail.startswith("doi:"):
            w = by_doi.get(tail[4:].lower())
        else:
            w = self.works.get(tail)
        return (200, w) if w else (404, None)


class Settings:
    keys: dict = {}
    email = ""

    def has(self, name):
        return False


LIB = [
    _item("A", "Play and learning", "10.1/a", "2010"),
    _item("B", "Games for children", "10.1/b", "2015"),
    _item("C", "A theory of play", "", "1990", author="Huizinga", itype="book"),
    _item("N", "A note", itype="note"),
]
WORKS = [
    _work("W1", "Play and learning", "10.1/a", refs=["W3", "W9"]),
    _work("W2", "Games for children", "10.1/b", refs=["W1", "W3", "W9", "W8"]),
    _work("W3", "A theory of play", None, year=1990, author="Johan Huizinga"),
    _work("W8", "Outside one", "10.9/o1", refs=[]),
    _work("W9", "Often cited classic", "10.9/classic", year=1978, author="Lev Vygotsky", cited=900),
    _work("W5", "A later paper", "10.9/later", refs=["W1"], cited=40),
]


def _run(**kw):
    oa = OpenAlex(WORKS)
    lib = Library(LIB)
    totals = citations.update(backend=lib, http=oa, settings=Settings(), log=lambda m: None, **kw)
    return oa, lib, totals


def test_papers_are_fetched_once_in_batches_and_kept():
    oa, lib, totals = _run()
    assert totals["added"] == 3 and totals["not_found"] == 0          # A and B by DOI, C by title; not the note
    store = citations.load()
    assert store["papers"]["B"]["refs"] == ["W1", "W3", "W9", "W8"]
    assert store["papers"]["C"]["by"] == "title"
    lists = [c for c in oa.calls if c[1].get("filter", "").startswith("doi:")]
    assert len(lists) == 1                                            # both DOIs in one request
    again = citations.update(backend=lib, http=oa, settings=Settings(), log=lambda m: None)
    assert again["added"] == 0                                        # nothing asked twice


def test_a_corrected_doi_is_fetched_again_and_unknown_papers_wait():
    oa, lib, _ = _run()
    lib.items["A"]["data"]["DOI"] = "10.9/unknown"
    totals = citations.update(backend=lib, http=oa, settings=Settings(), log=lambda m: None)
    assert totals["not_found"] == 1
    assert citations.load()["papers"]["A"]["missing"]
    assert citations.update(backend=lib, http=oa, settings=Settings(), log=lambda m: None)["not_found"] == 0


def test_a_title_match_needs_the_same_first_author():
    oa = OpenAlex([_work("W3", "A theory of play", None, year=1990, author="Someone Else")])
    citations.update(backend=Library([LIB[2]]), http=oa, settings=Settings(), log=lambda m: None)
    assert citations.load()["papers"]["C"]["missing"]


def test_a_refusal_stops_without_marking_papers_missing():
    class Refusing(OpenAlex):
        def api_json(self, url, params=None, **kw):
            return 429, None

    totals = citations.update(backend=Library(LIB), http=Refusing([]), settings=Settings(), log=lambda m: None)
    assert "allowance" in totals["stopped"]
    assert citations.load()["papers"] == {}


def test_references_cited_by_related_and_overview():
    oa, lib, _ = _run()
    kw = dict(backend=lib, http=oa, settings=Settings())
    refs = citations.references("B", **kw)
    assert "4 work(s)" in refs and "2 in your library" in refs
    assert "Vygotsky (1978) Often cited classic" in refs
    by = citations.cited_by("C", **kw)                                # the book without DOI, found by title
    assert "cited by 2 paper(s) in your library" in by
    assert "[A]" in by and "[B]" in by
    by_a = citations.cited_by("10.1/a", **kw)
    assert "[B]" in by_a and "A later paper" in by_a                  # outside citing papers, most cited first
    rel = citations.related("A", **kw)
    assert "[B]: 2 shared" in rel
    ov = citations.overview(**kw)
    assert "Often cited classic" in ov and "by 2" in ov
    assert "Cited often, not in your library" in ov


def test_a_work_listed_twice_by_openalex_is_recognised_as_in_the_library():
    works = WORKS + [_work("W7", "A theory of play", None, year=1991, author="J. Huizinga")]
    works[1] = _work("W2", "Games for children", "10.1/b", refs=["W7"])
    oa = OpenAlex(works)
    lib = Library(LIB)
    citations.update(backend=lib, http=oa, settings=Settings(), log=lambda m: None)
    refs = citations.references("B", backend=lib, http=oa, settings=Settings())
    assert "1 in your library" in refs and "[C]" in refs


def test_save_merges_with_what_another_process_wrote():
    store = citations.load()
    store["papers"]["X"] = {"doi": "10.1/x", "fetched": "2026-10-01"}
    other = citations.load()
    other["papers"]["Y"] = {"doi": "10.1/y", "fetched": "2026-10-02"}
    citations.save(other)
    citations.save(store)
    assert set(citations.load()["papers"]) == {"X", "Y"}


def test_maintenance_adds_the_checked_papers_to_the_graph(monkeypatch, tmp_path):
    from zotero_mcp import maintenance

    monkeypatch.setattr(maintenance, "_state_path", lambda: tmp_path / "maintenance.json")
    seen = []

    class Report:
        audits = []

        def totals(self):
            return {}

    lib = Library(LIB[:2])
    out = maintenance.run(keys=["A", "B"], backend=lib, audit_run=lambda **k: Report(),
                          fetch_run=lambda **k: None, log=lambda m: None, every=True,
                          citations_run=lambda keys, **k: seen.append(keys) or {"added": 2})
    assert seen == [["A", "B"]] and out["citations"] == {"added": 2}

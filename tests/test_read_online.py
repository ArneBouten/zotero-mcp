"""Reading a paper found online, without adding it to Zotero."""

from pathlib import Path

import pytest

from zotero_mcp import fulltext_fetch as ff
from zotero_mcp import read_online as ro


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("TMP", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _pdf(path, pages):
    import pymupdf

    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_text((72, 72), text)
    doc.save(path)
    return str(path)


class FakeHttp:
    log = None

    def __init__(self, routes):
        self.routes = routes

    def api_json(self, url, params=None, headers=None, **kw):
        for part, answer in self.routes.items():
            if part in url:
                return answer
        return 404, None


CROSSREF = {"message": {"DOI": "10.1037/0003-066x.55.1.68", "type": "journal-article",
                        "title": ["Self-determination theory and the facilitation of intrinsic motivation"],
                        "author": [{"family": "Ryan", "given": "Richard M."}, {"family": "Deci", "given": "E. L."}],
                        "issued": {"date-parts": [[2000]]}, "page": "68-78"}}


def test_a_paper_is_read_and_nothing_goes_to_zotero(tmp_path, monkeypatch):
    seen = {}

    def find(item, http, settings, budget, steps, log, workdir):
        seen["item"], seen["steps"] = item, list(steps)
        path = _pdf(Path(workdir) / "x.pdf", ["Self-determination theory ... Ryan and Deci", "page two"])
        return path, ff.Candidate("https://repo.org/x.pdf", "Unpaywall (repository)", "published"), \
            ff.Check(True, "matches the item", "published"), []

    # Any write to Zotero would fail the test.
    monkeypatch.setattr(ff, "ZoteroWriter", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no writes")))
    http = FakeHttp({"api.crossref.org/works/": (200, CROSSREF)})
    r = ro.read(doi="https://doi.org/10.1037/0003-066X.55.1.68", save_to=str(tmp_path / "kept"),
                http=http, settings=ff.Settings(host_delay=0), find=find)
    assert r.found and r.pages == 2 and "page two" in r.text
    assert seen["item"].title.startswith("Self-determination") and seen["item"].authors[0] == "Ryan"
    assert seen["item"].pages == "68-78" and "browser" not in seen["steps"]
    assert Path(r.text_path).read_text(encoding="utf-8") == r.text
    assert Path(r.saved_pdf).exists() and Path(r.saved_pdf).parent == tmp_path / "kept"
    md = r.markdown(max_chars=5)
    assert "Not added to Zotero" in md and "the whole text is in" in md


def test_not_found_says_why_and_a_title_alone_works():
    def find(item, http, settings, budget, steps, log, workdir):
        return None, None, None, [ff.Attempt("web search", "https://x.org/a.pdf", "another work")]

    r = ro.read(title="A paper nobody has", author="Nobody", year="2020", http=FakeHttp({}),
                settings=ff.Settings(host_delay=0), find=find)
    assert not r.found and "Nothing was added" in r.markdown()
    assert ro.read(http=FakeHttp({}), settings=ff.Settings(host_delay=0)).reason == "give a DOI or a title"

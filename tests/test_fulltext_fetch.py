"""The full-text fetcher: matching, PDF checks, the step cascade and the run."""

from pathlib import Path

import pytest

from zotero_mcp import fulltext_fetch as ff

pymupdf = pytest.importorskip("pymupdf")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ff, "MIN_PDF_BYTES", 500)  # the generated test PDFs are tiny
    for env in list(ff.KEY_ENV.values()) + ["UNPAYWALL_EMAIL"]:
        monkeypatch.delenv(env, raising=False)
    return tmp_path


def make_pdf(path, lines, pages=8, body=True):
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        text = lines if i == 0 else ([f"Page {i + 1} body text about the study and its results."] if body else [])
        y = 72
        for line in text:
            page.insert_text((72, y), line, fontsize=11)
            y += 16
    doc.save(str(path))
    doc.close()
    return Path(path).read_bytes()


ITEM = {
    "key": "ABCD1234",
    "data": {
        "key": "ABCD1234",
        "itemType": "journalArticle",
        "title": "Autonomy support and need frustration in adolescent sport",
        "creators": [{"lastName": "Van der Kaap", "creatorType": "author"}, {"lastName": "Soenens", "creatorType": "author"}],
        "date": "2021-03-02",
        "DOI": "https://doi.org/10.1234/ABC.5",
        "pages": "101-112",
        "tags": [],
    },
}


def good_pdf(tmp_path, pages=12):
    return make_pdf(
        tmp_path / "good.pdf",
        ["Autonomy support and need frustration", "in adolescent sport",
         "Jan Van der Kaap1, Bart Soenens2", "https://doi.org/10.1234/abc.5"],
        pages=pages,
    )


# --- items and matching ---------------------------------------------------------


def test_item_parsing():
    info = ff.ItemInfo.from_zotero(ITEM)
    assert info.doi == "10.1234/ABC.5"
    assert info.year == "2021"
    assert info.first_author == "Van der Kaap"
    assert info.expected_pages() == 12
    extra = ff.ItemInfo.from_zotero({"key": "K", "data": {"itemType": "journalArticle", "title": "T",
                                                          "extra": "DOI: 10.5/x\narXiv: 2101.01234", "pages": "1123-35"}})
    assert extra.doi == "10.5/x" and extra.arxiv == "2101.01234"
    assert extra.expected_pages() == 13


def test_title_matching_survives_markers_and_lost_spaces():
    title = "Nanometre-scale thermometry in a living cell"
    assert ff.title_in_text(title, "Nanometer scale thermometry1 in a living cell") >= 0.75
    assert ff.title_in_text("Psychological control and adolescent autonomy",
                            "Psychologicalcontrol and adolescent autonomy") == 1.0
    assert ff.title_in_text(title, "Something else entirely") < 0.3
    assert ff.title_similarity("The darker aspects of motivation: pathways to ill-being",
                               "The darker aspects of motivation") >= 0.9


# --- checking a PDF -------------------------------------------------------------


def _check(tmp_path, body, cand=None, item=ITEM):
    path = tmp_path / "x.pdf"
    path.write_bytes(body)
    return ff.check_pdf(ff.ItemInfo.from_zotero(item), ff.probe_pdf(path), cand or ff.Candidate("u", "s"), len(body))


def test_the_right_paper_passes_with_its_version(tmp_path):
    check = _check(tmp_path, good_pdf(tmp_path))
    assert check.ok, check.reason
    assert check.version == "published"  # its DOI is printed in it

    manuscript = make_pdf(tmp_path / "am.pdf", [
        "Author Accepted Manuscript", "Autonomy support and need frustration in adolescent sport",
        "Van der Kaap, Soenens"], pages=12)
    assert _check(tmp_path, manuscript).version == "accepted"


def test_previews_wrong_papers_and_wrong_authors_fail(tmp_path):
    assert not _check(tmp_path, good_pdf(tmp_path, pages=2)).ok  # 2 of 12 pages
    other = make_pdf(tmp_path / "o.pdf", ["A completely different study of plants", "Jones"], pages=12)
    check = _check(tmp_path, other)
    assert not check.ok and "title" in check.reason
    wrong_author = make_pdf(tmp_path / "w.pdf", ["Autonomy support and need frustration in adolescent sport",
                                                 "Someone Else"], pages=12)
    check = _check(tmp_path, wrong_author)
    assert not check.ok and "author" in check.reason


def test_a_scan_is_accepted_only_when_found_by_identifier(tmp_path):
    blank = make_pdf(tmp_path / "scan.pdf", [], pages=12, body=False)
    assert not _check(tmp_path, blank).ok
    assert _check(tmp_path, blank, ff.Candidate("u", "s", by_identifier=True)).ok


# --- the cascade ------------------------------------------------------------------


class FakeHttp:
    def __init__(self, pages):
        self.pages = pages
        self.fetched = []

    def fetch(self, url, max_bytes=0, referer=None):
        self.fetched.append(url)
        status, ctype, body = self.pages.get(url, (404, "text/html", b"<html>not found</html>"))
        return ff.Fetched(status, ctype, body, url)

    def fetch_unblocked(self, url, budget):
        return None


def test_cascade_skips_bad_copies_and_stops_at_the_first_good_one(tmp_path, monkeypatch):
    good = good_pdf(tmp_path)
    captcha = b"<html><title>Just a moment...</title>cf-chl</html>"
    landing = b'<html><head><meta name="citation_pdf_url" content="/paper.pdf"></head></html>'
    http = FakeHttp({
        "https://a.org/stub.pdf": (200, "application/pdf", good_pdf(tmp_path, pages=1)),
        "https://b.org/x": (403, "text/html", captcha),
        "https://c.org/landing": (200, "text/html", landing),
        "https://c.org/paper.pdf": (200, "application/pdf", good),
        "https://d.org/never.pdf": (200, "application/pdf", good),
    })

    def source(item, http_, settings, budget):
        yield ff.Candidate("https://a.org/stub.pdf", "A")
        yield ff.Candidate("https://b.org/x", "B")
        yield ff.Candidate("https://c.org/landing", "C")
        yield ff.Candidate("https://d.org/never.pdf", "D")

    monkeypatch.setattr(ff, "SOURCES", {"open-access": [source]})
    path, cand, check, attempts = ff.find_pdf_for(
        ff.ItemInfo.from_zotero(ITEM), http, ff.Settings(), ff.Budget(tmp_path / "b.json"),
        steps=["open-access"], workdir=str(tmp_path),
    )
    assert cand.source == "C" and check.ok
    assert [a.outcome for a in attempts][:2] == ["only 1 of about 12 pages (preview or cover page)",
                                                "captcha or bot check (not solved)"]
    assert "https://d.org/never.pdf" not in http.fetched


def test_paid_steps_need_a_key_and_respect_the_budget(tmp_path):
    item = ff.ItemInfo.from_zotero(ITEM)
    budget = ff.Budget(tmp_path / "b.json")
    assert list(ff.src_scholar(item, None, ff.Settings(), budget)) == []
    assert list(ff.src_web(item, None, ff.Settings(), budget)) == []
    s = ff.Settings(keys={"tavily": "k"}, tavily_monthly=0)
    assert list(ff.src_web(item, None, s, budget)) == []
    budget.spend("serpapi", 3)
    assert budget.used("serpapi") == 3
    assert not ff.Budget(tmp_path / "b.json").allows("serpapi", 3)


def test_keys_come_from_config_client_env(home):
    cfg = home / ".config" / "zotero-mcp"
    cfg.mkdir(parents=True)
    (cfg / "config.json").write_text(
        '{"client_env": {"SERPAPI_API_KEY": "s1", "UNPAYWALL_EMAIL": "me@x.org"},'
        ' "fulltext_fetch": {"serpapi_monthly": 100}}'
    )
    s = ff.Settings.load()
    assert s.has("serpapi") and not s.has("tavily")
    assert s.email == "me@x.org" and s.serpapi_monthly == 100


def test_private_addresses_are_refused():
    http = ff.Http(ff.Settings(host_delay=0))
    got = http.fetch("http://127.0.0.1/secret.pdf")
    assert got.error == "address not allowed"


# --- the run --------------------------------------------------------------------


class FakeBackend:
    def __init__(self, items, children):
        self.items, self.children = items, children

    def list_items(self, item_type=None, limit=100):
        return list(self.items.values())

    def get_items(self, keys):
        return {k: self.items[k] for k in keys if k in self.items}

    def collection_items(self, key):
        return list(self.items.values())

    def get_children(self, keys, item_type=None):
        return {k: self.children.get(k, []) for k in keys}


class FakeWriter:
    def __init__(self):
        self.attached, self.tags = [], []

    def attach_pdf(self, item, path, title, note):
        assert Path(path).exists() and path.endswith(".pdf")
        self.attached.append((item.key, Path(path).name, title, note))
        return "ATT00001"

    def set_tags(self, key, add=(), remove=()):
        self.tags.append((key, list(add), list(remove)))


def _backend():
    other = {"key": "NOPE0001", "data": {"key": "NOPE0001", "itemType": "journalArticle",
                                         "title": "A paper nobody has", "creators": [], "tags": []}}
    has_pdf = {"key": "HAVE0001", "data": {"key": "HAVE0001", "itemType": "journalArticle",
                                           "title": "Already here", "tags": []}}
    note = {"key": "NOTE0001", "data": {"key": "NOTE0001", "itemType": "note", "tags": []}}
    return FakeBackend(
        {"ABCD1234": ITEM, "NOPE0001": other, "HAVE0001": has_pdf, "NOTE0001": note},
        {"HAVE0001": [{"data": {"itemType": "attachment", "contentType": "application/pdf",
                                "linkMode": "imported_file"}}],
         "ABCD1234": [{"data": {"itemType": "attachment", "linkMode": "linked_url",
                                "contentType": "application/pdf"}}]},
    )


def test_run_attaches_tags_and_reports(tmp_path, monkeypatch):
    good = good_pdf(tmp_path)

    def source(item, http_, settings, budget):
        if item.key == "ABCD1234":
            yield ff.Candidate("https://repo.org/aam.pdf", "Unpaywall (repository)", "accepted", by_identifier=True)

    monkeypatch.setattr(ff, "SOURCES", {"open-access": [source]})
    writer = FakeWriter()
    report = ff.run(
        steps=["open-access"], log=lambda m: None, settings=ff.Settings(host_delay=0),
        http=FakeHttp({"https://repo.org/aam.pdf": (200, "application/pdf", good)}),
        writer_factory=lambda: writer, backend=_backend(),
    )
    statuses = {r.key: r.status for r in report.results}
    assert statuses == {"ABCD1234": "attached", "NOPE0001": "not found"}
    (key, filename, title, note), = writer.attached
    assert key == "ABCD1234"
    assert filename == "Van der Kaap 2021 - Autonomy support and need frustration in adolescent sport.pdf"
    # The source's own label wins: accepted manuscripts often print the DOI too.
    assert title == "Full Text PDF (accepted manuscript)"
    assert "Unpaywall (repository)" in note
    assert ("ABCD1234", [ff.TAG_FETCHED, "fulltext/accepted-manuscript"], [ff.TAG_NOT_FOUND]) in writer.tags
    assert ("NOPE0001", [ff.TAG_NOT_FOUND], []) in writer.tags
    assert Path(report.report_path).exists()
    assert "attached" in report.markdown()

    # A second run leaves the not-found item alone until --retry.
    items, _ = ff.select_items(backend=_backend())
    assert [i.key for i in items] == ["ABCD1234", "NOPE0001"]  # tags live in Zotero, not the fake
    b = _backend()
    b.items["NOPE0001"]["data"]["tags"] = [{"tag": ff.TAG_NOT_FOUND}]
    assert [i.key for i in ff.select_items(backend=b)[0]] == ["ABCD1234"]
    assert [i.key for i in ff.select_items(backend=b, retry=True)[0]] == ["ABCD1234", "NOPE0001"]


def test_dry_run_and_save_dir_write_nothing_to_zotero(tmp_path, monkeypatch):
    good = good_pdf(tmp_path)

    def source(item, http_, settings, budget):
        yield ff.Candidate("https://repo.org/p.pdf", "Repo", by_identifier=True)

    monkeypatch.setattr(ff, "SOURCES", {"open-access": [source]})

    def no_writer():
        raise AssertionError("must not write")

    http = FakeHttp({"https://repo.org/p.pdf": (200, "application/pdf", good)})
    report = ff.run(keys=["ABCD1234"], dry_run=True, steps=["open-access"], log=lambda m: None,
                    settings=ff.Settings(host_delay=0), http=http, writer_factory=no_writer, backend=_backend())
    assert [r.status for r in report.results] == ["found"]
    out = tmp_path / "saved"
    report = ff.run(keys=["ABCD1234", "HAVE0001"], save_dir=str(out), steps=["open-access"], log=lambda m: None,
                    settings=ff.Settings(host_delay=0), http=http, writer_factory=no_writer, backend=_backend())
    assert {r.key: r.status for r in report.results} == {"HAVE0001": "skipped", "ABCD1234": "found"}
    assert len(list(out.glob("ABCD1234 - *.pdf"))) == 1


def test_keys_file_wins_over_config_and_loses_to_the_environment(home, monkeypatch):
    cfg = home / ".config" / "zotero-mcp"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.json").write_text('{"client_env": {"SERPAPI_API_KEY": "old", "TAVILY_API_KEY": "t0"}}')
    (cfg / "keys.env").write_text(
        "﻿# my keys\nSERPAPI_API_KEY = new\nCORE_API_KEY=\"c1\"\n\nUNPAYWALL_EMAIL=me@ugent.be\n",
        encoding="utf-8",
    )
    s = ff.Settings.load()
    assert s.keys["serpapi"] == "new" and s.keys["tavily"] == "t0" and s.keys["core"] == "c1"
    assert s.email == "me@ugent.be"
    monkeypatch.setenv("SERPAPI_API_KEY", "env")
    assert ff.Settings.load().keys["serpapi"] == "env"


# --- new sources and the browser step ---------------------------------------------


def test_blocked_links_go_to_the_browser_step(tmp_path, monkeypatch):
    """A link that refused a plain download is retried once through the browser."""
    from zotero_mcp import fulltext_browser as fb

    good = good_pdf(tmp_path)
    blocked_page = b"<html><title>Just a moment...</title>cf-chl</html>"
    http = ff.Http(ff.Settings(host_delay=0))
    http.fetch = lambda url, max_bytes=0, referer=None: ff.Fetched(403, "text/html", blocked_page, url)
    http.fetch_unblocked = lambda url, budget: None

    class FakeSession:
        opened = []

        def start(self):
            return self

        def researchgate_allowed(self):
            return None

        def get_pdf(self, url, referer=None):
            raise AssertionError("ResearchGate is never fetched without the page")

        def goto(self, url):
            self.opened.append(url)
            return True

        def pdf_from_page(self):
            return ff.Fetched(200, "application/pdf", good, self.opened[-1])

        def close(self):
            pass

    http.browser = FakeSession()

    def plain(item, http_, settings, budget):
        yield ff.Candidate("https://www.researchgate.net/publication/1_X", "web search")

    monkeypatch.setattr(ff, "SOURCES", {"web": [plain], "browser": [fb.src_browser]})
    path, cand, check, attempts = ff.find_pdf_for(
        ff.ItemInfo.from_zotero(ITEM), http, ff.Settings(host_delay=0), ff.Budget(tmp_path / "b.json"),
        steps=["web", "browser"], workdir=str(tmp_path),
    )
    # No plain request to ResearchGate: repeated ones get the network flagged.
    assert attempts[0].outcome == "left for the browser step (site refuses plain downloads)"
    assert cand.source == "web search, in your browser" and check.ok
    assert FakeSession.opened == ["https://www.researchgate.net/publication/1_X"]


def test_browser_failure_is_reported_not_raised(tmp_path, monkeypatch):
    from zotero_mcp import fulltext_browser as fb

    def broken():
        raise fb.BrowserUnavailable("the browser step needs Playwright")

    cand = ff.Candidate("https://x.org/p", "publisher, in your browser", fetcher=fb._fetcher(broken))
    path, check, outcome = ff._try_candidate(
        ff.ItemInfo.from_zotero(ITEM), cand, None, ff.Settings(), ff.Budget(tmp_path / "b.json"), str(tmp_path)
    )
    assert path is None and outcome == "browser: the browser step needs Playwright"


def test_browser_step_is_not_in_the_default_run():
    assert "browser" in ff.STEPS and "browser" not in ff.DEFAULT_STEPS


def test_background_run_reports_through_status(home, monkeypatch):
    """The tool's fetch runs in its own process and reports through files."""
    import time

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ZOTERO_NO_CLAUDE", "1")
    monkeypatch.setenv("ZOTERO_LOCAL", "true")
    monkeypatch.setenv("ZOTERO_DB_PATH", str(home / "missing.sqlite"))
    run_id = ff.start_background_run({"keys": ["ABCD1234"], "steps": ["open-access"]})
    deadline = time.monotonic() + 90
    finished, text = False, ""
    while time.monotonic() < deadline and not finished:
        time.sleep(1)
        finished, text = ff.background_status(run_id)
    assert finished, text
    assert text  # a report, or the error it ran into: never silence

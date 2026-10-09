"""The full-text fetcher: matching, PDF checks, the step cascade and the run."""

import json
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

    def trash_child(self, parent, attachment_key):
        self.trashed = getattr(self, "trashed", []) + [(parent, attachment_key)]
        return True

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
    assert ("ABCD1234", [ff.TAG_FETCHED, "fulltext/accepted-manuscript"],
            [ff.TAG_NOT_FOUND, ff.TAG_CHECK_PDF, "fulltext/preprint", ff.TAG_PROOF]) \
        in writer.tags
    assert ("NOPE0001", [ff.TAG_NOT_FOUND], []) in writer.tags
    # A manuscript is the best copy for now: the published version is looked for again after the
    # retry period, not at the next run.
    entry = ff._load_state()["ABCD1234"]
    assert entry["bad_pdf"] == {"attachments": ["ATT00001"], "problem": "manuscript", "want_published": True}
    assert ff.published_searched_recently(entry, 30) and not ff.published_searched_recently(entry, 0)
    from zotero_mcp import maintenance

    assert maintenance._published_wanted_later(["ABCD1234", "NOPE0001"]) == ["ABCD1234"]
    assert Path(report.report_path).exists()
    assert "attached" in report.markdown()

    # A second run leaves the not-found item alone until --retry.
    items, _ = ff.select_items(backend=_backend())
    # ABCD1234 has its manuscript and waits for its published version; tags live in Zotero, not the fake.
    assert [i.key for i in items] == ["NOPE0001"]
    b = _backend()
    b.items["NOPE0001"]["data"]["tags"] = [{"tag": ff.TAG_NOT_FOUND}]
    assert [i.key for i in ff.select_items(backend=b)[0]] == []
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


def test_papers_are_searched_at_once_and_the_browser_comes_after(tmp_path, monkeypatch):
    good = good_pdf(tmp_path)
    order = []

    def open_access(item, http_, settings, budget):
        order.append(("open-access", item.key))
        return iter(())

    def browser(item, http_, settings, budget):
        order.append(("browser", item.key))
        if item.key == "ABCD1234":
            yield ff.Candidate("https://rg.net/a.pdf", "your browser", "published", by_identifier=True)

    monkeypatch.setattr(ff, "SOURCES", {"open-access": [open_access], "browser": [browser]})
    events = []
    writer = FakeWriter()
    report = ff.run(
        steps=["open-access", "browser"], log=lambda m: None, settings=ff.Settings(host_delay=0),
        http=FakeHttp({"https://rg.net/a.pdf": (200, "application/pdf", good)}),
        writer_factory=lambda: writer, backend=_backend(), workers=2, progress=events.append,
    )
    # Every paper is tried by the other steps first; the browser comes after, one at a time.
    assert [o[0] for o in order] == ["open-access", "open-access", "browser", "browser"]
    assert [(r.key, r.status) for r in report.results] == [("ABCD1234", "attached"), ("NOPE0001", "not found")]
    statuses = [(e["key"], e["status"]) for e in events if e["key"] == "NOPE0001"]
    assert statuses == [("NOPE0001", "waiting"), ("NOPE0001", "searching"), ("NOPE0001", "waiting for browser"),
                        ("NOPE0001", "browser"), ("NOPE0001", "not found")]
    # Not found is only tagged once, after the browser step.
    assert writer.tags.count(("NOPE0001", [ff.TAG_NOT_FOUND], [])) == 1
    assert events[-1]["status"] == "done"


def test_the_progress_window_bookkeeping():
    from zotero_mcp.fulltext_window import Progress

    p = Progress()
    p.active_runs = 1
    for key, status in [("A", "waiting"), ("B", "waiting"), ("C", "skipped"), ("A", "searching"), ("A", "attached"),
                        ("B", "not found")]:
        p.apply({"key": key, "label": key, "status": status, "detail": ""})
    assert p.browser_button() == (True, "Search 1 not found with the browser")
    assert p.summary().startswith("Searching (2 of 2 done)") and p.fraction() == 1.0
    p.browser_busy = True
    assert p.browser_button()[0] is False
    p.browser_busy, p.active_runs = False, 0
    p.apply({"key": "B", "label": "B", "status": "browser"})
    p.apply({"key": "B", "label": "B", "status": "not found"})
    assert p.not_found_for_browser() == []      # tried with the browser already: not offered again
    assert p.summary() == "Done: 1 attached, 1 not found, 1 skipped (already a PDF, or no title)."


def test_a_pdf_marked_wrong_is_replaced_and_a_manuscript_only_by_the_published_version(tmp_path, monkeypatch):
    good = good_pdf(tmp_path)
    item = json.loads(json.dumps(ITEM))
    backend = FakeBackend({"ABCD1234": item}, {"ABCD1234": [
        {"key": "BADPDF01", "data": {"key": "BADPDF01", "itemType": "attachment", "contentType": "application/pdf",
                                     "linkMode": "imported_file"}}]})
    version = {"v": "accepted"}

    def source(item_, http_, settings, budget):
        yield ff.Candidate("https://repo.org/x.pdf", "Unpaywall (repository)", version["v"], by_identifier=True)

    monkeypatch.setattr(ff, "SOURCES", {"open-access": [source]})
    http = FakeHttp({"https://repo.org/x.pdf": (200, "application/pdf", good)})
    # Without a mark, the item has a PDF and is left alone.
    items, skipped = ff.select_items(keys=["ABCD1234"], backend=backend)
    assert not items and skipped[0].reason == "already has a PDF or EPUB"
    # The audit found the PDF is the manuscript: only the published version replaces it.
    ff.mark_bad_pdf("ABCD1234", "BADPDF01", "manuscript", want_published=True)
    writer = FakeWriter()
    report = ff.run(steps=["open-access"], log=lambda m: None, settings=ff.Settings(host_delay=0), http=http,
                    writer_factory=lambda: writer, backend=backend, workers=1)
    assert report.results[0].status == "not found" and not writer.attached
    assert not [t for t in writer.tags if ff.TAG_NOT_FOUND in t[1]]      # it keeps its manuscript, not "not found"
    assert ff.bad_pdf("ABCD1234")["attachments"] == ["BADPDF01"]
    version["v"] = "published"
    # Searched in vain just now: a whole-library run waits; --retry (or naming the paper) searches again.
    assert ff.select_items(backend=backend)[0] == []
    report = ff.run(steps=["open-access"], log=lambda m: None, settings=ff.Settings(host_delay=0), http=http,
                    writer_factory=lambda: writer, backend=backend, workers=1, retry=True)
    assert report.results[0].status == "attached" and writer.trashed == [("ABCD1234", "BADPDF01")]
    assert ff.bad_pdf("ABCD1234") == {}


# --- bot checks only the user's own browser passes ------------------------------


def test_links_blocked_in_a_normal_run_are_remembered_for_a_browser_only_run(tmp_path, monkeypatch):
    captcha = b"<html><title>Just a moment...</title>cf-chl</html>"

    def web(item, http_, settings, budget):
        if item.key == "ABCD1234":
            yield ff.Candidate("https://pub.org/doi/pdf/1", "publisher", "published", by_identifier=True)

    monkeypatch.setattr(ff, "SOURCES", {"web": [web]})
    http = FakeHttp({"https://pub.org/doi/pdf/1": (403, "text/html", captcha)})
    http.blocked = {}
    ff.run(keys=["ABCD1234"], steps=["web"], log=lambda m: None, settings=ff.Settings(host_delay=0), http=http,
           writer_factory=FakeWriter, backend=_backend(), workers=1)
    remembered = ff.remembered_blocked("ABCD1234")
    assert [c.url for c in remembered] == ["https://pub.org/doi/pdf/1"] and remembered[0].by_identifier

    # A browser-only run (the "with browser" action, the window's button) tries each paper once.
    calls = []

    def browser(item, http_, settings, budget):
        calls.append(item.key)
        return iter(())

    monkeypatch.setattr(ff, "SOURCES", {"browser": [browser]})
    report = ff.run(keys=["ABCD1234", "NOPE0001"], steps=["browser"], log=lambda m: None,
                    settings=ff.Settings(host_delay=0), http=FakeHttp({}), writer_factory=FakeWriter,
                    backend=_backend())
    assert calls == ["ABCD1234", "NOPE0001"]
    assert [r.status for r in report.results] == ["not found", "not found"]


def test_a_bot_check_hands_the_paper_to_the_users_own_browser(tmp_path, monkeypatch):
    from zotero_mcp import fulltext_browser as fb

    link = "https://www.tandfonline.com/doi/pdf/10.1080/x"

    def refused():
        raise fb.BrowserUnavailable(f"{fb.OWN_BROWSER}: {link}")

    def browser(item, http_, settings, budget):
        if item.key == "ABCD1234":
            yield ff.Candidate(link, "publisher, in your browser", "published", by_identifier=True,
                               fetcher=fb._fetcher(refused))

    monkeypatch.setattr(ff, "SOURCES", {"browser": [browser]})
    writer, events = FakeWriter(), []
    report = ff.run(keys=["ABCD1234", "NOPE0001"], steps=["browser"], log=lambda m: None,
                    settings=ff.Settings(host_delay=0), http=FakeHttp({}), writer_factory=lambda: writer,
                    backend=_backend(), progress=events.append)
    first, second = report.results
    assert (first.status, first.url) == ("needs your browser", link)
    assert second.status == "not found"
    # Only the paper nobody has is tagged not found; the other one is reachable, just not by a script.
    assert [t[0] for t in writer.tags] == ["NOPE0001"]
    assert {"key": "ABCD1234", "label": first.label, "status": "needs your browser", "detail": link} in events
    assert f"open {link} in your own browser" in report.markdown()


def test_the_fetchers_chrome_does_not_reopen_a_site_whose_bot_check_refused_it():
    from zotero_mcp import fulltext_browser as fb

    session = fb.BrowserSession(ff.Settings())
    session.robot_hosts.add("tandfonline.com")
    with pytest.raises(fb.BrowserUnavailable, match=fb.OWN_BROWSER):
        session.goto("https://www.tandfonline.com/doi/full/10.1080/x")


def test_pdfs_saved_to_downloads_are_matched_and_attached(tmp_path):
    good = good_pdf(tmp_path)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    make_pdf(downloads / "unrelated.pdf", ["A completely different paper", "about something else"])
    (downloads / "17408989.2026.pdf").write_bytes(good)
    writer, found, rounds = FakeWriter(), [], []

    def stop():
        rounds.append(1)
        return len(rounds) > 4

    got = ff.watch_downloads(["ABCD1234", "NOPE0001"], folder=downloads, since=0, timeout=60, stop=stop,
                             on_found=lambda key, detail: found.append(key), log=lambda m: None,
                             backend=_backend(), writer_factory=lambda: writer, sleep=lambda s: None)
    assert got == ["ABCD1234"] and found == ["ABCD1234"]
    assert len(writer.attached) == 1 and "your own browser" in writer.attached[0][3]
    assert len(writer.tags) == 1 and writer.tags[0][0] == "ABCD1234" and ff.TAG_FETCHED in writer.tags[0][1]
    assert writer.tags[0][2][:2] == [ff.TAG_NOT_FOUND, ff.TAG_CHECK_PDF] and ff.TAG_PROOF in writer.tags[0][2]


def test_the_window_offers_papers_behind_a_bot_check_to_the_users_own_browser():
    from zotero_mcp.fulltext_window import Progress

    p = Progress()
    p.apply({"key": "A", "label": "A", "status": "needs your browser", "detail": "https://pub.org/a"})
    p.apply({"key": "B", "label": "B", "status": "not found", "detail": ""})
    assert p.for_own_browser() == ["A"] and p.own_links["A"] == "https://pub.org/a"
    assert p.summary() == "Done: 0 attached, 1 not found, 1 for your own browser."
    assert [t[2] for t in p.todo()] == ["own", "browser"]
    assert p.chips() == [("ChipWarn", "⚠ 1 bot check"), ("ChipBad", "✗ 1 not found")]
    assert p.headline() == "Done · 2 papers"
    p.apply({"key": "A", "label": "A", "status": "waiting for your download", "detail": "https://pub.org/a"})
    assert p.for_own_browser() == [] and p.fraction() == 0.5
    p.apply({"key": "A", "label": "A", "status": "attached", "detail": "from your download"})
    assert p.summary() == "Done: 1 attached, 1 not found."


def test_no_second_pdf_when_zotero_attached_one_meanwhile(tmp_path, monkeypatch):
    good = good_pdf(tmp_path)

    class Late(FakeBackend):
        calls = 0

        def get_children(self, keys, item_type=None):
            Late.calls += 1
            if Late.calls == 1:
                return {k: [] for k in keys}        # at selection: no PDF yet
            return {k: [{"data": {"itemType": "attachment", "contentType": "application/pdf",
                                  "linkMode": "imported_file"}}] for k in keys}

    def source(item, http_, settings, budget):
        yield ff.Candidate("https://repo.org/x.pdf", "Unpaywall (repository)", "published", by_identifier=True)

    monkeypatch.setattr(ff, "SOURCES", {"open-access": [source]})
    writer = FakeWriter()
    report = ff.run(keys=["ABCD1234"], steps=["open-access"], log=lambda m: None, settings=ff.Settings(host_delay=0),
                    http=FakeHttp({"https://repo.org/x.pdf": (200, "application/pdf", good)}),
                    writer_factory=lambda: writer, backend=Late({"ABCD1234": ITEM}, {}), workers=1)
    assert [(r.status, r.reason) for r in report.results] == [("skipped", "a PDF arrived meanwhile")]
    assert writer.attached == []


def test_the_fetchers_chrome_comes_forward_only_while_a_page_needs_you(monkeypatch):
    from zotero_mcp import fulltext_browser as fb

    session = fb.BrowserSession(ff.Settings(), log=lambda m: None, wait_for_user=30)
    calls, pages = [], iter(["login", "login", None])
    monkeypatch.setattr(session, "set_window", lambda state: calls.append(state) or True)
    monkeypatch.setattr(session, "_blocked_reason", lambda: next(pages))
    monkeypatch.setattr(session, "_robot_check", lambda: False)
    session.page = type("Page", (), {"url": "https://login.example.org/", "bring_to_front": lambda self: None})()
    monkeypatch.setattr(fb.time, "sleep", lambda s: None)
    assert session.wait_if_blocked() is True
    assert calls == ["normal", "maximized", "minimized"]          # forward for the login, then back
    assert ff.Settings().browser_minimized is True


# --- copies found by title must be the work itself -------------------------------


def _title_item(**data):
    base = {"key": "TITLE001", "data": {"key": "TITLE001", "itemType": "journalArticle", "date": "1994",
            "title": "Motivation and strategy use in science: Individual differences and classroom effects",
            "creators": [{"creatorType": "author", "lastName": "Anderman"}], "DOI": "10.1002/tea.3660310805"}}
    base["data"].update(data)
    return ff.ItemInfo.from_zotero(base)


def test_a_copy_found_by_title_must_carry_the_title_itself_at_the_top():
    item = _title_item()
    web = ff.Candidate("https://school.org/goal_structures.pdf", "web search", None)
    review = ("Annu. Rev. Psychol. 2006. 57:487-503 doi: 10.1146/annurev.psych.56.091103.070258 CLASSROOM GOAL "
              "STRUCTURE, STUDENT MOTIVATION, AND ACADEMIC ACHIEVEMENT Judith L. Meece, Eric M. Anderman, Lynley H. "
              "Anderman. Motivation, strategy use, science classrooms, individual differences and effects ... " * 3)
    check = ff.check_pdf(item, {"pages": 17, "text": review}, web, 100_000)
    assert not check.ok and "another work" in check.reason
    # The same words without another DOI, but not printed as the title: not this work either.
    words = ("A review of motivation in science classrooms: strategy use, individual differences, classroom "
             "effects. Anderman reviews ... " * 40)
    assert not ff.check_pdf(item, {"pages": 17, "text": words}, web, 100_000).ok
    own = ("Journal of Research in Science Teaching 31(8) Motivation and Strategy Use in Science: Individual "
           "Differences and Classroom Effects Lynley Hicks Anderman and Allison J. Young ... " * 5)
    assert ff.check_pdf(item, {"pages": 17, "text": own}, web, 100_000).ok
    # The item's own DOI near the top counts as much as its title (some PDFs lose the title).
    garbled = ("Original research Anderman ... http://dx.doi.org/10.​1002/tea.​3660310805 motivation "
               "strategy use science individual differences classroom effects ... " * 5)
    assert ff.check_pdf(item, {"pages": 17, "text": garbled}, web, 100_000).ok
    # A publisher's preview is never the paper.
    preview = ff.Candidate("https://api.pageplace.de/preview/DT0400/preview-978.pdf", "web search", None)
    assert ff.check_pdf(item, {"pages": 17, "text": own}, preview, 100_000).reason == "a publisher's preview"
    # Found by DOI: the usual check (the publisher's own copy).
    assert ff.check_pdf(item, {"pages": 17, "text": words}, ff.Candidate("https://pub.org/x.pdf", "Unpaywall", None,
                                                                         by_identifier=True), 100_000).ok


def test_recheck_moves_a_wrong_pdf_found_by_title_to_the_trash(tmp_path):
    import json as _json

    ff.state_dir().mkdir(parents=True, exist_ok=True)
    rows = [{"key": "TITLE001", "status": "attached", "source": "web search", "url": "https://school.org/x.pdf",
             "attachment_key": "ATTWRONG", "time": "2026-10-09T08:00:00"},
            {"key": "TITLE001", "status": "attached", "source": "Unpaywall", "url": "https://pub.org/y.pdf",
             "attachment_key": "ATTDOI01", "time": "2026-10-09T08:00:00"}]
    (ff.state_dir() / "log.jsonl").write_text("\n".join(_json.dumps(r) for r in rows), encoding="utf-8")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF" + b"0" * 10_000)
    raw = {"key": "TITLE001", "data": {"key": "TITLE001", "itemType": "journalArticle", "date": "1994", "tags": [],
           "title": "Motivation and strategy use in science: Individual differences and classroom effects",
           "creators": [{"creatorType": "author", "lastName": "Anderman"}]}}
    writer = FakeWriter()
    text = "CLASSROOM GOAL STRUCTURE ... motivation strategy use science individual differences classroom effects Anderman " * 30
    totals = ff.recheck_attached(backend=FakeBackend({"TITLE001": raw}, {}), writer_factory=lambda: writer,
                                 paths=lambda k, a: str(pdf), probe=lambda p: {"pages": 12, "text": text},
                                 log=lambda m: None)
    assert totals == {"checked": 1, "wrong": 1, "gone": 0}          # only the copy found by title
    assert writer.trashed == [("TITLE001", "ATTWRONG")]
    assert ff._load_state()["TITLE001"]["rejected_urls"] == ["https://school.org/x.pdf"]

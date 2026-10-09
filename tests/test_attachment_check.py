"""The attachment check: is the attached PDF the item itself, and the published version?"""

from pathlib import Path

import pytest

from zotero_mcp import attachment_check as ac
from zotero_mcp import fulltext_fetch as ff
from zotero_mcp import metadata_audit as ma


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def raw(**data):
    base = dict(itemType="journalArticle", title="Augmenting playspaces to enhance the game experience: A tag game case study",
                creators=[{"creatorType": "author", "lastName": "Moreno"}], date="2016",
                DOI="10.1016/j.entcom.2016.03.001", volume="16", pages="67-79", tags=[])
    base.update(data)
    return {"key": "EDP8CXSW", "data": base}


def info_data(r):
    return ff.ItemInfo.from_zotero(r), r["data"]


OWN = ("Entertainment Computing 16 (2016) 67-79\nAugmenting playspaces to enhance the game\nexperience: A tag "
       "game case study\nAlejandro Moreno a, Robby van Delden ...\nhttps://doi.org/10.1016/j.entcom.2016.03.001")
OTHER = ("International Journal of Human-Computer Studies 129 (2019) 55-63\nAutomated and unobtrusive measurement "
         "of physical activity in an interactive playground\nAlejandro Moreno, Ronald Poppe ...\n"
         "https://doi.org/10.1016/j.ijhcs.2019.03.010")


def pdf(text, key="ATT1", pages=12):
    return {"key": key, "path": f"/x/{key}.pdf", "pages": pages, "text": text}


def test_the_items_own_pdf_passes():
    assert ac.check(*info_data(raw()), [pdf(OWN)]) is None


def test_another_work_is_found_by_its_doi_or_by_gemini_and_named_when_in_the_library():
    i, d = info_data(raw())
    p = ac.check(i, d, [pdf(OTHER)])
    assert p.kind == "another work" and p.found_doi == "10.1016/j.ijhcs.2019.03.010"
    index = {"doi": {"10.1016/j.ijhcs.2019.03.010": "OTHER001"}, "title": {}}
    reading = {"title": "Automated and unobtrusive measurement of physical activity in an interactive playground",
               "doi": "10.1016/j.ijhcs.2019.03.010"}
    p = ac.check(i, d, [pdf(OTHER)], reading=lambda: reading, index=index)
    assert p.kind == "another work" and p.other_item == "OTHER001" and "Automated and unobtrusive" in p.found_title
    # Gemini reads the item's own title where the rules missed it (odd layout): no problem.
    assert ac.check(i, d, [pdf(OTHER)], reading=lambda: {"title": i.title}) is None
    # A translated title by the same first author in the same year is the same work.
    translated = {"title": "Speelruimtes uitbreiden om de spelervaring te verbeteren", "authors": ["Alejandro Moreno"],
                  "year": "2016"}
    assert ac.check(i, d, [pdf(OTHER)], reading=lambda: translated) is None
    assert ac.check(i, d, [pdf(OTHER)], reading=lambda: dict(translated, year="2019")).kind == "another work"


def test_supplements_beside_the_right_pdf_and_unreadable_scans_are_left_alone():
    i, d = info_data(raw())
    assert ac.check(i, d, [pdf("Supplementary tables S1-S4", "SUPP"), pdf(OWN)]) is None
    assert ac.check(i, d, [pdf("   ")]) is None
    # No DOI on the pages and no Gemini: no proof, nothing reported.
    assert ac.check(i, d, [pdf("A scanned page with little text")]) is None


def test_manuscripts_preprints_and_proofs_of_published_articles():
    i, d = info_data(raw())
    assert ac.check(i, d, [pdf(OWN + "\nThis is the accepted manuscript of an article published in ...")]).kind \
        == "manuscript"
    assert ac.check(i, d, [pdf(OWN + "\nPsyArXiv preprint, not peer reviewed")]).kind == "preprint"
    assert ac.check(i, d, [pdf(OWN + "\nEntertainment Computing xx (2016) 000-000")]).kind == "proof"
    # The same text for an unpublished item (no volume or pages) is fine.
    i2, d2 = info_data(raw(volume="", pages=""))
    assert ac.check(i2, d2, [pdf(OWN + "\nThis is the accepted manuscript")]) is None


def test_a_whole_book_attached_to_a_chapter():
    r = raw(itemType="bookSection", title="Flow theory and research", pages="195-206", DOI="",
            creators=[{"creatorType": "author", "lastName": "Nakamura"}])
    i, d = info_data(r)
    text = "Oxford Handbook of Positive Psychology\nContents\n15 Flow theory and research ... 195"
    assert ac.check(i, d, [pdf(text, pages=744)]).kind == "whole book"
    assert ac.check(i, d, [pdf(text, pages=14)]) is None


class Writer:
    def __init__(self):
        self.calls = []

    def apply(self, audit, changes, tags_add=(), tags_remove=()):
        self.calls.append(("tags", audit.key, list(tags_add), list(tags_remove)))

    def add_note(self, parent, body):
        self.calls.append(("note", parent, body))

    def move_attachment(self, att, new_parent):
        self.calls.append(("move", att, new_parent))


def test_fixes_move_a_stray_pdf_to_its_own_item_or_mark_it_for_replacement():
    audit = ma.ItemAudit("EDP8CXSW", "Moreno (2016)", "journalArticle")
    audit.attachment = ac.Problem("another work", "ATT1", "/x.pdf", "its first pages show ...", other_item="OTHER001")
    w = Writer()
    assert ma._fix_attachment(w, audit, lambda m: None, pdfs=lambda key: []) is True
    assert ("move", "ATT1", "OTHER001") in w.calls and ff.bad_pdf("EDP8CXSW") == {}
    assert any(c[0] == "tags" and ff.TAG_CHECK_PDF in c[2] for c in w.calls)
    # The other item has its own PDF: the stray one stays until the right PDF replaces it.
    w = Writer()
    assert ma._fix_attachment(w, audit, lambda m: None, pdfs=lambda key: [pdf(OTHER)]) is True
    assert not [c for c in w.calls if c[0] == "move"]
    assert ff.bad_pdf("EDP8CXSW") == {"attachments": ["ATT1"], "problem": "another work", "want_published": False}
    # A manuscript is the right paper: only a version tag (an earlier run's check-pdf tag goes), no note;
    # a fetch swaps in the published version when it finds it.
    audit.attachment = ac.Problem("manuscript", "ATT2", "/m.pdf", "it says ...")
    audit.tags = {ff.TAG_CHECK_PDF}
    w = Writer()
    assert ma._fix_attachment(w, audit, lambda m: None) is True
    assert w.calls == [("tags", "EDP8CXSW", ["fulltext/accepted-manuscript"], [ff.TAG_CHECK_PDF])]
    assert ff.bad_pdf("EDP8CXSW")["want_published"] is True
    audit.tags = {"fulltext/accepted-manuscript"}
    w = Writer()
    assert ma._fix_attachment(w, audit, lambda m: None) is True and w.calls == []     # tagged before


def test_the_audit_reports_attachments_and_fetches_the_right_pdfs():
    from test_metadata_audit import CROSSREF, FakeHttp, item

    r = item()
    http = FakeHttp({"api.crossref.org": (200, CROSSREF)})
    ctx = ma.Context(http, ff.Settings(), pdf_text=lambda k: "")
    ctx.pdfs = lambda key: [pdf("Some other paper entirely\nhttps://doi.org/10.9999/other.1")]
    a = ma.audit_item(r, ctx)
    assert a.attachment.kind == "another work" and any(f.startswith("attachment: ") for f in a.flags)
    report = ma.AuditReport([a], False, "now")
    assert report.totals()["attachments"] == 1 and "## Wrong PDFs (1)" in report.markdown()


def test_maintenance_audits_fetches_and_checks_again_what_no_registry_knew(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from zotero_mcp import maintenance

    monkeypatch.setattr(maintenance, "_state_path", lambda: tmp_path / "maintenance.json")

    class Backend:
        def list_items(self, item_type=None, limit=100):
            return [{"key": "OLD", "data": {"itemType": "journalArticle", "dateAdded": "2026-10-01T10:00:00Z"}},
                    {"key": "NEW1", "data": {"itemType": "journalArticle", "dateAdded": "2026-10-09T08:00:00Z"}},
                    {"key": "NEW2", "data": {"itemType": "book", "dateAdded": "2026-10-09T09:00:00Z"}},
                    {"key": "NOTE", "data": {"itemType": "note", "dateAdded": "2026-10-09T09:00:00Z"}}]

    calls = []

    def audit_run(keys=None, collection=None, apply=True, log=None, **kw):
        calls.append(("audit", list(keys or [])))
        audits = [SimpleNamespace(key=k, flags=["no registry record found"] if k == "NEW2" else []) for k in keys]
        return SimpleNamespace(audits=audits, totals=lambda: {"items": len(audits)})

    def fetch_run(keys=None, log=None, dry_run=False, **kw):
        calls.append(("fetch", list(keys)))
        return SimpleNamespace(results=[SimpleNamespace(key="NEW2", status="attached")])

    logs = []
    first = maintenance.run(new=True, backend=Backend(), audit_run=audit_run, fetch_run=fetch_run, log=logs.append)
    assert first == {"items": 0} and not calls and "First run" in logs[0]
    out = maintenance.run(since="2026-10-08T00:00:00", backend=Backend(), audit_run=audit_run,
                          fetch_run=fetch_run, log=logs.append)
    assert calls == [("audit", ["NEW1", "NEW2"]), ("fetch", ["NEW1", "NEW2"]), ("audit", ["NEW2"])]
    assert out["items"] == 2 and out["fetched"] == 1


def test_audit_events_say_what_was_found_or_done():
    a = ma.ItemAudit("K1", "Moreno (2016)", "journalArticle")
    assert ma.audit_event(a, applied=True)["status"] == "ok"
    a.changes = [ma.Change("issue", "", "1", "fill", ["Crossref"]), ma.Change("pages", "1", "67-79", "correct",
                                                                               ["Crossref", "OpenAlex"])]
    ev = ma.audit_event(a, applied=True)
    assert (ev["status"], ev["detail"], ev["phase"]) == ("updated", "1 filled, 1 corrected", "metadata")
    assert ma.audit_event(a, applied=False)["detail"] == "1 to fill, 1 to correct"
    a.changes.append(ma.Change("volume", "15", "16", "propose", ["OpenAlex"]))
    assert ma.audit_event(a, applied=True)["status"] == "review"
    a.attachment = ac.Problem("another work", "ATT2", "/m.pdf", "its first pages show ...")
    ev = ma.audit_event(a, applied=True)
    assert ev["status"] == "wrong pdf" and ev["detail"].endswith("the PDF is another paper")
    c = ma.ItemAudit("K3", "Y (2021)", "journalArticle", attachment=ac.Problem("manuscript", "A", "/p", "it says"))
    assert (ma.audit_event(c, applied=True)["status"], ma.audit_event(c, applied=True)["detail"]) == \
        ("other version", "accepted manuscript")
    b = ma.ItemAudit("K2", "X (2020)", "journalArticle", flags=["no registry record found"])
    assert ma.audit_event(b, applied=True) == {"key": "K2", "label": "X (2020)", "phase": "metadata",
                                               "status": "no record", "detail": "no registry knows it",
                                               "changed": False}


def test_maintenance_reports_its_steps_and_can_leave_out_the_pdfs(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from zotero_mcp import maintenance

    monkeypatch.setattr(maintenance, "_state_path", lambda: tmp_path / "maintenance.json")
    events, fetched = [], []

    def audit_run(keys=None, progress=None, **kw):
        for k in keys:
            progress({"key": k, "label": k, "phase": "metadata", "status": "ok", "detail": "OK"})
        return SimpleNamespace(audits=[SimpleNamespace(key=k, flags=[]) for k in keys], totals=lambda: {},
                               report_path="/r/audit.md")

    def fetch_run(keys=None, progress=None, **kw):
        fetched.append(keys)
        return SimpleNamespace(results=[])

    maintenance.run(keys=["A", "B"], backend=object(), audit_run=audit_run, fetch_run=fetch_run,
                    progress=events.append, log=lambda m: None)
    stages = [e["detail"] for e in events if e["status"] == "stage"]
    assert stages == ["Metadata", "PDFs", "Metadata again"] and fetched == [["A", "B"]]
    assert {"key": "", "status": "done", "detail": "/r/audit.md"} in events

    events.clear()
    fetched.clear()
    maintenance.run(keys=["A"], fetch=False, backend=object(), audit_run=audit_run, fetch_run=fetch_run,
                    progress=events.append, log=lambda m: None)
    assert [e["detail"] for e in events if e["status"] == "stage"] == ["Metadata"] and fetched == []

    cfg = tmp_path / "config.json"
    cfg.write_text('{"maintenance": {"new_items": true, "fetch": false}}', encoding="utf-8")
    assert maintenance.config_new_items(cfg) and not maintenance.config_fetch(cfg)
    cfg.write_text('{"maintenance": {"new_items": true}}', encoding="utf-8")
    assert maintenance.config_fetch(cfg)


def test_the_check_and_complete_window_follows_the_steps():
    from zotero_mcp.fulltext_window import Progress

    p = Progress(["Metadata", "PDFs", "Metadata again"])
    p.active_runs = 1
    p.apply({"key": "", "status": "stage", "detail": "Metadata", "index": 0})
    for k in ("A", "B"):
        p.apply({"key": k, "label": k, "phase": "metadata", "status": "waiting", "detail": ""})
    p.apply({"key": "A", "label": "A", "phase": "metadata", "status": "updated", "detail": "2 filled",
             "changed": True})
    assert p.todo() == []
    assert p.headline() == "Checking metadata · 1 of 2"
    assert abs(p.fraction() - 1 / 6) < 1e-9
    assert p.meta_text("A") == "✎ 2 filled" and p.meta_text("B") == "Waiting" and p.fetch_text("A") == ""
    p.apply({"key": "B", "label": "B", "phase": "metadata", "status": "review", "detail": "1 to review"})

    p.apply({"key": "", "status": "stage", "detail": "PDFs", "index": 1})
    p.apply({"key": "A", "label": "A", "status": "skipped", "detail": "already has a PDF or EPUB"})
    p.apply({"key": "B", "label": "B", "status": "waiting", "detail": ""})
    p.apply({"key": "B", "label": "B", "status": "attached", "detail": "Unpaywall, published version"})
    assert p.headline() == "Fetching PDFs · 1 of 1"
    assert p.fetch_text("A") == "– Has a PDF" and p.fetch_text("B") == "✓ Attached · Unpaywall"
    assert p.tone("A") == "ok" and p.tone("B") == "warn"       # attached, but a proposal to review
    assert p.chips() == [("ChipInfo", "✎ 1 fixed"), ("ChipReview", "⚑ 1 to review"), ("ChipOk", "✓ 1 attached")]
    assert [(name, [c[2] for c in chips]) for name, chips in p.chip_groups()] == [
        ("Metadata", [["A"], ["B"]]), ("PDFs", [["B"]])]
    assert [(t[0], t[2]) for t in p.todo()] == [("review", None)]
    assert "saved search 'Metadata to review'" in p.todo()[0][3]

    # A wrong PDF that the fetcher replaced.
    p.apply({"key": "C", "label": "C", "phase": "metadata", "status": "wrong pdf", "detail": "the PDF is a proof"})
    p.apply({"key": "C", "label": "C", "status": "attached", "detail": "Crossref, published version"})
    assert p.meta_text("C") == "✓ Wrong PDF replaced" and p.tone("C") == "ok"
    p.apply({"key": "", "status": "stage", "detail": "Metadata again", "index": 2})
    p.active_runs, p.main_done = 0, True
    assert p.headline() == "Done · 3 papers" and p.fraction() == 1.0
    p.apply({"key": "D", "label": "D", "status": "not found", "detail": ""})
    assert [(t[1], t[2]) for t in p.todo()] == [("✗ 1 not found", "browser"),
                                                 ("⚑ 1 with changes to review  ⓘ", None)]


def test_a_chapter_pdf_that_opens_with_its_books_title_is_not_another_work():
    r = raw(itemType="bookSection", title="Play, novelty, and stimulus seeking", DOI="", pages="209-246",
            bookTitle="Child's play: Developmental and applied",
            creators=[{"creatorType": "author", "lastName": "Ellis"}])
    i, d = info_data(r)
    scan = "CHILD'S PLAY: Developmental and Applied\nEdited by Thomas D. Yawkey and Anthony D. Pellegrini"
    assert ac.check(i, d, [pdf(scan, pages=38)]) is None
    assert ac.check(i, d, [pdf(scan, pages=420)]).kind == "whole book"
    # Gemini reads the book's title: the same.
    assert ac.check(i, d, [pdf("A scan", pages=38)], reading=lambda: {"title": "Child's Play"}) is None


def test_report_texts_from_registries_are_cleaned_up():
    assert ma._given_case("JOSJE M.") == "Josje M." and ma._given_case("K. Ann") == "K. Ann"
    assert ma._given_case("JC") == "JC"
    assert ma._clean_abstract("IntroductionSELF-DETERMINATION THEORY (SDT) defines") == \
        "SELF-DETERMINATION THEORY (SDT) defines"
    assert ma._clean_abstract("Comunicaciones brevesRESUMEN El objetivo de este estudio") == \
        "El objetivo de este estudio"
    assert ma._clean_abstract("The purpose of this study") == "The purpose of this study"
    body = " This study examined pride and shame in children of both genders in easy and hard tasks." * 4
    title = "Differences in shame and pride as a function of children's gender and task difficulty"
    assert ma._plausible_abstract("Shame and pride differences by gender and task difficulty." + body, title)
    assert not ma._plausible_abstract("Michael Lewis, Steven M. Alessandri, " + title + "." + body, title)
    assert not ma._plausible_abstract("Preface, Irving E. Sigel Foreword, Frank A. Pedersen" + body, title)
    assert not ma._plausible_abstract("e authors argue that shame and pride" + body, title)
    assert ma._same_book("Oxford handbook of positive psychology", "The Oxford Handbook of Positive Psychology")
    assert not ma._same_book("Evolutionary Perspectives on Child Development and Education", "Evolutionary Psychology")


def test_publisher_pdfs_that_only_mention_a_manuscript_are_the_published_version():
    """Real first pages from the library: the published PDFs passed, the manuscripts are still found."""
    i, d = info_data(raw())
    tf = (OWN + "\nEuropean Early Childhood Education Research Journal ISSN: 1350-293X (Print) Journal homepage: "
          "www.tandfonline.com/journals/recr20\nTo cite this article: Ole Johan Sando ...\nThe terms on which this "
          "article has been published allow the posting of the Accepted Manuscript in a repository by the author(s) "
          "or with their consent.")
    assert ac.check(i, d, [pdf(tf)]) is None
    old = OWN + "\nJ. Child Psychol. Psychiat., Vol. 17, 1976, pp. 89 to 100.\nAccepted manuscript received 1 September 1974"
    assert ac.check(i, d, [pdf(old)]) is None
    kent = (OWN + "\nKent Academic Repository ... Author Accepted Manuscripts If this document is identified as the "
            "Author Accepted Manuscript it is the version after peer review but before type setting.")
    assert ac.check(i, d, [pdf(kent)]) is None
    aam = OWN + "\nThis is an Author's Accepted Manuscript of: Aggerholm, K. (2018). Competition in Physical Education."
    assert ac.check(i, d, [pdf(aam)]).kind == "manuscript"
    tf_aam = (OWN + "\nThis is a peer-reviewed, post-print (final draft post-refereeing) version. This is an Accepted "
              "Manuscript of an article published by Taylor & Francis. To cite this article: ...")
    assert ac.check(i, d, [pdf(tf_aam)]).kind == "manuscript"
    proof = OWN + "\nInternational Journal of Rehabilitation Research XXX: 000–000 Copyright © 2022 Wolters Kluwer"
    assert ac.check(i, d, [pdf(proof)]).kind == "proof"


def test_a_whole_book_is_the_right_paper_the_chapter_is_cut_out_or_the_item_is_tagged(monkeypatch):
    audit = ma.ItemAudit("427BV3EH", "Nakamura (2009)", "bookSection")
    audit.attachment = ac.Problem("whole book", "BOOK1", "/book.pdf", "1033 pages for a chapter on pp. 195-206")
    audit.tags = {ff.TAG_CHECK_PDF}             # an earlier run took the book for another work
    ff.mark_bad_pdf("427BV3EH", "BOOK1", "another work", want_published=False)

    class W(Writer):
        def item_pages(self, key):
            return "195-206"

        def attach_file(self, key, path, title):
            self.calls.append(("attach", key, title))

    monkeypatch.setattr(ac, "extract_chapter", lambda book, pages, out: None)
    w = W()
    assert ma._fix_attachment(w, audit, lambda m: None) is False
    assert w.calls == [("tags", "427BV3EH", [ff.TAG_WHOLE_BOOK], [ff.TAG_CHECK_PDF])]
    assert ff.bad_pdf("427BV3EH") == {}         # the book is not replaced
    monkeypatch.setattr(ac, "extract_chapter", lambda book, pages, out: (205, 216))
    audit.tags = set()
    w = W()
    ma._fix_attachment(w, audit, lambda m: None)
    assert [c[0] for c in w.calls] == ["attach", "tags", "note"] and "Chapter cut out" in w.calls[2][2]


def test_the_window_counts_the_right_paper_in_another_form_apart_from_wrong_pdfs():
    from zotero_mcp.fulltext_window import Progress

    p = Progress(["Metadata"])
    p.apply({"key": "A", "label": "A", "phase": "metadata", "status": "other version", "detail": "accepted manuscript"})
    p.apply({"key": "B", "label": "B", "phase": "metadata", "status": "wrong pdf", "detail": "the PDF is another paper"})
    assert p.chips() == [("ChipWarn", "⚠ 1 wrong PDF"), ("ChipNeutral", "◐ 1 other form")]
    assert p.meta_text("A") == "◐ Accepted manuscript" and p.tone("A") == "ok"
    assert [t[1].split("  ")[0] for t in p.todo()] == ["⚠ 1 with another paper attached"]


def test_maintenance_can_update_the_search_index_last(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from zotero_mcp import maintenance
    from zotero_mcp.fulltext_window import Progress

    monkeypatch.setattr(maintenance, "_state_path", lambda: tmp_path / "maintenance.json")
    order, events = [], []

    def audit_run(keys=None, **kw):
        order.append("audit")
        return SimpleNamespace(audits=[SimpleNamespace(key=k, flags=[]) for k in keys], totals=lambda: {})

    def fetch_run(keys=None, **kw):
        order.append("fetch")
        return SimpleNamespace(results=[])

    def index_run(log=None):
        order.append("index")
        return True

    out = maintenance.run(keys=["A"], index=True, backend=object(), audit_run=audit_run, fetch_run=fetch_run,
                          index_run=index_run, progress=events.append, log=lambda m: None)
    assert order == ["audit", "fetch", "index"] and out["indexed"] is True
    assert [e["detail"] for e in events if e["status"] == "stage"][-1] == "Search index"
    p = Progress(maintenance.stages(True, True))
    p.active_runs = 1
    p.apply({"key": "", "status": "stage", "detail": "Search index", "index": 3})
    assert p.headline() == "Updating the search index…"
    order.clear()
    maintenance.run(keys=["A"], index=True, apply=False, backend=object(), audit_run=audit_run,
                    fetch_run=fetch_run, index_run=index_run, log=lambda m: None)
    assert "index" not in order                  # a report-only run changes nothing, the index neither


class Library:
    """A backend with dateModified, for the monthly check."""

    def __init__(self, items):
        self.items = items

    def list_items(self, item_type=None, limit=100):
        return list(self.items.values())

    def get_items(self, keys):
        return {k: self.items[k] for k in keys if k in self.items}

    children: dict = {}

    def get_children(self, keys, item_type=None):
        return {k: self.children.get(k, []) for k in keys}


def _paper(key, modified, doi="", tags=()):
    return {"key": key, "data": {"key": key, "itemType": "journalArticle", "title": f"Paper {key}",
                                 "creators": [{"creatorType": "author", "lastName": "Smith"}], "date": "2020",
                                 "DOI": doi, "dateModified": modified, "tags": [{"tag": t} for t in tags]}}


def test_the_monthly_check_checks_changed_papers_fully_and_the_rest_for_retractions(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from zotero_mcp import maintenance

    monkeypatch.setattr(maintenance, "_state_path", lambda: tmp_path / "maintenance.json")
    lib = Library({"OLD": _paper("OLD", "2026-01-01T10:00:00Z", "10.1/old"),
                   "NODOI": _paper("NODOI", "2026-01-01T10:00:00Z"),
                   "EDITED": _paper("EDITED", "2026-01-01T10:00:00Z", "10.1/edited"),
                   "MISS": _paper("MISS", "2026-01-01T10:00:00Z", tags=[ff.TAG_NOT_FOUND]),
                   "NEW": _paper("NEW", "2026-10-09T08:00:00Z", "10.1/new")})
    maintenance.remember_checked(lib, ["OLD", "NODOI", "EDITED"])
    lib.items["EDITED"]["data"]["dateModified"] = "2026-10-01T09:00:00Z"     # you changed it since
    full, recheck = maintenance.monthly_plan(lib)
    assert sorted(full) == ["EDITED", "MISS", "NEW"] and recheck == ["OLD"]
    # A PDF added by hand changes the attachment, not the paper: still a change.
    lib.children = {"OLD": [{"data": {"itemType": "attachment", "dateModified": "2026-10-05T12:00:00Z"}}]}
    assert "OLD" in maintenance.monthly_plan(lib)[0]
    lib.children = {}

    ff._save_item_state("MISS", {"last_attempt": "2026-10-08T10:00:00", "status": "not found"})
    calls, events = {}, []

    def audit_run(keys=None, **kw):
        return SimpleNamespace(audits=[SimpleNamespace(key=k, flags=[]) for k in keys], totals=lambda: {})

    def fetch_run(keys=None, **kw):
        calls["fetch"] = list(keys)
        return SimpleNamespace(results=[])

    def retraction_run(keys, **kw):
        calls["retractions"] = list(keys)
        return {"checked": len(keys)}

    out = maintenance.monthly(backend=lib, audit_run=audit_run, fetch_run=fetch_run, index_run=lambda log: True,
                              retraction_run=retraction_run, writer_factory=object, progress=events.append,
                              log=lambda m: None)
    assert calls["retractions"] == ["OLD"]
    assert sorted(calls["fetch"]) == ["EDITED", "NEW"]       # MISS was searched in vain yesterday
    assert [e["detail"] for e in events if e["status"] == "stage"] == maintenance.monthly_stages()
    assert out["indexed"] is True
    # Done: not due again for a month, and nothing changed means nothing to check fully.
    assert maintenance.monthly_due()[0] is False
    assert maintenance.monthly(log=lambda m: None) == {"due": False}
    assert maintenance.monthly_plan(lib)[0] == []


def test_a_large_first_check_is_spread_over_days(tmp_path, monkeypatch):
    import datetime as dt
    from types import SimpleNamespace

    from zotero_mcp import maintenance

    monkeypatch.setattr(maintenance, "_state_path", lambda: tmp_path / "maintenance.json")
    lib = Library({f"K{i}": _paper(f"K{i}", f"2026-01-0{i}T10:00:00Z") for i in range(1, 6)})
    seen = []

    def audit_run(keys=None, **kw):
        seen.append(list(keys))
        return SimpleNamespace(audits=[SimpleNamespace(key=k, flags=[]) for k in keys], totals=lambda: {})

    run = dict(backend=lib, audit_run=audit_run, fetch_run=lambda **kw: SimpleNamespace(results=[]),
               index_run=lambda log: True, retraction_run=lambda keys, **kw: {}, writer_factory=object,
               log=lambda m: None, max_full=2)
    maintenance.monthly(**run)
    assert seen[0] == ["K1", "K2"]                          # oldest changes first
    state = maintenance._load()
    assert state["monthly_backlog"] == 3 and maintenance.monthly_due()[0] is False     # not the same day
    state["last_monthly"] = (dt.datetime.now() - dt.timedelta(days=1, minutes=1)).isoformat(timespec="seconds")
    maintenance._save(state)
    assert maintenance.monthly_due() == (True, "3 papers left from the last run")
    maintenance.monthly(**run)
    assert seen[1] == ["K3", "K4"]


def test_new_retractions_are_tagged_once_and_old_corrections_stay_quiet(tmp_path):
    from test_metadata_audit import FakeHttp

    from zotero_mcp import maintenance

    lib = Library({"R1": _paper("R1", "2026-01-01T10:00:00Z", "10.1/r1")})
    message = {"message": {"DOI": "10.1/r1", "title": ["Paper R1"], "type": "journal-article", "updated-by": [
        {"type": "retraction", "updated": {"date-parts": [[2026, 9, 1]]}, "DOI": "10.1/retraction"},
        {"type": "correction", "updated": {"date-parts": [[2015, 3, 1]]}, "DOI": "10.1/old-correction"}]}}
    http = FakeHttp({"api.crossref.org": (200, message)})

    class W:
        def __init__(self):
            self.calls = []

        def apply(self, audit, changes, tags_add=(), tags_remove=()):
            self.calls.append(("tags", list(tags_add)))

        def add_note(self, key, body):
            self.calls.append(("note", body))

    w, events = W(), []
    totals = maintenance.check_retractions(["R1"], backend=lib, http=http, settings=ff.Settings(), writer=w,
                                           progress=events.append, log=lambda m: None, sleep=lambda s: None)
    assert totals["retracted"] == 1 and w.calls[0] == ("tags", [ma.TAG_RETRACTED])
    assert "RETRACTED" in w.calls[1][1] and "old-correction" not in w.calls[1][1]
    assert any(e.get("status") == "retracted" for e in events)
    w2 = W()
    maintenance.check_retractions(["R1"], backend=lib, http=http, settings=ff.Settings(), writer=w2,
                                  log=lambda m: None, sleep=lambda s: None)
    assert w2.calls == []                                   # known since the last check


def test_the_monthly_window_counts_the_retraction_step():
    from zotero_mcp import maintenance
    from zotero_mcp.fulltext_window import Progress

    p = Progress(maintenance.monthly_stages())
    p.active_runs = 1
    p.apply({"key": "", "status": "stage", "detail": "Retractions", "index": 0})
    p.apply({"key": "", "status": "count", "done": 120, "total": 1500})
    assert p.headline() == "Checking for retractions and corrections · 120 of 1500"
    assert 0 < p.fraction() < 0.1

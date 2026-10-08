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
    # A manuscript waits for the published version; an earlier run's tag means no second note.
    audit.attachment = ac.Problem("manuscript", "ATT2", "/m.pdf", "it says ...")
    audit.tags = {ff.TAG_CHECK_PDF}
    w = Writer()
    assert ma._fix_attachment(w, audit, lambda m: None) is True and w.calls == []


def test_the_audit_reports_attachments_and_fetches_the_right_pdfs():
    from test_metadata_audit import CROSSREF, FakeHttp, item

    r = item()
    http = FakeHttp({"api.crossref.org": (200, CROSSREF)})
    ctx = ma.Context(http, ff.Settings(), pdf_text=lambda k: "")
    ctx.pdfs = lambda key: [pdf("Some other paper entirely\nhttps://doi.org/10.9999/other.1")]
    a = ma.audit_item(r, ctx)
    assert a.attachment.kind == "another work" and any(f.startswith("attachment: ") for f in a.flags)
    report = ma.AuditReport([a], False, "now")
    assert report.totals()["attachments"] == 1 and "1 attached PDF(s) to check" in report.markdown()


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

    def fetch_run(keys=None, log=None, dry_run=False):
        calls.append(("fetch", list(keys)))
        return SimpleNamespace(results=[SimpleNamespace(key="NEW2", status="attached")])

    logs = []
    first = maintenance.run(new=True, backend=Backend(), audit_run=audit_run, fetch_run=fetch_run, log=logs.append)
    assert first == {"items": 0} and not calls and "First run" in logs[0]
    out = maintenance.run(since="2026-10-08T00:00:00", backend=Backend(), audit_run=audit_run,
                          fetch_run=fetch_run, log=logs.append)
    assert calls == [("audit", ["NEW1", "NEW2"]), ("fetch", ["NEW1", "NEW2"]), ("audit", ["NEW2"])]
    assert out["items"] == 2 and out["fetched"] == 1

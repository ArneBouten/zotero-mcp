"""The metadata audit: what it fills, corrects, proposes and leaves alone."""

import json
from pathlib import Path

import pytest

from zotero_mcp import fulltext_fetch as ff
from zotero_mcp import metadata_audit as ma

CROSSREF = {"message": {
    "type": "journal-article", "title": ["Self-determination theory and the facilitation of intrinsic motivation"],
    "container-title": ["American Psychologist"], "ISSN": ["1935-990X", "0003-066X"],
    "volume": "55", "issue": "1", "page": "68-78", "publisher": "American Psychological Association (APA)",
    "issued": {"date-parts": [[2000]]}, "DOI": "10.1037/0003-066x.55.1.68",
    "author": [{"family": "Ryan", "given": "Richard M."}, {"family": "Deci", "given": "Edward L."}],
    "abstract": "<jats:p>Human beings can be proactive.</jats:p>",
}}
PUBMED_ID = {"esearchresult": {"idlist": ["11392867"]}}
EUROPEPMC = {"resultList": {"result": [{
    "title": "Self-determination theory and the facilitation of intrinsic motivation.",
    "journalInfo": {"volume": "55", "issue": "1", "yearOfPublication": 2000,
                    "journal": {"title": "The American psychologist"}},
    "pageInfo": "68-78", "authorList": {"author": [{"lastName": "Ryan"}, {"lastName": "Deci"}]},
}]}}


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def api_json(self, url, params=None, headers=None, **kw):
        self.calls.append(url)
        for part, answer in self.routes.items():
            if part in url:
                return answer
        return 404, None


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def item(**data):
    base = dict(itemType="journalArticle", title="Self-determination theory and the facilitation of intrinsic motivation",
                creators=[{"creatorType": "author", "lastName": "Ryan", "firstName": "R. M."},
                          {"creatorType": "author", "lastName": "Deci", "firstName": "Edward L."}],
                date="2000", DOI="10.1037/0003-066X.55.1.68", publicationTitle="American Psychologist",
                journalAbbreviation="", volume="55", issue="1", pages="68–78", ISSN="0003-066X",
                abstractNote="An abstract.", publisher="", tags=[], extra="")
    base.update(data)
    return {"key": "ABCD1234", "data": base}


def audit(raw, routes=None, pdf="", rejected=None):
    http = FakeHttp(routes if routes is not None else {"api.crossref.org": (200, CROSSREF)})
    ctx = ma.Context(http, ff.Settings(), pdf_text=lambda k: pdf, rejected=rejected)
    return ma.audit_item(raw, ctx), http


def kinds(a):
    return {(c.kind, c.field) for c in a.changes}


def test_a_correct_item_needs_nothing_but_first_names():
    a, _ = audit(item())
    assert kinds(a) == {("fill", "creators")}  # "R. M." -> "Richard M."; Deci already complete
    (c,) = a.changes
    assert json.loads(c.new) == ["Richard M.", None]
    assert not a.flags


def test_empty_fields_are_filled_from_the_doi_record():
    a, _ = audit(item(volume="", issue="", pages="", abstractNote="", ISSN=""))
    assert {("fill", "volume"), ("fill", "issue"), ("fill", "pages"), ("fill", "abstractNote"),
            ("fill", "ISSN")} <= kinds(a)
    abstract = next(c for c in a.changes if c.field == "abstractNote")
    assert abstract.new == "Human beings can be proactive."


def test_a_difference_is_corrected_only_with_a_second_source():
    routes = {"api.crossref.org": (200, CROSSREF)}
    a, _ = audit(item(volume="56"), routes)
    assert ("propose", "volume") in kinds(a)  # only Crossref says 55

    routes.update({"eutils.ncbi": (200, PUBMED_ID), "europepmc": (200, EUROPEPMC)})
    a, _ = audit(item(volume="56"), routes)
    fix = next(c for c in a.changes if c.field == "volume")
    assert fix.kind == "correct" and fix.sources == ["Crossref", "PubMed"]

    a, _ = audit(item(volume="56"), {"api.crossref.org": (200, CROSSREF)}, pdf="American Psychologist, 55(1), 68-78")
    assert next(c for c in a.changes if c.field == "volume").sources == ["Crossref", "the item's PDF"]


def test_when_the_second_source_agrees_with_the_user_nothing_changes():
    pubmed = json.loads(json.dumps(EUROPEPMC))
    pubmed["resultList"]["result"][0]["journalInfo"]["volume"] = "56"
    a, _ = audit(item(volume="56"), {"api.crossref.org": (200, CROSSREF), "eutils.ncbi": (200, PUBMED_ID),
                                     "europepmc": (200, pubmed)})
    assert "volume" not in {c.field for c in a.changes}
    assert any("agrees with yours" in f for f in a.flags)


def test_titles_and_authors_are_only_proposed_and_case_is_ignored():
    a, _ = audit(item(title="SELF-DETERMINATION THEORY AND THE FACILITATION OF INTRINSIC MOTIVATION"))
    assert "title" not in {c.field for c in a.changes}
    a, _ = audit(item(title="Self-determination and the growth of motivation"))
    assert ("propose", "title") in kinds(a)
    swapped = item(creators=[{"creatorType": "author", "lastName": "Deci", "firstName": "E."},
                             {"creatorType": "author", "lastName": "Ryan", "firstName": "R."}])
    a, _ = audit(swapped)
    prop = next(c for c in a.changes if c.field == "creators")
    assert prop.kind == "propose" and prop.why == "the author order differs"


def test_abbreviated_journal_names_are_corrected_and_the_abbreviation_kept(tmp_path):
    assert ma.looks_abbreviated("Am. Psychol.", "American Psychologist")
    assert ma.looks_abbreviated("J Exp Child Psychol", "Journal of Experimental Child Psychology")
    routes = {"api.crossref.org": (200, CROSSREF), "eutils.ncbi": (200, PUBMED_ID), "europepmc": (200, EUROPEPMC)}
    a, _ = audit(item(publicationTitle="Am. Psychol."), routes)
    fix = next(c for c in a.changes if c.field == "publicationTitle")
    assert fix.kind == "correct"
    data = item(publicationTitle="Am. Psychol.")["data"]
    ma._set_field(data, fix)
    assert data["publicationTitle"] == "American Psychologist" and data["journalAbbreviation"] == "Am. Psychol."


def test_rejected_proposals_are_not_made_again():
    a, _ = audit(item(volume="56"), rejected={"ABCD1234": {"volume": "55"}})
    assert "volume" not in {c.field for c in a.changes}


def test_no_doi_no_match_is_reported_not_guessed():
    a, http = audit(item(DOI=""), routes={})
    assert a.flags[0] == "no registry record found"
    assert not a.changes


def test_pages_and_dois_compare_as_values():
    assert ma.norm_pages("1123-35") == "1123-1135"
    assert ma.same("pages", "68–78", "68-78")
    assert ma.same("DOI", "https://doi.org/10.1037/ABC", "10.1037/abc")
    assert ma.same("publicationTitle", "The American Psychologist", "American psychologist")


def test_a_doi_goes_to_extra_for_types_without_a_doi_field():
    data = {"itemType": "webpage", "extra": "Citation Key: x\nDOI: old"}
    ma._set_field(data, ma.Change("DOI", "", "10.1/new", "fill"))
    assert data["extra"] == "DOI: 10.1/new\nCitation Key: x"


# --- writing, review notes and decisions --------------------------------------------


class FakeWriter:
    def __init__(self, items):
        self.items = items
        self.notes = {}
        self.trashed = []
        self.saved = None

    def apply(self, audit, changes, tags_add=(), tags_remove=()):
        data = self.items[audit.key]["data"]
        for c in changes:
            ma._set_field(data, c)
        tags = [t for t in data["tags"] if t["tag"] not in set(tags_remove)]
        tags += [{"tag": t} for t in tags_add if t not in {x["tag"] for x in tags}]
        data["tags"] = tags

    def add_note(self, parent, body):
        self.notes.setdefault(parent, []).append({"key": f"N{len(self.trashed)}{len(self.notes)}",
                                                  "data": {"itemType": "note", "note": body}})

    def proposal_notes(self, parent):
        return [n for n in self.notes.get(parent, []) if ma.NOTE_MARK in n["data"]["note"]]

    def trash(self, child):
        for notes in self.notes.values():
            if child in notes:
                notes.remove(child)
        self.trashed.append(child)

    def ensure_saved_search(self):
        self.saved = "created"
        return "created"


class FakeBackend:
    def __init__(self, items):
        self.items = items

    def get_items(self, keys):
        return {k: self.items[k] for k in keys if k in self.items}

    def list_items(self, item_type=None, limit=100, tag=None):
        out = list(self.items.values())
        if tag:
            out = [i for i in out if all(t in {x["tag"] for x in i["data"]["tags"]} for t in tag)]
        return out

    def collection_items(self, key):
        return list(self.items.values())


def test_apply_writes_changes_tags_notes_and_proposals():
    raw = item(issue="", volume="56", title="A different title altogether")
    items = {"ABCD1234": raw}
    writer = FakeWriter(items)
    report = ma.run(apply=True, log=lambda m: None, settings=ff.Settings(),
                    http=FakeHttp({"api.crossref.org": (200, CROSSREF)}), backend=FakeBackend(items),
                    writer_factory=lambda: writer, pdf_text=lambda k: "", workers=1)
    data = items["ABCD1234"]["data"]
    assert data["issue"] == "1"                     # filled
    assert data["volume"] == "56"                   # only one source: proposed, not changed
    assert {t["tag"] for t in data["tags"]} == {ma.TAG_FILLED, ma.TAG_REVIEW}
    notes = writer.notes["ABCD1234"]
    assert len(notes) == 2 and "Metadata changes by zotero-mcp" in notes[0]["data"]["note"]
    proposals = ma.parse_proposals(notes[1]["data"]["note"])
    assert {c.field for c in proposals} == {"volume", "title"}
    assert writer.saved == "created"
    assert report.totals()["items_to_review"] == 1

    # Accept only the volume: it is applied, the title proposal is remembered as rejected.
    state = ma._load_state()
    ma.decide(writer, items["ABCD1234"], True, state, fields=["volume"], log=lambda m: None)
    assert data["volume"] == "55" and data["title"] == "A different title altogether"
    assert ma.TAG_REVIEW not in {t["tag"] for t in data["tags"]}
    assert state["ABCD1234"]["rejected"] == {"title": "Self-determination theory and the facilitation of intrinsic motivation"}
    assert not writer.proposal_notes("ABCD1234")


def test_reject_tag_is_processed_on_the_next_run():
    raw = item(volume="56")
    items = {"ABCD1234": raw}
    writer = FakeWriter(items)
    http = FakeHttp({"api.crossref.org": (200, CROSSREF)})
    ma.run(apply=True, log=lambda m: None, settings=ff.Settings(), http=http, backend=FakeBackend(items),
           writer_factory=lambda: writer, pdf_text=lambda k: "", workers=1)
    raw["data"]["tags"].append({"tag": ma.TAG_REJECT})
    counts = ma.process_review(writer, FakeBackend(items), log=lambda m: None)
    assert counts == {"accepted": 0, "rejected": 1}
    assert raw["data"]["volume"] == "56"
    assert not {t["tag"] for t in raw["data"]["tags"]} & {ma.TAG_REJECT, ma.TAG_REVIEW}
    # The rejected value is not proposed again.
    report = ma.run(apply=False, log=lambda m: None, settings=ff.Settings(), http=http,
                    backend=FakeBackend(items), pdf_text=lambda k: "", workers=1)
    assert "volume" not in {c.field for c in report.audits[0].changes}


# --- what the first real run showed --------------------------------------------------


def test_fields_missing_from_a_local_item_are_filled():
    # The local database leaves empty fields out of the item altogether.
    raw = item()
    for name in ("volume", "issue", "pages", "ISSN"):
        del raw["data"][name]
    a, _ = audit(raw)
    assert {("fill", "volume"), ("fill", "issue"), ("fill", "pages"), ("fill", "ISSN")} <= kinds(a)


def test_article_numbers_fill_empty_pages():
    rec = json.loads(json.dumps(CROSSREF))
    del rec["message"]["page"]
    rec["message"]["article-number"] = "e70024"
    raw = item()
    del raw["data"]["pages"]
    a, _ = audit(raw, {"api.crossref.org": (200, rec)})
    fill = next(c for c in a.changes if c.field == "pages")
    assert (fill.kind, fill.new, fill.why) == ("fill", "e70024", "article number")
    assert not any("without pages" in f for f in a.flags)


def test_registry_quirks_are_not_differences():
    rec = json.loads(json.dumps(CROSSREF))
    m = rec["message"]
    m["container-title"] = ["American Psychologist &amp; Friends"]
    m["title"] = ["Self-determination theory"]
    m["subtitle"] = ["And the facilitation of intrinsic motivation"]
    m["author"] = [{"family": "Ryan", "given": "Richard M"}, {"family": "Deci PhD", "given": "Edward L"}]
    a, _ = audit(item(publicationTitle="American Psychologist & Friends",
                      title="Self-determination theory: And the facilitation of intrinsic motivation"),
                 {"api.crossref.org": (200, rec)})
    assert not {c.field for c in a.changes} & {"title", "publicationTitle"}
    assert ("propose", "creators") not in kinds(a)
    # The registry dropped a subtitle you have: yours stays.
    m["subtitle"] = []
    a, _ = audit(item(title="Self-determination theory: And the facilitation of intrinsic motivation"),
                 {"api.crossref.org": (200, rec)})
    assert "title" not in {c.field for c in a.changes}


def test_series_notes_and_isbn_hyphens_are_ignored():
    assert ma.same("bookTitle", "Directions in Person-Environment Research and Practice",
                   "Directions in Person-Environment Research and Practice (Routledge Revivals)")
    assert ma.same("ISBN", "978-1-85168-480-9", "9781851684809")
    assert ma.same("ISBN", "1851684808 9781851684809", "1-85168-480-8")
    assert not ma.same("ISBN", "978-1-85168-480-9", "9780415000000")


def test_title_pages_and_other_works_are_not_abstracts():
    title = "Development of an electronic outdoor play device to maximise energy expenditure in children"
    stub = ("A Doctoral Thesis. Submitted in partial fulfilment of the requirements for the award of Doctor "
            "of Philosophy at Loughborough University. Development of an electronic outdoor play device to "
            "maximise energy expenditure in children, with a study of how children play outdoors and move.")
    assert not ma._plausible_abstract(stub, title)
    other = ("The present paper argues that health promotion efforts, particularly those directed at resistant "
             "and high risk workers, should be built on a careful analysis of the workplace and the many ways "
             "in which work organisation shapes what people can do about their health.")
    assert not ma._plausible_abstract(other, "Affordances of children's environments: a functional approach")
    good = ("This thesis describes the development of an electronic outdoor play device designed to maximise "
            "energy expenditure in children during break times, and tests it in four primary schools over a term, measuring heart rate and step counts against ordinary play.")
    assert ma._plausible_abstract(good, title)

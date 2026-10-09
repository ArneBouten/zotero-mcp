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
    raw = item(issue="", volume="56", title="Self-determination theory and the growth of intrinsic motivation")
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
    assert data["volume"] == "55" and data["title"] == "Self-determination theory and the growth of intrinsic motivation"
    assert ma.TAG_REVIEW not in {t["tag"] for t in data["tags"]}
    assert state["ABCD1234"]["rejected"] == {"title": "Self-determination theory and the facilitation of intrinsic motivation"}
    assert not writer.proposal_notes("ABCD1234")


def test_suggestions_are_decided_one_by_one_in_the_review_window():
    raw = item(volume="56", title="Self-determination theory and the growth of intrinsic motivation")
    items = {"ABCD1234": raw}
    writer = FakeWriter(items)
    report = ma.run(apply=True, log=lambda m: None, settings=ff.Settings(),
                    http=FakeHttp({"api.crossref.org": (200, CROSSREF)}), backend=FakeBackend(items),
                    writer_factory=lambda: writer, pdf_text=lambda k: "", workers=1)
    assert "Review suggested metadata" in writer.notes["ABCD1234"][-1]["data"]["note"]
    assert "**Review** in the progress window" in report.markdown()
    session = ma.ReviewSession(writer, FakeBackend(items))
    [(key, label, changes)] = session.load()
    assert key == "ABCD1234" and {c.field for c in changes} == {"volume", "title"}

    # Accept the volume only: the title stays waiting, in a new note, with the review tag.
    assert session.decide(key, ["volume"], []) == (1, 0, 1)
    data = raw["data"]
    assert data["volume"] == "55"
    assert ma.TAG_REVIEW in {t["tag"] for t in data["tags"]}
    assert [c.field for n in writer.proposal_notes(key) for c in ma.parse_proposals(n["data"]["note"])] == ["title"]
    # Reject the title: nothing waits any more, and the title is not suggested again.
    assert session.decide(key, [], ["title"]) == (0, 1, 0)
    assert ma.TAG_REVIEW not in {t["tag"] for t in data["tags"]} and not writer.proposal_notes(key)
    assert ma._load_state()[key]["rejected"]["title"].startswith("Self-determination theory and the facilitation")
    assert session.load() == []


def test_the_report_lists_papers_still_waiting_from_earlier_checks():
    waiting = item(title="An older paper")
    waiting["key"] = waiting["data"]["key"] = "OLD00001"
    waiting["data"]["tags"] = [{"tag": ma.TAG_REVIEW}]
    raw = item(volume="55", issue="1")
    items = {"ABCD1234": raw, "OLD00001": waiting}
    report = ma.run(keys=["ABCD1234"], apply=True, log=lambda m: None, settings=ff.Settings(),
                    http=FakeHttp({"api.crossref.org": (200, CROSSREF)}), backend=FakeBackend(items),
                    writer_factory=lambda: FakeWriter(items), pdf_text=lambda k: "", workers=1)
    text = report.markdown()
    assert [k for k, _l in report.earlier] == ["OLD00001"]
    assert "## Still to review from earlier checks (1)" in text and "[OLD00001]" in text
    assert "from earlier checks: 1 paper" in text or "and 1 paper from earlier checks" in text


def test_an_accept_tag_on_the_suggestions_note_counts_for_its_paper():
    raw = item(volume="56")
    items = {"ABCD1234": raw}
    writer = FakeWriter(items)
    ma.run(apply=True, log=lambda m: None, settings=ff.Settings(), http=FakeHttp({"api.crossref.org": (200, CROSSREF)}),
           backend=FakeBackend(items), writer_factory=lambda: writer, pdf_text=lambda k: "", workers=1)
    [note] = writer.proposal_notes("ABCD1234")
    tagged = {"key": "NOTE0001", "data": dict(note["data"], key="NOTE0001", itemType="note", parentItem="ABCD1234",
                                               tags=[{"tag": ma.TAG_ACCEPT}])}
    counts = ma.process_review(writer, FakeBackend({**items, "NOTE0001": tagged}), log=lambda m: None)
    assert counts == {"accepted": 1, "rejected": 0}
    assert raw["data"]["volume"] == "55" and not writer.proposal_notes("ABCD1234")


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


# --- what the full-library run showed -------------------------------------------------


def _rec(**changes):
    rec = json.loads(json.dumps(CROSSREF))
    rec["message"].update(changes)
    return {"api.crossref.org": (200, rec)}


def test_issn_differences_and_hyphens_are_not_proposals():
    for mine in ("0003066X", "1935-990X", "1234-5678"):
        a, _ = audit(item(ISSN=mine))
        assert "ISSN" not in {c.field for c in a.changes}


def test_page_ranges_completed_but_never_shortened():
    a, _ = audit(item(pages="68"))
    fill = next(c for c in a.changes if c.field == "pages")
    assert (fill.kind, fill.new) == ("fill", "68-78")
    a, _ = audit(item(pages="68-78"), _rec(page="68"))
    assert "pages" not in {c.field for c in a.changes}


def test_journal_clutter_is_corrected_but_subtitles_are_not_added():
    routes = {**_rec(**{"container-title": ["Sports Medicine"]}), "eutils.ncbi": (200, PUBMED_ID)}
    pm = json.loads(json.dumps(EUROPEPMC))
    pm["resultList"]["result"][0]["journalInfo"]["journal"]["title"] = "Sports medicine"
    routes["europepmc"] = (200, pm)
    a, _ = audit(item(publicationTitle="Sports Medicine (Auckland, N.Z.)"), routes)
    fix = next(c for c in a.changes if c.field == "publicationTitle")
    assert fix.kind == "correct" and fix.new == "Sports Medicine"
    a, _ = audit(item(publicationTitle="American Psychologist"),
                 _rec(**{"container-title": ["American Psychologist: The Voice of Psychology"]}))
    assert "publicationTitle" not in {c.field for c in a.changes}


def test_a_doi_alias_is_left_alone_and_large_year_gaps_are_only_proposed():
    a, _ = audit(item(DOI="10.1111/j.1467-8624.1991.tb01588.x"), _rec(DOI="10.2307/1131151"))
    assert "DOI" not in {c.field for c in a.changes}
    assert any("lists this work under" in f for f in a.flags)
    routes = {**_rec(issued={"date-parts": [[2016]]}), "eutils.ncbi": (200, PUBMED_ID)}
    pm = json.loads(json.dumps(EUROPEPMC))
    pm["resultList"]["result"][0]["journalInfo"]["yearOfPublication"] = 2016
    routes["europepmc"] = (200, pm)
    a, _ = audit(item(date="2000"), routes)
    year = next(c for c in a.changes if c.field == "year")
    assert year.kind == "propose" and "check that the DOI" in year.why


def test_a_doi_for_a_table_or_the_whole_book_compares_nothing():
    a, _ = audit(item(), _rec(title=["Self-determination theory and the facilitation of intrinsic motivation: Table 1"]))
    assert not a.changes and "table, figure" in a.flags[0]
    a, _ = audit(item(itemType="bookSection", bookTitle=""), _rec(type="book", title=["Handbook of Motivation"]))
    assert not a.changes and "whole book" in a.flags[0]


def test_chapter_prefixes_and_editions_are_not_title_differences():
    assert ma.same("title", "Writing to teach and reading to learn",
                   "Chapter IV: Writing to Teach and Reading to Learn")
    assert ma.same("title", "Multilevel analysis: Techniques and applications, Third Edition",
                   "Multilevel Analysis: Techniques and Applications")


def test_registry_mojibake_suffixes_and_initials_in_names_are_not_differences():
    names = [{"family": "RyanÃ¤", "given": "Richard M."}, {"family": "Deci Jr.", "given": "Edward L."}]
    a, _ = audit(item(creators=[{"creatorType": "author", "lastName": "Ryanä", "firstName": "Richard M."},
                                {"creatorType": "author", "lastName": "Deci", "firstName": "Edward L."}]),
                 _rec(author=names))
    assert not any(c.field == "creators" and c.kind == "propose" for c in a.changes)


def test_author_proposals_keep_your_fuller_first_names():
    names = [{"family": "Ryan", "given": "R"}, {"family": "Van Deci", "given": "E"}]
    a, _ = audit(item(creators=[{"creatorType": "author", "lastName": "Ryan", "firstName": "Richard M."},
                                {"creatorType": "author", "lastName": "Deci", "firstName": "Edward L."}]),
                 _rec(author=names))
    prop = next(c for c in a.changes if c.field == "creators")
    assert prop.new == "Ryan, Richard M.; Van Deci, E"


def test_a_doi_whose_record_has_another_title_is_flagged_not_compared():
    a, _ = audit(item(volume="99"), _rec(title=["Handbook of motivation at school"]))
    assert not a.changes and "another title" in a.flags[0]


def test_online_year_and_publisher_differences():
    routes = _rec(**{"published-print": {"date-parts": [[2001]]}, "published-online": {"date-parts": [[2000]]},
                     "publisher": "Taylor & Francis"})
    a, _ = audit(item(date="2000", publisher="Routledge"), routes)
    year = next(c for c in a.changes if c.field == "year")
    assert year.kind == "propose" and "online year" in year.why
    assert "publisher" not in {c.field for c in a.changes}


def test_decisions_you_make_consistently_are_made_for_you_later():
    learned = {"volume|no second source to confirm": {"accepted": 12, "rejected": 0}}
    http = FakeHttp({"api.crossref.org": (200, CROSSREF)})
    ctx = ma.Context(http, ff.Settings(), pdf_text=lambda k: "", learned=learned)
    a = ma.audit_item(item(volume="56"), ctx)
    fix = next(c for c in a.changes if c.field == "volume")
    assert fix.kind == "correct" and "you accepted 12 of 12" in fix.why
    learned = {"volume|no second source to confirm": {"accepted": 0, "rejected": 11}}
    ctx = ma.Context(http, ff.Settings(), pdf_text=lambda k: "", learned=learned)
    assert "volume" not in {c.field for c in ma.audit_item(item(volume="56"), ctx).changes}


def test_a_registry_with_fewer_authors_does_not_propose_deleting_yours():
    a, _ = audit(item(), _rec(author=[{"family": "Ryan", "given": "Richard M."}]))
    assert not any(c.field == "creators" and c.kind == "propose" for c in a.changes)


# --- identity, retractions, other registries, the PDF -------------------------------


def test_retractions_and_corrections_are_flagged():
    routes = _rec(**{"updated-by": [{"type": "retraction", "DOI": "10.1/notice",
                                     "updated": {"date-parts": [[2010, 2, 6]]}}]})
    a, _ = audit(item(), routes)
    assert a.retracted and a.flags[0].startswith("RETRACTED (2010-02-06")


def test_a_doi_with_other_authors_and_another_title_is_not_compared():
    routes = _rec(title=["Self-determination theory and teachers motivation at work"],
                  author=[{"family": "Smith", "given": "A."}])
    a, _ = audit(item(volume="99"), routes)
    assert not a.changes and "other authors" in a.flags[0]


def test_a_preprint_doi_proposes_the_published_one():
    routes = _rec(type="posted-content", relation={"is-preprint-of": [{"id": "10.1037/published"}]})
    a, _ = audit(item(), routes)
    (c,) = a.changes
    assert (c.field, c.kind, c.new) == ("DOI", "propose", "10.1037/published")


def test_other_doi_agencies_through_doi_org():
    csl = {"type": "article-journal", "title": "Self-determination theory and the facilitation of intrinsic motivation",
           "author": [{"family": "Ryan", "given": "Richard M."}, {"family": "Deci", "given": "Edward L."}],
           "issued": {"date-parts": [[2000]]}, "container-title": "American Psychologist", "volume": "55",
           "issue": "1", "page": "68-78"}
    routes = {"doi.org/ra/": (200, [{"DOI": "10.1400/1", "RA": "mEDRA"}]), "https://doi.org/10.": (200, csl)}
    a, _ = audit(item(DOI="10.1400/1", issue=""), routes)
    assert a.reference.startswith("mEDRA") and ("fill", "issue") in kinds(a)


def test_generic_titles_need_an_exact_match():
    info = ff.ItemInfo.from_zotero(item(title="Editorial", DOI=""))
    rec = ma.Record(source="OpenAlex", title="Editorial: new directions", authors=[("Ryan", "R.")], year="2000")
    assert not ma._matches_item(info, rec)
    info = ff.ItemInfo.from_zotero(item(DOI=""))
    rec = ma.Record(source="OpenAlex", title="Self-determination theory and the facilitation of intrinsic motivation",
                    authors=[("Ryan", "R.")], year="2000", kind="dataset")
    assert not ma._matches_item(info, rec)
    rec.kind = "book-chapter"   # a soft signal only
    assert ma._matches_item(info, rec)


def test_abstracts_in_another_language_or_boilerplate_are_refused():
    title = "Self-determination theory and the facilitation of intrinsic motivation, social development and well-being"
    spanish = ("La teoría de la autodeterminación y la motivación intrínseca: el desarrollo social y el bienestar "
               "de los estudiantes se estudian en una muestra de escuelas con cuestionarios validados. " * 2)
    assert not ma._plausible_abstract(spanish, title)
    english = ("Self-determination theory proposes that intrinsic motivation and social development depend on the "
               "satisfaction of needs for autonomy, competence and relatedness, which supports well-being. " * 2)
    assert ma._plausible_abstract(english, title)
    assert not ma._plausible_abstract("© 2020 Elsevier. All rights reserved. " * 10, title)


def test_the_pdf_read_by_gemini_confirms_a_difference():
    http = FakeHttp({"api.crossref.org": (200, CROSSREF)})
    reading = {"version": "published", "title": "Self-determination theory", "volume": "55",
               "authors": ["Richard M. Ryan", "Edward L. Deci"]}
    ctx = ma.Context(http, ff.Settings(), pdf_text=lambda k: "", pdf_read=lambda k: reading)
    fix = next(c for c in ma.audit_item(item(volume="56"), ctx).changes if c.field == "volume")
    assert fix.kind == "correct" and ma.PDF_SOURCE in fix.sources
    reading["version"] = "accepted manuscript"
    fix = next(c for c in ma.audit_item(item(volume="56"), ctx).changes if c.field == "volume")
    assert fix.kind == "propose"


def test_the_pdfs_author_list_decides_author_proposals():
    routes = {"api.crossref.org": (200, json.loads(json.dumps(CROSSREF)))}
    swapped = item(creators=[{"creatorType": "author", "lastName": "Deci", "firstName": "Edward"},
                             {"creatorType": "author", "lastName": "Ryan", "firstName": "Richard"}])
    agree = {"version": "published", "title": "x", "authors": ["Richard M. Ryan", "Edward L. Deci"]}
    ctx = ma.Context(FakeHttp(routes), ff.Settings(), pdf_text=lambda k: "", pdf_read=lambda k: agree)
    prop = next(c for c in ma.audit_item(swapped, ctx).changes if c.field == "creators")
    assert ma.PDF_SOURCE in prop.sources
    yours = {"version": "published", "title": "x", "authors": ["Edward Deci", "Richard Ryan"]}
    ctx = ma.Context(FakeHttp(routes), ff.Settings(), pdf_text=lambda k: "", pdf_read=lambda k: yours)
    a = ma.audit_item(swapped, ctx)
    assert "creators" not in {c.field for c in a.changes} and any("PDF agrees" in f for f in a.flags)


def test_items_no_registry_knows_get_proposals_from_their_pdf():
    reading = {"version": "published", "title": "Self-determination theory and the facilitation of intrinsic motivation",
               "year": "2000", "university": "Ghent University"}
    ctx = ma.Context(FakeHttp({}), ff.Settings(), pdf_text=lambda k: "", pdf_read=lambda k: reading)
    thesis = item(itemType="thesis", DOI="", date="")
    a = ma.audit_item(thesis, ctx)
    props = {(c.field, c.new) for c in a.changes if c.kind == "propose"}
    assert ("year", "2000") in props and ("university", "Ghent University") in props


def test_google_scholar_cite_is_parsed_and_only_proposed():
    apa = ("Gardner, R. C., & Smyihe, P. C. (1981). On the development of the attitude/motivation test battery. "
           "Canadian Modern Language Review, 37(3), 510-525.")
    f = ma.parse_apa(apa)
    assert (f["year"], f["journal"], f["volume"], f["issue"], f["pages"]) == (
        "1981", "Canadian Modern Language Review", "37", "3", "510-525")
    assert ma.parse_apa("Apter, M. J. (2007). Reversal theory: The dynamics of motivation. Oneworld.")["publisher"] \
        == "Oneworld"
    rec = ma.Record(source="Google Scholar (Cite)", year="1981", volume="37", issue="3", pages="510-525",
                    journal="Canadian Modern Language Review")
    ctx = ma.Context(FakeHttp({}), ff.Settings(), pdf_text=lambda k: "", pdf_read=lambda k: None,
                     scholar=lambda info: rec)
    a = ma.audit_item(item(DOI="", volume="", issue="", pages="", publicationTitle=""), ctx)
    assert {c.kind for c in a.changes} == {"propose"} and ("volume", "37") in {(c.field, c.new) for c in a.changes}


def test_title_search_asks_crossref_first_and_fills_the_doi():
    found = {"message": {"items": [CROSSREF["message"]]}}
    routes = {"api.crossref.org/works/": (200, CROSSREF), "api.crossref.org/works": (200, found)}
    a, http = audit(item(DOI=""), routes=routes)
    assert a.reference == "Crossref (by title)"
    doi = next(c for c in a.changes if c.field == "DOI")
    assert (doi.kind, doi.new) == ("fill", "10.1037/0003-066x.55.1.68")
    assert not any("openalex" in u for u in http.calls)   # OpenAlex's paid search was not needed


def test_a_registry_that_does_not_answer_is_not_a_missing_record():
    a, _ = audit(item(), routes={"api.crossref.org": (429, None)})
    assert a.flags and a.flags[0].startswith(ma.NOT_CHECKED) and "crossref.org" in a.flags[0]
    assert not any("wrong DOI" in f for f in a.flags)
    report = ma.AuditReport([a], False, "now")
    assert report.totals()["not_checked"] == 1 and report.totals()["no_source"] == 0
    assert "Not checked: 1, because a registry did not answer" in report.markdown()


def test_a_service_that_keeps_refusing_is_given_up_for_the_run():
    http = FakeHttp({"api.openalex.org": (429, None), "semanticscholar": (429, None)})
    ctx = ma.Context(http, ff.Settings(), pdf_text=lambda k: "")
    for _ in range(ma.DOWN_AFTER):
        a = ma.audit_item(item(DOI=""), ctx)
        assert a.flags[0].startswith(ma.NOT_CHECKED)
    assert "openalex.org" in ctx.down and "semanticscholar.org" in ctx.down
    http.calls.clear()
    a = ma.audit_item(item(DOI=""), ctx)
    assert a.flags[0].startswith(ma.NOT_CHECKED)
    assert not any("openalex" in u or "semanticscholar" in u for u in http.calls)


def test_publisher_differences_are_never_corrected():
    a, _ = audit(item(itemType="book", DOI="10.1/x", publisher="Association for Computing Machinery"),
                 routes=_rec(type="book", publisher="ACM"), pdf="ACM Press, New York")
    assert "publisher" not in {c.field for c in a.changes}


def test_values_that_would_make_a_field_worse_are_not_proposed():
    w = ma.worth_proposing
    assert not w("publicationTitle", "Journal of consulting and clinical psychology", "J Consult Clin Psychol")
    assert not w("publicationTitle", "Advances in neural information processing systems",
                 "Neural Information Processing Systems")
    assert w("publicationTitle", "Sensors (Basel, Switzerland)", "Sensors")
    assert w("publicationTitle", "Lancet (London, England)", "The Lancet")
    assert not w("publicationTitle", "Revista electrónica interuniversitaria de formación del profesorado",
                 "Revista Electronica Interuniversitaria de Formación del Profesorado")
    assert not w("issue", "Supplement 5", "5") and not w("volume", "27 Suppl 3", "27")
    assert not w("volume", "19", "2019") and not w("volume", "19", "19 3") and not w("issue", "3", "3_suppl")
    assert not w("pages", "1-18", "Article # 3") and w("pages", "1-18", "e30") and w("pages", "", "363-378")
    assert ma.same("volume", "4", "04") and ma.same("issue", "06", "6")


def test_a_doi_printed_on_the_pdf_or_saved_page_finds_the_record_without_a_title_search():
    http = FakeHttp({"api.crossref.org/works/10.1037": (200, CROSSREF)})
    ctx = ma.Context(http, ff.Settings(), pdf_text=lambda k: "American Psychologist 55 (2000) 68-78\n"
                                                              "https://doi.org/10.1037/0003-066X.55.1.68.")
    a = ma.audit_item(item(DOI=""), ctx)
    assert a.reference == "Crossref (by the DOI printed on its PDF)"
    doi = next(c for c in a.changes if c.field == "DOI")
    assert doi.kind == "fill" and doi.why == "the DOI printed on the item's PDF"
    assert not any("openalex" in u or "query" in u for u in http.calls)
    # From a saved web page's citation tags.
    page = ('<html><head><meta name="citation_title" content="Self-determination theory and the facilitation">'
            '<meta content="10.1037/0003-066X.55.1.68" name="citation_doi">'
            '<meta name="citation_author" content="Ryan, Richard M."><meta name="citation_firstpage" content="68">'
            '<meta name="citation_lastpage" content="78"></head>')
    meta = ma.page_meta(page)
    assert meta["doi"] == "10.1037/0003-066X.55.1.68" and meta["pages"] == "68-78"
    ctx = ma.Context(http, ff.Settings(), pdf_text=lambda k: "")
    ctx.page_meta = lambda key: meta
    a = ma.audit_item(item(DOI=""), ctx)
    assert a.reference == "Crossref (by the DOI on its saved web page)"

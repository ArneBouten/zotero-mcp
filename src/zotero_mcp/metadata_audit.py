"""Check item metadata against the registries, fill gaps and fix errors.

For every item the audit finds a reference record: Crossref (or DataCite)
by DOI, Open Library by ISBN, or, for items without either, an OpenAlex work
matched on title, first author and year. It then compares field by field:

* An **empty field** is filled from the reference record.
* A **filled field that differs** is corrected automatically only when a
  second, independent source agrees with the reference record: Europe PMC
  (PubMed), the item's own PDF, or for the journal name OpenAlex's journal
  record. OpenAlex's article data is largely copied from Crossref, so it
  does not count as a second opinion on those fields. Only volume, issue,
  pages, DOI, year, journal name and publisher are corrected this way.
* Everything else that differs (titles, author lists, unconfirmed values)
  is proposed: the item gets the tag ``metadata/review`` and a note listing
  the proposals. Tagging it ``metadata/accept`` or ``metadata/reject`` and
  running ``--process-review`` (or every audit run) applies or discards them.

Every change gets the tag ``auto-enriched`` or ``auto-corrected`` and a
note with the old value and the sources, so it can be traced and undone.
Without ``--apply`` the audit only reports.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from zotero_mcp import fulltext_fetch as ff

TAG_REVIEW = "metadata/review"
TAG_ACCEPT = "metadata/accept"
TAG_REJECT = "metadata/reject"
TAG_FILLED = "auto-enriched"
TAG_CORRECTED = "auto-corrected"
NOTE_MARK = "zotero-mcp-proposals"
SAVED_SEARCH = "Metadata to review"

#: Fields that may be corrected automatically when two independent sources
#: agree. Titles and authors never are: they only get proposals.
AUTO_CORRECT = {"volume", "issue", "pages", "DOI", "year", "publicationTitle", "publisher"}
FIELD_LABELS = {
    "title": "Title", "creators": "Authors", "year": "Year", "DOI": "DOI", "ISSN": "ISSN",
    "publicationTitle": "Journal", "volume": "Volume", "issue": "Issue", "pages": "Pages",
    "publisher": "Publisher", "place": "Place", "abstractNote": "Abstract", "bookTitle": "Book title",
    "ISBN": "ISBN",
}
AUDITED_TYPES = {"journalArticle", "conferencePaper", "preprint", "book", "bookSection", "thesis", "report"}


def meta_dir() -> Path:
    return ff.config_dir() / "metadata"


# ---------------------------------------------------------------------------
# Records from the sources
# ---------------------------------------------------------------------------


@dataclass
class Record:
    source: str
    by: str = "doi"                 # doi / isbn / title
    title: str = ""
    authors: list[tuple[str, str]] = field(default_factory=list)   # (family, given)
    year: str = ""
    date: str = ""
    journal: str = ""
    issn: list[str] = field(default_factory=list)
    volume: str = ""
    issue: str = ""
    pages: str = ""
    doi: str = ""
    publisher: str = ""
    place: str = ""
    abstract: str = ""
    kind: str = ""
    container: str = ""             # book title for a chapter
    isbn: str = ""


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _date_from_parts(obj: dict | None) -> str:
    parts = ((obj or {}).get("date-parts") or [[]])[0]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts))


def crossref(doi: str, http: ff.Http, settings: ff.Settings) -> Record | None:
    headers = {"User-Agent": f"zotero-mcp metadata-audit (mailto:{settings.email})"} if settings.email else None
    status, data = http.api_json(f"https://api.crossref.org/works/{quote(doi, safe='/')}", headers=headers)
    if status != 200 or not data:
        return None
    m = data.get("message") or {}
    # APA dates a journal article by its issue; fall back to the first publication.
    date = _date_from_parts(m.get("published-print")) or _date_from_parts(m.get("issued"))
    kind = m.get("type", "")
    container = (m.get("container-title") or [""])[0]
    return Record(
        source="Crossref",
        title=_strip_tags((m.get("title") or [""])[0]).rstrip("."),
        authors=[(a.get("family", ""), a.get("given", "")) for a in m.get("author") or [] if a.get("family")],
        year=date[:4], date=date,
        journal=container if kind in ("journal-article", "proceedings-article") else "",
        container=container if kind in ("book-chapter", "book-section", "reference-entry") else "",
        issn=list(m.get("ISSN") or []),
        volume=str(m.get("volume") or ""), issue=str(m.get("issue") or ""), pages=str(m.get("page") or ""),
        doi=(m.get("DOI") or doi).lower(), publisher=m.get("publisher") or "",
        place=m.get("publisher-location") or "", abstract=_strip_tags(m.get("abstract") or ""),
        kind=kind, isbn=(m.get("ISBN") or [""])[0],
    )


def datacite(doi: str, http: ff.Http, settings: ff.Settings) -> Record | None:
    status, data = http.api_json(f"https://api.datacite.org/dois/{quote(doi, safe='/')}")
    if status != 200 or not data:
        return None
    a = (data.get("data") or {}).get("attributes") or {}
    cont = a.get("container") or {}
    pages = "-".join(p for p in (cont.get("firstPage"), cont.get("lastPage")) if p)
    abstract = next((d.get("description", "") for d in a.get("descriptions") or []
                     if d.get("descriptionType") == "Abstract"), "")
    return Record(
        source="DataCite",
        title=((a.get("titles") or [{}])[0].get("title") or "").rstrip("."),
        authors=[(c.get("familyName", ""), c.get("givenName", "")) for c in a.get("creators") or [] if c.get("familyName")],
        year=str(a.get("publicationYear") or ""), date=str(a.get("publicationYear") or ""),
        journal=cont.get("title", "") if cont.get("type") == "Journal" else "",
        volume=str(cont.get("volume") or ""), issue=str(cont.get("issue") or ""), pages=pages,
        doi=doi.lower(), publisher=(a.get("publisher") if isinstance(a.get("publisher"), str)
                                    else (a.get("publisher") or {}).get("name", "")) or "",
        abstract=_strip_tags(abstract), kind=((a.get("types") or {}).get("resourceTypeGeneral") or ""),
    )


def _openalex_record(work: dict, by: str) -> Record:
    biblio = work.get("biblio") or {}
    source = ((work.get("primary_location") or {}).get("source") or {})
    pages = "-".join(p for p in (biblio.get("first_page"), biblio.get("last_page")) if p)
    abstract = ""
    inv = work.get("abstract_inverted_index") or {}
    if inv:
        words = sorted(((pos, w) for w, positions in inv.items() for pos in positions))
        abstract = " ".join(w for _pos, w in words)
    authors = []
    for au in work.get("authorships") or []:
        name = ((au.get("author") or {}).get("display_name") or "").strip()
        if name:
            family = name.split()[-1]
            authors.append((family, name[: -len(family)].strip()))
    return Record(
        source="OpenAlex", by=by,
        title=(work.get("title") or "").rstrip("."), authors=authors,
        year=str(work.get("publication_year") or ""), date=str(work.get("publication_date") or ""),
        journal=source.get("display_name", "") if source.get("type") == "journal" else "",
        issn=list(source.get("issn") or []),
        volume=str(biblio.get("volume") or ""), issue=str(biblio.get("issue") or ""), pages=pages,
        doi=re.sub(r"^https?://doi\.org/", "", work.get("doi") or "").lower(),
        abstract=abstract, kind=work.get("type") or "",
    )


def openalex_by_doi(doi: str, http: ff.Http, settings: ff.Settings) -> Record | None:
    status, data = http.api_json(f"https://api.openalex.org/works/doi:{quote(doi, safe='/')}",
                                 params=ff._openalex_params(settings))
    return _openalex_record(data, "doi") if status == 200 and data else None


def openalex_by_title(item: ff.ItemInfo, http: ff.Http, settings: ff.Settings) -> Record | None:
    """A work that is clearly the same: title, first author and year."""
    work = ff._openalex_work(ff.ItemInfo(**{**asdict(item), "doi": ""}), http, settings)
    if not work:
        return None
    rec = _openalex_record(work, "title")
    if ff.title_similarity(item.title, rec.title) < 0.9:
        return None
    if item.first_author and rec.authors:
        mine = ff._fold(item.first_author).split()[-1:] or [""]
        theirs = {ff._fold(f).split()[-1] for f, _g in rec.authors[:5] if ff._fold(f)}
        if mine[0] not in theirs:
            return None
    if item.year.isdigit() and rec.year.isdigit() and abs(int(item.year) - int(rec.year)) > 1:
        return None
    return rec


def europepmc(doi: str, http: ff.Http, settings: ff.Settings) -> Record | None:
    """PubMed's record of the article, through its PMID: an independent source."""
    status, data = http.api_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                                 params={"db": "pubmed", "term": f"{doi}[doi]", "retmode": "json"})
    ids = ((data or {}).get("esearchresult") or {}).get("idlist") or [] if status == 200 else []
    if len(ids) != 1:
        return None
    status, data = http.api_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                                 params={"query": f"EXT_ID:{ids[0]} AND SRC:MED", "format": "json",
                                         "resultType": "core"})
    hits = ((data or {}).get("resultList") or {}).get("result") or [] if status == 200 else []
    if not hits:
        return None
    x = hits[0]
    ji = x.get("journalInfo") or {}
    return Record(
        source="PubMed", title=(x.get("title") or "").rstrip("."),
        authors=[(a.get("lastName", ""), a.get("firstName", "")) for a in (x.get("authorList") or {}).get("author") or []
                 if a.get("lastName")],
        year=str(ji.get("yearOfPublication") or x.get("pubYear") or ""),
        journal=(ji.get("journal") or {}).get("title", ""),
        volume=str(ji.get("volume") or ""), issue=str(ji.get("issue") or ""), pages=str(x.get("pageInfo") or ""),
        doi=doi.lower(), abstract=_strip_tags(x.get("abstractText") or ""),
    )


def openlibrary(isbn: str, http: ff.Http, settings: ff.Settings) -> Record | None:
    status, data = http.api_json(f"https://openlibrary.org/isbn/{isbn}.json")
    if status != 200 or not data:
        return None
    title = data.get("title", "")
    if data.get("subtitle"):
        title += ": " + data["subtitle"]
    m = re.search(r"(\d{4})", data.get("publish_date") or "")
    return Record(
        source="Open Library", by="isbn", title=title, year=m.group(1) if m else "",
        publisher=(data.get("publishers") or [""])[0], place=(data.get("publish_places") or [""])[0],
        isbn=isbn, kind="book",
    )


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------


def norm_pages(value: str) -> str:
    v = re.sub(r"\s+", "", str(value or "")).replace("–", "-").replace("—", "-")
    m = re.fullmatch(r"(\d+)-(\d+)", v)
    if m:
        first, last = m.group(1), m.group(2)
        if len(last) < len(first) and int(last) < int(first[-len(last):] or 0) + 10 ** len(last):
            last = first[: len(first) - len(last)] + last
        return f"{int(first)}-{int(last)}"
    return v.lower()


def norm_simple(value: str) -> str:
    return re.sub(r"^(vol\.?|no\.?|issue)\s*", "", str(value or "").strip().lower())


def norm_journal(value: str) -> str:
    v = ff._fold(value or "").replace(" and ", " ")
    return re.sub(r"^the ", "", v).strip()


def norm_doi(value: str) -> str:
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", (value or "").strip(), flags=re.I).lower()


def same(field_name: str, a: str, b: str) -> bool:
    if field_name == "pages":
        return norm_pages(a) == norm_pages(b)
    if field_name in ("volume", "issue"):
        return norm_simple(a) == norm_simple(b)
    if field_name == "publicationTitle":
        return norm_journal(a) == norm_journal(b)
    if field_name == "DOI":
        return norm_doi(a) == norm_doi(b)
    if field_name in ("publisher", "place"):
        return ff._fold(a) == ff._fold(b) or ff._fold(a) in ff._fold(b) or ff._fold(b) in ff._fold(a)
    return ff._fold(str(a)) == ff._fold(str(b))


def looks_abbreviated(journal: str, full: str) -> bool:
    """"J. Exp. Child Psychol." against "Journal of Experimental Child Psychology"."""
    if "." in journal and len(journal) < len(full):
        return True
    words = [w for w in ff._fold(journal).split() if w]
    full_words = [w for w in ff._fold(full).split() if w not in ("of", "and", "the", "for", "in")]
    return bool(words) and len(words) == len(full_words) and all(f.startswith(w) for w, f in zip(words, full_words))


def record_value(rec: Record, field_name: str) -> str:
    return {
        "title": rec.title, "year": rec.year, "DOI": rec.doi, "publicationTitle": rec.journal,
        "volume": rec.volume, "issue": rec.issue, "pages": rec.pages, "publisher": rec.publisher,
        "place": rec.place, "abstractNote": rec.abstract, "bookTitle": rec.container,
        "ISSN": ", ".join(rec.issn[:2]), "ISBN": rec.isbn,
    }.get(field_name, "")


def pdf_confirms(text: str, field_name: str, value: str) -> bool:
    """Does the item's own PDF show this value on its first pages?"""
    if not text or not value:
        return False
    low = text.lower()
    if field_name == "DOI":
        return norm_doi(value) in low.replace(" ", "")
    if field_name == "pages":
        p = norm_pages(value).split("-")
        return len(p) == 2 and bool(re.search(rf"\b{p[0]}\s*[-–—]\s*{p[1]}\b", low))
    if field_name == "year":
        # A year alone is everywhere in a reference list; only a copyright
        # line or a "Volume (Year)" style citation line counts.
        return bool(re.search(rf"(©|\(c\)|copyright)\s*{re.escape(value)}\b|\b\d+\s*\({re.escape(value)}\)", low))
    if field_name == "volume":
        return bool(re.search(rf"\b(vol(ume)?\.?\s*{re.escape(value)}\b|\b{re.escape(value)}\s*\(\s*\d+)", low))
    if field_name == "issue":
        return bool(re.search(rf"(\(\s*{re.escape(value)}\s*\)|\bno\.?\s*{re.escape(value)}\b|\bissue\s*{re.escape(value)}\b)", low))
    if field_name == "publicationTitle":
        return norm_journal(value) in ff._fold(text)
    return False


# ---------------------------------------------------------------------------
# Auditing one item
# ---------------------------------------------------------------------------


@dataclass
class Change:
    field: str
    old: str
    new: str
    kind: str                       # fill / correct / propose
    sources: list[str] = field(default_factory=list)
    why: str = ""


@dataclass
class ItemAudit:
    key: str
    label: str
    item_type: str
    changes: list[Change] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    reference: str = ""
    error: str = ""

    def by_kind(self, kind: str) -> list[Change]:
        return [c for c in self.changes if c.kind == kind]


class Context:
    """What an audit run shares: HTTP, settings, the PDF reader, rejections."""

    def __init__(self, http: ff.Http, settings: ff.Settings, pdf_text: Callable[[str], str] | None = None,
                 rejected: dict | None = None):
        self.http = http
        self.settings = settings
        self.pdf_text = pdf_text or (lambda key: "")
        self.rejected = rejected or {}


def _current(data: dict, field_name: str) -> str:
    if field_name == "year":
        m = re.search(r"(\d{4})", str(data.get("date") or ""))
        return m.group(1) if m else ""
    if field_name == "DOI":
        if "DOI" in data:
            return str(data.get("DOI") or "")
        m = re.search(r"^DOI:\s*(\S+)", data.get("extra") or "", re.I | re.M)
        return m.group(1) if m else ""
    return str(data.get(field_name) or "")


def _fields_for(item_type: str) -> list[str]:
    common = ["title", "year", "DOI", "abstractNote"]
    return common + {
        "journalArticle": ["publicationTitle", "volume", "issue", "pages", "ISSN"],
        "conferencePaper": ["pages", "publisher"],
        "preprint": [],
        "book": ["publisher", "place", "ISBN"],
        "bookSection": ["bookTitle", "pages", "publisher", "place"],
        "thesis": [],
        "report": ["publisher", "place"],
    }.get(item_type, [])


def audit_item(raw: dict, ctx: Context) -> ItemAudit:
    data = raw.get("data", raw)
    info = ff.ItemInfo.from_zotero(raw)
    audit = ItemAudit(info.key, info.label, info.item_type)
    http, settings = ctx.http, ctx.settings

    ref: Record | None = None
    if info.doi:
        ref = crossref(info.doi, http, settings) or datacite(info.doi, http, settings)
        if ref is None:
            audit.flags.append(f"DOI {info.doi} not found in Crossref or DataCite (wrong DOI?)")
    elif info.isbn and info.item_type == "book":
        ref = openlibrary(info.isbn, http, settings)
    if ref is None and not info.doi:
        match = openalex_by_title(info, http, settings)
        if match and match.doi:
            ref = crossref(match.doi, http, settings) or match
            ref.by = "title"
        elif match:
            ref = match
    if ref is None:
        audit.flags.append("no registry record found")
        _type_flags(audit, data)
        return audit
    audit.reference = f"{ref.source} (by {ref.by})"

    second: list[Record] = []          # independent records, fetched only when needed
    fetched_second = False
    pdf_text: str | None = None

    def confirmations(field_name: str, value: str) -> list[str]:
        nonlocal fetched_second, pdf_text
        found = []
        if not fetched_second:
            fetched_second = True
            doi = ref.doi or info.doi
            if doi:
                pm = europepmc(doi, http, settings)
                if pm:
                    second.append(pm)
                if field_name == "publicationTitle" and ref.source != "OpenAlex":
                    oa = openalex_by_doi(doi, http, settings)
                    if oa and oa.journal:
                        second.append(Record(source="OpenAlex journal record", journal=oa.journal))
        for rec in second:
            other = record_value(rec, field_name)
            if other and same(field_name, other, value):
                found.append(rec.source)
        if pdf_text is None:
            try:
                pdf_text = ctx.pdf_text(info.key) or ""
            except Exception:
                pdf_text = ""
        if pdf_confirms(pdf_text, field_name, value):
            found.append("the item's PDF")
        return found

    for name in _fields_for(info.item_type):
        if name not in data and name not in ("year", "DOI"):
            continue
        new = record_value(ref, name)
        if not new and name == "abstractNote" and not _current(data, name).strip() and (ref.doi or info.doi):
            # Crossref often has no abstract; OpenAlex usually does.
            oa = ref if ref.source == "OpenAlex" else openalex_by_doi(ref.doi or info.doi, http, settings)
            if oa and oa.abstract:
                audit.changes.append(Change(name, "", oa.abstract, "fill", ["OpenAlex"]))
            continue
        if not new:
            continue
        old = _current(data, name)
        if name == "abstractNote":
            if not old.strip():
                audit.changes.append(Change(name, "", new, "fill", [ref.source]))
            continue
        if not old.strip():
            audit.changes.append(Change(name, "", new, "fill", [ref.source],
                                        "matched by title" if ref.by == "title" else ""))
            continue
        if name == "ISSN":
            mine = {x.strip().upper() for x in re.split(r"[,;\s]+", old) if x.strip()}
            if mine & {x.upper() for x in ref.issn}:
                continue
        if same(name, old, new):
            continue
        if name == "title":
            if ff.title_similarity(old, new) < 0.97:
                audit.changes.append(Change(name, old, new, "propose", [ref.source], "the wording differs"))
            continue
        if name not in AUTO_CORRECT or ref.by not in ("doi", "isbn"):
            audit.changes.append(Change(name, old, new, "propose", [ref.source], "only one source"))
            continue
        if name == "publicationTitle" and not looks_abbreviated(old, new) and ff.title_similarity(old, new) < 0.8:
            audit.changes.append(Change(name, old, new, "propose", [ref.source], "a different journal name"))
            continue
        agree = confirmations(name, new)
        if agree:
            audit.changes.append(Change(name, old, new, "correct", [ref.source, *agree]))
        elif any(same(name, record_value(r, name), old) for r in second if record_value(r, name)):
            audit.flags.append(f"{FIELD_LABELS.get(name, name)}: {ref.source} says {new!r}, "
                               f"but another source agrees with yours ({old!r}); left as is")
        else:
            audit.changes.append(Change(name, old, new, "propose", [ref.source], "no second source to confirm"))

    _compare_authors(audit, data, ref)
    _type_flags(audit, data, ref)
    # Proposals the user rejected before are not made again.
    rejected = ctx.rejected.get(info.key) or {}
    audit.changes = [c for c in audit.changes if not (c.kind == "propose" and rejected.get(c.field) == c.new)]
    return audit


def _compare_authors(audit: ItemAudit, data: dict, ref: Record) -> None:
    mine = [c for c in data.get("creators") or [] if c.get("creatorType", "author") == "author"]
    if not mine or not ref.authors or ref.source == "OpenAlex":
        return
    my_family = [ff._fold(c.get("lastName") or c.get("name") or "") for c in mine]
    their_family = [ff._fold(f) for f, _g in ref.authors]
    fmt = "; ".join(f"{f}, {g}".strip(", ") for f, g in ref.authors)
    if my_family != their_family:
        if set(my_family) == set(their_family):
            why = "the author order differs"
        elif len(my_family) != len(their_family):
            why = f"{len(their_family)} authors instead of {len(my_family)}"
        else:
            why = "the author names differ"
        old = "; ".join(f"{c.get('lastName') or c.get('name')}, {c.get('firstName', '')}".strip(", ") for c in mine)
        audit.changes.append(Change("creators", old, fmt, "propose", [ref.source], why))
        return
    # Same people in the same order: complete missing or initial-only first names.
    fills, givens = [], []
    for c, (_f, given) in zip(mine, ref.authors):
        have = (c.get("firstName") or "").strip()
        initials_only = len(have.replace(".", "").replace(" ", "").replace("-", "")) <= 2
        if given and (not have or (initials_only and ff._fold(given)[:1] == ff._fold(have)[:1]
                                   and len(given.replace(".", "")) > len(have.replace(".", "")))):
            fills.append(f"{c.get('lastName')}: {have or '(none)'} → {given}")
            givens.append(given)
        else:
            givens.append(None)   # left as it is
    if fills:
        audit.changes.append(Change("creators", "", json.dumps(givens), "fill", [ref.source],
                                    "first names: " + "; ".join(fills)))


def _type_flags(audit: ItemAudit, data: dict, ref: Record | None = None) -> None:
    filled = {c.field for c in audit.changes if c.kind == "fill"}
    t = audit.item_type
    if t == "bookSection" and not data.get("bookTitle") and "bookTitle" not in filled:
        audit.flags.append("chapter without a book title")
    if t == "bookSection" and not any(c.get("creatorType") == "editor" for c in data.get("creators") or []):
        audit.flags.append("chapter without editors (APA lists the book's editors)")
    if t == "thesis" and not data.get("university"):
        audit.flags.append("thesis without a university")
    if t == "journalArticle":
        for name in ("volume", "pages"):
            if not data.get(name) and name not in filled:
                audit.flags.append(f"article without {FIELD_LABELS[name].lower()}")
        if not _current(data, "DOI") and "DOI" not in filled:
            audit.flags.append("article without a DOI")
    if t in ("book", "bookSection") and not data.get("publisher") and "publisher" not in filled:
        audit.flags.append("no publisher (APA needs it for books and chapters)")


# ---------------------------------------------------------------------------
# Writing to Zotero
# ---------------------------------------------------------------------------


def _note_html(title: str, audit: ItemAudit, changes: list[Change], proposals: bool) -> str:
    rows = []
    for c in changes:
        label = FIELD_LABELS.get(c.field, c.field)
        if c.field == "creators" and c.kind == "fill":
            rows.append(f"<li><b>{label}</b>: {html.escape(c.why)} ({', '.join(c.sources)})</li>")
            continue
        old = html.escape(c.old[:300]) if c.old else "<i>empty</i>"
        new = html.escape(c.new[:300])
        why = f" — {html.escape(c.why)}" if c.why else ""
        rows.append(f"<li><b>{label}</b>: {old} → {new} ({html.escape(', '.join(c.sources))}){why}</li>")
    body = f"<h2>{html.escape(title)}</h2><ul>{''.join(rows)}</ul>"
    if proposals:
        body += ("<p>Tag this item <b>metadata/accept</b> to apply these, or <b>metadata/reject</b> to "
                 "discard them; the next metadata run does the rest.</p>")
        payload = json.dumps([asdict(c) for c in changes], ensure_ascii=False)
        body += f"<pre>{NOTE_MARK} {html.escape(payload)}</pre>"
    return body


def _set_field(data: dict, change: Change) -> None:
    name, new = change.field, change.new
    if name == "year":
        old = str(data.get("date") or "")
        data["date"] = re.sub(r"\d{4}", new, old, count=1) if re.search(r"\d{4}", old) else new
    elif name == "DOI" and "DOI" not in data:
        extra = data.get("extra") or ""
        extra = re.sub(r"^DOI:.*$\n?", "", extra, flags=re.I | re.M).strip()
        data["extra"] = (f"DOI: {new}\n{extra}").strip()
    elif name == "creators":
        if change.kind == "fill":
            givens = json.loads(new)
            authors = [c for c in data.get("creators") or [] if c.get("creatorType", "author") == "author"]
            for c, given in zip(authors, givens):
                if given and "lastName" in c:
                    c["firstName"] = given
        else:
            people = []
            for part in new.split(";"):
                family, _, given = part.strip().partition(",")
                people.append({"creatorType": "author", "lastName": family.strip(), "firstName": given.strip()})
            others = [c for c in data.get("creators") or [] if c.get("creatorType", "author") != "author"]
            data["creators"] = people + others
    elif name == "publicationTitle":
        if "journalAbbreviation" in data and not data.get("journalAbbreviation") and change.old \
                and looks_abbreviated(change.old, new):
            data["journalAbbreviation"] = change.old
        data[name] = new
    elif name == "pages":
        data[name] = new.replace("-", "–") if re.fullmatch(r"\d+-\d+", new) else new
    else:
        data[name] = new


class MetadataWriter:
    def __init__(self):
        from zotero_mcp.tools import _helpers

        self._helpers = _helpers
        self.ctx = ff._Ctx()
        _read, self.zot, _mode = _helpers.resolve_write_client(self.ctx, op_description="correcting metadata")

    def apply(self, audit: ItemAudit, changes: list[Change], tags_add=(), tags_remove=()) -> None:
        item = self.zot.item(audit.key)
        data = item["data"]
        for c in changes:
            _set_field(data, c)
        tags = [t for t in data.get("tags") or [] if t.get("tag") not in set(tags_remove)]
        have = {t.get("tag") for t in tags}
        tags += [{"tag": t} for t in tags_add if t not in have]
        data["tags"] = tags
        self.zot.update_item(item)

    def add_note(self, parent: str, body: str) -> None:
        note = self._helpers.item_template_for(self.zot, "note")
        note["note"] = body
        note["parentItem"] = parent
        self.zot.create_items([note])

    def proposal_notes(self, parent: str) -> list[dict]:
        out = []
        for child in self.zot.children(parent):
            d = child.get("data", {})
            if d.get("itemType") == "note" and NOTE_MARK in (d.get("note") or ""):
                out.append(child)
        return out

    def trash(self, child: dict) -> None:
        self._helpers.trash_item(self.zot, child)

    def ensure_saved_search(self) -> str:
        try:
            for s in self.zot.searches() or []:
                if (s.get("data") or {}).get("name") == SAVED_SEARCH:
                    return "exists"
            self.zot.saved_search(SAVED_SEARCH, [{"condition": "tag", "operator": "is", "value": TAG_REVIEW}])
            return "created"
        except Exception as e:
            return f"not created ({type(e).__name__})"


def parse_proposals(note_html: str) -> list[Change]:
    m = re.search(rf"{NOTE_MARK}\s*(\[.*\])", html.unescape(re.sub(r"<[^>]+>", "", note_html)), re.S)
    if not m:
        return []
    try:
        return [Change(**c) for c in json.loads(m.group(1))]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    try:
        return json.loads((meta_dir() / "state.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        meta_dir().mkdir(parents=True, exist_ok=True)
        (meta_dir() / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError:
        pass


def _pdf_text_reader() -> Callable[[str], str]:
    """First pages of an item's PDF, read in a child process (PyMuPDF can crash)."""
    try:
        from zotero_mcp.local_db import get_local_zotero_reader

        reader = get_local_zotero_reader()
    except Exception:
        reader = None

    def text(key: str) -> str:
        if reader is None:
            return ""
        for att in reader.get_attachment_paths(key):
            path = att.get("resolved_path")
            if att.get("exists") and path and str(path).lower().endswith(".pdf"):
                probe = ff.probe_pdf(path, pages=2)
                return (probe or {}).get("text", "")
        return ""

    return text


def decide(writer, raw: dict, accept: bool, state: dict, fields: list[str] | None = None,
           log: Callable[[str], None] = print) -> int:
    """Apply (accept) or discard an item's proposals. ``fields`` limits the
    acceptance to those fields; the item's other proposals are rejected."""
    key = raw.get("key") or raw.get("data", {}).get("key")
    label = ff.ItemInfo.from_zotero(raw).label
    notes = writer.proposal_notes(key)
    changes = [c for n in notes for c in parse_proposals(n["data"].get("note", ""))]
    take = [c for c in changes if accept and (not fields or c.field in fields)]
    drop = [c for c in changes if c not in take]
    audit = ItemAudit(key, label, raw.get("data", {}).get("itemType", ""))
    writer.apply(audit, take, tags_add=[TAG_CORRECTED] if take else [],
                 tags_remove=[TAG_ACCEPT, TAG_REJECT, TAG_REVIEW])
    if take:
        writer.add_note(key, _note_html(f"Metadata changes you accepted ({_dt.date.today()})",
                                        audit, take, proposals=False))
    rejected = state.setdefault(key, {}).setdefault("rejected", {})
    for c in drop:
        rejected[c.field] = c.new
    for n in notes:
        writer.trash(n)
    log(f"{label} [{key}]: {len(take)} applied, {len(drop)} discarded")
    return len(take)


def process_review(writer, backend, log: Callable[[str], None] = print) -> dict[str, int]:
    """Apply or discard proposals on items tagged metadata/accept or /reject."""
    counts = {"accepted": 0, "rejected": 0}
    state = _load_state()
    for tag, accept in ((TAG_ACCEPT, True), (TAG_REJECT, False)):
        for raw in backend.list_items(None, limit=10000, tag=[tag]):
            decide(writer, raw, accept, state, log=log)
            counts["accepted" if accept else "rejected"] += 1
    _save_state(state)
    return counts


def review_list(writer, backend, keys: list[str] | None = None) -> list[tuple[dict, list[Change]]]:
    """Items waiting for review, with their proposals."""
    if keys:
        found = backend.get_items(keys)
        raws = [found[k] for k in keys if k in found]
    else:
        raws = backend.list_items(None, limit=10000, tag=[TAG_REVIEW])
    out = []
    for raw in raws:
        key = raw.get("key") or raw.get("data", {}).get("key")
        changes = [c for n in writer.proposal_notes(key) for c in parse_proposals(n["data"].get("note", ""))]
        if changes:
            out.append((raw, changes))
    return out


@dataclass
class AuditReport:
    audits: list[ItemAudit]
    applied: bool
    started: str
    review_counts: dict[str, int] = field(default_factory=dict)
    report_path: str = ""
    saved_search: str = ""

    def totals(self) -> dict[str, int]:
        t = {"items": len(self.audits), "filled": 0, "corrected": 0, "proposals": 0, "items_to_review": 0,
             "flags": 0, "no_source": 0, "errors": 0}
        for a in self.audits:
            t["filled"] += len(a.by_kind("fill"))
            t["corrected"] += len(a.by_kind("correct"))
            t["proposals"] += len(a.by_kind("propose"))
            t["items_to_review"] += bool(a.by_kind("propose"))
            t["flags"] += len(a.flags)
            t["no_source"] += any("no registry record" in f for f in a.flags)
            t["errors"] += bool(a.error)
        return t

    def markdown(self, limit: int | None = None) -> str:
        t = self.totals()
        verb = "Filled" if self.applied else "Would fill"
        lines = [
            f"# Metadata audit ({self.started})", "",
            ("" if self.applied else "Report only: nothing was changed (run with --apply to change). ")
            + f"{t['items']} items checked. {verb} {t['filled']} empty fields; "
            f"{'corrected' if self.applied else 'would correct'} {t['corrected']} fields confirmed by two sources; "
            f"{t['proposals']} proposals on {t['items_to_review']} items for your review; "
            f"{t['flags']} other findings; no registry record for {t['no_source']} items.",
            "",
        ]
        if self.review_counts:
            lines += [f"Review tags processed: {self.review_counts}", ""]
        if self.saved_search:
            lines += [f"Saved search \"{SAVED_SEARCH}\": {self.saved_search}", ""]
        shown = 0
        for a in self.audits:
            if not (a.changes or a.flags or a.error):
                continue
            if limit and shown >= limit:
                lines.append("- … more in the report file")
                break
            shown += 1
            lines.append(f"## {a.label} [{a.key}]" + (f" — {a.reference}" if a.reference else ""))
            for c in a.changes:
                label = FIELD_LABELS.get(c.field, c.field)
                if c.field == "creators" and c.kind == "fill":
                    lines.append(f"- {c.kind}: {label}: {c.why} ({', '.join(c.sources)})")
                    continue
                old = c.old[:120] if c.old else "(empty)"
                why = f" — {c.why}" if c.why else ""
                lines.append(f"- {c.kind}: {label}: {old} → {c.new[:120]} ({', '.join(c.sources)}){why}")
            for f in a.flags:
                lines.append(f"- note: {f}")
            if a.error:
                lines.append(f"- error: {a.error}")
            lines.append("")
        if self.report_path:
            lines.append(f"Full report: {self.report_path}")
        return "\n".join(lines)


def run(
    *,
    keys: list[str] | None = None,
    collection: str | None = None,
    limit: int | None = None,
    apply: bool = False,
    review_only: bool = False,
    log: Callable[[str], None] = print,
    settings: ff.Settings | None = None,
    http: ff.Http | None = None,
    backend=None,
    writer_factory: Callable[[], Any] | None = None,
    pdf_text: Callable[[str], str] | None = None,
    workers: int = 4,
) -> AuditReport:
    from zotero_mcp import library as _library

    settings = settings or ff.Settings.load()
    http = http or ff.Http(settings)
    backend = backend or _library.get_library_backend()
    started = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    writer = (writer_factory or MetadataWriter)() if apply else None
    review_counts: dict[str, int] = {}
    if writer is not None:
        review_counts = process_review(writer, backend, log)
    if review_only:
        return AuditReport([], bool(apply), started, review_counts)

    if keys:
        found = backend.get_items(keys)
        items = [found[k] for k in keys if k in found]
    elif collection:
        items = list(backend.collection_items(collection) or [])
    else:
        items = backend.list_items("-attachment", limit=100000)
    items = [i for i in items if i.get("data", {}).get("itemType") in AUDITED_TYPES]
    if limit:
        items = items[:limit]
    state = _load_state()
    ctx = Context(http, settings, pdf_text or _pdf_text_reader(),
                  {k: v.get("rejected", {}) for k, v in state.items() if isinstance(v, dict)})
    log(f"{len(items)} item(s) to check{'' if apply else ' (report only)'}.")

    def one(raw):
        try:
            return audit_item(raw, ctx)
        except Exception as e:
            info = ff.ItemInfo.from_zotero(raw)
            return ItemAudit(info.key, info.label, info.item_type, error=f"{type(e).__name__}: {e}")

    audits: list[ItemAudit] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for n, audit in enumerate(pool.map(one, items), 1):
            audits.append(audit)
            fills, fixes, props = (len(audit.by_kind(k)) for k in ("fill", "correct", "propose"))
            summary = ", ".join(s for s in (
                f"{fills} to fill" if fills else "", f"{fixes} to correct" if fixes else "",
                f"{props} to review" if props else "", f"{len(audit.flags)} note(s)" if audit.flags else "",
                audit.error and f"error: {audit.error}") if s) or "ok"
            log(f"[{n}/{len(items)}] {audit.label} [{audit.key}]: {summary}")
            if writer is not None and not audit.error:
                _write(writer, audit, log)
            state.setdefault(audit.key, {})["last_audit"] = _dt.datetime.now().isoformat(timespec="seconds")
            if n % 50 == 0:
                _save_state(state)
    _save_state(state)
    report = AuditReport(audits, bool(apply), started, review_counts)
    if writer is not None and any(a.by_kind("propose") for a in audits):
        report.saved_search = writer.ensure_saved_search()
    try:
        runs = meta_dir() / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        path = runs / f"{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        path.write_text(report.markdown(), encoding="utf-8")
        report.report_path = str(path)
    except OSError:
        pass
    return report


def _write(writer, audit: ItemAudit, log: Callable[[str], None]) -> None:
    auto = audit.by_kind("fill") + audit.by_kind("correct")
    props = audit.by_kind("propose")
    try:
        if auto:
            tags = ([TAG_FILLED] if audit.by_kind("fill") else []) + ([TAG_CORRECTED] if audit.by_kind("correct") else [])
            writer.apply(audit, auto, tags_add=tags)
            writer.add_note(audit.key, _note_html(f"Metadata changes by zotero-mcp ({_dt.date.today()})",
                                                  audit, auto, proposals=False))
        if props:
            for old in writer.proposal_notes(audit.key):
                writer.trash(old)
            writer.apply(audit, [], tags_add=[TAG_REVIEW])
            writer.add_note(audit.key, _note_html(f"Proposed metadata changes ({_dt.date.today()})",
                                                  audit, props, proposals=True))
    except Exception as e:
        audit.error = f"writing failed: {type(e).__name__}: {e}"
        log(f"    -> {audit.error}")

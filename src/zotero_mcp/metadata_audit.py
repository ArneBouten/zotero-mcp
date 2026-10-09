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
import os
import re
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from zotero_mcp import fulltext_fetch as ff

TAG_REVIEW = "metadata/review"
TAG_ACCEPT = "metadata/accept"
TAG_REJECT = "metadata/reject"
TAG_FILLED = "auto-enriched"
TAG_CORRECTED = "auto-corrected"
TAG_RETRACTED = "retracted"
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
    containers: list = field(default_factory=list)  # every container title (book and series)
    isbn: str = ""
    article_number: str = ""        # e.g. "e70024": APA's stand-in for pages
    online_year: str = ""           # Crossref's published-online year, when it differs from the issue's
    updates: list = field(default_factory=list)     # (type, date, doi): retractions, corrections
    published_doi: str = ""         # a preprint's published version
    editors: list[tuple[str, str]] = field(default_factory=list)


def _given_case(given: str) -> str:
    """ "JOSJE M." as "Josje M.": some publishers send first names in capitals."""
    if given and given.isupper() and len(re.sub(r"[^A-Za-z]", "", given)) > 2:
        return given.title()
    return given


#: A heading glued to the abstract's first word ("IntroductionSELF-DETERMINATION", "Comunicaciones brevesRESUMEN El").
_GLUED_HEADING_RE = re.compile(r"^(?:Comunicaciones breves|Short communication)?\s*"
                               r"(?:[Aa]bstract|ABSTRACT|[Ss]ummary|SUMMARY|[Ii]ntroduction|INTRODUCTION|"
                               r"[Rr]esumen|RESUMEN|[Rr]ésumé|RÉSUMÉ)?\s*[:.]?\s*(?=[A-ZÀ-Ý])")


def _clean_abstract(text: str) -> str:
    t = re.sub(r"^abstract\b\s*[:.]?\s*", "", (text or "").strip(), flags=re.I)
    m = _GLUED_HEADING_RE.match(t)
    return t[m.end():] if m and m.end() else t


def _demojibake(text: str) -> str:
    """UTF-8 read as Latin-1 somewhere upstream ("JÃ¤ger" -> "Jäger")."""
    if text and re.search(r"Ã.|â€", text):
        try:
            return text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text
    return text


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return _demojibake(re.sub(r"\s+", " ", html.unescape(text)).strip())


def _date_from_parts(obj: dict | None) -> str:
    parts = ((obj or {}).get("date-parts") or [[]])[0]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts))


#: Degrees publishers sometimes put into a name ("Lambiase MS").
_DEGREES = {"ms", "msc", "ma", "ba", "bsc", "phd", "md", "mph", "rn", "dphil", "edd", "psyd", "med", "mba"}


def _clean_family(name: str) -> str:
    words = _strip_tags(name).replace(",", " ").split()
    while len(words) > 1 and words[-1].lower().replace(".", "") in _DEGREES:
        words.pop()
    return " ".join(words)


def _crossref_headers(settings: ff.Settings) -> dict | None:
    return {"User-Agent": f"zotero-mcp metadata-audit (mailto:{settings.email})"} if settings.email else None


def crossref(doi: str, http: ff.Http, settings: ff.Settings) -> Record | None:
    status, data = http.api_json(f"https://api.crossref.org/works/{quote(doi, safe='/')}",
                                 headers=_crossref_headers(settings))
    if status != 200 or not data:
        return None
    return _crossref_record(data.get("message") or {}, doi)


def crossref_by_title(item: ff.ItemInfo, http: ff.Http, settings: ff.Settings) -> Record | None:
    """Crossref's bibliographic search (free, unlike OpenAlex's): a work that is clearly the item."""
    if not item.title or norm_title(item.title) in GENERIC_TITLES:
        return None
    query = f"{item.title[:250]} {item.first_author or ''}".strip()
    params = {"query.bibliographic": query, "rows": "5"}
    if item.year.isdigit():
        y = int(item.year)
        params["filter"] = f"from-pub-date:{y - 1},until-pub-date:{y + 1}"
    status, data = http.api_json("https://api.crossref.org/works", params=params,
                                 headers=_crossref_headers(settings))
    if status != 200 or not data:
        return None
    for m in ((data.get("message") or {}).get("items") or [])[:5]:
        if not isinstance(m, dict) or not m.get("DOI"):
            continue
        rec = _crossref_record(m, m["DOI"])
        rec.by = "title"
        if _matches_item(item, rec):
            return rec
    return None


def _crossref_record(m: dict, doi: str) -> Record:
    # APA dates a journal article by its issue; fall back to the first publication.
    date = _date_from_parts(m.get("published-print")) or _date_from_parts(m.get("issued"))
    online = _date_from_parts(m.get("published-online"))
    kind = m.get("type", "")
    containers = [_strip_tags(c) for c in m.get("container-title") or [] if c]
    container = containers[0] if containers else ""
    if kind in ("book-chapter", "book-section", "reference-entry") and len(containers) > 1:
        container = max(containers, key=len)   # the book, not its series ("Evolutionary Psychology")
    title = _strip_tags((m.get("title") or [""])[0]).rstrip(".")
    subtitle = _strip_tags((m.get("subtitle") or [""])[0]).rstrip(".")
    if subtitle and ff._fold(subtitle) not in ff._fold(title):
        title = f"{title}: {subtitle}"
    return Record(
        source="Crossref",
        title=title,
        authors=[(_clean_family(a["family"]), _given_case(_strip_tags(a.get("given", ""))))
                 for a in m.get("author") or [] if a.get("family")],
        year=date[:4], date=date,
        journal=container if kind in ("journal-article", "proceedings-article") else "",
        container=container if kind in ("book-chapter", "book-section", "reference-entry") else "",
        containers=containers,
        issn=list(m.get("ISSN") or []),
        volume=str(m.get("volume") or ""), issue=str(m.get("issue") or ""), pages=str(m.get("page") or ""),
        doi=(m.get("DOI") or doi).lower(), publisher=m.get("publisher") or "",
        place=m.get("publisher-location") or "",
        abstract=_clean_abstract(_strip_tags(m.get("abstract") or "")),
        kind=kind, isbn=(m.get("ISBN") or [""])[0], article_number=str(m.get("article-number") or ""),
        online_year=online[:4] if online[:4] != date[:4] else "",
        updates=[(u.get("type", ""), _date_from_parts(u.get("updated")), (u.get("DOI") or "").lower())
                 for u in m.get("updated-by") or [] if isinstance(u, dict)],
        published_doi=(((m.get("relation") or {}).get("is-preprint-of") or [{}])[0].get("id") or "").lower(),
        editors=[(_clean_family(a["family"]), _strip_tags(a.get("given", ""))) for a in m.get("editor") or []
                 if a.get("family")],
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
        abstract = _clean_abstract(re.sub(r"\s+", " ", " ".join(w for _pos, w in words)).strip())
    authors = []
    for au in work.get("authorships") or []:
        name = ((au.get("author") or {}).get("display_name") or "").strip()
        if name:
            family = name.split()[-1]
            authors.append((family, name[: -len(family)].strip()))
    return Record(
        source="OpenAlex", by=by,
        title=_strip_tags(work.get("title") or "").rstrip("."), authors=authors,
        year=str(work.get("publication_year") or ""), date=str(work.get("publication_date") or ""),
        journal=_strip_tags(source.get("display_name", "")) if source.get("type") == "journal" else "",
        issn=list(source.get("issn") or []),
        volume=str(biblio.get("volume") or ""), issue=str(biblio.get("issue") or ""), pages=pages,
        doi=re.sub(r"^https?://doi\.org/", "", work.get("doi") or "").lower(),
        abstract=abstract, kind=work.get("type") or "",
    )


def openalex_by_doi(doi: str, http: ff.Http, settings: ff.Settings) -> Record | None:
    status, data = http.api_json(f"https://api.openalex.org/works/doi:{quote(doi, safe='/')}",
                                 params=ff._openalex_params(settings))
    return _openalex_record(data, "doi") if status == 200 and data else None


#: Titles too generic to match on without an exact match.
GENERIC_TITLES = {
    "introduction", "editorial", "book review", "review", "foreword", "preface", "commentary", "reply",
    "response", "letter", "correction", "erratum", "abstract", "abstracts", "conclusion", "discussion", "index",
    "contents", "notes", "news", "obituary", "in memoriam", "guest editorial", "editorial introduction",
    "introduction to the special issue", "afterword", "epilogue", "prologue", "acknowledgements",
}
#: Work types that are never the same thing as a Zotero item (a dataset or an
#: erratum with the same title). Other type differences are only a soft
#: signal: OpenAlex often calls a chapter an article.
_REJECT_KINDS = {"dataset", "paratext", "peer-review", "erratum", "retraction", "grant", "supplementary-materials",
                 "libguides", "component"}


def _content_words(text: str) -> list[str]:
    return [w for w in ff._fold(text).split() if len(w) >= 4 and w not in ff._STOPWORDS]


def _main_title(text: str) -> str:
    return ff._fold(re.split(r"[:?!.]\s", str(text or ""), maxsplit=1)[0])


def title_match(mine: str, theirs: str) -> float:
    """How alike two titles are, 0..1.

    Character-based (difflib's ratio on lower-cased, accent-free words), with
    a series note, edition or "Chapter 4:" removed first. When the main titles
    (before a colon) are equal and at least four words long, a missing or
    different subtitle still counts as 0.95.
    """
    sim = ff.title_similarity(norm_title(mine), norm_title(theirs))
    a, b = _main_title(mine), _main_title(theirs)
    if len(a.split()) >= 4 and a == b:
        sim = max(sim, 0.95)
    return sim


def _matches_item(item: ff.ItemInfo, rec: Record, year_tolerance: int = 1) -> bool:
    """Is this record clearly the item: title, first author, year (and not a dataset)?"""
    if not item.title or not rec.title:
        return False
    if norm_title(item.title) in GENERIC_TITLES or len(_content_words(item.title)) < 3:
        if norm_title(item.title) != norm_title(rec.title):
            return False
    if title_match(item.title, rec.title) < 0.9:
        return False
    if item.first_author and rec.authors:
        mine = _family_key(item.first_author).split()[-1:] or [""]
        theirs = {(_family_key(f).split() or [""])[-1] for f, _g in rec.authors[:5]}
        if mine[0] and mine[0] not in theirs:
            return False
    if item.year.isdigit() and rec.year.isdigit() and abs(int(item.year) - int(rec.year)) > year_tolerance:
        return False
    return (rec.kind or "").lower() not in _REJECT_KINDS


def openalex_by_title(item: ff.ItemInfo, http: ff.Http, settings: ff.Settings) -> Record | None:
    """A work that is clearly the same: title, first author and year."""
    if not item.title or (norm_title(item.title) in GENERIC_TITLES):
        return None
    work = ff._openalex_work(ff.ItemInfo(**{**asdict(item), "doi": ""}), http, settings)
    if not work:
        return None
    rec = _openalex_record(work, "title")
    return rec if _matches_item(item, rec) else None


def _split_name(name: str) -> tuple[str, str]:
    parts = (name or "").split()
    return (parts[-1], " ".join(parts[:-1])) if parts else ("", "")


_S2_KINDS = {"Dataset": "dataset", "Book": "book", "BookSection": "book-chapter", "JournalArticle": "article",
             "Conference": "proceedings-article", "Review": "article", "Editorial": "editorial"}


def semantic_scholar_by_title(item: ff.ItemInfo, http: ff.Http, settings: ff.Settings) -> Record | None:
    """Semantic Scholar's best title match, when it is clearly the item."""
    if not item.title or norm_title(item.title) in GENERIC_TITLES:
        return None
    headers = {"x-api-key": settings.keys["semantic_scholar"]} if settings.has("semantic_scholar") else None
    status, data = http.api_json(
        "https://api.semanticscholar.org/graph/v1/paper/search/match",
        params={"query": item.title[:300],
                "fields": "title,authors,year,venue,journal,externalIds,publicationTypes"},
        headers=headers)
    hits = (data or {}).get("data") or [] if status == 200 else []
    if not hits:
        return None
    p = hits[0]
    journal = p.get("journal") or {}
    kinds = [_S2_KINDS.get(t, "") for t in p.get("publicationTypes") or []]
    rec = Record(
        source="Semantic Scholar", by="title", title=(p.get("title") or "").rstrip("."),
        authors=[_split_name(a.get("name", "")) for a in p.get("authors") or [] if a.get("name")],
        year=str(p.get("year") or ""), journal=journal.get("name") or p.get("venue") or "",
        volume=str(journal.get("volume") or "").strip(), pages=re.sub(r"\s", "", str(journal.get("pages") or "")),
        doi=((p.get("externalIds") or {}).get("DOI") or "").lower(), kind=next((k for k in kinds if k), ""),
    )
    return rec if _matches_item(item, rec) else None


_CSL_KINDS = {"article-journal": "journal-article", "chapter": "book-chapter", "book": "book",
              "paper-conference": "proceedings-article", "dataset": "dataset", "report": "report",
              "thesis": "dissertation"}


def doi_registry(doi: str, http: ff.Http, settings: ff.Settings) -> Record | None:
    """The record from another DOI agency (mEDRA, JaLC, KISTI, ...) through doi.org."""
    status, ra = http.api_json(f"https://doi.org/ra/{quote(doi, safe='/')}")
    agency = (ra[0].get("RA") if status == 200 and isinstance(ra, list) and ra and isinstance(ra[0], dict)
              else "") or ""
    if not agency or agency in ("Crossref", "DataCite") or "not" in agency.lower():
        return None
    status, m = http.api_json(f"https://doi.org/{quote(doi, safe='/')}",
                              headers={"Accept": "application/vnd.citationstyles.csl+json"})
    if status != 200 or not isinstance(m, dict):
        return None
    date = _date_from_parts(m.get("issued"))
    kind = _CSL_KINDS.get(m.get("type", ""), m.get("type", ""))
    container = _strip_tags(m.get("container-title") or "")
    container = container[0] if isinstance(container, list) else container
    return Record(
        source=f"{agency} (DOI registry)", title=_strip_tags(m.get("title") or "").rstrip("."),
        authors=[(_clean_family(a["family"]), _strip_tags(a.get("given", ""))) for a in m.get("author") or []
                 if isinstance(a, dict) and a.get("family")],
        year=date[:4], date=date,
        journal=container if kind in ("journal-article", "proceedings-article") else "",
        container=container if kind == "book-chapter" else "",
        volume=str(m.get("volume") or ""), issue=str(m.get("issue") or ""), pages=str(m.get("page") or ""),
        doi=doi.lower(), publisher=m.get("publisher") or "", kind=kind,
        issn=[m["ISSN"]] if isinstance(m.get("ISSN"), str) else list(m.get("ISSN") or []),
    )


#: SerpApi searches the audit leaves for the full-text fetcher each month.
SCHOLAR_RESERVE = 60


def parse_apa(citation: str) -> dict:
    """Fields of an APA reference as Google Scholar's Cite gives it (plain text)."""
    m = re.match(r"^(?P<authors>.+?)\s\((?P<year>\d{4})[a-z]?\)\.\s(?P<rest>.+)$", (citation or "").strip())
    if not m:
        return {}
    out = {"year": m.group("year")}
    parts = re.split(r"(?<=[.?!])\s+(?=[A-Z0-9])", m.group("rest"), maxsplit=1)
    out["title"] = parts[0].rstrip(".").strip()
    tail = parts[1].strip() if len(parts) > 1 else ""
    j = re.match(r"^(?P<journal>[^,]+?),\s*(?P<volume>\d+)(?:\((?P<issue>[^)]+)\))?"
                 r"(?:,\s*(?P<pages>[eE]?\d+(?:\s*[-–]\s*[eE]?\d+)?))?\.?$", tail)
    if j:
        out.update({k: v for k, v in j.groupdict().items() if v})
    elif tail and not re.search(r"\d", tail):
        out["publisher"] = tail.rstrip(".").strip()
    return out


def scholar_cite(item: ff.ItemInfo, http: ff.Http, settings: ff.Settings, budget=None) -> Record | None:
    """Google Scholar's record (search, then Cite) through SerpApi: last resort, two searches."""
    if not settings.has("serpapi") or not item.title or norm_title(item.title) in GENERIC_TITLES:
        return None
    budget = budget or ff.Budget()
    if not budget.allows("serpapi", settings.serpapi_monthly - SCHOLAR_RESERVE, cost=2):
        return None
    budget.spend("serpapi")
    status, data = http.api_json("https://serpapi.com/search.json", timeout=60, params={
        "engine": "google_scholar", "q": item.title[:250], "num": "5", "api_key": settings.keys["serpapi"]})
    hits = (data or {}).get("organic_results") or [] if status == 200 else []
    family = (_family_key(item.first_author).split() or [""])[-1] if item.first_author else ""
    hit = None
    for r in hits:
        summary = ff._fold((r.get("publication_info") or {}).get("summary") or "")
        if title_match(item.title, r.get("title") or "") >= 0.9 and (not family or family in summary):
            hit = r
            break
    if not hit or not hit.get("result_id"):
        return None
    budget.spend("serpapi")
    status, data = http.api_json("https://serpapi.com/search.json", timeout=60, params={
        "engine": "google_scholar_cite", "q": hit["result_id"], "api_key": settings.keys["serpapi"]})
    apa = next((c.get("snippet", "") for c in (data or {}).get("citations") or [] if c.get("title") == "APA"), "")
    f = parse_apa(apa) if status == 200 else {}
    if not f:
        return None
    return Record(source="Google Scholar (Cite)", by="title", title=f.get("title", hit.get("title", "")),
                  year=f.get("year", ""), journal=f.get("journal", ""), volume=f.get("volume", ""),
                  issue=f.get("issue", ""), pages=f.get("pages", ""), publisher=f.get("publisher", ""))


def google_books(isbn: str, http: ff.Http, settings: ff.Settings) -> Record | None:
    """Google Books' record for an ISBN: a second source for books (year, publisher)."""
    params = {"q": f"isbn:{isbn}"}
    if settings.has("google_books"):
        params["key"] = settings.keys["google_books"]   # optional: a higher daily limit
    status, data = http.api_json("https://www.googleapis.com/books/v1/volumes", params=params)
    items = (data or {}).get("items") or [] if status == 200 else []
    if not items:
        return None
    v = items[0].get("volumeInfo") or {}
    m = re.search(r"\d{4}", v.get("publishedDate") or "")
    title = v.get("title") or ""
    if v.get("subtitle"):
        title += ": " + v["subtitle"]
    return Record(source="Google Books", by="isbn", title=title, year=m.group(0) if m else "",
                  publisher=v.get("publisher") or "", isbn=isbn, kind="book",
                  authors=[_split_name(a) for a in v.get("authors") or []])


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
        source="PubMed", title=_strip_tags(x.get("title") or "").rstrip("."),
        authors=[(a.get("lastName", ""), a.get("firstName", "")) for a in (x.get("authorList") or {}).get("author") or []
                 if a.get("lastName")],
        year=str(ji.get("yearOfPublication") or x.get("pubYear") or ""),
        journal=_strip_tags((ji.get("journal") or {}).get("title", "")),
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
    return re.sub(r"^the | the$", "", v).strip()


def journal_core(value: str) -> str:
    """A journal name without the clutter some imports add: "(Auckland, N.Z.)",
    " - ELEM SCH J", ": A Journal of the American Psychological Society"."""
    v = str(value or "")
    v = re.sub(r"\s+-\s+[A-Z][A-Z .&]+$", "", v)
    v = re.sub(r"\s*\([^)]*\)\s*$", "", v)
    v = re.split(r"\s*:\s+", v, maxsplit=1)[0]
    return norm_journal(v)


def norm_doi(value: str) -> str:
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", (value or "").strip(), flags=re.I).lower()


def isbn13s(value: str) -> set[str]:
    """Every ISBN in a field, as ISBN-13 digits ("978-1-85168-480-9" -> "9781851684809")."""
    out = set()
    for raw in re.findall(r"[\dXx][\dXx\-]{8,16}[\dXx]", str(value or "")):
        d = re.sub(r"[^0-9Xx]", "", raw).upper()
        if len(d) == 10:
            core = "978" + d[:9]
            check = (10 - sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(core)) % 10) % 10
            d = core + str(check)
        if len(d) == 13:
            out.add(d)
    return out


def _same_book(a: str, b: str) -> bool:
    """ "Oxford handbook of positive psychology" and "The Oxford Handbook of Positive Psychology"."""
    def drop(v: str) -> str:
        return re.sub(r"^(?:the|a|an|de|het|een)\s+", "", norm_title(v))
    return bool(a and b) and ff.title_similarity(drop(a), drop(b)) >= 0.97


def norm_title(value: str) -> str:
    """A title without a series note ("(Routledge Revivals)"), edition, "Chapter 4:" or punctuation."""
    v = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", str(value or ""))
    v = re.sub(r"^\s*(?:chapter|ch\.)\s+(?:\d+|[ivxlc]+)\s*[:.\-–]\s*", "", v, flags=re.I)
    v = re.sub(r"[,:\s]+(?:\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth|sixth|revised|new)"
               r"\s+(?:ed\.?|edition)\s*$", "", v, flags=re.I)
    return ff._fold(v)


def same_title(mine: str, theirs: str) -> bool:
    """Equal, or one is the other's main title and the other only adds a subtitle."""
    a, b = norm_title(mine), norm_title(theirs)
    if a == b:
        return True
    main_a = ff._fold(re.split(r"[:?!.]\s", str(mine or ""), maxsplit=1)[0])
    # The registry dropped the subtitle you have: keep yours.
    return bool(b) and main_a == b and len(a) > len(b)


def same(field_name: str, a: str, b: str) -> bool:
    if field_name == "ISBN":
        return bool(isbn13s(a) & isbn13s(b))
    if field_name in ("title", "bookTitle"):
        return same_title(a, b)
    if field_name == "pages":
        return norm_pages(a) == norm_pages(b)
    if field_name in ("volume", "issue"):
        return re.sub(r"^0+(?=\d)", "", norm_simple(a)) == re.sub(r"^0+(?=\d)", "", norm_simple(b))
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


def _loses_accents(old: str, new: str) -> bool:
    """"Revista electrónica ..." -> "Revista Electronica ...": a word lost its accents."""
    words = {ff._fold(w): w for w in re.findall(r"[^\W\d_]+", old)}
    for w in re.findall(r"[^\W\d_]+", new):
        mine = words.get(ff._fold(w))
        if mine and not mine.isascii() and w.isascii():
            return True
    return False


_PAGE_LIKE = re.compile(r"(?:[A-Za-z]{0,2}\d+[A-Za-z]?|[ivxlcdm]+)(?:\s*[-–—]\s*(?:[A-Za-z]{0,2}\d+[A-Za-z]?|[ivxlcdm]+))?",
                        re.I)


def worth_proposing(field_name: str, old: str, new: str) -> bool:
    """Leave out differences that would only make the field worse: an abbreviated journal,
    a lost accent or supplement, a year as the volume, "Article # 3" as pages."""
    old, new = str(old or "").strip(), str(new or "").strip()
    if old and _loses_accents(old, new):
        return False
    if field_name == "publicationTitle" and old:
        if looks_abbreviated(new, old):
            return False
        o, n = norm_journal(old), norm_journal(new)
        # Shorter is fine only as the same name without clutter ("Sensors (Basel, Switzerland)"),
        # not with its first words gone ("Advances in Neural ..." -> "Neural ...").
        if len(n) < len(o) and not o.startswith(n) and journal_core(old) != n:
            return False
    if field_name in ("volume", "issue"):
        if re.search(r"suppl", old, re.I) and not re.search(r"suppl", new, re.I):
            return False
        if re.search(r"_suppl|\s", new) and not re.search(r"\s", old):
            return False
        if field_name == "volume" and re.fullmatch(r"(?:19|20)\d\d", new) and not re.fullmatch(r"(?:19|20)\d\d", old):
            return False
    if field_name == "pages" and not _PAGE_LIKE.fullmatch(new):
        return False
    return True


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
    retracted: bool = False
    #: a problem with the attached PDF (attachment_check.Problem), or None
    attachment: Any = None
    #: the item's tags when it was read
    tags: set = field(default_factory=set)
    #: tagged fulltext/check-pdf by an earlier run, but its PDF passes now
    attachment_ok: bool = False
    #: the notices (retractions, corrections) the DOI's record lists, when it was read: a later
    #: retraction check of an unchanged paper then knows them already; None = not read
    notices: list | None = None

    def by_kind(self, kind: str) -> list[Change]:
        return [c for c in self.changes if c.kind == kind]


class _Recorder:
    """The run's Http for one item, noting which services could not answer
    (HTTP 429, a server error or no connection): their silence is not "no record"."""

    def __init__(self, http):
        self._http = http
        self.failed: set[str] = set()
        self.answered: set[str] = set()

    @staticmethod
    def service(url: str) -> str:
        host = urlparse(url).hostname or ""
        return ".".join(host.split(".")[-2:])

    def api_json(self, url, *args, **kwargs):
        status, data = self._http.api_json(url, *args, **kwargs)
        (self.failed if status in (0, None, 429) or (status or 0) >= 500 else self.answered).add(self.service(url))
        return status, data

    def __getattr__(self, name):
        return getattr(self._http, name)


#: Services that refused this many times are not asked again in this run
#: (OpenAlex's free daily allowance, for instance, is used up).
DOWN_AFTER = 3


class Context:
    """What an audit run shares: HTTP, settings, the PDF reader, rejections."""

    def __init__(self, http: ff.Http, settings: ff.Settings, pdf_text: Callable[[str], str] | None = None,
                 rejected: dict | None = None, learned: dict | None = None,
                 pdf_read: Callable[[str], dict | None] | None = None,
                 scholar: Callable[[ff.ItemInfo], Record | None] | None = None):
        self.http = http
        self.settings = settings
        self.pdf_text = pdf_text or (lambda key: "")
        self.rejected = rejected or {}
        self.learned = learned or {}
        #: the item's PDF attachments with their first pages (attachment check), and the library's
        #: DOI/title index to name the item a stray PDF belongs to
        self.pdfs: Callable[[str], list[dict]] | None = None
        self.index: dict | None = None
        #: citation data of the item's saved web page (HTML snapshot): {"title", "doi", ...}
        self.page_meta: Callable[[str], dict] | None = None
        #: Gemini's reading of the item's PDF (first pages): a dict of fields, or None.
        self.pdf_read = pdf_read
        #: Google Scholar's Cite (SerpApi), for items with no record and no readable PDF.
        self.scholar = scholar
        #: refusals per service in this run, and the services given up on
        self.refusals: Counter = Counter()
        self.down: set[str] = set()
        self.log: Callable[[str], None] = lambda m: None
        self._lock = threading.Lock()

    def note_failures(self, failed: set[str]) -> None:
        with self._lock:
            for service in failed:
                self.refusals[service] += 1
                if self.refusals[service] == DOWN_AFTER and service not in self.down:
                    self.down.add(service)
                    self.log(f"  {service} keeps refusing (HTTP 429 or no connection, e.g. a used-up daily "
                             f"allowance); not asked again in this run.")


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
        # Not the place: APA 7 leaves it out, and the registries' versions are messy.
        "book": ["publisher", "ISBN"],
        "bookSection": ["bookTitle", "pages", "publisher"],
        "thesis": ["university"],
        "report": ["institution"],
    }.get(item_type, [])


def audit_item(raw: dict, ctx: Context) -> ItemAudit:
    data = raw.get("data", raw)
    info = ff.ItemInfo.from_zotero(raw)
    audit = ItemAudit(info.key, info.label, info.item_type)
    settings = ctx.settings
    http = _Recorder(ctx.http)
    audit.tags = {t.get("tag") for t in data.get("tags") or [] if isinstance(t, dict)}
    try:
        audit = _audit_item(raw, data, info, audit, http, settings, ctx)
    finally:
        ctx.note_failures(http.failed)
    if ctx.pdfs is not None:
        from zotero_mcp import attachment_check

        pdfs: list = []
        try:
            pdfs = ctx.pdfs(info.key) or []
            problem = attachment_check.check(
                info, data, pdfs,
                reading=(lambda: ctx.pdf_read(info.key)) if ctx.pdf_read else None, index=ctx.index)
        except Exception as e:
            problem = None
            audit.flags.append(f"attachment not checked ({type(e).__name__})")
        if problem is not None:
            audit.attachment = problem
            audit.flags.append(f"attachment: {problem.describe()}")
        elif (ff.TAG_CHECK_PDF in audit.tags and any((p.get("text") or "").strip() for p in pdfs)
              and "attachment not checked" not in " ".join(audit.flags)):
            audit.attachment_ok = True      # an earlier run's finding no longer holds (or the PDF was replaced)
    return audit


def _audit_item(raw: dict, data: dict, info: ff.ItemInfo, audit: ItemAudit, http: _Recorder,
                settings: ff.Settings, ctx: Context) -> ItemAudit:

    pdf_cache: list = []

    def pdf_reading() -> dict | None:
        """Gemini's reading of the item's PDF, at most once per item."""
        if not pdf_cache:
            try:
                pdf_cache.append(ctx.pdf_read(info.key) if ctx.pdf_read else None)
            except Exception:
                pdf_cache.append(None)
        return pdf_cache[0]

    ref: Record | None = None
    if info.doi:
        ref = (crossref(info.doi, http, settings) or datacite(info.doi, http, settings)
               or doi_registry(info.doi, http, settings))
        if ref is None:
            if http.failed:
                return _not_checked(audit, data, http)
            audit.flags.append(f"DOI {info.doi} not found at any DOI registry (wrong DOI?)")
    elif info.isbn and info.item_type == "book":
        ref = openlibrary(info.isbn, http, settings) or google_books(info.isbn, http, settings)
    if ref is None and not info.doi:
        # A DOI printed on the item's own PDF or saved web page: free, and surer than a title search.
        ref = _doi_from_attachments(info, ctx, http, settings)
    if ref is None and not info.doi:
        match = crossref_by_title(info, http, settings)
        if match is not None and match.kind == "posted-content" and info.item_type != "preprint":
            match = None    # a preprint of the item: look for the published record elsewhere
        if match is not None:
            ref = match
        else:
            if "openalex.org" not in ctx.down:
                match = openalex_by_title(info, http, settings)
            else:
                http.failed.add("openalex.org")
            if match is None and "semanticscholar.org" not in ctx.down:
                match = semantic_scholar_by_title(info, http, settings)
            elif match is None:
                http.failed.add("semanticscholar.org")
        if ref is None and match is None:
            # Gemini's reading of the PDF may give the DOI the rules missed (an image, an odd layout).
            reading = pdf_reading()
            doi = norm_doi((reading or {}).get("doi") or "")
            if doi:
                rec = crossref(doi, http, settings) or datacite(doi, http, settings)
                if rec is not None and _matches_item(info, rec, 1):
                    ref, rec.by = rec, "pdf-doi"
        if ref is None and match is None and http.failed:
            return _not_checked(audit, data, http)
        if ref is None and match and match.doi:
            # The title match points to a DOI: use its Crossref record only if
            # that is clearly the item too (OpenAlex sometimes links a preprint).
            cr = crossref(match.doi, http, settings)
            preprint = cr is not None and cr.kind == "posted-content" and info.item_type != "preprint"
            if cr is not None and not preprint and _matches_item(info, cr, 1):
                ref = cr
            else:
                ref = match
                if cr is None or preprint or not _matches_item(info, cr, 1):
                    ref.doi = ""    # not a DOI to fill in
            ref.by = "title"
        elif ref is None and match:
            ref = match
    if ref is None:
        audit.flags.append("no registry record found")
        reading = pdf_reading()
        page = ctx.page_meta(info.key) if ctx.page_meta else {}
        if reading and reading.get("title"):
            _from_pdf_only(audit, data, info, reading)
        elif page.get("title"):
            _from_pdf_only(audit, data, info, page, source=PAGE_SOURCE)
        elif ctx.scholar is not None:
            _from_scholar(audit, data, info, ctx.scholar(info))
        _type_flags(audit, data)
        return audit
    audit.reference = f"{ref.source} (by {_BY.get(ref.by, ref.by)})"
    if (problem := _kind_mismatch(info.item_type, ref)):
        audit.flags.append(problem)
        _type_flags(audit, data)
        return audit
    if ref.by in ("doi", "pdf-doi", "page-doi"):
        if ref.kind == "posted-content" and info.item_type not in ("preprint", "report", "manuscript"):
            if ref.published_doi:
                audit.changes.append(Change("DOI", info.doi, ref.published_doi, "propose", [ref.source],
                                            "your DOI is the preprint's; this is the published version's"))
            else:
                audit.flags.append("the DOI is a preprint's; the published version has its own DOI")
            _type_flags(audit, data)
            return audit
        if (problem := _doi_identity_problem(info, ref)):
            audit.flags.append(problem)
            _type_flags(audit, data)
            return audit
        _note_updates(audit, ref)
        audit.notices = [u[2] or u[0] + u[1] for u in ref.updates]

    second: list[Record] = []          # independent records, fetched only when needed
    fetched_second = False
    pdf_text: str | None = None
    pm_cache: list = []

    def pubmed() -> Record | None:
        if not pm_cache:
            doi = ref.doi or info.doi
            pm_cache.append(europepmc(doi, http, settings) if doi else None)
        return pm_cache[0]

    def confirmations(field_name: str, value: str) -> list[str]:
        nonlocal fetched_second, pdf_text
        found = []
        if not fetched_second:
            fetched_second = True
            doi = ref.doi or info.doi
            if doi:
                pm = pubmed()
                if pm:
                    second.append(pm)
                if field_name == "publicationTitle" and ref.source != "OpenAlex":
                    oa = openalex_by_doi(doi, http, settings)
                    if oa and oa.journal:
                        second.append(Record(source="OpenAlex journal record", journal=oa.journal))
            if info.item_type == "book" and info.isbn and ref.source != "Google Books":
                gb = google_books(info.isbn, http, settings)
                if gb:
                    second.append(gb)
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
        elif not found and field_name in _PDF_FIELDS:
            # Rules found nothing: let Gemini read the first pages (once per item).
            read = _pdf_value(pdf_reading(), field_name)
            if read and same(field_name, read, value):
                found.append(PDF_SOURCE)
        return found

    # Every field listed is valid for its item type. The local database leaves
    # empty fields out of an item, so absence means empty, not "no such field".
    for name in _fields_for(info.item_type):
        new = record_value(ref, name)
        if name == "pages" and not new and ref.article_number and not _current(data, name).strip():
            # Articles without page numbers: APA cites the article number instead.
            audit.changes.append(Change(name, "", ref.article_number, "fill", [ref.source], "article number"))
            continue
        if not new and name == "abstractNote" and not _current(data, name).strip() and (ref.doi or info.doi):
            # Crossref often has no abstract; OpenAlex usually does.
            pm = pubmed() if info.item_type == "journalArticle" else None
            if pm and pm.abstract and _plausible_abstract(pm.abstract, info.title):
                audit.changes.append(Change(name, "", pm.abstract, "fill", ["PubMed"]))
                continue
            oa = None
            if info.item_type in ("journalArticle", "conferencePaper", "preprint"):
                oa = ref if ref.source == "OpenAlex" else openalex_by_doi(ref.doi or info.doi, http, settings)
            if oa and _plausible_abstract(oa.abstract, info.title):
                audit.changes.append(Change(name, "", oa.abstract, "fill", ["OpenAlex"]))
            continue
        if not new or not re.search(r"[A-Za-z0-9]", new):
            continue
        old = _current(data, name)
        if name == "abstractNote":
            if not old.strip() and (ref.source != "OpenAlex" or _plausible_abstract(new, info.title)):
                audit.changes.append(Change(name, "", new, "fill", [ref.source]))
            continue
        if not old.strip():
            audit.changes.append(Change(name, "", new, "fill", [ref.source], _FILL_WHY.get(ref.by, "")))
            continue
        if name == "ISSN":
            continue    # a journal has several valid ISSNs (print, online); only empty ones are filled
        if same(name, old, new):
            continue
        if name == "pages":
            mine, theirs = norm_pages(old), norm_pages(new)
            if "-" in mine and theirs == mine.split("-")[0]:
                continue    # the registry only has the first page
            if "-" not in mine and "-" in theirs and theirs.split("-")[0] == mine:
                audit.changes.append(Change(name, old, new, "fill", [ref.source], "completed the page range"))
                continue
        if name == "DOI":
            if norm_doi(old).replace("//", "/") != norm_doi(new).replace("//", "/"):
                audit.flags.append(f"DOI: {ref.source} lists this work under {new} as well; "
                                   f"yours ({old}) works, so it is left as is")
                continue
        if name == "bookTitle" and any(_same_book(old, c) for c in (ref.containers or [new])):
            continue
        if name in ("title", "bookTitle"):
            if ff.title_similarity(norm_title(old), norm_title(new)) < 0.97 and not _loses_accents(old, new):
                audit.changes.append(Change(name, old, new, "propose", [ref.source], "the wording differs"))
            continue
        if name == "publisher":
            continue    # imprint, parent company or spelling ("ACM"): not worth a decision
        if name == "publicationTitle" and ref.source == "Semantic Scholar":
            continue    # its venue names are normalised, not the journal's own title
        if not worth_proposing(name, old, new):
            continue
        if name not in AUTO_CORRECT:
            audit.changes.append(Change(name, old, new, "propose", [ref.source], "only one source"))
            continue
        if name == "publicationTitle" and norm_journal(new).startswith(norm_journal(old) + " "):
            continue    # the registry adds a subtitle or suffix to the same name: yours is fine
        if name == "year" and old.isdigit() and new.isdigit() and abs(int(old) - int(new)) > 2:
            audit.changes.append(Change(name, old, new, "propose", [ref.source],
                                        "a large difference: check that the DOI belongs to this item"))
            continue
        if name == "publicationTitle" and not looks_abbreviated(old, new) \
                and journal_core(old) != journal_core(new) and ff.title_similarity(old, new) < 0.8:
            audit.changes.append(Change(name, old, new, "propose", [ref.source], "a different journal name"))
            continue
        agree = confirmations(name, new)
        if agree:
            audit.changes.append(Change(name, old, new, "correct", [ref.source, *agree]))
        elif any(same(name, record_value(r, name), old) for r in second if record_value(r, name)):
            audit.flags.append(f"{FIELD_LABELS.get(name, name)}: {ref.source} says {new!r}, "
                               f"but another source agrees with yours ({old!r}); left as is")
        elif name == "year" and ref.online_year == old:
            audit.changes.append(Change(name, old, new, "propose", [ref.source],
                                        "yours is the online year; APA uses the issue year"))
        else:
            audit.changes.append(Change(name, old, new, "propose", [ref.source], "no second source to confirm"))

    _compare_authors(audit, data, ref, pdf_reading)
    _type_flags(audit, data, ref)
    # Proposals the user rejected before are not made again.
    rejected = ctx.rejected.get(info.key) or {}
    audit.changes = [c for c in audit.changes if not (c.kind == "propose" and rejected.get(c.field) == c.new)]
    _apply_learning(audit, ctx.learned)
    return audit


NOT_CHECKED = "not checked this time"


def _not_checked(audit: ItemAudit, data: dict, http: _Recorder) -> ItemAudit:
    """A registry could not be asked (refused or unreachable): no conclusion, try again later."""
    audit.flags.append(f"{NOT_CHECKED}: {', '.join(sorted(http.failed))} did not answer "
                       f"(HTTP 429, a server error or no connection); run the audit again later")
    _type_flags(audit, data)
    return audit


#: A kind of proposal you decided the same way this often (and at least this
#: share of the time) is decided for you from then on.
LEARN_MIN = 10
LEARN_SHARE = 0.9


def category(change: Change) -> str:
    """What kind of proposal this is, without its values: "year|yours is the online year..."."""
    why = re.sub(r"\d+", "N", change.why or "")
    why = re.sub(r"^first names:.*", "first names", why)
    return f"{change.field}|{why[:80]}"


def _apply_learning(audit: ItemAudit, learned: dict | None) -> None:
    if not learned:
        return
    kept = []
    for c in audit.changes:
        if c.kind == "propose":
            st = learned.get(category(c)) or {}
            yes, no = int(st.get("accepted", 0)), int(st.get("rejected", 0))
            if yes + no >= LEARN_MIN and yes >= LEARN_SHARE * (yes + no):
                c.kind = "correct"
                c.why = f"{c.why}; you accepted {yes} of {yes + no} like this"
            elif yes + no >= LEARN_MIN and no >= LEARN_SHARE * (yes + no):
                continue
        kept.append(c)
    audit.changes = kept


_BOILERPLATE_RE = re.compile(r"©|all rights reserved|protected by copyright|this is an open access article|"
                             r"creative commons|published by elsevier|apa psycinfo database record", re.I)
_EN_WORDS = {"the", "and", "of", "to", "in", "for", "with", "on", "is", "are", "was", "were", "that", "this",
             "by", "from", "as", "we", "these", "their"}


def _english_share(text: str) -> float:
    words = ff._fold(text).split()
    return sum(w in _EN_WORDS for w in words) / max(1, len(words))


def _plausible_abstract(text: str, title: str) -> bool:
    """An abstract that belongs to this item: not another work's, a citation
    stub, a thesis title page, boilerplate or another language."""
    t = (text or "").strip()
    if not 200 <= len(t) <= 6000 or re.match(r"^\(?\d{4}\)", t):
        return False
    if re.search(r"submitted in (partial )?fulfil|^(a )?(doctoral |master'?s? )?(thesis|dissertation)\b", t, re.I):
        return False
    if len(_BOILERPLATE_RE.findall(t)) >= 2:
        return False
    if re.match(r"^(?:preface|foreword|contents|table of contents)\b", t, re.I) or re.match(r"^[a-z]\s", t):
        return False    # a table of contents, or a first word that lost its drop cap ("e authors argue")
    head = ff._fold(t[:300])
    title_words = ff._fold(title or "").split()[:8]
    if len(title_words) >= 4 and " ".join(title_words) in head:
        return False    # a citation line ("HARTER, SUSAN. Pleasure derived from ...") rather than an abstract
    words = set(_content_words(title))
    if words:
        found = sum(1 for w in words if w in ff._fold(t))
        if found < (3 if len(words) >= 5 else min(2, len(words))):
            return False
    if _english_share(title) >= 0.12 and _english_share(t) < 0.04:
        return False    # an English item with an abstract in another language
    return True


_NOTICE_RE = re.compile(r"^(?:correction|corrigendum|erratum|retraction|retracted|notice of retraction|"
                        r"expression of concern|withdrawn)\b", re.I)


def _doi_identity_problem(info: ff.ItemInfo, ref: Record) -> str | None:
    """Does the record found by the item's DOI describe this item at all?"""
    if ref.title and _NOTICE_RE.match(ref.title) and not _NOTICE_RE.match(info.title or ""):
        return "the DOI is a correction or retraction notice, not the work itself (check the DOI)"
    if not (info.title and ref.title) or same_title(info.title, ref.title):
        return None
    sim = title_match(info.title, ref.title)
    a, b = norm_title(info.title), norm_title(ref.title)
    if sim < 0.5 and a not in b and b not in a:
        return f"the DOI's record has another title ({ref.title[:90]}): check the DOI"
    if info.first_author and ref.authors and sim < 0.75:
        mine = (_family_key(info.first_author).split() or [""])[-1]
        theirs = {(_family_key(f).split() or [""])[-1] for f, _g in ref.authors[:5]}
        if mine and mine not in theirs:
            return f"the DOI's record has another title and other authors ({ref.title[:90]}): check the DOI"
    if info.year.isdigit() and ref.year.isdigit() and abs(int(info.year) - int(ref.year)) > 3 and sim < 0.9:
        return f"the DOI's record is from {ref.year} with another title: check the DOI"
    return None


def _note_updates(audit: ItemAudit, ref: Record) -> None:
    """Retractions, expressions of concern and corrections Crossref knows of."""
    for kind, date, doi in ref.updates:
        k = (kind or "").lower().replace("-", "_")
        where = f" ({date[:10]}{', ' + doi if doi else ''})"
        if k in ("retraction", "withdrawal", "removal", "partial_retraction"):
            audit.retracted = True
            audit.flags.append(f"RETRACTED{where}")
        elif k == "expression_of_concern":
            audit.flags.append(f"an expression of concern was published{where}")
        elif k in ("correction", "erratum", "corrigendum", "addendum", "clarification"):
            audit.flags.append(f"{'an' if k[0] in 'aeiou' else 'a'} {k} was published{where}")


PDF_SOURCE = "Gemini, reading its PDF"
PAGE_SOURCE = "the item's saved web page"
_BY = {"pdf-doi": "the DOI printed on its PDF", "page-doi": "the DOI on its saved web page"}
_FILL_WHY = {"title": "matched by title", "pdf-doi": "the DOI printed on the item's PDF",
             "page-doi": "the DOI on the item's saved web page"}
_DOI_IN_TEXT = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)


def _doi_from_attachments(info: ff.ItemInfo, ctx, http, settings) -> Record | None:
    """The record of a DOI printed on the item's PDF or saved web page, when it is clearly the item."""
    tried: list[tuple[str, str]] = []
    page = ctx.page_meta(info.key) if ctx.page_meta else {}
    if page.get("doi"):
        tried.append((norm_doi(page["doi"]), "page-doi"))
    try:
        text = ctx.pdf_text(info.key) or ""
    except Exception:
        text = ""
    for m in _DOI_IN_TEXT.findall(text[:20000])[:3]:
        doi = norm_doi(m.rstrip(".,;)]"))
        if doi and all(doi != d for d, _ in tried):
            tried.append((doi, "pdf-doi"))
    for doi, by in tried:
        rec = crossref(doi, http, settings) or datacite(doi, http, settings)
        if rec is not None and _matches_item(info, rec, 1):
            rec.by = by
            return rec
    return None
_PDF_FIELDS = {"year": "year", "publicationTitle": "journal", "volume": "volume", "issue": "issue",
               "pages": "pages", "DOI": "doi", "publisher": "publisher", "ISBN": "isbn",
               "bookTitle": "book_title", "university": "university", "institution": "publisher"}


def _pdf_value(reading: dict | None, field_name: str) -> str:
    """A field as Gemini read it from the PDF; volume and pages only from a published version."""
    if not reading:
        return ""
    if field_name in ("volume", "issue", "pages", "year") and reading.get("version") in ("preprint",
                                                                                         "accepted manuscript"):
        return ""
    return str(reading.get(_PDF_FIELDS.get(field_name, "")) or "").strip()


def _from_scholar(audit: ItemAudit, data: dict, info: ff.ItemInfo, rec: Record | None) -> None:
    """No registry and no readable PDF: Google Scholar's Cite, as proposals only."""
    if rec is None:
        return
    for name in _fields_for(info.item_type):
        if name in ("title", "abstractNote", "DOI", "ISSN", "ISBN"):
            continue
        new = record_value(rec, name)
        if not new:
            continue
        old = _current(data, name)
        if not worth_proposing(name, old, new):
            continue
        if not old.strip():
            audit.changes.append(Change(name, "", new, "propose", [rec.source], "from Google Scholar"))
        elif not same(name, old, new):
            audit.changes.append(Change(name, old, new, "propose", [rec.source], "Google Scholar says otherwise"))


def _from_pdf_only(audit: ItemAudit, data: dict, info: ff.ItemInfo, reading: dict | None,
                   source: str = "") -> None:
    """No registry knows the item: what its own PDF (or saved web page) says becomes proposals."""
    source = source or PDF_SOURCE
    what = "PDF" if source == PDF_SOURCE else "saved web page"
    if not reading or not reading.get("title"):
        return
    main = re.split(r"[:?!]\s", info.title or "", maxsplit=1)[0]
    if info.title and max(title_match(info.title, reading["title"]), title_match(main, reading["title"])) < 0.8:
        audit.flags.append(f"the attached {what} looks like another work (its title: {reading['title'][:90]})")
        return
    for name in _fields_for(info.item_type):
        if name in ("title", "abstractNote"):
            continue
        new = _pdf_value(reading, name)
        if not new:
            continue
        old = _current(data, name)
        if not worth_proposing(name, old, new):
            continue
        if not old.strip():
            audit.changes.append(Change(name, "", new, "propose", [source], f"read from the {what}"))
        elif not same(name, old, new):
            audit.changes.append(Change(name, old, new, "propose", [source], f"the {what} says otherwise"))


def _kind_mismatch(item_type: str, ref: Record) -> str | None:
    """A DOI that belongs to something else than the item: nothing is compared then."""
    kind = (ref.kind or "").lower()
    if (ref.doi or "").startswith("10.5860/choice"):
        return "the DOI is a CHOICE review of the book, not the book (check the DOI)"
    if kind == "component" or re.search(r":\s*(?:table|figure|fig\.|supplementary\b.*)\s*\d*$", ref.title or "", re.I):
        return "the DOI points to a table, figure or supplement, not the work itself (check the DOI)"
    if item_type == "bookSection" and kind in ("book", "edited-book", "monograph", "reference-book"):
        return "the DOI is the whole book's, not this chapter's (check the DOI)"
    if item_type == "journalArticle" and kind in ("book", "edited-book", "monograph", "book-chapter", "dataset"):
        return f"the DOI is registered as a {kind}, not a journal article (check the DOI or the item type)"
    return None


def _family_key(name: str) -> str:
    """A surname for comparing: no suffix (Jr., III) and no stray initials ("B. Owen")."""
    words = [w for w in ff._fold(name or "").split() if len(w) > 1 and w not in {"jr", "sr", "ii", "iii", "iv"}]
    return " ".join(words)


def _letters(text: str) -> int:
    return len(re.sub(r"[^A-Za-zÀ-ÿ]", "", text or ""))


def _last_names(names: list[str]) -> list[str]:
    return [(ff._fold(n).split() or [""])[-1] for n in names]


def _compare_authors(audit: ItemAudit, data: dict, ref: Record,
                     pdf_reading: Callable[[], dict | None] | None = None) -> None:
    mine = [c for c in data.get("creators") or [] if c.get("creatorType", "author") == "author"]
    if not mine or not ref.authors or ref.source == "OpenAlex":
        return
    my_family = [_family_key(c.get("lastName") or c.get("name") or "") for c in mine]
    their_family = [_family_key(f) for f, _g in ref.authors]
    # The proposal keeps your fuller first names where the registry has initials.
    my_given = {k: (c.get("firstName") or "") for k, c in zip(my_family, mine)}
    merged = []
    for f, g in ref.authors:
        have = my_given.get(_family_key(f), "")
        if have and _letters(have) > _letters(g) and ff._fold(have)[:1] == ff._fold(g)[:1]:
            g = have
        merged.append((f, g))
    fmt = "; ".join(f"{f}, {g}".strip(", ") for f, g in merged)
    if my_family != their_family and len(their_family) < len(my_family) \
            and all(f in my_family for f in their_family):
        return    # the registry left out authors you have (group authors, often): keep yours
    if my_family != their_family:
        if set(my_family) == set(their_family):
            why = "the author order differs"
        elif len(my_family) != len(their_family):
            why = f"{len(their_family)} authors instead of {len(my_family)}"
        else:
            why = "the author names differ"
        old = "; ".join(f"{c.get('lastName') or c.get('name')}, {c.get('firstName', '')}".strip(", ") for c in mine)
        sources = [ref.source]
        reading = pdf_reading() if pdf_reading else None
        printed = _last_names(list((reading or {}).get("authors") or []))
        if printed:
            if printed == _last_names([f for f, _g in ref.authors]):
                sources.append(PDF_SOURCE)
                why += "; the PDF lists the same authors"
            elif printed == _last_names([c.get("lastName") or c.get("name") or "" for c in mine]):
                audit.flags.append(f"Authors: {ref.source}'s list differs from yours, but the PDF agrees "
                                   "with yours; left as is")
                return
        audit.changes.append(Change("creators", old, fmt, "propose", sources, why))
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
        body += ("<p>To accept or reject them: right-click the item › <b>Review suggested metadata</b>. "
                 "(Or tag it <b>metadata/accept</b> or <b>metadata/reject</b>; the next check applies that.)</p>")
        payload = json.dumps([asdict(c) for c in changes], ensure_ascii=False)
        body += f"<pre>{NOTE_MARK} {html.escape(payload)}</pre>"
    return body


def _set_field(data: dict, change: Change) -> None:
    name, new = change.field, change.new
    if name == "year":
        old = str(data.get("date") or "")
        data["date"] = re.sub(r"\d{4}", new, old, count=1) if re.search(r"\d{4}", old) else new
    elif name == "DOI" and "DOI" not in data and data.get("itemType") not in AUDITED_TYPES:
        # Every audited type has a DOI field (Zotero 7); older types keep it in Extra.
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

    def finding_notes(self, parent: str) -> list[dict]:
        """The notes of an earlier attachment finding ("PDF to check", "Attached PDF to check")."""
        out = []
        for child in self.zot.children(parent):
            d = child.get("data", {})
            text = re.sub(r"<[^>]+>", "", d.get("note") or "")[:60]
            if d.get("itemType") == "note" and re.match(r"\s*(Attached )?PDF to check\b", text):
                out.append(child)
        return out

    def trash(self, child: dict) -> None:
        self._helpers.trash_item(self.zot, child)

    def move_attachment(self, attachment_key: str, new_parent: str) -> None:
        att = self.zot.item(attachment_key)
        att["data"]["parentItem"] = new_parent
        self.zot.update_item(att)

    def item_pages(self, key: str) -> str:
        return str(self.zot.item(key).get("data", {}).get("pages") or "")

    def attach_file(self, parent: str, path: str, title: str) -> str | None:
        ok, detail, att_key = self._helpers._attach_and_verify(self.zot, title, path, parent, self.ctx,
                                                               content_type="application/pdf")
        if not ok:
            raise RuntimeError(detail)
        return att_key

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
        from zotero_mcp.local_db import get_serial_reader

        reader = get_serial_reader()
    except Exception:
        reader = None

    def text(key: str) -> str:
        if reader is None:
            return ""
        for att in reader.get_attachment_paths(key):
            path = att.get("resolved_path")
            if att.get("exists") and path and str(path).lower().endswith(".pdf"):
                from zotero_mcp import structure

                pages = structure.read_first_pages(path, 4, 0)
                return "\n".join(t for _p, t in (pages or {}).get("texts") or [])
        return ""

    return text


PDF_PROMPT = """Below is the text of the first pages of a PDF from a researcher's library (a cover sheet from a
repository or database may come first; its citation data counts too). Give the bibliographic data of the
document itself as printed there. Leave a field empty when it is not printed; never guess.

- authors: every author in byline order, as "Given Family" (not only the corresponding author).
- year: the year of the issue or publication in the citation line; for a book the year of this edition
  (its copyright year, not a reprint or an earlier edition).
- pages: the document's own printed page range ("68-78") or article number ("e70024").
- version: "published" for the typeset version of record, "accepted manuscript" or "preprint" when the PDF
  says so or is clearly an author's manuscript, else "unknown".
- book_title, editors: for a chapter in an edited book; university: for a thesis.

Text:
{text}
"""

PDF_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "document": {"type": "STRING", "enum": ["journal article", "book chapter", "book", "thesis", "report",
                                                 "conference paper", "preprint", "other"]},
        "version": {"type": "STRING", "enum": ["published", "accepted manuscript", "preprint", "unknown"]},
        "title": {"type": "STRING"}, "authors": {"type": "ARRAY", "items": {"type": "STRING"}},
        "year": {"type": "STRING"}, "journal": {"type": "STRING"}, "volume": {"type": "STRING"},
        "issue": {"type": "STRING"}, "pages": {"type": "STRING"}, "doi": {"type": "STRING"},
        "book_title": {"type": "STRING"}, "editors": {"type": "ARRAY", "items": {"type": "STRING"}},
        "publisher": {"type": "STRING"}, "isbn": {"type": "STRING"}, "university": {"type": "STRING"},
    },
    "required": ["document", "version", "title"],
}


_META_RE = re.compile(r"<meta\s+[^>]*?(?:name|property)\s*=\s*[\"']([^\"']+)[\"'][^>]*?content\s*=\s*[\"']([^\"']*)[\"']"
                      r"|<meta\s+[^>]*?content\s*=\s*[\"']([^\"']*)[\"'][^>]*?(?:name|property)\s*=\s*[\"']([^\"']+)[\"']",
                      re.I)


def page_meta(page_html: str) -> dict:
    """The citation data publishers put in a page's head (Highwire ``citation_*`` and Dublin Core tags)."""
    tags: dict[str, list[str]] = {}
    for m in _META_RE.finditer(page_html[:400000]):
        name = (m.group(1) or m.group(4) or "").strip().lower()
        value = html.unescape(m.group(2) if m.group(1) else m.group(3) or "").strip()
        if name and value:
            tags.setdefault(name, []).append(value)

    def first(*names: str) -> str:
        for n in names:
            if tags.get(n):
                return tags[n][0]
        return ""

    doi = first("citation_doi", "dc.identifier", "prism.doi")
    m = re.search(r"10\.\d{4,9}/\S+", doi)
    year = re.search(r"\d{4}", first("citation_publication_date", "citation_date", "citation_online_date",
                                     "dc.date", "prism.publicationdate"))
    fp, lp = first("citation_firstpage"), first("citation_lastpage")
    return {
        "title": first("citation_title", "dc.title"), "doi": m.group(0) if m else "",
        "authors": tags.get("citation_author") or tags.get("dc.creator") or [],
        "year": year.group(0) if year else "",
        "journal": first("citation_journal_title", "prism.publicationname"),
        "volume": first("citation_volume", "prism.volume"), "issue": first("citation_issue", "prism.number"),
        "pages": f"{fp}-{lp}" if fp and lp else fp, "publisher": first("citation_publisher", "dc.publisher"),
        "isbn": first("citation_isbn"), "book_title": first("citation_inbook_title", "citation_book_title"),
        "version": "published",
    }


def page_meta_reader() -> Callable[[str], dict]:
    """Citation data of the item's saved web page (an HTML snapshot), read from the file."""
    try:
        from zotero_mcp.local_db import get_serial_reader

        reader = get_serial_reader()
    except Exception:
        reader = None

    def read(key: str) -> dict:
        if reader is None:
            return {}
        for att in reader.get_attachment_paths(key) or []:
            path = att.get("resolved_path")
            ctype = (att.get("content_type") or "").lower()
            if att.get("exists") and path and ("html" in ctype or str(path).lower().endswith((".html", ".htm"))):
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        return page_meta(f.read(400000))
                except OSError:
                    continue
        return {}

    return read


def pdf_prompt(path: str | Path) -> str | None:
    """The prompt for Gemini's reading of a PDF's first pages; None when they have no text."""
    from zotero_mcp import structure

    pages = structure.read_first_pages(path, 4, 15)
    if not pages or not pages.get("texts"):
        return None
    text = "\n\n".join(f"[PDF page {p}]\n{t}" for p, t in pages["texts"])[:40000]
    if len(text.strip()) < 200:
        return None
    return PDF_PROMPT.format(text=text)


def _pdf_gemini_reader(model: str | None = None, config_path: str | None = None,
                       log: Callable[[str], None] = print) -> Callable[[str], dict | None] | None:
    """Gemini reading the first pages of an item's PDF; answers cached per file.

    None when Gemini is unavailable (no key, no package).
    """
    from zotero_mcp import gemini_util, structure

    cfg = gemini_util.load_semantic_config(config_path)
    cfg_model, thinking = gemini_util.model_settings(cfg)
    model = model or cfg_model
    try:
        raw_ask = gemini_util.json_asker(model, PDF_SCHEMA, cfg.get("embedding_config"), thinking=thinking)
    except Exception:
        return None
    failures: list[str] = []

    def ask(prompt: str) -> str:
        try:
            return raw_ask(prompt)
        except Exception as e:
            if not failures:
                log(f"Gemini ({model}) failed: {type(e).__name__}: {str(e)[:200]}; PDFs are checked by rules only.")
            failures.append(type(e).__name__)
            raise
    try:
        from zotero_mcp.local_db import get_serial_reader

        reader = get_serial_reader()
    except Exception:
        reader = None
    if reader is None:
        return None
    cache = meta_dir() / "pdf-cache"

    def read(key: str) -> dict | None:
        for att in reader.get_attachment_paths(key):
            path = att.get("resolved_path")
            if not (att.get("exists") and path and str(path).lower().endswith(".pdf")):
                continue
            sig = structure.file_signature(path)
            hit = cache / f"{sig}.json"
            try:
                return json.loads(hit.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            prompt = pdf_prompt(path)
            if prompt is None:
                return None
            data = gemini_util.ask_json(ask, prompt)
            if isinstance(data, dict):
                try:
                    cache.mkdir(parents=True, exist_ok=True)
                    hit.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
                return data
            return None
        return None

    return read


def resolve(writer, raw: dict, accept: set[str] | list[str], reject: set[str] | list[str], state: dict,
            log: Callable[[str], None] = print) -> tuple[int, int, int]:
    """Accept some of an item's suggestions (by field) and reject others; the rest stay waiting
    in a new note. Returns (applied, rejected, left)."""
    key = raw.get("key") or raw.get("data", {}).get("key")
    label = ff.ItemInfo.from_zotero(raw).label
    notes = writer.proposal_notes(key)
    by_field: dict[str, Change] = {}
    for n in notes:
        for c in parse_proposals(n["data"].get("note", "")):
            by_field[c.field] = c                    # one suggestion per field (the newest note wins)
    changes = list(by_field.values())
    take = [c for c in changes if c.field in set(accept)]
    drop = [c for c in changes if c.field in set(reject) and c not in take]
    left = [c for c in changes if c not in take and c not in drop]
    audit = ItemAudit(key, label, raw.get("data", {}).get("itemType", ""))
    writer.apply(audit, take, tags_add=([TAG_CORRECTED] if take else []) + ([TAG_REVIEW] if left else []),
                 tags_remove=[TAG_ACCEPT, TAG_REJECT] + ([] if left else [TAG_REVIEW]))
    if take:
        writer.add_note(key, _note_html(f"Metadata changes you accepted ({_today()})",
                                        audit, take, proposals=False))
    rejected = state.setdefault(key, {}).setdefault("rejected", {})
    for c in drop:
        rejected[c.field] = c.new
    stats = state.setdefault("_learned", {})
    for c, verdict in [(c, "accepted") for c in take] + [(c, "rejected") for c in drop]:
        stats.setdefault(category(c), {"accepted": 0, "rejected": 0})[verdict] += 1
    for n in notes:
        writer.trash(n)
    if left:
        writer.add_note(key, _note_html(f"Proposed metadata changes ({_today()})", audit, left, proposals=True))
    log(f"{label} [{key}]: {len(take)} applied, {len(drop)} discarded"
        + (f", {len(left)} still to review" if left else ""))
    return len(take), len(drop), len(left)


def decide(writer, raw: dict, accept: bool, state: dict, fields: list[str] | None = None,
           log: Callable[[str], None] = print) -> int:
    """Apply (accept) or discard all of an item's proposals. ``fields`` limits the
    acceptance to those fields; the item's other proposals are rejected."""
    key = raw.get("key") or raw.get("data", {}).get("key")
    every = {c.field for n in writer.proposal_notes(key) for c in parse_proposals(n["data"].get("note", ""))}
    take = {f for f in every if accept and (not fields or f in fields)}
    return resolve(writer, raw, take, every - take, state, log=log)[0]


class ReviewSession:
    """The suggestions waiting for review, for the review window: load them, then accept or
    reject them one by one."""

    def __init__(self, writer=None, backend=None):
        self._writer, self._backend = writer, backend
        self.raws: dict[str, dict] = {}

    @property
    def writer(self):
        if self._writer is None:
            self._writer = MetadataWriter()
        return self._writer

    @property
    def backend(self):
        if self._backend is None:
            from zotero_mcp import library

            self._backend = library.get_library_backend()
        return self._backend

    def load(self, keys: list[str] | None = None, collection: str | None = None) -> list[tuple[str, str, list[Change]]]:
        if collection and not keys:
            keys = [i.get("key") or i.get("data", {}).get("key") for i in self.backend.collection_items(collection) or []
                    if TAG_REVIEW in {t.get("tag") for t in i.get("data", {}).get("tags") or []}]
            if not keys:
                return []
        out = []
        for raw, changes in review_list(self.writer, self.backend, keys):
            key = raw.get("key") or raw.get("data", {}).get("key")
            self.raws[key] = raw
            by_field = {c.field: c for c in changes}
            out.append((key, ff.ItemInfo.from_zotero(raw).label, list(by_field.values())))
        return out

    def decide(self, key: str, accept: list[str], reject: list[str]) -> tuple[int, int, int]:
        state = _load_state()
        try:
            return resolve(self.writer, self.raws[key], accept, reject, state, log=lambda m: None)
        finally:
            _save_state(state)


def process_review(writer, backend, log: Callable[[str], None] = print) -> dict[str, int]:
    """Apply or discard proposals on items tagged metadata/accept or /reject."""
    counts = {"accepted": 0, "rejected": 0}
    state = _load_state()
    for tag, accept in ((TAG_ACCEPT, True), (TAG_REJECT, False)):
        done: set[str] = set()
        for raw in backend.list_items(None, limit=10000, tag=[tag]):
            data = raw.get("data", raw)
            if data.get("itemType") == "note":
                # The tag on the suggestions note itself (natural to do): it means its paper.
                parent = data.get("parentItem")
                if not parent or NOTE_MARK not in (data.get("note") or ""):
                    continue
                raw = (backend.get_items([parent]) or {}).get(parent)
                if raw is None:
                    continue
            key = raw.get("key") or raw.get("data", {}).get("key")
            if key in done:
                continue
            done.add(key)
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
    #: Papers still waiting for review from earlier checks: (key, label).
    earlier: list[tuple[str, str]] = field(default_factory=list)

    def totals(self) -> dict[str, int]:
        t = {"items": len(self.audits), "filled": 0, "corrected": 0, "proposals": 0, "items_to_review": 0,
             "flags": 0, "no_source": 0, "errors": 0, "retracted": 0, "not_checked": 0, "attachments": 0,
             "other_versions": 0}
        for a in self.audits:
            t["filled"] += len(a.by_kind("fill"))
            t["corrected"] += len(a.by_kind("correct"))
            t["proposals"] += len(a.by_kind("propose"))
            t["items_to_review"] += bool(a.by_kind("propose"))
            t["flags"] += len(a.flags)
            t["no_source"] += any("no registry record" in f for f in a.flags)
            t["not_checked"] += any(f.startswith(NOT_CHECKED) for f in a.flags)
            t["attachments"] += a.attachment is not None and a.attachment.wrong
            t["other_versions"] += a.attachment is not None and not a.attachment.wrong
            t["errors"] += bool(a.error)
            t["retracted"] += a.retracted
        return t

    def markdown(self, limit: int | None = None) -> str:
        """The report: a summary, then what needs you (suggestions, wrong PDFs, papers not
        checked or unknown, other findings), then what was changed. ``limit``: papers per section."""
        t = self.totals()
        done = self.applied
        started = self.started
        try:
            started = _dt.datetime.strptime(self.started, "%Y-%m-%d %H:%M").strftime("%d-%m-%Y %H:%M")
        except ValueError:
            pass
        changed = [a for a in self.audits if a.by_kind("fill") or a.by_kind("correct")]
        review = [a for a in self.audits if a.by_kind("propose")]
        wrong = [a for a in self.audits if a.attachment is not None and a.attachment.wrong]
        older = [a for a in self.audits if a.attachment is not None and not a.attachment.wrong]
        not_checked = [a for a in self.audits if any(f.startswith(NOT_CHECKED) for f in a.flags)]
        unknown = [a for a in self.audits if any("no registry record" in f for f in a.flags)]
        errors = [a for a in self.audits if a.error]

        def n(count: int, word: str) -> str:
            return f"{count} {word}{'' if count == 1 else 's'}"

        def short(a: ItemAudit) -> str:
            return a.label.split(") ", 1)[0] + ")" if ") " in a.label else a.label

        def more(n: int) -> list[str]:
            return [f"- … {n} more in the report file"] if n > 0 else []

        def cut(items: list) -> tuple[list, int]:
            return (items[:limit], len(items) - limit) if limit else (items, 0)

        lines = [f"# Metadata check, {started}", ""]
        if not done:
            lines += ["Report only: nothing was changed.", ""]
        lines.append(f"**{t['items']} paper{'s' if t['items'] != 1 else ''} checked.**")
        lines.append("")
        fixes = f", {t['corrected']} corrected" if t["corrected"] else ""
        summary = [
            (f"{'Changed' if done else 'To change'} automatically: {n(t['filled'], 'empty field')} filled{fixes}"
             f" on {n(len(changed), 'paper')}" if changed else ""),
            (f"For you to review: {n(t['proposals'], 'suggested change')} on {n(len(review), 'paper')}"
             + (f", and {n(len(self.earlier), 'paper')} from earlier checks" if self.earlier else "")
             if review else
             f"Still to review from earlier checks: {n(len(self.earlier), 'paper')}" if self.earlier else ""),
            (f"Wrong PDFs: {len(wrong)} (tag {ff.TAG_CHECK_PDF})" if wrong else ""),
            (f"Right paper, other form: {len(older)} (accepted manuscript, preprint, proof or the whole book; "
             "tagged, nothing to check)" if older else ""),
            (f"Retracted: {t['retracted']} (tag {TAG_RETRACTED})" if t["retracted"] else ""),
            (f"Not checked: {len(not_checked)}, because a registry did not answer; run again later"
             if not_checked else ""),
            (f"Unknown to every registry: {len(unknown)}" if unknown else ""),
            (f"Errors: {len(errors)}" if errors else ""),
        ]
        lines += [f"- {x}" for x in summary if x] + [""]
        if self.review_counts and any(self.review_counts.values()):
            lines += [f"Your review decisions applied: {self.review_counts.get('accepted', 0)} accepted, "
                      f"{self.review_counts.get('rejected', 0)} rejected.", ""]

        def change_line(c: Change) -> str:
            label = FIELD_LABELS.get(c.field, c.field)
            src = ", ".join(c.sources)
            why = f" — {c.why}" if c.why else ""
            if c.field == "creators" and c.kind == "fill":
                return f"- {c.why[:1].upper()}{c.why[1:]} ({src})"
            if c.field == "abstractNote" and not c.old:
                return f"- Abstract added ({src})"
            if not c.old:
                return f"- {label}: {c.new[:120]} ({src}){why}"
            return f"- {label}: {c.old[:120]} → {c.new[:120]} ({src}){why}"

        how = ("Accept or reject with a click: **Review** in the progress window, or in Zotero right-click "
               "› *Review suggested metadata* (Tools menu: all papers).")
        if review:
            lines += [f"## To review ({len(review)})", "", how, ""]
            shown, rest = cut(review)
            for a in shown:
                lines.append(f"### {a.label} [{a.key}]")
                lines += [change_line(c) for c in a.by_kind("propose")] + [""]
            lines += more(rest)
        if self.earlier:
            lines += [f"## Still to review from earlier checks ({len(self.earlier)})", ""]
            if not review:
                lines += [how, ""]
            shown, rest = cut(self.earlier)
            lines += [f"- {label} [{key}]" for key, label in shown] + more(rest) + [""]
        if wrong:
            lines += [f"## Wrong PDFs ({len(wrong)})", ""]
            shown, rest = cut(wrong)
            for a in shown:
                p = a.attachment
                what = {"another work": "another paper", "manuscript": "the accepted manuscript",
                        "preprint": "a preprint", "proof": "a proof", "whole book": "the whole book"}.get(p.kind, p.kind)
                extra = (f" ({p.detail.removeprefix('its first pages show ')})" if p.kind == "another work"
                         else f" ({p.detail})" if p.kind == "whole book" else "")
                lines.append(f"- {a.label} [{a.key}]: {what}{extra}"
                             + (f", which belongs to item {p.other_item}" if p.other_item else ""))
            lines += more(rest) + [""]
        if older:
            lines += [f"## Right paper, other form ({len(older)})", ""]
            shown, rest = cut(older)
            for a in shown:
                lines.append(f"- {a.label} [{a.key}]: {_PDF_PROBLEM.get(a.attachment.kind, a.attachment.kind)}")
            lines += more(rest) + [""]
        for title, group, why in ((f"Not checked ({len(not_checked)})", not_checked, None),
                                  (f"Unknown to every registry ({len(unknown)})", unknown, None),
                                  (f"Errors ({len(errors)})", errors, "error")):
            if not group:
                continue
            lines += [f"## {title}", ""]
            shown, rest = cut(group)
            for a in shown:
                lines.append(f"- {a.label} [{a.key}]" + (f": {a.error}" if why else ""))
            lines += more(rest) + [""]

        # Other findings, grouped: "Article without a DOI (8): Gray (2011), ..."
        groups: dict[str, list[ItemAudit]] = {}
        for a in self.audits:
            for f in a.flags:
                if (f.startswith(NOT_CHECKED) or "no registry record" in f or f.startswith("attachment: ")
                        or f.startswith("RETRACTED")):
                    continue
                groups.setdefault(f, []).append(a)
        if groups:
            lines += ["## Other findings", ""]
            for f, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                text = f[:1].upper() + f[1:]
                if len(group) == 1:
                    lines.append(f"- {short(group[0])} [{group[0].key}]: {f}")
                else:
                    names = ", ".join(f"{short(a)} [{a.key}]" for a in group[: limit or len(group)])
                    lines.append(f"- {text} ({len(group)}): {names}")
            lines.append("")

        if changed:
            lines += [f"## {'Changed' if done else 'To change'} automatically ({len(changed)})", ""]
            shown, rest = cut(changed)
            for a in shown:
                lines.append(f"### {a.label} [{a.key}]")
                lines += [change_line(c) for c in a.by_kind("fill") + a.by_kind("correct")] + [""]
            lines += more(rest)
        if self.report_path:
            lines += ["", f"Full report: {self.report_path}"]
        return "\n".join(lines).rstrip() + "\n"

_PDF_PROBLEM = {"another work": "the PDF is another paper", "manuscript": "accepted manuscript",
                "preprint": "preprint", "proof": "proof (page numbers not final)",
                "whole book": "the whole book (the chapter is in it)"}


def audit_event(audit: ItemAudit, applied: bool) -> dict:
    """An item's outcome for the progress window: a status (ok, updated, review, wrong pdf,
    no record, not checked, retracted, error) and a line saying what was found or done."""
    fills, fixes, props = (len(audit.by_kind(k)) for k in ("fill", "correct", "propose"))
    no_record = any("no registry record" in f for f in audit.flags)
    not_checked = any(f.startswith(NOT_CHECKED) for f in audit.flags)
    parts = []
    if fills:
        parts.append(f"{fills} filled" if applied else f"{fills} to fill")
    if fixes:
        parts.append(f"{fixes} corrected" if applied else f"{fixes} to correct")
    if props:
        parts.append(f"{props} to review")
    if audit.attachment is not None:
        parts.append(_PDF_PROBLEM.get(getattr(audit.attachment, "kind", ""), "the PDF needs a check"))
    wrong_pdf = audit.attachment is not None and getattr(audit.attachment, "wrong", True)
    if audit.retracted:
        parts.append("retracted")
    if no_record and not (fills or fixes or props):
        parts.append("no registry knows it")
    if audit.error:
        status, parts = "error", [audit.error[:120]]
    elif not_checked:
        status, parts = "not checked", ["not checked: a registry did not answer"]
    elif audit.retracted:
        status = "retracted"
    elif wrong_pdf:
        status = "wrong pdf"
    elif props:
        status = "review"
    elif fills or fixes:
        status = "updated"
    elif no_record:
        status = "no record"
    elif audit.attachment is not None:
        status = "other version"        # the right paper in another form (manuscript, proof, whole book)
    else:
        status, parts = "ok", ["OK"]
    return {"key": audit.key, "label": audit.label, "phase": "metadata", "status": status,
            "detail": ", ".join(parts), "changed": bool(fills or fixes) and status != "error"}


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
    gemini: bool | None = None,
    pdf_read: Callable[[str], dict | None] | None = None,
    check_attachments: bool = True,
    pdfs: Callable[[str], list[dict]] | None = None,
    fetch: Callable[..., Any] | None = None,
    fetch_replacements: bool = True,
    progress: Callable[[dict], None] | None = None,
) -> AuditReport:
    """Audit the items. ``progress`` receives an event per item ({"key", "label", "phase":
    "metadata", "status", "detail"}), for the progress window."""
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
    if pdf_read is None and gemini is not False and pdf_text is None:
        pdf_read = _pdf_gemini_reader(log=log)
        if pdf_read is None and gemini:
            log("Gemini is not available (key or package missing); the PDF is checked by rules only.")
    ctx = Context(http, settings, pdf_text or _pdf_text_reader(),
                  {k: v.get("rejected", {}) for k, v in state.items() if isinstance(v, dict) and k != "_learned"},
                  pdf_read=pdf_read,
                  scholar=(lambda info, _b=ff.Budget(): scholar_cite(info, http, settings, _b))
                  if settings.has("serpapi") and pdf_text is None else None,
                  learned=state.get("_learned") or {})
    ctx.log = log
    if pdf_text is None:
        ctx.page_meta = page_meta_reader()
    if check_attachments and (pdfs is not None or pdf_text is None):
        from zotero_mcp import attachment_check

        ctx.pdfs = pdfs or attachment_check.pdf_reader()
        try:
            whole = items if not (keys or collection) else backend.list_items("-attachment", limit=100000)
            ctx.index = attachment_check.library_index(whole)
        except Exception:
            ctx.index = attachment_check.library_index(items)
    log(f"{len(items)} item(s) to check{'' if apply else ' (report only)'}"
        f"{', with their PDFs' if ctx.pdfs is not None else ''}.")

    notify = progress or (lambda event: None)
    for raw in items:
        info = ff.ItemInfo.from_zotero(raw)
        notify({"key": info.key, "label": info.label, "phase": "metadata", "status": "waiting", "detail": ""})

    def one(raw):
        info = ff.ItemInfo.from_zotero(raw)
        notify({"key": info.key, "label": info.label, "phase": "metadata", "status": "checking", "detail": ""})
        try:
            return audit_item(raw, ctx)
        except Exception as e:
            info = ff.ItemInfo.from_zotero(raw)
            return ItemAudit(info.key, info.label, info.item_type, error=f"{type(e).__name__}: {e}")

    audits: list[ItemAudit] = []
    to_fetch: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for n, audit in enumerate(pool.map(one, items), 1):
            audits.append(audit)
            fills, fixes, props = (len(audit.by_kind(k)) for k in ("fill", "correct", "propose"))
            done = writer is not None and not audit.error
            summary = ", ".join(s for s in (
                f"{fills} {'filled' if done else 'to fill'}" if fills else "",
                f"{fixes} {'corrected' if done else 'to correct'}" if fixes else "",
                f"{props} to review" if props else "", f"{len(audit.flags)} note(s)" if audit.flags else "",
                audit.error and f"error: {audit.error}") if s) or "ok"
            log(f"[{n}/{len(items)}] {audit.label} [{audit.key}]: {summary}")
            if writer is not None and not audit.error:
                _write(writer, audit, log)
                if audit.attachment is not None and not audit.error:
                    if _fix_attachment(writer, audit, log, pdfs=ctx.pdfs):
                        to_fetch.append(audit.key)
                elif audit.attachment_ok:
                    try:
                        writer.apply(audit, [], tags_add=[], tags_remove=[ff.TAG_CHECK_PDF])
                        # The earlier finding was wrong: its note goes (to the trash), no new note.
                        for old_note in writer.finding_notes(audit.key):
                            writer.trash(old_note)
                        ff.clear_bad_pdf(audit.key)
                        log("    -> the attached PDF is right after all; tag fulltext/check-pdf and its note removed")
                    except Exception as e:
                        log(f"    -> could not remove the check-pdf tag: {type(e).__name__}: {e}")
            notify(audit_event(audit, applied=writer is not None))
            if not audit.error and not any(f.startswith(NOT_CHECKED) for f in audit.flags):
                entry = state.setdefault(audit.key, {})
                entry["last_audit"] = _dt.datetime.now().isoformat(timespec="seconds")
                if audit.notices is not None:
                    entry["notices_checked"] = _dt.date.today().isoformat()
                    entry["notices"] = sorted(set(entry.get("notices") or []) | set(audit.notices))
            if n % 50 == 0:
                _save_state(state)
    _save_state(state)
    if to_fetch and fetch_replacements:
        # The right PDF for items whose attachment is wrong: it replaces the wrong one once found.
        log(f"Looking for the right PDF of {len(to_fetch)} item(s) ...")
        try:
            (fetch or ff.run)(keys=to_fetch, log=log, retry=True)
        except Exception as e:
            log(f"  the search for the right PDFs failed: {type(e).__name__}: {e}")
    from zotero_mcp.gemini_util import usage_summary

    if (usage := usage_summary()):
        log(f"Gemini (reading PDFs): {usage}")
    report = AuditReport(audits, bool(apply), started, review_counts)
    if writer is not None and any(a.by_kind("propose") for a in audits):
        report.saved_search = writer.ensure_saved_search()
    if writer is not None:
        # The overview in the report: papers still waiting from earlier checks (one request).
        try:
            here = {a.key for a in audits}
            for raw in backend.list_items(None, limit=10000, tag=[TAG_REVIEW]) or []:
                key = raw.get("key") or raw.get("data", {}).get("key")
                if key not in here:
                    report.earlier.append((key, ff.ItemInfo.from_zotero(raw).label))
        except Exception:
            pass
    try:
        runs = meta_dir() / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        path = runs / f"{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        path.write_text(report.markdown(), encoding="utf-8")
        report.report_path = str(path)
    except OSError:
        pass
    return report


def _today() -> str:
    return _dt.date.today().strftime("%d-%m-%Y")


#: A right paper, but not the published version: a tag, no warning.
_VERSION_TAG = {"manuscript": ff.VERSION_TAGS["accepted"], "preprint": ff.VERSION_TAGS["preprint"],
                "proof": ff.TAG_PROOF}

_PROBLEM_NOTE = {
    "another work": "another paper is attached",
    "manuscript": "this is the accepted manuscript, not the published version",
    "preprint": "this is a preprint, not the published version",
    "proof": "this is a proof (page numbers not final), not the published version",
    "whole book": "the whole book is attached, not just this chapter",
}


def _fix_attachment(writer, audit: ItemAudit, log: Callable[[str], None],
                    pdfs: Callable[[str], list[dict]] | None = None) -> bool:
    """Act on a wrong attachment; True when the fetcher should look for the right PDF."""
    from zotero_mcp import attachment_check

    problem = audit.attachment
    if problem.kind in _VERSION_TAG:
        # The right paper in an earlier version: tagged (no note, nothing to check); a fetch quietly
        # looks for the published version and swaps it in when found.
        tag = _VERSION_TAG[problem.kind]
        try:
            remove = [ff.TAG_CHECK_PDF] if ff.TAG_CHECK_PDF in audit.tags else []
            if tag not in audit.tags or remove:
                writer.apply(audit, [], tags_add=[tag], tags_remove=remove)
            ff.mark_bad_pdf(audit.key, problem.attachment_key, problem.kind, want_published=True)
        except Exception as e:
            log(f"    -> could not tag the attachment: {type(e).__name__}: {e}")
            return False
        return True
    if ff.TAG_CHECK_PDF in audit.tags and problem.kind != "whole book":
        return True     # noted in an earlier run: only look for the right PDF again
    seen = f" ({problem.detail})" if problem.kind in ("another work", "whole book") else ""
    note = [f"<p><b>PDF to check ({_today()})</b>: {_PROBLEM_NOTE.get(problem.kind, problem.kind)}"
            f"{html.escape(seen)}.</p>"]
    fetch = False
    try:
        if problem.kind == "another work":
            other = problem.other_item
            if other and pdfs is not None and not pdfs(other):
                writer.move_attachment(problem.attachment_key, other)
                writer.add_note(other, f"<p>zotero-mcp moved this PDF here from item {audit.key} "
                                       f"({html.escape(audit.label)}), where it was attached by mistake.</p>")
                note.append(f"<p>It belongs to item {other}, which had no PDF, and was moved there.</p>")
            else:
                ff.mark_bad_pdf(audit.key, problem.attachment_key, problem.kind, want_published=False)
                note.append("<p>'Check & complete' or 'Fetch PDF only' looks for the right one; once found, it "
                            "replaces this PDF (which goes to Zotero's trash).</p>")
            fetch = True
        elif problem.kind == "whole book":
            # The right content, with the rest of the book around it: the chapter is cut out when
            # its printed pages can be found; otherwise the item is only tagged.
            import tempfile

            ff.clear_bad_pdf(audit.key)     # an earlier run may have taken the book for another work
            if ff.TAG_WHOLE_BOOK in audit.tags:
                return False                # tried before
            folder = tempfile.mkdtemp(prefix="zmcp-chapter-")
            out = os.path.join(folder, "chapter.pdf")
            span = attachment_check.extract_chapter(problem.path, writer.item_pages(audit.key), out)
            remove = [ff.TAG_CHECK_PDF] if ff.TAG_CHECK_PDF in audit.tags else []
            if span:
                writer.attach_file(audit.key, out, f"Chapter PDF (from the book, PDF pages {span[0]}-{span[1]})")
                writer.apply(audit, [], tags_add=[], tags_remove=remove)
                writer.add_note(audit.key, f"<p>Chapter cut out of the attached book ({_today()}): PDF pages "
                                           f"{span[0]}-{span[1]}, attached as its own PDF. The book stays.</p>")
                log(f"    -> attached the chapter cut from the book (PDF pages {span[0]}-{span[1]})")
            else:
                writer.apply(audit, [], tags_add=[ff.TAG_WHOLE_BOOK], tags_remove=remove)
                log("    -> the whole book is attached; the chapter's pages were not found in it")
            return False
        writer.apply(audit, [], tags_add=[ff.TAG_CHECK_PDF])
        writer.add_note(audit.key, "".join(note))
        log(f"    -> {problem.describe()}")
    except Exception as e:
        log(f"    -> could not act on the attachment: {type(e).__name__}: {e}")
        return False
    return fetch


def _write(writer, audit: ItemAudit, log: Callable[[str], None]) -> None:
    auto = audit.by_kind("fill") + audit.by_kind("correct")
    props = audit.by_kind("propose")
    try:
        if audit.retracted:
            writer.apply(audit, [], tags_add=[TAG_RETRACTED])
        if auto:
            tags = ([TAG_FILLED] if audit.by_kind("fill") else []) + ([TAG_CORRECTED] if audit.by_kind("correct") else [])
            writer.apply(audit, auto, tags_add=tags)
            writer.add_note(audit.key, _note_html(f"Metadata changes by zotero-mcp ({_today()})",
                                                  audit, auto, proposals=False))
        if props:
            for old in writer.proposal_notes(audit.key):
                writer.trash(old)
            writer.apply(audit, [], tags_add=[TAG_REVIEW])
            writer.add_note(audit.key, _note_html(f"Proposed metadata changes ({_today()})",
                                                  audit, props, proposals=True))
    except Exception as e:
        audit.error = f"writing failed: {type(e).__name__}: {e}"
        log(f"    -> {audit.error}")

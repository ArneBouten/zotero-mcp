"""Is the attached PDF the item itself, and the published version?

Part of the metadata audit. For every item with a PDF the first pages are read
(free, in a child process) and compared with the item:

- **another work**: neither the item's DOI nor its title is on the first pages
  (and Gemini, when available, reads another title there);
- **manuscript / preprint**: the PDF says it is an accepted manuscript or a
  preprint, while the item is a published article;
- **proof**: page numbers "000-000", "uncorrected proof", volume "XX";
- **whole book**: a chapter's item with the whole book attached.

Supplementary files are left alone: when one of an item's PDFs matches it, the
others are not questioned. With ``--apply`` the audit tags the item
``fulltext/check-pdf``, adds a note, moves a PDF that belongs to another item
without a PDF to that item, cuts a chapter out of an attached book, and has the
fetcher look for the right PDF, which replaces the wrong one (the wrong one goes
to Zotero's trash) once it is found and checked.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from zotero_mcp import fulltext_fetch as ff

TAG_CHECK_PDF = "fulltext/check-pdf"

_MANUSCRIPT_RE = re.compile(
    r"author(?:['’]s|s['’])? accepted manuscript|accepted author manuscript|\baccepted manuscript\b|"
    r"this is (?:an|the) (?:author['’]?s? )?(?:accepted|peer[- ]reviewed|pre-?print|final draft|post-?print)|"
    r"\bpost-?print\b|author['’]?s? final (?:draft|version)|has been accepted for publication in", re.I)
_PREPRINT_RE = re.compile(r"\bpre-?print\b(?! server)|not (?:yet )?(?:been )?peer[- ]reviewed|"
                          r"\b(?:psyarxiv|biorxiv|medrxiv|arxiv|osf preprints|ssrn)\b", re.I)
_PROOF_RE = re.compile(r"\b000\s*[–—-]\s*000\b|\buncorrected (?:page )?proofs?\b|\bvol(?:ume)?\.?\s*X{2,}\b|"
                       r"\bvolume\s+\d+\s*,?\s*issue\s+X+\b", re.I)
#: Sentences that mention a manuscript without the PDF being one: Taylor & Francis's open-access
#: licence ("allow the posting of the Accepted Manuscript in a repository"), an old journal's
#: "Accepted manuscript received 1 September 1974", a repository's general cover text.
_NOT_A_STATEMENT_RE = re.compile(
    r"allows? the posting of the accepted manuscript[^.]*\.?|accepted manuscript received\b|"
    r"(?:author accepted manuscripts\s+)?if this document is identified as the author accepted manuscript[^.]*\.?",
    re.I)
#: A publisher's own cover or first page: the published version.
_PUBLISHED_RE = re.compile(r"to cite this article:|to link to this article:|journal homepage:|published online:|"
                           r"full terms & conditions of access", re.I)
#: The PDF says outright what it is ("This is an Accepted Manuscript of an article published by ...").
_SAYS_MANUSCRIPT_RE = re.compile(r"this is (?:an?|the) (?:author['’]?s? )?(?:peer[- ]reviewed, )?"
                                 r"(?:accepted manuscript|post-?print|accepted version)", re.I)
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.I)


@dataclass
class Problem:
    kind: str                     # another work / manuscript / preprint / proof / whole book
    attachment_key: str
    path: str
    detail: str
    found_title: str = ""
    found_doi: str = ""
    other_item: str = ""          # the library item the PDF belongs to, when it is another work

    @property
    def wrong(self) -> bool:
        """Another work: an error. A manuscript, preprint, proof or the whole book around a chapter
        is the right paper in another form."""
        return self.kind == "another work"

    def describe(self) -> str:
        what = {
            "another work": "the attached PDF is another work",
            "manuscript": "the attached PDF is the accepted manuscript, not the published version",
            "preprint": "the attached PDF is a preprint, not the published version",
            "proof": "the attached PDF is a proof (page numbers not final)",
            "whole book": "the whole book is attached to this chapter",
            "book beside chapter": "the whole book is attached beside the chapter's own PDF",
        }[self.kind]
        return f"{what}: {self.detail}" + (f" (it belongs to item {self.other_item})" if self.other_item else "")


def _content_words(text: str) -> list[str]:
    return [w for w in ff._fold(text).split() if len(w) >= 4 and w not in ff._STOPWORDS]


def title_on_pages(title: str, text: str) -> bool:
    """The item's title (or its main title) printed on the first pages, allowing line breaks and hyphens."""
    folded = " " + ff._fold(text) + " "
    main = re.split(r"[:?!.]\s", title or "", maxsplit=1)[0]
    for candidate in (title, main):
        words = ff._fold(candidate).split()[:10]
        if len(words) >= 3 and " " + " ".join(words) + " " in folded:
            return True
    words = _content_words(main)
    if len(words) < 3:
        return False
    present = sum(1 for w in words if f" {w} " in folded)
    return present / len(words) >= 0.8


def _doi_on_pages(doi: str, text: str) -> bool:
    if not doi:
        return False
    flat = re.sub(r"\s+", "", text.lower())
    return doi.lower().replace("//", "/") in flat.replace("//", "/")


def _pages_range(pages: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d+)\s*[–—-]\s*(\d+)\s*", pages or "")
    if not m:
        return None
    first, last = int(m.group(1)), int(m.group(2))
    return (first, last) if last >= first else None


def _same_work(info: ff.ItemInfo, got: dict) -> bool:
    """Gemini's reading names the item's DOI, or its first author and year (a translated title)."""
    doi = (got.get("doi") or "").lower().strip()
    if doi and info.doi and doi == info.doi.lower():
        return True
    authors = got.get("authors") or []
    first = authors[0] if authors and isinstance(authors[0], str) else ""
    mine = (ff._fold(info.first_author or "").split() or [""])[-1]
    theirs = ff._fold(first).split()
    year = str(got.get("year") or "").strip()[:4]
    return bool(mine and theirs and info.year and year == info.year[:4]
                and (mine == theirs[-1] or mine == theirs[0]))


def check(info: ff.ItemInfo, data: dict, pdfs: list[dict], reading: Callable[[], dict | None] | None = None,
          index: dict | None = None) -> Problem | None:
    """The problem with the item's PDF, or None.

    ``pdfs``: [{"key", "path", "pages", "text"}] for the item's PDF attachments
    (first pages' text). ``reading``: Gemini's reading of the item's PDF (asked only
    when the rules find neither DOI nor title). ``index``: {"doi": {doi: key},
    "title": {folded title: key}} of the library, to name the item a stray PDF belongs to.
    """
    readable = [p for p in pdfs if (p.get("text") or "").strip()]
    if not readable:
        return None                 # a scan without text: nothing to compare
    # This work: its DOI or its title printed at the top of the first page. The title's words further
    # down are not enough: a review, a protocol or a later paper by the same author has them too.
    limit = 8000 if info.item_type in ff.BOOK_TYPES else 2500
    matching = [p for p in readable if ff.doi_printed(info.doi, p["text"])
                or ff.title_near_top(info.title, p["text"], limit)]
    book = (data.get("bookTitle") or "") if info.item_type == "bookSection" else ""
    if not matching and book:
        # A chapter's PDF often opens with the book's title page: the chapter itself, or the whole book.
        matching = [p for p in readable if title_on_pages(book, p["text"][:4000])]
    if not matching:
        first = readable[0]
        found_title, found_doi = "", ""
        got = reading() if reading else None
        if got and got.get("title"):
            from zotero_mcp.metadata_audit import title_match

            if title_match(info.title, got["title"]) >= 0.8:
                return None         # Gemini sees the item's title (the rules missed it: layout, OCR)
            book_main = re.split(r"[:?!]\s", book, maxsplit=1)[0]
            if book and max(title_match(book, got["title"]), title_match(book_main, got["title"])) >= 0.8:
                whole = _whole_book(info, data, first)   # the book this chapter is in
                if whole is not None:
                    whole.other_item = _book_item(data, index)
                return whole
            if _same_work(info, got):
                return None         # same DOI, or same first author and year: a translated or reworded title
            found_title, found_doi = got.get("title", ""), (got.get("doi") or "").lower()
        else:
            dois = [d.rstrip(".,;)").lower() for d in _DOI_RE.findall(first["text"])]
            other = [d for d in dois if info.doi and d != info.doi.lower()]
            if not (info.doi and other and not any(d == info.doi.lower() for d in dois)):
                return None         # no proof either way: leave it
            found_doi = other[0]
        other_item = ""
        if index:
            other_item = (index.get("doi") or {}).get(found_doi, "") or \
                (index.get("title") or {}).get(ff._fold(found_title), "")
            if other_item == info.key:
                other_item = ""
        detail = f"its first pages show \"{found_title[:90]}\"" if found_title else f"its first pages show DOI {found_doi}"
        return Problem("another work", first["key"], first["path"], detail, found_title, found_doi, other_item)

    # Several PDFs of the item (a merged duplicate, a manuscript beside the published version):
    # fine as soon as one of them is.
    problems = [_form_problem(info, data, pdf) for pdf in matching]
    if any(p is None for p in problems):
        # The chapter has its own PDF; a whole book beside it is set aside (moved to the book's
        # own item, or to the trash) so that only the chapter stays.
        books = [p for p in problems if p is not None and p.kind == "whole book"]
        if books and info.item_type == "bookSection":
            b = books[0]
            return Problem("book beside chapter", b.attachment_key, b.path, b.detail,
                           other_item=_book_item(data, index))
        return None
    found = problems[0]
    if found is not None and found.kind == "whole book" and not found.other_item:
        found.other_item = _book_item(data, index)
    return found


def _book_item(data: dict, index: dict | None) -> str:
    """The library's own item for the book a chapter is in (by ISBN or title), or ""."""
    if not index:
        return ""
    books = index.get("book") or {}
    isbn = re.sub(r"[^0-9Xx]", "", (data.get("ISBN") or "").split()[0]) if data.get("ISBN") else ""
    if isbn and isbn in (index.get("isbn") or {}):
        return index["isbn"][isbn]
    title = data.get("bookTitle") or ""
    for t in (title, re.split(r"[:?!]\s", title, maxsplit=1)[0]):
        key = books.get(ff._fold(t))
        if key:
            return key
    return ""


def _form_problem(info: ff.ItemInfo, data: dict, pdf: dict) -> Problem | None:
    """The item's own PDF in another form (whole book, proof, manuscript, preprint), or None."""
    text = _NOT_A_STATEMENT_RE.sub(" ", pdf["text"][:12000])
    if info.item_type == "bookSection":
        return _whole_book(info, data, pdf)
    published = info.item_type == "journalArticle" and bool((data.get("volume") or data.get("pages") or "").strip())
    if not published:
        return None
    if _PUBLISHED_RE.search(text[:4000]) and not _SAYS_MANUSCRIPT_RE.search(text[:4000]):
        return None             # the publisher's cover page: the published version
    if _PROOF_RE.search(text):
        return Problem("proof", pdf["key"], pdf["path"], "it has placeholder page numbers or says it is a proof")
    if _MANUSCRIPT_RE.search(text):
        return Problem("manuscript", pdf["key"], pdf["path"], "it says it is the accepted or author's version")
    if _PREPRINT_RE.search(text[:3000]):
        return Problem("preprint", pdf["key"], pdf["path"], "it says it is a preprint or not peer reviewed")
    return None


def _whole_book(info: ff.ItemInfo, data: dict, pdf: dict) -> Problem | None:
    """A chapter's item with the whole book attached: far more pages than the chapter's range."""
    rng = _pages_range(data.get("pages") or "")
    if rng and pdf.get("pages", 0) > max(60, 3 * (rng[1] - rng[0] + 1)):
        return Problem("whole book", pdf["key"], pdf["path"],
                       f"{pdf['pages']} pages for a chapter on pp. {rng[0]}-{rng[1]}")
    return None


def library_index(items: list[dict]) -> dict:
    """DOI and folded title -> item key, to name the item a stray PDF belongs to."""
    by_doi, by_title, books, by_isbn = {}, {}, {}, {}
    for raw in items:
        info = ff.ItemInfo.from_zotero(raw)
        if info.doi:
            by_doi.setdefault(info.doi.lower(), info.key)
        if info.title:
            by_title.setdefault(ff._fold(info.title), info.key)
        if info.item_type == "book":
            for t in (info.title, re.split(r"[:?!]\s", info.title, maxsplit=1)[0]):
                if t:
                    books.setdefault(ff._fold(t), info.key)
            if info.isbn:
                by_isbn.setdefault(info.isbn, info.key)
    return {"doi": by_doi, "title": by_title, "book": books, "isbn": by_isbn}


def pdf_reader() -> Callable[[str], list[dict]]:
    """The item's PDF attachments with their first pages' text (a child process per file)."""
    from zotero_mcp import structure

    try:
        from zotero_mcp.local_db import get_serial_reader

        reader = get_serial_reader()
    except Exception:
        reader = None

    def read(key: str) -> list[dict]:
        if reader is None:
            return []
        out = []
        for att in reader.get_attachment_paths(key) or []:
            path = att.get("resolved_path")
            if not (att.get("exists") and path and str(path).lower().endswith(".pdf")):
                continue
            pages = structure.read_first_pages(path, 3, 0) or {}
            out.append({"key": att.get("key", ""), "path": str(path), "pages": int(pages.get("pages") or 0),
                        "text": "\n".join(t for _p, t in pages.get("texts") or [])})
        return out

    return read


def extract_chapter(book_pdf: str, pages_field: str, out_path: str, title: str = "") -> tuple[int, int] | None:
    """Cut a chapter's printed pages (the item's Pages field) out of the whole book, using the
    book's printed page numbers. Returns the PDF page range (1-based) or None.

    Cut only when sure: at least 80 % of the chapter's page numbers are found printed in the book,
    and (with ``title``) the cut's first page, or the page before it, shows the chapter's title:
    another edition of the book has other chapters on those page numbers."""
    from zotero_mcp import structure

    rng = _pages_range(pages_field)
    scan = structure.read_pdf(book_pdf)
    if not rng or not scan:
        return None
    labels, _how = structure.page_labels(scan, "", "book")
    if not labels:
        return None
    want = {str(n) for n in range(rng[0], rng[1] + 1)}
    idx = [i for i, lab in enumerate(labels) if lab in want]
    if not idx or len(idx) < 0.8 * len(want):
        return None             # not sure where the chapter's pages are: no cut
    first, last = min(idx), max(idx)
    if title:
        start = _chapter_start(book_pdf, first, title)
        if start is None:
            return None
        first = start
    if not structure.extract_pages(book_pdf, out_path, first, last):
        return None
    return first + 1, last + 1


def _chapter_start(book_pdf: str, first: int, title: str) -> int | None:
    """Where the chapter starts (0-based): its title as a phrase at the top of the first numbered
    page, or of the page before it (a title page of its own, then included). Not merely its words:
    a running head, or a chapter cut mid-way, has those too. None when it is on neither."""
    try:
        import pymupdf

        with pymupdf.open(book_pdf) as doc:
            for i in (first, first - 1):
                if 0 <= i < doc.page_count:
                    top = doc[i].get_text()[:1500]
                    if top.strip() and ff.title_near_top(title, top, 1500):
                        return i
    except Exception:
        pass
    return None             # cannot confirm it: no cut (the item is tagged whole-book instead)

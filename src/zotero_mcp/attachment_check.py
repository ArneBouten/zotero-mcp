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
    r"author(?:'s|s')? accepted manuscript|accepted author manuscript|\baccepted manuscript\b|"
    r"this is (?:an|the) (?:author'?s? )?(?:accepted|peer[- ]reviewed|pre-?print|final draft|post-?print)|"
    r"\bpost-?print\b|author'?s? final (?:draft|version)|has been accepted for publication in", re.I)
_PREPRINT_RE = re.compile(r"\bpre-?print\b(?! server)|not (?:yet )?(?:been )?peer[- ]reviewed|"
                          r"\b(?:psyarxiv|biorxiv|medrxiv|arxiv|osf preprints|ssrn)\b", re.I)
_PROOF_RE = re.compile(r"\b000\s*[–—-]\s*000\b|\buncorrected (?:page )?proofs?\b|\bvol(?:ume)?\.?\s*X{2,}\b|"
                       r"\bvolume\s+\d+\s*,?\s*issue\s+X+\b", re.I)
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

    def describe(self) -> str:
        what = {
            "another work": "the attached PDF is another work",
            "manuscript": "the attached PDF is the accepted manuscript, not the published version",
            "preprint": "the attached PDF is a preprint, not the published version",
            "proof": "the attached PDF is a proof (page numbers not final)",
            "whole book": "the whole book is attached to this chapter",
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
    matching = [p for p in readable if _doi_on_pages(info.doi, p["text"]) or title_on_pages(info.title, p["text"])]
    if not matching:
        first = readable[0]
        found_title, found_doi = "", ""
        got = reading() if reading else None
        if got and got.get("title"):
            from zotero_mcp.metadata_audit import title_match

            if title_match(info.title, got["title"]) >= 0.8:
                return None         # Gemini sees the item's title (the rules missed it: layout, OCR)
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

    pdf = matching[0]
    text = pdf["text"][:12000]
    if info.item_type == "bookSection":
        rng = _pages_range(data.get("pages") or "")
        if rng and pdf.get("pages", 0) > max(60, 3 * (rng[1] - rng[0] + 1)):
            return Problem("whole book", pdf["key"], pdf["path"],
                           f"{pdf['pages']} pages for a chapter on pp. {rng[0]}-{rng[1]}")
    published = info.item_type == "journalArticle" and bool((data.get("volume") or data.get("pages") or "").strip())
    if not published:
        return None
    if _PROOF_RE.search(text):
        return Problem("proof", pdf["key"], pdf["path"], "it has placeholder page numbers or says it is a proof")
    if _MANUSCRIPT_RE.search(text):
        return Problem("manuscript", pdf["key"], pdf["path"], "it says it is the accepted or author's version")
    if _PREPRINT_RE.search(text[:3000]):
        return Problem("preprint", pdf["key"], pdf["path"], "it says it is a preprint or not peer reviewed")
    return None


def library_index(items: list[dict]) -> dict:
    """DOI and folded title -> item key, to name the item a stray PDF belongs to."""
    by_doi, by_title = {}, {}
    for raw in items:
        info = ff.ItemInfo.from_zotero(raw)
        if info.doi:
            by_doi.setdefault(info.doi.lower(), info.key)
        if info.title:
            by_title.setdefault(ff._fold(info.title), info.key)
    return {"doi": by_doi, "title": by_title}


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


def extract_chapter(book_pdf: str, pages_field: str, out_path: str) -> tuple[int, int] | None:
    """Cut a chapter's printed pages (the item's Pages field) out of the whole book, using the
    book's printed page numbers. Returns the PDF page range (1-based) or None."""
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
    if not idx or len(idx) < 0.6 * len(want):
        return None
    first, last = min(idx), max(idx)
    if not structure.extract_pages(book_pdf, out_path, first, last):
        return None
    return first + 1, last + 1

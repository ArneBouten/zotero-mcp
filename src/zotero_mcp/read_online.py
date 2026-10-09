"""Find a paper's full text online and read it, without adding anything to Zotero.

For a paper you are only curious about, one for someone else's project, or one you are not
sure is worth keeping: the same search and checks as the PDF fetcher (open access, the
publisher on the university network, Google Scholar, a web search; each PDF checked to be this
work), but the result is text for Claude to read, optionally a PDF saved to a folder you name.
Nothing is written to the library and no record of the search is kept.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from zotero_mcp import fulltext_fetch as ff

#: Crossref's kinds as the fetcher's item types (they decide how strict the checks are).
_KINDS = {
    "journal-article": "journalArticle", "book-chapter": "bookSection", "book-section": "bookSection",
    "book": "book", "monograph": "book", "edited-book": "book", "reference-book": "book",
    "proceedings-article": "conferencePaper", "posted-content": "preprint", "dissertation": "thesis",
    "report": "report",
}
#: The text returned to Claude; the whole text is in the text file beside it.
DEFAULT_MAX_CHARS = 60_000


@dataclass
class Reading:
    found: bool
    label: str
    reason: str = ""
    source: str = ""
    url: str = ""
    version: str | None = None
    pages: int = 0
    text: str = ""
    text_path: str = ""
    saved_pdf: str = ""
    attempts: list = field(default_factory=list)

    def markdown(self, max_chars: int = DEFAULT_MAX_CHARS) -> str:
        if not self.found:
            lines = [f"# Not found: {self.label}", "", self.reason or "no copy passed the checks."]
            for a in self.attempts[-8:]:
                lines.append(f"- {a.source}: {a.outcome}")
            lines.append("")
            lines.append("Nothing was added to Zotero.")
            return "\n".join(lines)
        head = [
            f"# {self.label}",
            "",
            f"Found at {self.source} ({ff._host_of(self.url)}), "
            f"{ff.VERSION_LABELS.get(self.version, 'version unknown')}, {self.pages} pages. "
            "Not added to Zotero.",
        ]
        if self.saved_pdf:
            head.append(f"PDF saved to {self.saved_pdf}.")
        text = self.text
        if len(text) > max_chars:
            head.append(f"Text: the first {max_chars:,} of {len(text):,} characters; the whole text is in "
                        f"{self.text_path}.")
            text = text[:max_chars]
        return "\n".join(head) + "\n\n---\n\n" + text


def describe(doi: str = "", title: str = "", author: str = "", year: str = "",
             http: ff.Http | None = None, settings: ff.Settings | None = None) -> ff.ItemInfo:
    """The paper as the fetcher sees an item: from its DOI's record, or a title search, or as given."""
    from zotero_mcp import metadata_audit as ma

    settings = settings or ff.Settings.load()
    http = http or ff.Http(settings)
    doi = ma.norm_doi(doi or "") if doi else ""
    given = ff.ItemInfo(key="READONLINE", item_type="journalArticle", title=title.strip(),
                        authors=[author.strip()] if author.strip() else [], year=str(year or "").strip(),
                        doi=doi)
    rec = None
    if doi:
        rec = ma.crossref(doi, http, settings) or ma.datacite(doi, http, settings)
    elif given.title:
        rec = ma.crossref_by_title(given, http, settings)
    if rec is None:
        return given
    return ff.ItemInfo(
        key="READONLINE",
        item_type=_KINDS.get((rec.kind or "").lower(), "journalArticle"),
        title=rec.title or given.title,
        authors=[f for f, _g in rec.authors] or given.authors,
        year=rec.year or given.year,
        doi=rec.doi or doi,
        isbn=re.sub(r"[^0-9Xx]", "", rec.isbn or "")[:13],
        pages=rec.pages or "",
    )


def full_text(path: str) -> tuple[int, str]:
    """Every page's text, pages separated by a form feed."""
    import pymupdf

    with pymupdf.open(path) as doc:
        return doc.page_count, "\f".join(page.get_text() for page in doc)


def read(doi: str = "", title: str = "", author: str = "", year: str = "",
         save_to: str = "", steps: Iterable[str] | None = None,
         log: Callable[[str], None] = lambda m: None,
         http: ff.Http | None = None, settings: ff.Settings | None = None,
         find: Callable | None = None) -> Reading:
    """Find, check and read the paper. Nothing goes to Zotero."""
    settings = settings or ff.Settings.load()
    http = http or ff.Http(settings)
    http.log = log
    if not (doi or title):
        return Reading(False, "(no paper)", "give a DOI or a title")
    item = describe(doi, title, author, year, http, settings)
    if not item.title:
        return Reading(False, doi or title, "the DOI is unknown to Crossref and DataCite; give the title too")
    chosen = [s for s in (steps or ff.DEFAULT_STEPS) if s in ff.STEPS and s != "browser"] or list(ff.DEFAULT_STEPS)
    workdir = tempfile.mkdtemp(prefix="zmcp-read-")
    path, cand, check, attempts = (find or ff.find_pdf_for)(item, http, settings, ff.Budget(), chosen, log, workdir)
    if not (path and cand and check):
        return Reading(False, item.label, ff._not_found_reason(attempts), attempts=list(attempts))
    pages, text = full_text(path)
    out_dir = os.path.join(tempfile.gettempdir(), "zotero-mcp-read")
    os.makedirs(out_dir, exist_ok=True)
    stem = ff._safe_filename(item)[:-4]
    text_path = os.path.join(out_dir, stem + ".txt")
    with open(text_path, "w", encoding="utf-8") as f:
        f.write(text)
    saved = ""
    if save_to:
        folder = os.path.expanduser(save_to)
        os.makedirs(folder, exist_ok=True)
        saved = os.path.join(folder, stem + ".pdf")
        shutil.copyfile(path, saved)
    return Reading(True, item.label, source=cand.source, url=ff._redact(cand.url), version=check.version,
                   pages=pages, text=text, text_path=text_path, saved_pdf=saved, attempts=list(attempts))

"""Inspect and edit the citations in a Word document.

:mod:`zotero_mcp.word_citations` turns typed markers into live Zotero
citations. This module covers the rest of the work on a manuscript:

* :func:`inspect_docx` lists what a document cites. That includes its live
  Zotero citations (with the items each one points to), its Zotero
  bibliographies, typed reference lists, citations typed as plain text, and
  markers not yet converted. Each gets a short id (``C3``, ``B1``, ``R1``,
  ``P2``, ``D14`` for paragraph 14 of the body) that edits refer to.
* :func:`edit_docx` applies a list of edits:

  - replace or delete a citation;
  - replace text, for example a plain-text "(Smith, 2020)" with a marker;
  - add a comment;
  - insert, rebuild or delete a bibliography;
  - replace a typed reference list with a Zotero bibliography.

  It also converts any markers, as the insert tool does.

Every edit can be written in one of three ways, chosen per call: to a new
file, over the original as tracked changes, or over the original outright.
Both of the last two keep a backup copy first. Tracked changes carry the
document author's name, so they read in Word like any other reviewer's
changes and can be accepted or rejected one by one.

Zotero's own Refresh still does the final formatting: ordering within a
citation, disambiguation, and the bibliography's exact rendering. New
citations are written with Zotero's rendering of each item, so they read
correctly before that.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zotero_mcp import word_citations as wc
from zotero_mcp.word_citations import XML_SPACE, W, WordCitationError

WRITE_MODES = ("new_file", "tracked_changes", "overwrite")
PART_PREFIX = {"word/document.xml": "D", "word/footnotes.xml": "F", "word/endnotes.xml": "E"}
COMMENTS_PART = "word/comments.xml"
COMMENTS_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
COMMENTS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
DOC_RELS = "word/_rels/document.xml.rels"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
BACKUP_DIR = "Zotero backups"

#: Headings that open a typed reference list (English and Dutch).
REFERENCE_HEADINGS = {
    "references", "reference list", "bibliography", "literature", "works cited",
    "referenties", "referentielijst", "literatuurlijst", "literatuur", "bronnen", "bronnenlijst",
}

_NAME = r"[A-ZÀ-ɏ][A-Za-zÀ-ɏ'’\-]+"
_YEAR = r"(?:\d{4}[a-z]?|n\.\s?d\.)"
#: "(Smith, 2020)", "(see Smith & Jones, 2020a; Lee et al., 2019, p. 4)".
PAREN_CITATION_RE = re.compile(rf"\([^()]*?{_NAME}[^()]*?,\s*{_YEAR}[^()]*\)")
#: "Smith (2020)", "Smith and Jones (2020)", "Smith et al. (2020, p. 4)".
NARRATIVE_CITATION_RE = re.compile(
    rf"\b{_NAME}(?:\s+et\s+al\.|\s+(?:and|&)\s+{_NAME})?\s+\({_YEAR}(?:,\s*pp?\.\s*[\d\-–]+)?\)"
)
_MASK = "\x00"


# ---------------------------------------------------------------------------
# Reading a document
# ---------------------------------------------------------------------------


@dataclass
class CitedItem:
    key: str
    label: str  # "Soenens 2009": first author's family name and year
    locator: str = ""
    locator_label: str = ""
    prefix: str = ""
    suffix: str = ""
    suppress_author: bool = False
    uri: str = ""
    library: str = ""  # "users/7756991", "users/local/AbCd1234", "groups/2547418"
    doi: str = ""
    title: str = ""
    year: str = ""
    entry: dict = field(default_factory=dict, repr=False)  # the field's citationItems entry


@dataclass
class FieldInfo:
    id: str  # C1 / B1
    kind: str  # "citation" | "bibliography"
    part: str
    paragraph: str  # where the field begins, e.g. D14
    end_paragraph: str
    text: str  # visible text
    items: list[CitedItem] = field(default_factory=list)
    hand_edited: bool = False
    context: str = ""
    runs: list = field(default_factory=list, repr=False)
    result_ts: list = field(default_factory=list, repr=False)
    begin_p: Any = field(default=None, repr=False)
    end_p: Any = field(default=None, repr=False)


@dataclass
class PlainCitation:
    id: str  # P1
    paragraph: str
    text: str
    context: str = ""


@dataclass
class ReferenceList:
    id: str  # R1
    heading: str  # paragraph id of the heading
    heading_text: str
    entries: list[tuple[str, str]] = field(default_factory=list)  # (paragraph id, text)
    paragraphs: list = field(default_factory=list, repr=False)


@dataclass
class MarkerInfo:
    paragraph: str
    text: str


@dataclass
class Inventory:
    path: str
    style: str = ""
    citations: list[FieldInfo] = field(default_factory=list)
    bibliographies: list[FieldInfo] = field(default_factory=list)
    reference_lists: list[ReferenceList] = field(default_factory=list)
    plain_citations: list[PlainCitation] = field(default_factory=list)
    markers: list[MarkerInfo] = field(default_factory=list)
    bibliography_markers: list[str] = field(default_factory=list)  # paragraph ids
    #: How many cited items come from each library ("users/<id>", "groups/<id>", ...).
    libraries: dict[str, int] = field(default_factory=dict)
    #: The same work cited as different items: each group is [(citation id, item), ...].
    duplicates: list[list[tuple[str, CitedItem]]] = field(default_factory=list)


class _Doc:
    """A loaded .docx: its parts, parsed XML trees, and the ids of their parts."""

    def __init__(self, path: Path):
        self.etree = wc._etree()
        self.path = path
        try:
            with zipfile.ZipFile(path) as zf:
                self.infos = zf.infolist()
                self.files = {i.filename: zf.read(i.filename) for i in self.infos}
        except zipfile.BadZipFile as e:
            raise WordCitationError(f"{path.name} is not a valid .docx file: {e}") from e
        if "word/document.xml" not in self.files:
            raise WordCitationError(f"{path.name} has no word/document.xml; is it a Word document?")
        self.trees = {p: self.etree.fromstring(self.files[p]) for p in wc.TEXT_PARTS if p in self.files}
        for root in self.trees.values():
            _separate_field_runs(self.etree, root)
        self.para_ids: dict[int, str] = {}
        self.paras: dict[str, Any] = {}
        self.para_part: dict[str, str] = {}
        for part, root in self.trees.items():
            for n, p in enumerate(root.iter(f"{W}p"), start=1):
                pid = f"{PART_PREFIX[part]}{n}"
                self.para_ids[id(p)] = pid
                self.paras[pid] = p
                self.para_part[pid] = part

    def pid(self, p) -> str:
        return self.para_ids.get(id(p), "?")


_FIELD_PARTS = {f"{W}fldChar", f"{W}instrText", f"{W}delInstrText"}


def _separate_field_runs(etree, root) -> None:
    """Give field characters runs of their own.

    Word writes a field as separate runs, but some documents (saved by other
    tools, or merged runs) hold a whole field, and the text around it, in one
    run. Edits replace and delete fields run by run, so such a run is split
    into one run per child, each with the same formatting. Nothing visible
    changes.
    """
    for r in list(root.iter(f"{W}r")):
        children = [c for c in r if c.tag != f"{W}rPr"]
        if len(children) < 2 or not any(c.tag in _FIELD_PARTS for c in children):
            continue
        rpr = r.find(f"{W}rPr")
        anchor = r
        for child in children:
            new = etree.Element(f"{W}r")
            for attr, value in r.attrib.items():
                new.set(attr, value)
            if rpr is not None:
                new.append(deepcopy(rpr))
            new.append(child)  # moves it out of the old run
            anchor.addnext(new)
            anchor = new
        r.getparent().remove(r)


def _paragraph_of(node):
    while node is not None and node.tag != f"{W}p":
        node = node.getparent()
    return node


def _scan_fields(root) -> list[dict]:
    """Every complex field in document order, nested ones included.

    Each is a dict with its instruction text, the runs from its begin to its
    end, and the text nodes of its result. Deleted (tracked) runs are skipped.
    """
    done: list[dict] = []
    stack: list[dict] = []
    for r in root.iter(f"{W}r"):
        if wc._inside(r, {f"{W}del"}):
            continue
        touched = list(stack)
        closed: list[dict] = []
        for child in r:
            if child.tag == f"{W}fldChar":
                kind = child.get(f"{W}fldCharType")
                if kind == "begin":
                    f = {"instr": [], "sep": False, "runs": [], "result_ts": []}
                    stack.append(f)
                    touched.append(f)
                elif kind == "separate" and stack:
                    stack[-1]["sep"] = True
                elif kind == "end" and stack:
                    f = stack.pop()
                    closed.append(f)
            elif child.tag == f"{W}instrText" and stack:
                stack[-1]["instr"].append(child.text or "")
            elif child.tag == f"{W}t" and stack and stack[-1]["sep"]:
                stack[-1]["result_ts"].append(child)
        for f in touched:
            if not f["runs"] or f["runs"][-1] is not r:
                f["runs"].append(r)
        for f in closed:
            f["instr"] = "".join(f["instr"])
            done.append(f)
    # Fields close inner-first; list them by where they begin.
    order = {id(e): i for i, e in enumerate(root.iter(f"{W}r"))}
    done.sort(key=lambda f: order.get(id(f["runs"][0]), 0))
    return done


def _parse_citation_code(code: str) -> tuple[list[CitedItem], str | None]:
    m = re.search(r"CSL_CITATION\s+(\{.*\})\s*$", code, re.S)
    if not m:
        return [], None
    try:
        payload = json.loads(m.group(1))
    except ValueError:
        return [], None
    items: list[CitedItem] = []
    for entry in payload.get("citationItems") or []:
        uris = entry.get("uris") or entry.get("uri") or []
        if isinstance(uris, str):
            uris = [uris]
        uri = str(uris[0]) if uris else ""
        key = uri.rstrip("/").rsplit("/", 1)[-1] if uri else str(entry.get("id", ""))
        lib = re.search(r"zotero\.org/((?:users|groups)/(?:local/)?[^/]+)/items/", uri)
        data = entry.get("itemData") or {}
        authors = data.get("author") or data.get("editor") or []
        name = ""
        if authors:
            first = authors[0]
            name = first.get("family") or first.get("literal") or ""
        year = ""
        parts = (data.get("issued") or {}).get("date-parts") or []
        if parts and parts[0]:
            year = str(parts[0][0])
        items.append(
            CitedItem(
                key=key,
                label=" ".join(b for b in (name, year) if b) or (data.get("title") or "")[:40],
                locator=str(entry.get("locator") or ""),
                locator_label=str(entry.get("label") or ""),
                prefix=str(entry.get("prefix") or ""),
                suffix=str(entry.get("suffix") or ""),
                suppress_author=bool(entry.get("suppress-author")),
                uri=uri,
                library=lib.group(1) if lib else "",
                doi=_norm_doi(data.get("DOI") or ""),
                title=str(data.get("title") or ""),
                year=year,
                entry=entry,
            )
        )
    plain = (payload.get("properties") or {}).get("plainCitation")
    return items, plain


def _norm_doi(doi: str) -> str:
    doi = str(doi).strip().lower()
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi)


def _work_keys(doi: str, title: str, year: str) -> list[str]:
    """What identifies a work across libraries: its DOI, and its title with the year.

    Two items are the same work when they share either: one library may have
    the DOI filled in and the other not.
    """
    keys = []
    if doi:
        keys.append("doi:" + _norm_doi(doi))
    words = re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower()).split()
    if words:
        keys.append("title:" + " ".join(words[:12]) + "|" + (year or ""))
    return keys


def _find_duplicates(citations: list[FieldInfo]) -> list[list[tuple[str, CitedItem]]]:
    entries = [(c.id, it) for c in citations for it in c.items]
    parent = list(range(len(entries)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_with: dict[str, int] = {}
    for i, (_, it) in enumerate(entries):
        for k in _work_keys(it.doi, it.title, it.year):
            if k in first_with:
                parent[find(i)] = find(first_with[k])
            else:
                first_with[k] = i
    groups: dict[int, list[tuple[str, CitedItem]]] = {}
    for i, entry in enumerate(entries):
        groups.setdefault(find(i), []).append(entry)
    return [g for g in groups.values() if len({it.uri or it.key for _, it in g}) > 1]


def _context(text: str, start: int, end: int, width: int = 160) -> str:
    a = max(0, start - width)
    b = min(len(text), end + width)
    snippet = re.sub(f"{_MASK}+", "[citation]", text[a:b]).strip()
    return ("…" if a > 0 else "") + snippet + ("…" if b < len(text) else "")


def _masked_text(paragraph, masked_ts: set[int]) -> str:
    out = []
    for t in wc._text_nodes(paragraph):
        s = t.text or ""
        out.append(_MASK * len(s) if id(t) in masked_ts else s)
    return "".join(out)


def _is_heading(p) -> bool:
    ppr = p.find(f"{W}pPr")
    if ppr is None:
        return False
    style = ppr.find(f"{W}pStyle")
    if style is not None:
        val = (style.get(f"{W}val") or "").lower()
        if val.startswith(("heading", "kop", "title", "titel")):
            return True
    return ppr.find(f"{W}outlineLvl") is not None


def _read(doc: _Doc) -> tuple[Inventory, dict[str, FieldInfo]]:
    inv = Inventory(path=str(doc.path))
    prefs = wc.read_document_prefs(doc.files.get("docProps/custom.xml"))
    style, _ = wc.prefs_style(prefs)
    inv.style = wc.style_short_name(style) if style else ""
    by_id: dict[str, FieldInfo] = {}
    field_ts: set[int] = set()
    bib_paras: set[int] = set()
    keep: list = []  # holds the nodes whose id()s are stored above

    for part, root in doc.trees.items():
        for f in _scan_fields(root):
            instr = f["instr"]
            if "ADDIN ZOTERO_ITEM" in instr:
                kind = "citation"
            elif "ADDIN ZOTERO_BIBL" in instr:
                kind = "bibliography"
            else:
                continue
            begin_p, end_p = _paragraph_of(f["runs"][0]), _paragraph_of(f["runs"][-1])
            text = "".join(t.text or "" for t in f["result_ts"])
            info = FieldInfo(
                id="", kind=kind, part=part, paragraph=doc.pid(begin_p), end_paragraph=doc.pid(end_p),
                text=text, runs=f["runs"], result_ts=f["result_ts"], begin_p=begin_p, end_p=end_p,
            )
            if kind == "citation":
                info.items, plain = _parse_citation_code(instr)
                info.hand_edited = plain is not None and plain != text
                inv.citations.append(info)
            else:
                inv.bibliographies.append(info)
                node = begin_p
                while node is not None:
                    keep.append(node)
                    bib_paras.add(id(node))
                    if node is end_p:
                        break
                    node = node.getnext()
            field_ts.update(id(t) for t in f["result_ts"])

    for n, info in enumerate(inv.citations, start=1):
        info.id = f"C{n}"
        by_id[info.id] = info
        if info.begin_p is not None and info.result_ts:
            nodes = wc._text_nodes(info.begin_p)
            full = "".join(t.text or "" for t in nodes)
            pos = 0
            for t in nodes:
                if t is info.result_ts[0]:
                    break
                pos += len(t.text or "")
            info.context = _context(full, pos, pos + len(info.text))
    for c in inv.citations:
        for it in c.items:
            name = it.library or "unknown"
            inv.libraries[name] = inv.libraries.get(name, 0) + 1
    inv.duplicates = _find_duplicates(inv.citations)
    for n, info in enumerate(inv.bibliographies, start=1):
        info.id = f"B{n}"
        info.text = f"{len(_bibliography_paragraphs(info))} entries"
        by_id[info.id] = info

    pn = 0
    for part, root in doc.trees.items():
        for p in root.iter(f"{W}p"):
            pid = doc.pid(p)
            raw = wc._paragraph_text(p)
            if "@" in raw:
                for cl in wc.find_clusters(raw):
                    inv.markers.append(MarkerInfo(pid, cl.raw))
            if part == "word/document.xml" and wc.BIBLIOGRAPHY_MARKER_RE.match(raw):
                inv.bibliography_markers.append(pid)
            masked = _masked_text(p, field_ts)
            spans: list[tuple[int, int]] = []
            for rx in (PAREN_CITATION_RE, NARRATIVE_CITATION_RE):
                for m in rx.finditer(masked):
                    if _MASK in m.group(0) or "@" in m.group(0):
                        continue
                    if any(m.start() < e and s < m.end() for s, e in spans):
                        continue
                    spans.append((m.start(), m.end()))
            for s, e in sorted(spans):
                pn += 1
                inv.plain_citations.append(
                    PlainCitation(f"P{pn}", pid, masked[s:e], _context(masked, s, e))
                )

    root = doc.trees["word/document.xml"]
    body = root.find(f"{W}body")
    if body is not None:
        children = [c for c in body if c.tag == f"{W}p"]
        rn = 0
        i = 0
        while i < len(children):
            p = children[i]
            text = wc._paragraph_text(p).strip()
            if text.lower().rstrip(":") in REFERENCE_HEADINGS and len(text) < 40:
                entries = []
                j = i + 1
                while j < len(children):
                    q = children[j]
                    if _is_heading(q) or id(q) in bib_paras:
                        break
                    qt = wc._paragraph_text(q).strip()
                    if qt and not wc.BIBLIOGRAPHY_MARKER_RE.match(qt):
                        entries.append((doc.pid(q), qt, q))
                    j += 1
                if entries:
                    rn += 1
                    rl = ReferenceList(f"R{rn}", doc.pid(p), text,
                                       [(e[0], e[1]) for e in entries], [e[2] for e in entries])
                    inv.reference_lists.append(rl)
                i = j
                continue
            i += 1
    return inv, by_id


def _bibliography_paragraphs(info: FieldInfo) -> list:
    out = []
    node = info.begin_p
    while node is not None:
        if node.tag == f"{W}p":
            out.append(node)
        if node is info.end_p:
            break
        node = node.getnext()
    return out


def inspect_docx(path: str | os.PathLike) -> Inventory:
    """What a document cites, with the ids :func:`edit_docx` takes."""
    src = Path(path).expanduser()
    if not src.is_file():
        raise WordCitationError(f"No such file: {src}")
    if src.suffix.lower() != ".docx":
        raise WordCitationError("Only .docx files are supported (save .doc files as .docx first).")
    inv, _ = _read(_Doc(src))
    return inv


# ---------------------------------------------------------------------------
# Tracked changes
# ---------------------------------------------------------------------------


class _Revisions:
    """Makes w:ins / w:del elements, or does nothing when changes are not tracked."""

    def __init__(self, etree, tracked: bool, author: str, next_id: int):
        self.etree = etree
        self.tracked = tracked
        self.author = author
        self.date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.next_id = next_id
        # w:ins elements this edit created. Kept as objects, not id()s: lxml
        # hands out a new proxy (and id) for a node once the old one is gone.
        self.own: list = []

    def new_id(self) -> int:
        n = self.next_id
        self.next_id += 1
        return n

    def _mark(self, tag: str):
        el = self.etree.Element(f"{W}{tag}")
        el.set(f"{W}id", str(self.new_id()))
        el.set(f"{W}author", self.author)
        el.set(f"{W}date", self.date)
        return el

    def ins(self):
        el = self._mark("ins")
        self.own.append(el)
        return el

    def is_own(self, el) -> bool:
        return el is not None and any(el is x for x in self.own)

    def delete_runs(self, runs: list) -> bool:
        """Remove *runs*, or mark them deleted. False if they cannot be tracked."""
        if not self.tracked:
            for r in runs:
                parent = r.getparent()
                if parent is not None:
                    parent.remove(r)
            return True
        for r in runs:
            parent = r.getparent()
            if parent is not None and parent.tag == f"{W}ins" and not self.is_own(parent):
                return False  # a deletion cannot sit inside someone else's insertion
        group = None
        for r in runs:
            parent = r.getparent()
            if parent is None:
                continue
            if self.is_own(parent):
                parent.remove(r)
                continue
            for child in r:
                if child.tag == f"{W}t":
                    child.tag = f"{W}delText"
                elif child.tag == f"{W}instrText":
                    child.tag = f"{W}delInstrText"
            if group is not None and r.getprevious() is group:
                group.append(r)
            else:
                group = self._mark("del")
                r.addprevious(group)
                group.append(r)
        return True

    def wrap_inserted(self, runs: list) -> list:
        """The elements to place for newly written *runs*."""
        if not self.tracked:
            return runs
        ins = self.ins()
        for r in runs:
            ins.append(r)
        return [ins]

    def _ppr_rpr(self, p):
        ppr = p.find(f"{W}pPr")
        if ppr is None:
            ppr = self.etree.Element(f"{W}pPr")
            p.insert(0, ppr)
        rpr = ppr.find(f"{W}rPr")
        if rpr is None:
            rpr = self.etree.Element(f"{W}rPr")
            tail = [c for c in ppr if c.tag in (f"{W}sectPr", f"{W}pPrChange")]
            if tail:
                tail[0].addprevious(rpr)
            else:
                ppr.append(rpr)
        return rpr

    def mark_paragraph_inserted(self, p) -> None:
        if self.tracked:
            # Revision marks come first in a paragraph mark's rPr (schema order).
            self._ppr_rpr(p).insert(0, self._mark("ins"))

    def delete_paragraph(self, p) -> bool:
        if not self.tracked:
            parent = p.getparent()
            if parent is not None:
                parent.remove(p)
            return True
        runs = [r for r in p.iter(f"{W}r") if not wc._inside(r, {f"{W}del"})]
        if not self.delete_runs(runs):
            return False
        rpr = self._ppr_rpr(p)
        existing = rpr.find(f"{W}ins")
        mark = self._mark("del")
        if existing is not None:
            existing.addnext(mark)
        else:
            rpr.insert(0, mark)
        return True


def _max_annotation_id(doc: _Doc) -> int:
    best = 0
    for name, data in doc.files.items():
        if not name.startswith("word/") or not name.endswith(".xml"):
            continue
        for m in re.finditer(rb'w:id="(-?\d+)"', data):
            best = max(best, int(m.group(1)))
    return best


#: Names that an assistant or tool, not a person, leaves in "last modified by".
_TOOL_NAMES = re.compile(r"claude|anthropic|copilot|chatgpt|openai|gemini|python-docx|docx4j|aspose", re.I)
AUTHOR_ENV = "ZOTERO_MCP_WORD_AUTHOR"


def _document_author(files: dict[str, bytes]) -> str:
    """The name tracked changes and comments go under.

    ZOTERO_MCP_WORD_AUTHOR when set; otherwise the document's "last modified
    by", then its creator, skipping names an assistant or library leaves there.
    """
    env = os.getenv(AUTHOR_ENV, "").strip()
    if env:
        return env
    core = files.get("docProps/core.xml")
    if core:
        for tag in (b"cp:lastModifiedBy", b"dc:creator"):
            m = re.search(rb"<" + tag + rb">([^<]+)</" + tag + rb">", core)
            if m and m.group(1).strip():
                name = m.group(1).decode("utf-8", "replace").strip()
                if not _TOOL_NAMES.search(name):
                    return name
    return "Zotero"


# ---------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------


@dataclass
class EditResult:
    op: str
    target: str
    status: str  # "done" | "skipped"
    detail: str = ""


@dataclass
class EditReport:
    output_path: str
    write_mode: str
    backup_path: str = ""
    author: str = ""
    results: list[EditResult] = field(default_factory=list)
    markers_converted: int = 0
    unresolved: list[str] = field(default_factory=list)
    skipped_markers: list[str] = field(default_factory=list)
    comments: int = 0
    bibliography_written: bool = False
    prefs_written: bool = False
    dry_run: bool = False
    style: str = ""
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, op: dict, status: str, detail: str = "") -> None:
        target = op.get("citation") or op.get("bibliography") or op.get("reference_list") or op.get("paragraph") or op.get("after") or ""
        self.results.append(EditResult(str(op.get("op", "?")), str(target), status, detail))


def _cluster_from_marker(marker: str) -> wc.Cluster | None:
    marker = (marker or "").strip()
    if not marker.startswith("["):
        marker = f"[{marker}]"
    found = wc.find_clusters(marker)
    if len(found) != 1 or found[0].raw != marker:
        return None
    return found[0]


def _field_elements(etree, rev: _Revisions, rpr, cluster: wc.Cluster, resolved) -> tuple[list, str]:
    text = wc.provisional_text(cluster, resolved)
    code = wc.citation_field_code(cluster, resolved, text)
    return rev.wrap_inserted(wc._field_runs(etree, rpr, code, text)), text


def _run_rpr(run):
    rpr = run.find(f"{W}rPr") if run is not None else None
    return deepcopy(rpr) if rpr is not None else None


def _space_run_before(etree, first_run, last_run):
    """The space left over once a citation goes.

    "as shown (Smith, 2020)." loses its citation and would read "as shown .",
    and "as (Smith, 2020) shown" would read "as  shown";
    this splits that space into a run of its own and returns it, so it can be
    deleted with the citation. None when there is no such space.
    """
    prev = first_run.getprevious()
    while prev is not None and prev.tag != f"{W}r":
        prev = prev.getprevious()
    nxt = last_run.getnext()
    while nxt is not None and nxt.tag != f"{W}r":
        nxt = nxt.getnext()
    if prev is None:
        return None
    prev_ts = [t for t in prev if t.tag == f"{W}t"]
    if not prev_ts or not (prev_ts[-1].text or "").endswith(" ") or prev_ts[-1] is not list(prev)[-1]:
        return None
    next_text = "".join(t.text or "" for t in nxt.iter(f"{W}t")) if nxt is not None else ""
    if next_text[:1] not in ("", " ", ".", ",", ";", ":", ")", "?", "!"):
        return None
    t = prev_ts[-1]
    _, space = wc._split_run(etree, prev, t, len(t.text) - 1)
    return space


def _same_parent(runs: list) -> bool:
    parents = {id(r.getparent()) for r in runs}
    return len(parents) == 1


def _isolate(etree, paragraph, start: int, end: int, field_ts: set[int]):
    """Split runs so paragraph text [start, end) is exactly a list of sibling runs.

    Returns the runs, or a reason string when that is not possible.
    """
    nodes = wc._text_nodes(paragraph)
    span = wc._locate(nodes, start, end)
    if span is None:
        return "text not found"
    pos = 0
    for t in nodes:
        n = len(t.text or "")
        if pos < end and start < pos + n and id(t) in field_ts:
            return "the text overlaps a Zotero citation; edit the citation instead"
        pos += n
    start_run, end_run = span.start_t.getparent(), span.end_t.getparent()
    if start_run is None or end_run is None or start_run.tag != f"{W}r" or end_run.tag != f"{W}r":
        return "text sits in an unusual element"
    if start_run.getparent() is not end_run.getparent():
        return "text straddles a hyperlink, field or tracked change"
    _, tail = wc._split_run(etree, end_run, span.end_t, span.end_off)
    stop = tail
    if stop is None:  # the span ends where its run ends: mark the spot
        stop = etree.Element(f"{W}proofErr")
        end_run.addnext(stop)
    _, first = wc._split_run(etree, start_run, span.start_t, span.start_off)
    runs = []
    node = first
    while node is not None and node is not stop:
        if node.tag == f"{W}r":
            runs.append(node)
        node = node.getnext()
    if tail is None:
        stop.getparent().remove(stop)
    if first is None or not runs:
        return "empty text"
    return runs


def _text_run(etree, rpr, text: str):
    r = etree.Element(f"{W}r")
    if rpr is not None:
        r.append(deepcopy(rpr))
    t = etree.SubElement(r, f"{W}t")
    t.set(XML_SPACE, "preserve")
    t.text = text
    return r


def _place_before(anchor, elements: list) -> None:
    for el in elements:
        anchor.addprevious(el)


def _place_after(anchor, elements: list) -> None:
    for el in reversed(elements):
        anchor.addnext(el)


# --- comments ------------------------------------------------------------------------


class _Comments:
    def __init__(self, doc: _Doc, rev: _Revisions):
        self.doc = doc
        self.rev = rev
        self.etree = doc.etree
        data = doc.files.get(COMMENTS_PART)
        if data:
            self.root = self.etree.fromstring(data)
        else:
            self.root = self.etree.Element(f"{W}comments", nsmap={"w": wc.W_NS})
        self.added = 0

    def add(self, first_el, last_el, text: str) -> None:
        etree = self.etree
        cid = str(self.rev.new_id())
        start = etree.Element(f"{W}commentRangeStart")
        start.set(f"{W}id", cid)
        end = etree.Element(f"{W}commentRangeEnd")
        end.set(f"{W}id", cid)
        ref_run = etree.Element(f"{W}r")
        ref = etree.SubElement(ref_run, f"{W}commentReference")
        ref.set(f"{W}id", cid)
        first_el.addprevious(start)
        last_el.addnext(end)
        end.addnext(ref_run)

        comment = etree.SubElement(self.root, f"{W}comment")
        comment.set(f"{W}id", cid)
        comment.set(f"{W}author", self.rev.author)
        comment.set(f"{W}date", self.rev.date)
        initials = "".join(w[0] for w in self.rev.author.split() if w)[:4]
        if initials:
            comment.set(f"{W}initials", initials)
        for i, line in enumerate((text or "").split("\n")):
            p = etree.SubElement(comment, f"{W}p")
            if i == 0:
                r0 = etree.SubElement(p, f"{W}r")
                etree.SubElement(r0, f"{W}annotationRef")
            r = etree.SubElement(p, f"{W}r")
            t = etree.SubElement(r, f"{W}t")
            t.set(XML_SPACE, "preserve")
            t.text = line
        self.added += 1

    def save(self) -> None:
        if not self.added:
            return
        files = self.doc.files
        files[COMMENTS_PART] = self.etree.tostring(self.root, xml_declaration=True, encoding="UTF-8", standalone=True)
        ct = self.etree.fromstring(files["[Content_Types].xml"])
        if not any(o.get("PartName") == "/word/comments.xml" for o in ct.findall(f"{{{CT_NS}}}Override")):
            o = self.etree.SubElement(ct, f"{{{CT_NS}}}Override")
            o.set("PartName", "/word/comments.xml")
            o.set("ContentType", COMMENTS_CT)
            files["[Content_Types].xml"] = self.etree.tostring(ct, xml_declaration=True, encoding="UTF-8", standalone=True)
        if DOC_RELS in files:
            rels = self.etree.fromstring(files[DOC_RELS])
        else:
            rels = self.etree.Element(f"{{{PKG_REL_NS}}}Relationships", nsmap={None: PKG_REL_NS})
        existing = rels.findall(f"{{{PKG_REL_NS}}}Relationship")
        if not any(r.get("Type") == COMMENTS_REL for r in existing):
            ids = {r.get("Id") for r in existing}
            n = 1
            while f"rId{n}" in ids:
                n += 1
            r = self.etree.SubElement(rels, f"{{{PKG_REL_NS}}}Relationship")
            r.set("Id", f"rId{n}")
            r.set("Type", COMMENTS_REL)
            r.set("Target", "comments.xml")
        files[DOC_RELS] = self.etree.tostring(rels, xml_declaration=True, encoding="UTF-8", standalone=True)


# --- bibliography --------------------------------------------------------------------


def _bibliography_block(etree, rev: _Revisions, entries: list[str], ppr=None, rpr=None) -> list:
    """New paragraphs holding a ZOTERO_BIBL field with *entries*."""
    entries = entries or ["{Bibliography: click Refresh in Word's Zotero tab}"]
    paragraphs = []

    def run(*children):
        r = etree.Element(f"{W}r")
        if rpr is not None:
            r.append(deepcopy(rpr))
        for c in children:
            r.append(c)
        return r

    def fld(kind):
        el = etree.Element(f"{W}fldChar")
        el.set(f"{W}fldCharType", kind)
        return el

    def text(value):
        t = etree.Element(f"{W}t")
        t.set(XML_SPACE, "preserve")
        t.text = value
        return t

    for i, entry in enumerate(entries):
        p = etree.Element(f"{W}p")
        if ppr is not None:
            p.append(deepcopy(ppr))
        runs = []
        if i == 0:
            instr = etree.Element(f"{W}instrText")
            instr.set(XML_SPACE, "preserve")
            instr.text = wc.BIBL_CODE
            runs += [run(fld("begin")), run(instr), run(fld("separate"))]
        runs.append(run(text(entry)))
        if i == len(entries) - 1:
            runs.append(run(fld("end")))
        for el in rev.wrap_inserted(runs):
            p.append(el)
        rev.mark_paragraph_inserted(p)
        paragraphs.append(p)
    return paragraphs


def _cited_item_keys(doc: _Doc) -> list[str]:
    """Item keys of every live citation, in document order (after edits)."""
    keys: list[str] = []
    for root in doc.trees.values():
        for f in _scan_fields(root):
            if "ADDIN ZOTERO_ITEM" not in f["instr"]:
                continue
            items, _ = _parse_citation_code(f["instr"])
            for it in items:
                if it.key and it.key not in keys:
                    keys.append(it.key)
    return keys


def _bibliography_entries(doc: _Doc, by_item_key: dict[str, wc.ResolvedItem]) -> tuple[list[str], list[str]]:
    entries, missing = [], []
    for key in _cited_item_keys(doc):
        item = by_item_key.get(key)
        if item is None:
            missing.append(key)
            continue
        entries.append(item.bibliography or item.citation or key)
    entries.sort(key=lambda e: e.lower())
    return entries, missing


# --- markers -------------------------------------------------------------------------


def _convert_markers(doc: _Doc, rev: _Revisions, resolved, report: EditReport, dry_run: bool) -> None:
    etree = doc.etree
    for part, root in doc.trees.items():
        for p in list(root.iter(f"{W}p")):
            text = wc._paragraph_text(p)
            if "@" not in text:
                continue
            for cluster in sorted(wc.find_clusters(text), key=lambda c: c.start, reverse=True):
                if any(c.key not in resolved for c in cluster.items):
                    report.skipped_markers.append(cluster.raw)
                    continue
                if dry_run:
                    report.markers_converted += 1
                    continue
                runs = _isolate(etree, p, cluster.start, cluster.end, set())
                if isinstance(runs, str):
                    report.skipped_markers.append(cluster.raw)
                    continue
                container = runs[0].getparent()
                rpr = _run_rpr(runs[0])
                if rev.tracked and container.tag == f"{W}ins" and not rev.is_own(container):
                    report.skipped_markers.append(cluster.raw)
                    continue
                if rev.is_own(container):
                    # Text this edit inserted: the field simply takes its place.
                    shown = wc.provisional_text(cluster, resolved)
                    code = wc.citation_field_code(cluster, resolved, shown)
                    _place_before(runs[0], wc._field_runs(etree, rpr, code, shown))
                    for r in runs:
                        container.remove(r)
                else:
                    new, _ = _field_elements(etree, rev, rpr, cluster, resolved)
                    _place_before(runs[0], new)
                    rev.delete_runs(runs)
                report.markers_converted += 1


# --- the edit ------------------------------------------------------------------------


REUSE_KEY_RE = re.compile(r"^C(\d+)(?:\.(\d+))?$")


def _short_citation(data: dict) -> str:
    names = [a.get("family") or a.get("literal") or "" for a in (data.get("author") or data.get("editor") or [])]
    names = [n for n in names if n]
    if not names:
        who = (data.get("title") or "")[:40]
    elif len(names) == 1:
        who = names[0]
    elif len(names) == 2:
        who = f"{names[0]} & {names[1]}"
    else:
        who = f"{names[0]} et al."
    parts = (data.get("issued") or {}).get("date-parts") or []
    year = str(parts[0][0]) if parts and parts[0] else "n.d."
    return f"({who}, {year})"


def _reuse_item(key: str, by_id: dict[str, FieldInfo]) -> wc.ResolvedItem | None:
    m = REUSE_KEY_RE.match(key)
    f = by_id.get(f"C{m.group(1)}") if m else None
    if f is None or not f.items:
        return None
    n = int(m.group(2) or 1)
    if not 1 <= n <= len(f.items):
        return None
    it = f.items[n - 1]
    data = dict(it.entry.get("itemData") or {})
    if not it.uri or not data:
        return None
    short = _short_citation(data)
    container = str(data.get("container-title") or "").rstrip(".")
    title = str(data.get("title") or "").rstrip(".")
    bib = f"{short[1:-1]}. {title}." + (f" {container}." if container else "")
    return wc.ResolvedItem(key=it.key, uri=it.uri, item_data=data, citation=short,
                           bibliography=bib, item_id=it.entry.get("id"))


def _same_work_elsewhere(resolved: dict, inv: Inventory, by_id: dict[str, FieldInfo],
                         report: EditReport, reuse: bool) -> None:
    """Handle new citations of a work the document already cites as another item.

    With *reuse*, the new citation points to that item instead (from whatever
    library it came from), so the bibliography lists the work once; for a
    co-author who owns that item it is an ordinary citation from their own
    library. Without, the report warns.
    """
    existing: dict[str, tuple[str, int, CitedItem]] = {}
    for c in inv.citations:
        for n, it in enumerate(c.items, start=1):
            for wk in _work_keys(it.doi, it.title, it.year):
                existing.setdefault(wk, (c.id, n, it))
    for key, item in list(resolved.items()):
        if REUSE_KEY_RE.match(key):
            continue
        data = item.item_data or {}
        parts = (data.get("issued") or {}).get("date-parts") or []
        year = str(parts[0][0]) if parts and parts[0] else ""
        hit = next((existing[k] for k in _work_keys(data.get("DOI") or "", data.get("title") or "", year)
                    if k in existing), None)
        if not hit or hit[2].uri == item.uri:
            continue
        cid, n, it = hit
        where = it.library or "another library"
        reused = _reuse_item(f"{cid}.{n}", by_id) if reuse else None
        if reused is not None:
            resolved[key] = reused
            report.notes.append(
                f"{key} ({it.label}) is already cited in {cid} ({where}): the new citation "
                "uses that item, so the bibliography lists it once."
            )
        else:
            report.warnings.append(
                f"{key} ({it.label}) is already cited in {cid} as a different item"
                f" ({where}); this adds a second entry to the bibliography."
                f" Cite [@{cid}] instead to reuse that one."
            )


def _merge_duplicates(inv: Inventory, by_id: dict[str, FieldInfo], keep: str | None) -> list[str]:
    """Point every citation of a duplicated work to one item.

    For each work cited as several items (inv.duplicates), the item kept is
    the one in citation *keep* when that citation is in the group, else the
    one cited most often, else the first. Only the field instructions change
    (which item a citation links to); the visible text stays, and Refresh
    renders it from the kept item. Returns a line per merged work.
    """
    done: list[str] = []
    for group in inv.duplicates:
        counts: dict[str, int] = {}
        first: dict[str, tuple[str, CitedItem]] = {}
        for cid, it in group:
            counts[it.uri] = counts.get(it.uri, 0) + 1
            first.setdefault(it.uri, (cid, it))
        keeper = None
        if keep:
            keeper = next((it for cid, it in group if cid == keep), None)
        if keeper is None:
            best = max(counts.values())
            keeper = next(it for cid, it in group if counts[it.uri] == best)
        others = {it.uri for _, it in group if it.uri != keeper.uri}
        changed = []
        for cid in dict.fromkeys(cid for cid, it in group if it.uri in others):
            f = by_id[cid]
            if _rewrite_citation_items(f, others, keeper):
                changed.append(cid)
        if changed:
            done.append(f"{keeper.label}: {', '.join(changed)} now cite the item in "
                        f"{first[keeper.uri][0]} ({keeper.library or 'unknown library'})")
    return done


def _rewrite_citation_items(f: FieldInfo, others: set[str], keeper: CitedItem) -> bool:
    instr_nodes = [c for r in f.runs for c in r if c.tag == f"{W}instrText"]
    code = "".join(t.text or "" for t in instr_nodes)
    m = re.search(r"CSL_CITATION\s+(\{.*\})\s*$", code, re.S)
    if not m or not instr_nodes:
        return False
    try:
        payload = json.loads(m.group(1))
    except ValueError:
        return False
    entries = []
    seen: set[str] = set()
    for entry in payload.get("citationItems") or []:
        uris = entry.get("uris") or entry.get("uri") or []
        uri = uris[0] if isinstance(uris, list) and uris else (uris if isinstance(uris, str) else "")
        if uri in others:
            entry = {**entry, "uris": [keeper.uri], "itemData": keeper.entry.get("itemData") or entry.get("itemData")}
            if keeper.entry.get("id") is not None:
                entry["id"] = keeper.entry["id"]
                entry["itemData"] = {**entry["itemData"], "id": keeper.entry["id"]}
            uri = keeper.uri
        if uri in seen:
            continue  # the same work twice in one citation
        seen.add(uri)
        entries.append(entry)
    payload["citationItems"] = entries
    instr_nodes[0].text = code[: m.start(1)] + json.dumps(payload, ensure_ascii=False) + code[m.end(1):]
    for t in instr_nodes[1:]:
        t.text = ""
    return True


def _parse_edits(edits) -> list[dict]:
    if edits is None:
        return []
    if isinstance(edits, str):
        try:
            edits = json.loads(edits)
        except ValueError as e:
            raise WordCitationError(f"edits is not valid JSON: {e}") from e
    if isinstance(edits, dict):
        edits = [edits]
    if not isinstance(edits, list) or not all(isinstance(e, dict) for e in edits):
        raise WordCitationError("edits must be a list of objects, each with an 'op'.")
    return edits


KNOWN_OPS = {
    "replace_citation", "delete_citation", "replace_text", "comment",
    "insert_bibliography", "rebuild_bibliography", "delete_bibliography", "replace_reference_list",
    "merge_duplicates",
}


def edit_docx(
    input_path: str | os.PathLike,
    resolver: wc.Resolver,
    *,
    write_mode: str,
    edits=None,
    output_path: str | os.PathLike | None = None,
    author: str | None = None,
    style: str | None = None,
    locale: str | None = None,
    zotero_version: str = "7.0",
    dry_run: bool = False,
    reuse_cited: bool = True,
) -> EditReport:
    """Apply *edits* (and convert any markers) in a .docx.

    ``write_mode``: ``new_file`` writes "<name> (Zotero).docx" (or
    ``output_path``) and leaves the original alone; ``tracked_changes`` and
    ``overwrite`` write over the original, as tracked changes or outright,
    after copying it to a "Zotero backups" folder beside it.
    """
    if write_mode not in WRITE_MODES:
        raise WordCitationError(
            "write_mode is required: ask the user whether to write a new file "
            "('new_file'), tracked changes in the original ('tracked_changes'), "
            "or overwrite the original ('overwrite'); both of the last keep a backup."
        )
    ops = _parse_edits(edits)
    for op in ops:
        if op.get("op") not in KNOWN_OPS:
            raise WordCitationError(f"Unknown edit op {op.get('op')!r}; known: {', '.join(sorted(KNOWN_OPS))}.")

    src = Path(input_path).expanduser()
    if not src.is_file():
        raise WordCitationError(f"No such file: {src}")
    if src.suffix.lower() != ".docx":
        raise WordCitationError("Only .docx files are supported (save .doc files as .docx first).")
    if write_mode == "new_file":
        dest = Path(output_path).expanduser() if output_path else wc.default_output_path(src)
        if dest.resolve() == src.resolve():
            raise WordCitationError("For new_file, output_path must differ from the original.")
    else:
        dest = src
    if not dry_run:
        for path in {src, dest}:
            lock = wc.word_lock_file(path)
            if lock is not None:
                raise WordCitationError(f"{path.name} is open in Word (found {lock.name}). Close it first.")

    doc = _Doc(src)
    etree = doc.etree
    inv, by_id = _read(doc)
    existing_prefs = wc.read_document_prefs(doc.files.get("docProps/custom.xml"))
    doc_style, doc_locale = wc.prefs_style(existing_prefs)
    use_style = wc.style_id(style) if style else (doc_style or wc.style_id("apa"))
    report = EditReport(
        output_path=str(dest), write_mode=write_mode, dry_run=dry_run,
        author=author or _document_author(doc.files), style=wc.style_short_name(use_style),
    )
    rev = _Revisions(etree, write_mode == "tracked_changes", report.author, _max_annotation_id(doc) + 1)
    refs = {r.id: r for r in inv.reference_lists}

    # Resolve every key any edit or marker needs, in one call.
    clusters: dict[int, wc.Cluster] = {}
    keys: set[str] = set()
    for i, op in enumerate(ops):
        if op["op"] == "replace_citation":
            cl = _cluster_from_marker(op.get("marker", ""))
            if cl:
                clusters[i] = cl
                keys.update(c.key for c in cl.items)
        elif op["op"] == "replace_text":
            for cl in wc.find_clusters(str(op.get("with", ""))):
                keys.update(c.key for c in cl.items)
    for m in inv.markers:
        for cl in wc.find_clusters(m.text):
            keys.update(c.key for c in cl.items)
    needs_bib = bool(inv.bibliography_markers) or any(
        op["op"] in ("insert_bibliography", "rebuild_bibliography", "replace_reference_list") for op in ops
    )
    existing_keys: set[str] = set()
    if needs_bib:
        for c in inv.citations:
            existing_keys.update(it.key for it in c.items if it.key)
        keys |= existing_keys
    # "C5" (or "C5.2" for its second item) cites what citation C5 cites, from
    # whichever library it came from: in a shared document that avoids a
    # second bibliography entry for a work a co-author already cited.
    reused = {k: _reuse_item(k, by_id) for k in keys if REUSE_KEY_RE.match(k)}
    reused = {k: v for k, v in reused.items() if v is not None}
    lookup = sorted(k for k in keys if not REUSE_KEY_RE.match(k))
    resolved = resolver(lookup) if lookup else {}
    resolved.update(reused)
    report.unresolved = sorted(k for k in keys if k not in resolved and k not in existing_keys)
    _same_work_elsewhere(resolved, inv, by_id, report, reuse_cited)
    # Bibliography entries: items cited from other libraries (a co-author's,
    # a group's) come from the copy stored in their citation; items from this
    # library from Zotero's own rendering.
    by_item_key: dict[str, wc.ResolvedItem] = {}
    for c in inv.citations:
        for n in range(1, len(c.items) + 1):
            item = _reuse_item(f"{c.id}.{n}", by_id)
            if item is not None:
                by_item_key.setdefault(item.key, item)
    for r in resolved.values():
        if r.key not in by_item_key or by_item_key[r.key].uri == r.uri:
            by_item_key[r.key] = r

    field_ts: set[int] = set()
    for c in inv.citations + inv.bibliographies:
        field_ts.update(id(t) for t in c.result_ts)
    comments = _Comments(doc, rev)

    def target_field(op, kind: str) -> FieldInfo | None:
        fid = str(op.get("citation") or op.get("bibliography") or "")
        f = by_id.get(fid)
        if f is None or f.kind != kind:
            report.add(op, "skipped", f"no {kind} {fid!r} in this document")
            return None
        expect = op.get("expect")
        if expect and expect not in f.text:
            report.add(op, "skipped", f"its text is now {f.text!r}, not {expect!r}; inspect again")
            return None
        return f

    # 0. Merging duplicates changes only field instructions, not runs or text.
    for op in [o for o in ops if o["op"] == "merge_duplicates"]:
        if not inv.duplicates:
            report.add(op, "skipped", "no work is cited as more than one item")
            continue
        lines = _merge_duplicates(inv, by_id, op.get("keep")) if not dry_run else [
            f"{g[0][1].label}: {len(g)} citations" for g in inv.duplicates]
        report.add(op, "done", "; ".join(lines))

    # 1. Comments: they add no text, so later offsets stay valid.
    for op in [o for o in ops if o["op"] == "comment"]:
        text = str(op.get("text") or "").strip()
        if not text:
            report.add(op, "skipped", "no comment text")
            continue
        if op.get("citation"):
            f = target_field(op, "citation")
            if f is None:
                continue
            if not _same_parent(f.runs):
                report.add(op, "skipped", "citation spans several elements")
                continue
            if not dry_run:
                comments.add(f.runs[0], f.runs[-1], text)
            report.add(op, "done")
            continue
        p = doc.paras.get(str(op.get("paragraph") or ""))
        if p is None:
            report.add(op, "skipped", f"no paragraph {op.get('paragraph')!r}")
            continue
        find = op.get("find")
        if find:
            ptext = wc._paragraph_text(p)
            pos = ptext.find(find)
            if pos < 0:
                report.add(op, "skipped", f"{find!r} not found in {op.get('paragraph')}")
                continue
            if dry_run:
                report.add(op, "done")
                continue
            runs = _isolate(etree, p, pos, pos + len(find), set())
            if isinstance(runs, str):
                report.add(op, "skipped", runs)
                continue
            comments.add(runs[0], runs[-1], text)
        else:
            content = [c for c in p if c.tag != f"{W}pPr"]
            if not content:
                report.add(op, "skipped", "empty paragraph")
                continue
            if not dry_run:
                comments.add(content[0], content[-1], text)
        report.add(op, "done")

    # 2. Text replacements, right to left within each paragraph.
    text_ops: dict[str, list[tuple[int, dict]]] = {}
    for op in [o for o in ops if o["op"] == "replace_text"]:
        pid = str(op.get("paragraph") or "")
        p = doc.paras.get(pid)
        find = str(op.get("find") or "")
        if p is None or not find:
            report.add(op, "skipped", "needs 'paragraph' and 'find'")
            continue
        ptext = wc._paragraph_text(p)
        occurrence = int(op.get("occurrence") or 1)
        pos = -1
        for _ in range(occurrence):
            pos = ptext.find(find, pos + 1)
            if pos < 0:
                break
        if pos < 0:
            report.add(op, "skipped", f"{find!r} not found in {pid}")
            continue
        text_ops.setdefault(pid, []).append((pos, op))
    for pid, items in text_ops.items():
        p = doc.paras[pid]
        for pos, op in sorted(items, key=lambda x: x[0], reverse=True):
            find, new = str(op["find"]), str(op.get("with", ""))
            if dry_run:
                report.add(op, "done")
                continue
            runs = _isolate(etree, p, pos, pos + len(find), field_ts)
            if isinstance(runs, str):
                report.add(op, "skipped", runs)
                continue
            if rev.tracked and runs[0].getparent().tag == f"{W}ins" and not rev.is_own(runs[0].getparent()):
                report.add(op, "skipped", "the text is part of an earlier tracked change; accept it first")
                continue
            rpr = _run_rpr(runs[0])
            if new:
                _place_before(runs[0], rev.wrap_inserted([_text_run(etree, rpr, new)]))
            rev.delete_runs(runs)
            report.add(op, "done")

    # 3. Citation fields.
    for i, op in enumerate(ops):
        if op["op"] not in ("replace_citation", "delete_citation"):
            continue
        f = target_field(op, "citation")
        if f is None:
            continue
        if not _same_parent(f.runs):
            report.add(op, "skipped", "citation spans several elements (e.g. a hyperlink)")
            continue
        if op["op"] == "replace_citation":
            cl = clusters.get(i)
            if cl is None:
                report.add(op, "skipped", f"not a citation marker: {op.get('marker')!r}")
                continue
            missing = [c.key for c in cl.items if c.key not in resolved]
            if missing:
                report.add(op, "skipped", "not in the library: " + ", ".join(missing))
                continue
            if not dry_run:
                new, _ = _field_elements(etree, rev, _run_rpr(f.runs[0]), cl, resolved)
                _place_before(f.runs[0], new)
                if not rev.delete_runs(f.runs):
                    report.add(op, "skipped", "inside an earlier tracked change; accept it first")
                    continue
            report.add(op, "done", f"{f.text!r} → {wc.provisional_text(cl, resolved)!r}")
        else:
            if not dry_run:
                space = _space_run_before(etree, f.runs[0], f.runs[-1])
                if not rev.delete_runs(([space] if space is not None else []) + f.runs):
                    report.add(op, "skipped", "inside an earlier tracked change; accept it first")
                    continue
            report.add(op, "done", f"removed {f.text!r}")

    # 4. Markers typed in the text, and those the replacements above inserted.
    _convert_markers(doc, rev, resolved, report, dry_run)

    # 5. Bibliographies, built from what the document cites after the edits.
    def entries() -> list[str]:
        found, missing = _bibliography_entries(doc, by_item_key)
        for key in missing:
            if key not in report.unresolved:
                report.unresolved.append(key)
        return found

    body = doc.trees["word/document.xml"].find(f"{W}body")
    for op in ops:
        kind = op["op"]
        if kind == "delete_bibliography":
            f = target_field(op, "bibliography")
            if f is None:
                continue
            if not dry_run:
                for p in _bibliography_paragraphs(f):
                    rev.delete_paragraph(p)
            report.add(op, "done")
        elif kind == "rebuild_bibliography":
            f = target_field(op, "bibliography")
            if f is None:
                continue
            if not dry_run:
                old = _bibliography_paragraphs(f)
                new = _bibliography_block(etree, rev, entries(), old[0].find(f"{W}pPr"), _run_rpr(f.runs[0]))
                _place_before(old[0], new)
                for p in old:
                    rev.delete_paragraph(p)
                report.bibliography_written = True
            report.add(op, "done")
        elif kind == "replace_reference_list":
            rl = refs.get(str(op.get("reference_list") or ""))
            if rl is None:
                report.add(op, "skipped", f"no reference list {op.get('reference_list')!r}")
                continue
            if not dry_run:
                first = rl.paragraphs[0]
                new = _bibliography_block(etree, rev, entries(), first.find(f"{W}pPr"))
                _place_before(first, new)
                for p in rl.paragraphs:
                    rev.delete_paragraph(p)
                report.bibliography_written = True
            report.add(op, "done", f"{len(rl.entries)} typed entries replaced")
        elif kind == "insert_bibliography":
            after = op.get("after")
            anchor = doc.paras.get(str(after)) if after else None
            if after and (anchor is None or doc.para_part.get(str(after)) != "word/document.xml"):
                report.add(op, "skipped", f"no body paragraph {after!r}")
                continue
            if not dry_run:
                new = _bibliography_block(etree, rev, entries())
                if anchor is not None:
                    _place_after(anchor, new)
                else:
                    sect = body.find(f"{W}sectPr")
                    if sect is not None:
                        _place_before(sect, new)
                    else:
                        for p in new:
                            body.append(p)
                report.bibliography_written = True
            report.add(op, "done")

    for pid in inv.bibliography_markers:
        p = doc.paras[pid]
        if dry_run:
            report.bibliography_written = True
            continue
        content_runs = [r for r in p.iter(f"{W}r") if not wc._inside(r, {f"{W}del"})]
        rpr = _run_rpr(content_runs[0]) if content_runs else None
        new = _bibliography_block(etree, rev, entries(), p.find(f"{W}pPr"), rpr)
        _place_before(p, new)
        rev.delete_paragraph(p)
        report.bibliography_written = True

    if dry_run:
        return report

    changed = any(r.status == "done" for r in report.results) or report.markers_converted or report.bibliography_written
    if not changed:
        return report
    comments.save()
    report.comments = comments.added
    for part, root in doc.trees.items():
        doc.files[part] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    if existing_prefs is None and (report.markers_converted or report.bibliography_written or any(
        r.op == "replace_citation" and r.status == "done" for r in report.results
    )):
        doc.files["docProps/custom.xml"] = wc.write_document_prefs(
            doc.files.get("docProps/custom.xml"),
            wc.document_prefs_xml(use_style, locale or doc_locale or "en-US", zotero_version),
        )
        wc._ensure_custom_part_registered(doc.files)
        report.prefs_written = True
    if write_mode != "new_file":
        backup_dir = src.parent / BACKUP_DIR
        backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
        backup = backup_dir / f"{src.stem} {stamp}{src.suffix}"
        shutil.copy2(src, backup)
        report.backup_path = str(backup)
    wc._write_zip(src, dest, doc.infos, doc.files, backup=False)
    return report

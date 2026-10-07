"""Live Zotero citations in Word documents.

Turns citation markers typed in a ``.docx`` into the same fields the Zotero
Word plugin writes, so the result behaves exactly as if each citation had
been inserted with *Add/Edit Citation*: *Refresh* updates it, a style change
re-renders it, and a ``{{bibliography}}`` paragraph becomes a bibliography
that tracks the citations.

Markers use Pandoc's citation syntax, which keeps them readable in the draft:

=================================  ==========================================
``[@KEY]``                         one item (Zotero item key or BBT citekey)
``[@KEY1; @KEY2]``                 several items in one citation
``[@KEY, p. 12]``                  a locator (p., pp., ch., sec., para., fig.)
``[see @KEY, pp. 3-5; @KEY2]``     prefix text, locator
``[-@KEY]``                        suppress the author: "(2009)"
``{{bibliography}}``               on a paragraph of its own: the bibliography
=================================  ==========================================

What Zotero stores in a citation field (reverse-engineered from documents
the plugin wrote, and stable since Zotero 5)::

    ADDIN ZOTERO_ITEM CSL_CITATION {"citationID": "...",
        "properties": {"formattedCitation": "...", "plainCitation": "...",
                       "noteIndex": 0},
        "citationItems": [{"id": 861, "uris": ["http://zotero.org/users/<id>/items/<KEY>"],
                           "itemData": {...CSL JSON...}, "locator": "12", "label": "page"}],
        "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json"}

The plugin finds the item by its URI; ``itemData`` is the fallback for a
document opened without that library. ``plainCitation`` must equal the
field's visible text, or the plugin takes the citation for hand-edited and
asks before replacing it. Document preferences (style, field type) live in
the custom document properties ``ZOTERO_PREF_1..n`` in 255-character pieces.

The rewrite works on the XML with lxml and leaves every part it does not
need byte-for-byte untouched; namespace prefixes survive, which matters
because Word's ``mc:Ignorable`` attribute refers to them by name.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import shutil
import string
import tempfile
import zipfile
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
CUSTOM_PROPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
CUSTOM_PROPS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties"
CUSTOM_PROPS_CT = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
PROPS_FMTID = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"
CSL_SCHEMA = "https://github.com/citation-style-language/schema/raw/master/csl-citation.json"
BIBL_CODE = ' ADDIN ZOTERO_BIBL {"uncited":[],"omitted":[],"custom":[]} CSL_BIBLIOGRAPHY '

#: Parts that can carry citations. Headers and footers are left alone, as the
#: Zotero plugin does not cite there either.
TEXT_PARTS = ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml")

ITEM_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
#: A bracketed span containing an @; parse_cluster decides if it is a citation.
CLUSTER_SCAN_RE = re.compile(r"\[[^\[\]]*@[^\[\]]*\]")
#: One cited key: "@key" or "-@key" (suppress author), at the start of its
#: part or after whitespace, so "me@example.org" is never read as a key.
KEY_IN_PART_RE = re.compile(r"(?:^|(?<=\s))(?P<suppress>-)?@(?P<key>[^\s,;\]]+)")
#: A locator value: page-like numbers or roman numerals, possibly a list.
_LOC_PIECE = r"(?:\d[\d\-–—]*[a-z]?|[ivxlcIVXLC]+\b(?:[\-–—][ivxlcIVXLC]+\b)?)"
_LOC_VALUE = rf"{_LOC_PIECE}(?:\s*[,&]\s*{_LOC_PIECE})*"
_LABELLED_LOC_RE = re.compile(rf"^(?P<label>[A-Za-z§¶]+\.?)\s*(?P<loc>{_LOC_VALUE})(?P<suffix>.*)$", re.S)
_BARE_LOC_RE = re.compile(r"^(?P<loc>\d[\d\-–—]*[a-z]?(?:\s*[,&]\s*\d[\d\-–—]*[a-z]?)*)(?P<suffix>.*)$", re.S)
BIBLIOGRAPHY_MARKER_RE = re.compile(r"^\s*\{\{\s*bibliography\s*\}\}\s*$", re.IGNORECASE)

#: Locator labels as Pandoc writes them, mapped to CSL's locator types.
LOCATOR_LABELS: dict[str, str] = {
    "p.": "page", "pp.": "page", "p": "page", "pp": "page", "page": "page", "pages": "page",
    "ch.": "chapter", "chap.": "chapter", "chapter": "chapter", "chapters": "chapter",
    "sec.": "section", "section": "section", "sections": "section", "§": "section",
    "para.": "paragraph", "paras.": "paragraph", "paragraph": "paragraph", "¶": "paragraph",
    "fig.": "figure", "figs.": "figure", "figure": "figure", "figures": "figure",
    "vol.": "volume", "vols.": "volume", "volume": "volume",
    "l.": "line", "ll.": "line", "line": "line", "lines": "line",
    "n.": "note", "nn.": "note", "note": "note",
    "no.": "issue", "col.": "column", "art.": "article", "bk.": "book", "pt.": "part",
}
#: How a locator is shown in the provisional text (Refresh re-renders it).
LOCATOR_SHORT = {
    "page": ("p.", "pp."), "chapter": ("chapter", "chapters"), "section": ("section", "sections"),
    "paragraph": ("para.", "paras."), "figure": ("figure", "figures"), "volume": ("vol.", "vols."),
    "line": ("line", "lines"), "note": ("note", "notes"), "issue": ("no.", "nos."),
    "column": ("col.", "cols."), "article": ("art.", "arts."), "book": ("book", "books"),
    "part": ("part", "parts"),
}


class WordCitationError(Exception):
    """The document cannot be converted (unreadable, open in Word, ...)."""


# ---------------------------------------------------------------------------
# Marker parsing
# ---------------------------------------------------------------------------


@dataclass
class CiteItem:
    key: str
    prefix: str = ""
    suffix: str = ""
    locator: str = ""
    label: str = ""
    suppress_author: bool = False


@dataclass
class Cluster:
    start: int
    end: int
    raw: str
    items: list[CiteItem] = field(default_factory=list)


def _parse_locator(rest: str) -> tuple[str, str, str]:
    """Split the text after a key into (label, locator, suffix), Pandoc-style.

    ", p. 12" -> page 12; ", pp. 33-35, emphasis added" -> pages 33-35 plus a
    suffix; ", 12" -> page 12 (a bare number is a page); anything else is
    suffix text.
    """
    rest = rest.strip()
    if rest.startswith(","):
        rest = rest[1:].strip()
    if not rest:
        return "", "", ""

    def tidy(suffix: str) -> str:
        return suffix.strip().lstrip(",").strip()

    m = _LABELLED_LOC_RE.match(rest)
    if m:
        label = LOCATOR_LABELS.get(m.group("label").lower())
        if label:
            return label, m.group("loc").strip(), tidy(m.group("suffix"))
    m = _BARE_LOC_RE.match(rest)
    if m:
        return "page", m.group("loc").strip(), tidy(m.group("suffix"))
    return "", "", rest


def parse_cluster(body: str) -> list[CiteItem]:
    """Parse the inside of ``[...]`` into citation items; [] if not a citation."""
    items: list[CiteItem] = []
    for part in body.split(";"):
        m = KEY_IN_PART_RE.search(part)
        if not m:
            return []
        prefix = part[: m.start()].strip()
        label, locator, suffix = _parse_locator(part[m.end():])
        items.append(
            CiteItem(
                key=m.group("key").rstrip(".:"),
                prefix=prefix,
                suffix=suffix,
                locator=locator,
                label=label,
                suppress_author=bool(m.group("suppress")),
            )
        )
    return items


def find_clusters(text: str) -> list[Cluster]:
    """Every citation marker in *text*, in order."""
    found: list[Cluster] = []
    for m in CLUSTER_SCAN_RE.finditer(text):
        raw = m.group(0)
        items = parse_cluster(raw[1:-1])
        if items:
            found.append(Cluster(start=m.start(), end=m.end(), raw=raw, items=items))
    return found


# ---------------------------------------------------------------------------
# Resolved items and citation text
# ---------------------------------------------------------------------------


@dataclass
class ResolvedItem:
    key: str  # 8-character Zotero item key
    uri: str  # http://zotero.org/users/<id>/items/<KEY>
    item_data: dict[str, Any]  # CSL JSON
    citation: str = ""  # Zotero-rendered in-text citation, plain text
    bibliography: str = ""  # Zotero-rendered reference entry, plain text
    item_id: int | str | None = None


#: Resolves marker keys (item keys or citekeys) to items. Returns a mapping
#: from every key it could resolve; unresolved keys are simply absent.
Resolver = Callable[[list[str]], dict[str, ResolvedItem]]


def _strip_brackets(text: str) -> tuple[str, str, str]:
    text = text.strip()
    for open_, close in (("(", ")"), ("[", "]")):
        if text.startswith(open_) and text.endswith(close):
            return open_, text[1:-1].strip(), close
    return "", text, ""


def _year(item: ResolvedItem) -> str:
    parts = (item.item_data.get("issued") or {}).get("date-parts") or []
    if parts and parts[0]:
        return str(parts[0][0])
    raw = (item.item_data.get("issued") or {}).get("raw") or ""
    m = re.search(r"\d{4}", str(raw))
    return m.group(0) if m else "n.d."


def _locator_text(cite: CiteItem) -> str:
    if not cite.locator:
        return ""
    single, plural = LOCATOR_SHORT.get(cite.label, ("", ""))
    many = bool(re.search(r"[\-–—,]", cite.locator))
    word = plural if many else single
    return f"{word} {cite.locator}".strip()


def provisional_text(cluster: Cluster, resolved: dict[str, ResolvedItem]) -> str:
    """The visible text written into the field before Zotero first refreshes it.

    Built from Zotero's own rendering of each item, so for author-date styles
    it already reads right; Refresh then applies what only a whole-document
    pass can (sorting, disambiguation, "et al." rules, numbering).
    """
    pieces: list[str] = []
    open_, close = "(", ")"
    for cite in cluster.items:
        item = resolved[cite.key]
        o, inner, c = _strip_brackets(item.citation or "")
        if o:
            open_, close = o, c
        if cite.suppress_author or not inner:
            inner = _year(item) if cite.suppress_author or not inner else inner
        bits = [b for b in (cite.prefix, inner) if b]
        text = " ".join(bits)
        loc = _locator_text(cite)
        if loc:
            text = f"{text}, {loc}"
        if cite.suffix:
            text = f"{text}, {cite.suffix}" if not cite.suffix.startswith(",") else f"{text}{cite.suffix}"
        pieces.append(text)
    sep = "; " if open_ == "(" else ", "
    return f"{open_}{sep.join(pieces)}{close}"


def _random_id(n: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def citation_field_code(cluster: Cluster, resolved: dict[str, ResolvedItem], text: str) -> str:
    """The instruction text of a ZOTERO_ITEM field for *cluster*."""
    items: list[dict[str, Any]] = []
    for cite in cluster.items:
        item = resolved[cite.key]
        data = dict(item.item_data)
        item_id = item.item_id if item.item_id is not None else data.get("id", item.key)
        data["id"] = item_id
        entry: dict[str, Any] = {"id": item_id, "uris": [item.uri], "itemData": data}
        if cite.locator:
            entry["locator"] = cite.locator
            entry["label"] = cite.label or "page"
        if cite.prefix:
            entry["prefix"] = cite.prefix
        if cite.suffix:
            entry["suffix"] = cite.suffix
        if cite.suppress_author:
            entry["suppress-author"] = True
        items.append(entry)
    payload = {
        "citationID": _random_id(),
        "properties": {"formattedCitation": text, "plainCitation": text, "noteIndex": 0},
        "citationItems": items,
        "schema": CSL_SCHEMA,
    }
    return " ADDIN ZOTERO_ITEM CSL_CITATION " + json.dumps(payload, ensure_ascii=False) + " "


# ---------------------------------------------------------------------------
# WordprocessingML helpers
# ---------------------------------------------------------------------------


def _etree():
    try:
        from lxml import etree
    except ImportError as e:  # pragma: no cover - exercised only without lxml
        raise WordCitationError(
            "Writing Word citations needs lxml (pip install lxml)."
        ) from e
    return etree


def _run_of(t):
    node = t.getparent()
    while node is not None and node.tag != f"{W}r":
        node = node.getparent()
    return node


def _inside(node, tags: set[str]) -> bool:
    parent = node.getparent()
    while parent is not None:
        if parent.tag in tags:
            return True
        parent = parent.getparent()
    return False


def _text_nodes(paragraph) -> list:
    """The w:t nodes that make up a paragraph's visible text, in order."""
    nodes = []
    for t in paragraph.iter(f"{W}t"):
        if _inside(t, {f"{W}del", f"{W}instrText"}):
            continue
        nodes.append(t)
    return nodes


def _field_runs(etree, rpr, code: str, text: str) -> list:
    """The five runs of a complex field: begin, code, separate, result, end."""

    def run(*children):
        r = etree.Element(f"{W}r")
        if rpr is not None:
            r.append(deepcopy(rpr))
        for child in children:
            r.append(child)
        return r

    def fld(kind):
        el = etree.Element(f"{W}fldChar")
        el.set(f"{W}fldCharType", kind)
        return el

    instr = etree.Element(f"{W}instrText")
    instr.set(XML_SPACE, "preserve")
    instr.text = code
    result = etree.Element(f"{W}t")
    result.set(XML_SPACE, "preserve")
    result.text = text
    return [run(fld("begin")), run(instr), run(fld("separate")), run(result), run(fld("end"))]


def _split_run(etree, run, t, offset: int):
    """Split *run* at character *offset* of its text node *t*.

    Returns ``(left, right)``: *left* is the original run, keeping everything
    before the split, or None if that would be empty (the run is removed);
    *right* is a new run with the same properties holding the rest, inserted
    right after it, or None if there is no rest.
    """
    rpr = run.find(f"{W}rPr")
    children = [c for c in run if c.tag != f"{W}rPr"]
    idx = children.index(t)
    text = t.text or ""
    before, after = text[:offset], text[offset:]

    moving = children[idx + 1:]
    right = None
    if after or moving:
        right = etree.Element(f"{W}r")
        for attr, value in run.attrib.items():
            right.set(attr, value)
        if rpr is not None:
            right.append(deepcopy(rpr))
        if after:
            t2 = etree.SubElement(right, f"{W}t")
            t2.set(XML_SPACE, "preserve")
            t2.text = after
        for child in moving:
            run.remove(child)
            right.append(child)
        run.addnext(right)

    if before:
        t.text = before
        t.set(XML_SPACE, "preserve")
    else:
        run.remove(t)
    if not any(c.tag != f"{W}rPr" for c in run):
        run.getparent().remove(run)
        return None, right
    return run, right


@dataclass
class _Span:
    start_t: Any
    start_off: int
    end_t: Any
    end_off: int


def _locate(nodes: list, start: int, end: int) -> _Span | None:
    pos = 0
    start_t = end_t = None
    start_off = end_off = 0
    for t in nodes:
        n = len(t.text or "")
        if start_t is None and start < pos + n:
            start_t, start_off = t, start - pos
        if start_t is not None and end <= pos + n:
            end_t, end_off = t, end - pos
            break
        pos += n
    if start_t is None or end_t is None:
        return None
    return _Span(start_t, start_off, end_t, end_off)


def _replace_span(etree, paragraph, span: _Span, code: str, text: str) -> bool:
    """Replace the text in *span* with a citation field. False if impossible."""
    start_run, end_run = span.start_t.getparent(), span.end_t.getparent()
    if start_run is None or end_run is None or start_run.tag != f"{W}r" or end_run.tag != f"{W}r":
        return False
    if start_run.getparent() is not end_run.getparent():
        return False  # marker straddles a hyperlink, field or tracked change
    rpr = start_run.find(f"{W}rPr")
    rpr = deepcopy(rpr) if rpr is not None else None

    # Split at the end first: the start's text node stays in the left part.
    # When the marker ends exactly where its run ends there is no tail run,
    # so a placeholder marks where the marker stops; without it everything
    # after the marker in the paragraph would be taken for marker text.
    _, tail = _split_run(etree, end_run, span.end_t, span.end_off)
    stop = tail
    if stop is None:
        stop = etree.Element(f"{W}proofErr")  # placeholder, removed below
        end_run.addnext(stop)
    _, marker_first = _split_run(etree, start_run, span.start_t, span.start_off)
    if marker_first is None:
        if tail is None:
            stop.getparent().remove(stop)
        return False

    parent = marker_first.getparent()
    doomed = []
    node = marker_first
    while node is not None and node is not stop:
        if node.tag == f"{W}r":
            doomed.append(node)
        node = node.getnext()
    if tail is None:
        parent.remove(stop)
    for r in _field_runs(etree, rpr, code, text):
        marker_first.addprevious(r)
    for r in doomed:
        parent.remove(r)
    return True


def _paragraph_text(paragraph) -> str:
    return "".join(t.text or "" for t in _text_nodes(paragraph))


# ---------------------------------------------------------------------------
# Document preferences (custom properties)
# ---------------------------------------------------------------------------


def style_id(style: str) -> str:
    style = (style or "apa").strip()
    if style.startswith("http"):
        return style
    return f"http://www.zotero.org/styles/{style}"


def style_short_name(style_uri: str) -> str:
    return style_uri.rstrip("/").rsplit("/", 1)[-1]


def document_prefs_xml(style: str, locale: str, zotero_version: str = "7.0") -> str:
    return (
        f'<data data-version="3" zotero-version="{html.escape(zotero_version)}">'
        f'<session id="{_random_id()}"/>'
        f'<style id="{html.escape(style_id(style))}" locale="{html.escape(locale)}" '
        'hasBibliography="1" bibliographyStyleHasBeenSet="1"/>'
        '<prefs><pref name="fieldType" value="Field"/></prefs></data>'
    )


def read_document_prefs(custom_xml: bytes | None) -> str | None:
    """The joined ZOTERO_PREF_n value, or None when the document has none."""
    if not custom_xml:
        return None
    etree = _etree()
    root = etree.fromstring(custom_xml)
    pieces: dict[int, str] = {}
    for prop in root.findall(f"{{{CUSTOM_PROPS_NS}}}property"):
        name = prop.get("name") or ""
        m = re.fullmatch(r"ZOTERO_PREF_(\d+)", name)
        if not m:
            continue
        value = prop.find(f"{{{VT_NS}}}lpwstr")
        pieces[int(m.group(1))] = (value.text or "") if value is not None else ""
    if not pieces:
        return None
    return "".join(pieces[i] for i in sorted(pieces))


def prefs_style(prefs: str | None) -> tuple[str | None, str | None]:
    """(style id, locale) recorded in a document's Zotero preferences."""
    if not prefs:
        return None, None
    m = re.search(r'<style id="([^"]+)"(?:[^>]*?locale="([^"]*)")?', prefs)
    if not m:
        return None, None
    return html.unescape(m.group(1)), (m.group(2) or None)


def write_document_prefs(custom_xml: bytes | None, prefs: str) -> bytes:
    """custom.xml with ZOTERO_PREF_1..n set to *prefs* (255-character pieces)."""
    etree = _etree()
    if custom_xml:
        root = etree.fromstring(custom_xml)
    else:
        root = etree.Element(f"{{{CUSTOM_PROPS_NS}}}Properties", nsmap={None: CUSTOM_PROPS_NS, "vt": VT_NS})
    for prop in list(root.findall(f"{{{CUSTOM_PROPS_NS}}}property")):
        if re.fullmatch(r"ZOTERO_PREF_\d+", prop.get("name") or ""):
            root.remove(prop)
    pids = [int(p.get("pid")) for p in root.findall(f"{{{CUSTOM_PROPS_NS}}}property") if (p.get("pid") or "").isdigit()]
    next_pid = max(pids + [1]) + 1
    for i in range(0, len(prefs), 255):
        prop = etree.SubElement(root, f"{{{CUSTOM_PROPS_NS}}}property")
        prop.set("fmtid", PROPS_FMTID)
        prop.set("pid", str(next_pid))
        prop.set("name", f"ZOTERO_PREF_{i // 255 + 1}")
        value = etree.SubElement(prop, f"{{{VT_NS}}}lpwstr")
        value.text = prefs[i:i + 255]
        next_pid += 1
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _ensure_custom_part_registered(files: dict[str, bytes]) -> None:
    """Declare docProps/custom.xml in [Content_Types].xml and _rels/.rels."""
    etree = _etree()
    ct_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    ct = etree.fromstring(files["[Content_Types].xml"])
    if not any(
        o.get("PartName") == "/docProps/custom.xml" for o in ct.findall(f"{{{ct_ns}}}Override")
    ):
        o = etree.SubElement(ct, f"{{{ct_ns}}}Override")
        o.set("PartName", "/docProps/custom.xml")
        o.set("ContentType", CUSTOM_PROPS_CT)
        files["[Content_Types].xml"] = etree.tostring(ct, xml_declaration=True, encoding="UTF-8", standalone=True)

    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    rels = etree.fromstring(files["_rels/.rels"])
    existing = rels.findall(f"{{{rel_ns}}}Relationship")
    if not any(r.get("Type") == CUSTOM_PROPS_REL for r in existing):
        ids = {r.get("Id") for r in existing}
        n = 1
        while f"rId{n}" in ids:
            n += 1
        r = etree.SubElement(rels, f"{{{rel_ns}}}Relationship")
        r.set("Id", f"rId{n}")
        r.set("Type", CUSTOM_PROPS_REL)
        r.set("Target", "docProps/custom.xml")
        files["_rels/.rels"] = etree.tostring(rels, xml_declaration=True, encoding="UTF-8", standalone=True)


# ---------------------------------------------------------------------------
# Bibliography
# ---------------------------------------------------------------------------


def _insert_bibliography(etree, paragraph, entries: list[str]) -> None:
    """Turn a {{bibliography}} paragraph into a ZOTERO_BIBL field.

    The field opens in this paragraph and closes in the last entry's, like
    the one the plugin writes; each entry is its own paragraph.
    """
    first_t = next(iter(_text_nodes(paragraph)), None)
    rpr = None
    if first_t is not None:
        run = _run_of(first_t)
        if run is not None and run.find(f"{W}rPr") is not None:
            rpr = run.find(f"{W}rPr")
            rpr = deepcopy(rpr)
    for child in list(paragraph):
        if child.tag != f"{W}pPr":
            paragraph.remove(child)
    entries = entries or ["{Bibliography: click Refresh in Word's Zotero tab}"]

    def run(*children):
        r = etree.SubElement(target, f"{W}r")
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

    target = paragraph
    instr = etree.Element(f"{W}instrText")
    instr.set(XML_SPACE, "preserve")
    instr.text = BIBL_CODE
    run(fld("begin"))
    run(instr)
    run(fld("separate"))
    run(text(entries[0]))
    anchor = paragraph
    for entry in entries[1:]:
        new_p = etree.Element(f"{W}p")
        ppr = paragraph.find(f"{W}pPr")
        if ppr is not None:
            new_p.append(deepcopy(ppr))
        anchor.addnext(new_p)
        anchor = new_p
        target = new_p
        run(text(entry))
    run(fld("end"))


# ---------------------------------------------------------------------------
# The conversion
# ---------------------------------------------------------------------------


@dataclass
class ConversionReport:
    output_path: str
    citations: int = 0
    items: int = 0
    bibliography: bool = False
    existing_citations: int = 0
    unresolved: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    style: str = ""
    prefs_written: bool = False
    dry_run: bool = False


def _count_existing(xml: bytes) -> int:
    return xml.count(b"ADDIN ZOTERO_ITEM")


def word_lock_file(path: Path) -> Path | None:
    """Word's owner file for *path*, if one exists (the document is open).

    Word names it after the document with the first characters replaced by
    "~$": none for short names, one for 7-character names, two beyond that.
    All three spellings are checked rather than reproducing the rule.
    """
    name = path.name
    for candidate in (f"~${name}", f"~${name[1:]}", f"~${name[2:]}"):
        lock = path.with_name(candidate)
        if lock.exists():
            return lock
    return None


def default_output_path(path: Path) -> Path:
    return path.with_name(f"{path.stem} (Zotero){path.suffix}")


def convert_docx(
    input_path: str | os.PathLike,
    resolver: Resolver,
    *,
    output_path: str | os.PathLike | None = None,
    in_place: bool = False,
    style: str | None = None,
    locale: str | None = None,
    zotero_version: str = "7.0",
    dry_run: bool = False,
) -> ConversionReport:
    """Convert citation markers in a .docx into live Zotero fields.

    Writes to ``output_path`` (default: "<name> (Zotero).docx" beside the
    input), or over the input when ``in_place`` -- refused while Word has the
    file open, and the original is kept as "<name>.bak.docx". With
    ``dry_run`` nothing is written; the report says what would happen.
    """
    etree = _etree()
    src = Path(input_path).expanduser()
    if not src.is_file():
        raise WordCitationError(f"No such file: {src}")
    if src.suffix.lower() != ".docx":
        raise WordCitationError("Only .docx files are supported (save .doc files as .docx first).")
    if in_place:
        dest = src
    else:
        dest = Path(output_path).expanduser() if output_path else default_output_path(src)
    lock = None if dry_run else word_lock_file(dest)
    if lock is not None:
        raise WordCitationError(
            f"{dest.name} is open in Word (found {lock.name}). "
            "Close it first, or write to a different output_path."
        )

    try:
        with zipfile.ZipFile(src) as zf:
            infos = zf.infolist()
            files = {info.filename: zf.read(info.filename) for info in infos}
    except zipfile.BadZipFile as e:
        raise WordCitationError(f"{src.name} is not a valid .docx file: {e}") from e
    if "word/document.xml" not in files:
        raise WordCitationError(f"{src.name} has no word/document.xml; is it a Word document?")

    existing_prefs = read_document_prefs(files.get("docProps/custom.xml"))
    doc_style, doc_locale = prefs_style(existing_prefs)
    use_style = style_id(style) if style else (doc_style or style_id("apa"))
    use_locale = locale or doc_locale or "en-US"
    report = ConversionReport(output_path=str(dest), style=style_short_name(use_style), dry_run=dry_run)

    parsed: dict[str, Any] = {}
    work: list[tuple[str, Any, Cluster]] = []
    bib_paragraphs: list[tuple[str, Any]] = []
    for part in TEXT_PARTS:
        if part not in files:
            continue
        report.existing_citations += _count_existing(files[part])
        root = etree.fromstring(files[part])
        parsed[part] = root
        for p in root.iter(f"{W}p"):
            text = _paragraph_text(p)
            if "@" in text:
                for cluster in find_clusters(text):
                    work.append((part, p, cluster))
            if part == "word/document.xml" and BIBLIOGRAPHY_MARKER_RE.match(text):
                bib_paragraphs.append((part, p))

    keys = sorted({c.key for _, _, cl in work for c in cl.items})
    resolved = resolver(keys) if keys else {}
    report.unresolved = [k for k in keys if k not in resolved]
    report.items = len([k for k in keys if k in resolved])

    # Right to left within each paragraph, so earlier offsets stay valid.
    by_paragraph: dict[int, list[tuple[str, Any, Cluster]]] = {}
    for entry in work:
        by_paragraph.setdefault(id(entry[1]), []).append(entry)
    cited_in_order: list[str] = []
    for entries in by_paragraph.values():
        for part, p, cluster in sorted(entries, key=lambda e: e[2].start, reverse=True):
            if any(c.key not in resolved for c in cluster.items):
                report.skipped.append(cluster.raw)
                continue
            text = provisional_text(cluster, resolved)
            code = citation_field_code(cluster, resolved, text)
            span = _locate(_text_nodes(p), cluster.start, cluster.end)
            if span is None or not (dry_run or _replace_span(etree, p, span, code, text)):
                report.skipped.append(cluster.raw)
                continue
            report.citations += 1
            for c in cluster.items:
                if c.key not in cited_in_order:
                    cited_in_order.append(c.key)

    if bib_paragraphs:
        report.bibliography = True
        if not dry_run:
            seen: set[str] = set()
            entries = []
            for key in cited_in_order:
                item = resolved[key]
                if item.key in seen:
                    continue
                seen.add(item.key)
                entries.append(item.bibliography or provisional_text(Cluster(0, 0, "", [CiteItem(key)]), resolved))
            entries.sort(key=lambda e: e.lower())
            _, p = bib_paragraphs[0]
            _insert_bibliography(etree, p, entries)

    if dry_run:
        return report

    if report.citations or report.bibliography:
        for part, root in parsed.items():
            files[part] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        if existing_prefs is None:
            files["docProps/custom.xml"] = write_document_prefs(
                files.get("docProps/custom.xml"),
                document_prefs_xml(use_style, use_locale, zotero_version),
            )
            _ensure_custom_part_registered(files)
            report.prefs_written = True

    _write_zip(src, dest, infos, files, backup=in_place)
    return report


def _write_zip(src: Path, dest: Path, infos: Iterable[zipfile.ZipInfo], files: dict[str, bytes], *, backup: bool) -> None:
    names = [i.filename for i in infos]
    extra = [n for n in files if n not in names]
    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=str(dest.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w") as out:
            for info in infos:
                out.writestr(info, files[info.filename], compress_type=info.compress_type)
            for name in extra:
                out.writestr(name, files[name], compress_type=zipfile.ZIP_DEFLATED)
        if backup and dest.exists():
            shutil.copy2(dest, dest.with_name(f"{dest.stem}.bak{dest.suffix}"))
        os.replace(tmp, dest)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# ---------------------------------------------------------------------------
# Zotero item -> CSL JSON (fallback when the API cannot export CSL JSON)
# ---------------------------------------------------------------------------

_CSL_TYPES = {
    "journalArticle": "article-journal", "magazineArticle": "article-magazine",
    "newspaperArticle": "article-newspaper", "book": "book", "bookSection": "chapter",
    "thesis": "thesis", "report": "report", "conferencePaper": "paper-conference",
    "webpage": "webpage", "preprint": "article", "manuscript": "manuscript",
    "presentation": "speech", "document": "document", "dataset": "dataset",
    "encyclopediaArticle": "entry-encyclopedia", "dictionaryEntry": "entry-dictionary",
    "blogPost": "post-weblog", "film": "motion_picture", "interview": "interview",
    "letter": "personal_communication", "patent": "patent", "statute": "legislation",
    "case": "legal_case", "map": "map", "computerProgram": "software",
}
_CSL_FIELDS = {
    "title": "title", "publicationTitle": "container-title", "bookTitle": "container-title",
    "proceedingsTitle": "container-title", "websiteTitle": "container-title",
    "volume": "volume", "issue": "issue", "pages": "page", "DOI": "DOI", "ISBN": "ISBN",
    "ISSN": "ISSN", "publisher": "publisher", "place": "publisher-place", "url": "URL",
    "edition": "edition", "abstractNote": "abstract", "shortTitle": "title-short",
    "university": "publisher", "institution": "publisher", "language": "language",
}
_CSL_CREATORS = {"author": "author", "editor": "editor", "translator": "translator", "bookAuthor": "container-author"}


def zotero_data_to_csl(data: dict[str, Any], item_id: Any = None) -> dict[str, Any]:
    """A reasonable CSL JSON rendering of a Zotero item's ``data``."""
    csl: dict[str, Any] = {"id": item_id or data.get("key"), "type": _CSL_TYPES.get(data.get("itemType", ""), "document")}
    for zf, cf in _CSL_FIELDS.items():
        value = data.get(zf)
        if value and cf not in csl:
            csl[cf] = value
    for creator in data.get("creators") or []:
        role = _CSL_CREATORS.get(creator.get("creatorType", "author"))
        if not role:
            continue
        if creator.get("name"):
            person = {"literal": creator["name"]}
        else:
            person = {"family": creator.get("lastName", ""), "given": creator.get("firstName", "")}
        csl.setdefault(role, []).append(person)
    date = str(data.get("date") or "")
    m = re.search(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", date)
    if m:
        csl["issued"] = {"date-parts": [[p for p in m.groups() if p]]}
    elif date:
        csl["issued"] = {"raw": date}
    return csl

"""Document structure: printed page numbers, headings, chapters, reference lists.

What the index records per passage beyond its text: the page number as printed
(for citing), the heading and chapter it falls under, and its section
(Introduction, Methods, ... References). Each comes from the most reliable
source the file offers, in order:

Printed page numbers
    1. The PDF's own page labels.
    2. Page numbers printed in the header or footer, by majority vote over the
       pages (a cover page, a chapter opener without a number or an OCR misread
       does not move it).
    3. The item's Pages field, when the PDF has exactly that many pages.
    Otherwise none: a passage then shows its PDF page, never a guess.

Headings
    1. The PDF's bookmarks, when they name the document's real sections.
    2. Candidate lines (short lines set apart by size, weight, font, colour,
       capitals, numbering or space), together with the printed table of
       contents, judged by Gemini; it can only pick candidates, never invent a
       heading, and its choice is checked against the reading order.
    3. Candidates whose text is a known section name (no API needed).

Reading the PDF happens in a child process (``python -m zotero_mcp.structure``)
because PyMuPDF can crash on damaged files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Bumped when the output changes, so stored labels from an older version are
#: recomputed.
STRUCTURE_VERSION = 3

SECTIONS = ("Abstract", "Introduction", "Methods", "Results", "Discussion", "Conclusion",
            "References", "Appendix", "Back matter")

# Section names in English, Dutch, French, Spanish, Portuguese, Italian and German. Matched against
# the whole heading after its numbering, or its first part before "and", ":"
# or "&" ("Results and discussion" -> Results).
_ALIASES: tuple[tuple[str, str], ...] = (
    (r"abstract|summary|samenvatting|resume|resumen|resumo|riassunto|sommario|zusammenfassung|synopsis|"
     r"highlights", "Abstract"),
    (r"introduction|background|theoretical (?:background|framework)|literature review|related work|"
     r"the present stud(?:y|ies)|present research|aims?|objectives?|hypothes[ie]s|rationale|"
     r"inleiding|introductie|achtergrond|probleemstelling|introduccion|introducao|introduzione|einleitung|"
     r"hintergrund", "Introduction"),
    (r"materials? and methods?|methods?|methodology|study design|design|participants?|subjects?|sample|"
     r"measures?|measurements?|instruments?|materials?|procedures?|data collection|data analys[ie]s|"
     r"statistical analys[ie]s|analys[ie]s|intervention|protocol|methode|methoden|methodologie|werkwijze|"
     r"onderzoeksopzet|metodo|metodos|metodi|metodologia|methodik|"
     # systematic and scoping reviews
     r"search strategy|literature search|search and selection|information sources|eligibility criteria|"
     r"inclusion criteria|exclusion criteria|inclusion and exclusion criteria|study selection|screening|"
     r"data extraction|data items|risk of bias(?: assessment)?|quality assessment|critical appraisal|"
     r"synthesis methods?|coding", "Methods"),
    (r"results?|findings|outcomes|resultaten|bevindingen|resultats|resultados|risultati|ergebnisse|"
     r"study characteristics|characteristics of (?:the )?included studies|included studies|themes", "Results"),
    (r"general discussion|discussion|limitations?|strengths and limitations|implications|"
     r"practical implications|future (?:research|directions)|discussie|beschouwing|discusion|discussao|"
     r"discussione|diskussion", "Discussion"),
    (r"conclusions?|concluding remarks|conclusie|conclusies|conclusion generale|conclusiones|conclusao|"
     r"conclusoes|considerac(?:ao|oes) finais|conclusioni|fazit|schlussfolgerung", "Conclusion"),
    (r"references?|reference list|bibliography|literature cited|works cited|cited literature|literatuur|"
     r"literatuurlijst|referenties|bronnen|bibliografie|bibliographie|references bibliographiques|referencias|"
     r"bibliografia|referencias bibliograficas|riferimenti bibliografici|literaturverzeichnis|literatur",
     "References"),
    (r"appendi(?:x|ces)(?: [a-z0-9]+)?|supplementary(?: [\w ]+)?|supporting information|bijlagen?|annexes?|"
     r"anexos?|apendices?|appendice|allegati|anhang", "Appendix"),
    (r"acknowledge?ments?|funding|author contributions?|conflicts? of interest|competing interests?|"
     r"declarations?|ethics(?: statement| approval)?|data availability(?: statement)?|disclosure|"
     r"dankwoord|woord vooraf|remerciements|agradecimientos|agradecimentos|ringraziamenti|danksagung",
     "Back matter"),
)
_ALIAS_RE = [(re.compile(rf"(?:{pat})", re.I), label) for pat, label in _ALIASES]
_NUMBERING_RE = re.compile(r"^\s*(?:(?:\d{1,2}(?:\.\d{1,2}){0,3}|[ivxlc]{1,6}|[a-h])[.)]?\s+|\d{1,2}(?:\.\d{1,2}){1,3}\s*)",
                           re.I)
_CHAPTER_RE = re.compile(r"^\s*(?:chapter|hoofdstuk|chapitre|cap[ií]tulo|kapitel|part|deel|partie|parte|teil)"
                         r"\s+(?:\d{1,3}|[ivxlc]{1,7}|one|two|three|four|five|six|seven|eight|nine|ten|"
                         r"eleven|twelve|een|twee|drie|vier|vijf|zes|zeven|acht|negen|tien)\b", re.I)


# A heading that names the introduction itself (not "Background" or "The present
# study", which APA papers use inside an introduction that has no heading).
_INTRO_WORD_RE = re.compile(r"\b(?:introduction|inleiding|introductie|introduccion|introducao|introduzione|"
                            r"einleitung)\b")


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


_ABSTRACT_LEAD_RE = re.compile(r"^\s*(?:abstract|summary|resumen|resumo|r[ée]sum[ée]|samenvatting|zusammenfassung|"
                               r"riassunto)\s*[:—–.-]\s*\S", re.I)
_APPENDIX_RE = re.compile(r"^(?:appendix|appendices|bijlage|bijlagen|annexe?s?|anexos?|anhang|allegato|apendice)\b")
#: "Study 2", "Experiment 1 and 2", "Phase 1": a part holding its own Method and Results.
_STUDY_RE = re.compile(r"^(?:study|studies|experiment|experiments|exp|phase|stage|studie|experimento|estudio|"
                       r"estudo|etude|studio)\s+(?:\d+|[ivx]+|one|two|three|four|five|[a-d])\b")


def is_study_heading(heading: str) -> bool:
    words = fold(_NUMBERING_RE.sub("", heading or ""))
    return bool(_STUDY_RE.match(words)) and not re.search(r"\b(?:method|methods|results?|discussion)\b", words)


def canonical_section(heading: str) -> str | None:
    """The section a heading opens, or None ("Kestrel surveys", "Study 2")."""
    if _ABSTRACT_LEAD_RE.match(heading or ""):
        return "Abstract"           # "Abstract: Teachers' professional development is ..."
    text = _NUMBERING_RE.sub("", heading or "").strip().rstrip(".:")
    words = fold(text)
    if _APPENDIX_RE.match(words):
        return "Appendix"           # "Appendix 2: Factor analyses ..."
    if not words or len(words) > 60:
        return None
    for pattern, label in _ALIAS_RE:
        if pattern.fullmatch(words):
            return label
    # "Results and discussion", "Analyses and results", "Discussion and conclusions":
    # the part that names the later stage of the paper wins.
    parts = re.split(r"\s+(?:and|en|et|y|und)\s+|\s*[:&]\s*", words)
    found = []
    for part in parts[:3]:
        for pattern, label in _ALIAS_RE:
            if pattern.fullmatch(part.strip()):
                found.append(label)
                break
    order = ["Results", "Discussion", "Conclusion", "Methods", "Introduction"]
    for label in order:
        if label in found:
            return label
    return found[0] if found else None


def second_section(heading: str) -> str | None:
    """The other section a combined heading names ("Results and Discussion" -> Discussion), as in
    JATS, where a section may carry two types."""
    first = canonical_section(heading)
    words = fold(_NUMBERING_RE.sub("", heading or ""))
    for part in re.split(r"\s+(?:and|en|et|y|e|und)\s+|\s*[:&]\s*", words)[:3]:
        for pattern, label in _ALIAS_RE:
            if pattern.fullmatch(part.strip()) and label != first and label in (
                    "Introduction", "Methods", "Results", "Discussion", "Conclusion"):
                return label
    return None


# ---------------------------------------------------------------------------
# Reading the PDF (child process)
# ---------------------------------------------------------------------------

_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
_CAPTION_RE = re.compile(r"^\s*(?:fig(?:ure)?|table|tab|scheme|box|plate|chart|graph|note|notes|source|"
                         r"figuur|tabel|bron)\b\.?\s*\d*", re.I)
_TOC_TITLE_RE = re.compile(r"^(?:table of )?contents?$|^brief contents$|^inhoud(?:sopgave|stafel)?$|^sommaire$|"
                           r"^table des matieres$|^indice$|^contenido$|^inhaltsverzeichnis$|^inhalt$", re.I)
_TOC_LINE_RE = re.compile(r"(?:\.{2,}|…|\s)\s*(?:\d{1,4}|[ivxlc]{1,6})\s*$", re.I)


def roman_value(token: str) -> int | None:
    t = token.lower()
    if not t or any(ch not in _ROMAN for ch in t) or len(t) > 7:
        return None
    total, prev = 0, 0
    for ch in reversed(t):
        v = _ROMAN[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    # Reject non-canonical forms ("iiii", "vx") by round-tripping.
    return total if to_roman(total) == t else None


def to_roman(n: int) -> str:
    out = []
    for value, sym in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"),
                       (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= value:
            out.append(sym)
            n -= value
    return "".join(out)


def _family(font: str) -> str:
    name = re.sub(r"^[A-Z]{6}\+", "", font or "")
    name = re.split(r"[-,]", name)[0]
    name = re.sub(r"\.(?:B|I|BI|R)$", "", name)
    name = re.sub(r"(?i)(bold|italic|oblique|semibold|medium|light|regular|roman|black|heavy|demi|mt|ps|it|bd)+$",
                  "", name)
    return name.lower()


def _is_bold(span: dict) -> bool:
    return bool(span.get("flags", 0) & 16) or bool(
        re.search(r"bold|black|heavy|semibold|demi|\.B$|\.BI$|[-,]B$|[-,]Bd|Bd$", span.get("font", ""), re.I))


def _is_italic(span: dict) -> bool:
    return bool(span.get("flags", 0) & 2) or bool(
        re.search(r"italic|oblique|\.I$|\.BI$|[-,]It|[-,]I$|Ital", span.get("font", ""), re.I))


def _is_coloured(color: int) -> bool:
    r, g, b = (color >> 16) & 255, (color >> 8) & 255, color & 255
    return max(r, g, b) - min(r, g, b) > 40


def scan_pdf(path: str, max_pages: int = 2000) -> dict:
    """Everything the structure needs from a PDF, as plain data."""
    import pymupdf

    out: dict[str, Any] = {"pages": 0, "labels": None, "toc": [], "margins": [], "candidates": [],
                           "contents": "", "running": [], "body": {}}
    with pymupdf.open(path) as doc:
        n = doc.page_count
        out["pages"] = n
        try:
            if doc.get_page_labels():
                out["labels"] = [doc[i].get_label() or "" for i in range(n)]
        except Exception:
            pass
        try:
            out["toc"] = [[int(lvl), str(title), int(page)] for lvl, title, page in doc.get_toc(simple=True)]
        except Exception:
            pass

        lines: list[dict] = []
        style_chars: Counter = Counter()
        size_chars: Counter = Counter()
        color_chars: Counter = Counter()
        heights: list[float] = []
        margins: list[list] = []
        for pno in range(min(n, max_pages)):
            page = doc[pno]
            width, height = page.rect.width or 1, page.rect.height or 1
            page_margin: list = []
            try:
                blocks = page.get_text("dict", flags=0)["blocks"]
            except Exception:
                margins.append(page_margin)
                continue
            prev_bottom = None
            for block in blocks:
                if block.get("type") != 0:
                    continue
                block_lines = [ln for ln in block.get("lines", []) if any(s["text"].strip() for s in ln["spans"])]
                for li, ln in enumerate(block_lines):
                    spans = [s for s in ln["spans"] if s["text"].strip()]
                    text = re.sub(r"\s+", " ", "".join(s["text"] for s in ln["spans"])).strip()
                    if not text:
                        continue
                    x0, y0, x1, y1 = ln["bbox"]
                    for s in spans:
                        k = len(s["text"].strip())
                        style_chars[(_family(s["font"]), round(s["size"], 1))] += k
                        size_chars[round(s["size"], 1)] += k
                        color_chars[s.get("color", 0)] += k
                    heights.append(y1 - y0)
                    in_margin = y1 < 0.085 * height or y0 > 0.915 * height
                    if in_margin:
                        page_margin.append(text)
                    first = spans[0]
                    dominant = max(spans, key=lambda s: len(s["text"]))
                    lines.append({
                        "p": pno, "y": round(y0, 1), "h": round(y1 - y0, 1), "x0": round(x0 / width, 3),
                        "x1": round(x1 / width, 3), "text": text[:300],
                        "size": round(dominant["size"], 1), "font": dominant["font"],
                        "bold": all(_is_bold(s) for s in spans), "italic": all(_is_italic(s) for s in spans),
                        "color": dominant.get("color", 0), "margin": in_margin,
                        "block_lines": len(block_lines), "li": li,
                        "gap": round(y0 - prev_bottom, 1) if prev_bottom is not None else None,
                        "first": {"text": first["text"].strip()[:120], "font": first["font"],
                                  "size": round(first["size"], 1), "bold": _is_bold(first),
                                  "italic": _is_italic(first), "color": first.get("color", 0)},
                        "spans": len(spans),
                    })
                    prev_bottom = y1
            margins.append(page_margin)

        out["margins"] = [_margin_numbers(m) for m in margins]
        if not style_chars:
            return out
        (body_family, body_size), _ = style_chars.most_common(1)[0]
        body_color = color_chars.most_common(1)[0][0]
        single_font = style_chars and max(Counter({k[0]: v for k, v in style_chars.items()}).values()) \
            >= 0.97 * sum(style_chars.values())
        med_h = sorted(heights)[len(heights) // 2] if heights else 10
        out["body"] = {"family": body_family, "size": body_size, "color": body_color,
                       "uniform_font": bool(single_font), "line_height": med_h}
        out["running"] = _running_headers(margins)
        out["contents"] = _contents_text(doc, n)
        out["candidates"] = _candidates(lines, out["body"], n)
    return out


def _margin_numbers(texts: list[str]) -> list[list]:
    """Page-number-like tokens at the start or end of header/footer lines."""
    found = []
    for text in texts:
        words = text.split()
        if not words:
            continue
        for word in {words[0], words[-1]}:
            w = word.strip("|·•—–-[]()")
            if re.fullmatch(r"\d{1,4}", w):
                found.append([int(w), "a"])
            elif (v := roman_value(w)) is not None:
                found.append([v, "r"])
    return found


def _running_headers(margins: list[list[str]]) -> list[list]:
    """Stretches of pages sharing a header/footer line: [first_page, last_page, text]."""
    def key(texts):
        words = [fold(re.sub(r"\b\d{1,4}\b", "", t)) for t in texts]
        words = [w for w in words if len(w) >= 4]
        return max(words, key=len) if words else ""
    out: list[list] = []
    for pno, texts in enumerate(margins):
        k = key(texts)
        if out and k and out[-1][2] == k and pno - out[-1][1] <= 2:
            out[-1][1] = pno
        elif k:
            out.append([pno, pno, k])
    return [r for r in out if r[1] > r[0]]


def _contents_text(doc, n: int) -> str:
    """Text of the printed table of contents, if one is found in the first pages."""
    pages = []
    for pno in range(min(n, 25)):
        try:
            text = doc[pno].get_text()
        except Exception:
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            if pages:
                break
            continue
        titled = any(_TOC_TITLE_RE.match(fold(ln)) for ln in lines[:6])
        numbered = sum(1 for ln in lines if _TOC_LINE_RE.search(ln) and re.search(r"[A-Za-z]{3}", ln))
        if titled or (numbered >= 6 and numbered >= 0.3 * len(lines)):
            pages.append(f"[PDF page {pno + 1}]\n" + "\n".join(lines))
        elif pages:
            break
        if len(pages) >= 6:
            break
    return "\n".join(pages)[:15000]


def _candidates(lines: list[dict], body: dict, n_pages: int) -> list[dict]:
    """Lines that could be headings: short and set apart from the body text."""
    margin_texts = Counter(fold(ln["text"]) for ln in lines if ln["margin"])
    repeated = Counter((fold(ln["text"]), round(ln["y"] / 5)) for ln in lines)
    out = []
    for ln in lines:
        text = ln["text"]
        words = text.split()
        if ln["margin"] or not re.search(r"[A-Za-zÀ-ÿ]{2}", text):
            continue
        key = fold(text)
        if margin_texts[key] >= 2 or repeated[(key, round(ln["y"] / 5))] >= 4:
            continue
        if _CAPTION_RE.match(text):
            continue
        feats = []
        larger = ln["size"] >= body["size"] * 1.12 or (body["uniform_font"] and ln["h"] >= body["line_height"] * 1.3)
        if larger:
            feats.append("larger")
        if ln["bold"]:
            feats.append("bold")
        if ln["italic"]:
            feats.append("italic")
        if _family(ln["font"]) != body["family"] and not body["uniform_font"]:
            feats.append("font")
        if ln["color"] != body["color"] and _is_coloured(ln["color"]):
            feats.append("colour")
        letters = re.sub(r"[^A-Za-z]", "", text)
        if len(letters) >= 4 and letters.isupper():
            feats.append("caps")
        numbered = bool(re.match(r"^\s*(?:\d{1,2}(?:\.\d{1,2}){0,3}\.?|[IVX]{1,5}\.|[A-H]\.)\s+\S", text)) \
            or bool(_CHAPTER_RE.match(text))
        alone = ln["block_lines"] <= 3
        gap = ln["gap"] is not None and ln["gap"] > body["line_height"] * 0.9
        centered = abs((ln["x0"] + ln["x1"]) / 2 - 0.5) < 0.05 and (ln["x1"] - ln["x0"]) < 0.6
        runin = None
        if not feats and ln["spans"] > 1:
            first = ln["first"]
            styled = (first["bold"] and not ln["bold"]) or (first["italic"] and not ln["italic"]) \
                or (_family(first["font"]) != body["family"] and not body["uniform_font"])
            if styled and 2 <= len(first["text"]) <= 80 and re.search(r"[.:]$", first["text"]):
                runin = first["text"].rstrip(".:").strip()
        if runin:
            text, feats = runin, ["run-in"]
        stripped = _NUMBERING_RE.sub("", text) if numbered else text
        if not re.match(r"^[\W\d]*[A-ZÀ-Þ]", stripped):
            continue  # headings start with a capital; "analysis." or "12 intervention and" do not
        elif len(words) > 16 or len(text) > 140:
            continue
        elif not feats and not (numbered and (alone or gap)) and not (centered and gap and alone):
            continue
        if not runin and not alone and not gap and not numbered and "larger" not in feats:
            continue  # a styled word inside a paragraph
        out.append({"p": ln["p"], "y": ln["y"], "text": text, "size": ln["size"], "feats": feats,
                    "numbered": numbered, "alone": alone, "gap": gap, "centered": centered})
    # Long books: keep the strongest candidates.
    limit = 1500 if n_pages > 60 else 400
    if len(out) > limit:
        ranked = sorted(out, key=lambda c: -(len(c["feats"]) + c["numbered"] + c["gap"] + c["size"] / 100))
        keep = {id(c) for c in ranked[:limit]}
        out = [c for c in out if id(c) in keep]
    for i, c in enumerate(out):
        c["id"] = f"c{i}"
    return out


def read_pdf(path: str | Path, timeout: int = 300) -> dict | None:
    """``scan_pdf`` in a child process; None when the file cannot be read."""
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-m", "zotero_mcp.structure", "scan", str(path)],
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        logger.info("structure scan failed for %s: %s", path, e)
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Printed page numbers
# ---------------------------------------------------------------------------


def _pages_field_range(pages_field: str) -> tuple[int, int] | None:
    m = re.match(r"^\s*(\d{1,5})\s*[-–—]\s*(\d{1,5})\s*$", pages_field or "")
    if not m:
        return None
    first, last = int(m.group(1)), int(m.group(2))
    if last < first and len(m.group(2)) < len(m.group(1)):
        last = int(m.group(1)[: len(m.group(1)) - len(m.group(2))] + m.group(2))
    return (first, last) if last >= first else None


def _runs(numbers: list[list[list]], kind: str) -> list[tuple[int, int, int]]:
    """Stretches of pages whose printed number = PDF index + offset: (start, end, offset).

    An offset counts when at least three pages carry it and it holds for most
    numbered pages of its stretch; a stretch runs on over pages without a
    number until the next offset takes over.
    """
    per_page = [{v - i for v, k in nums if k == kind} for i, nums in enumerate(numbers)]
    support = Counter(o for offs in per_page for o in offs)
    numbered_pages = sum(1 for offs in per_page if offs)
    if not numbered_pages:
        return []
    good = {o for o, c in support.items() if c >= 3 and c >= 0.1 * numbered_pages}
    if not good:
        return []
    # Walk the pages; switch offset when a new good offset shows up twice in a row
    # (or once when the current one has no support on that page).
    runs: list[list[int]] = []
    current = None
    pending: tuple[int, int] | None = None
    for i, offs in enumerate(per_page):
        hits = offs & good
        if current is not None and current in offs:
            pending = None
            runs[-1][1] = i
            continue
        if not hits:
            if current is not None:
                runs[-1][1] = i
            continue
        new = min(hits, key=lambda o: -support[o])
        if current is None:
            current = new
            runs.append([i, i, new])
            continue
        if pending and pending[1] == new:
            # Second page in a row with the new offset: it starts at the first.
            runs[-1][1] = pending[0] - 1
            runs.append([pending[0], i, new])
            current, pending = new, None
        else:
            pending = (i, new)
            runs[-1][1] = i
    # Check each stretch: most of its numbered pages must agree.
    checked = []
    for start, end, off in runs:
        numbered = [i for i in range(start, end + 1) if per_page[i]]
        agree = [i for i in numbered if off in per_page[i]]
        if len(agree) >= 2 and len(agree) >= 0.5 * len(numbered):
            checked.append((start, end, off))
    return checked


def _trivial_labels(labels: list[str], field_range: tuple[int, int] | None) -> bool:
    """Labels that only count PDF pages (1, 2, 3...) while the article is printed elsewhere."""
    numeric = [int(x) for x in labels if x.isdigit()]
    if not field_range or len(numeric) < 0.8 * len(labels):
        return False
    first, last = field_range
    return max(numeric, default=0) < first or (first > 1 and numeric and numeric[0] <= 2)


def _implausible_labels(labels: list[str], book_like: bool) -> bool:
    """PDF labels that are not printed page numbers: "image 1", a page "0",
    numbers running backwards, or only roman numerals in an article."""
    given = [x.strip() for x in labels if x and x.strip()]
    if not given:
        return True
    arabic = [int(re.sub(r"^[A-Za-z]{1,2}", "", x)) for x in given if re.fullmatch(r"[A-Za-z]{0,2}\d+", x)]
    roman = [x for x in given if roman_value(x.lower()) is not None and not re.search(r"\d", x)]
    if len(arabic) + len(roman) < 0.7 * len(given):
        return True                         # "image 1", "Cover", "A-1" ...
    if 0 in arabic:
        return True
    drops = sum(1 for a, b in zip(arabic, arabic[1:]) if b <= a)
    if drops > (2 if book_like else 0):
        return True                         # 1, 0, 1, 2 ... or restarting numbers
    return not book_like and not arabic and len(given) > 4


def page_labels(scan: dict, pages_field: str = "", item_type: str = "") -> tuple[list[str] | None, str]:
    """Printed page label for every PDF page, and how it was found."""
    n = scan.get("pages") or 0
    if not n:
        return None, "none"
    field_range = _pages_field_range(pages_field)
    pdf_labels = [str(x) for x in (scan.get("labels") or [])]
    book_like = item_type in BOOK_TYPES or n > 80
    if pdf_labels and any(pdf_labels) and not _trivial_labels(pdf_labels, field_range) \
            and not _implausible_labels(pdf_labels, book_like):
        return pdf_labels, "pdf-labels"
    numbers = scan.get("margins") or []
    numbers = numbers + [[] for _ in range(n - len(numbers))]
    arabic = _runs(numbers, "a")
    if arabic:
        labels: list[str] = [""] * n
        roman = _runs(numbers, "r")
        first_arabic = arabic[0][0]
        for start, end, off in roman:
            for i in range(start, min(end, first_arabic - 1) + 1):
                if i + off >= 1:
                    labels[i] = to_roman(i + off)
        for idx, (start, end, off) in enumerate(arabic):
            lo = start
            if idx == 0:
                # Extend back over unnumbered opening pages (a first page with
                # no footer number), not over a cover the Pages field excludes.
                while lo > 0 and not numbers[lo - 1] and lo - 1 + off >= 1:
                    lo -= 1
            hi = end if idx + 1 < len(arabic) else n - 1
            for i in range(lo, hi + 1):
                value = i + off
                if value < 1 or (field_range and value < field_range[0]):
                    continue
                labels[i] = str(value)
        if sum(1 for x in labels if x) >= max(2, 0.5 * n):
            return labels, "printed numbers"
    if field_range:
        first, last = field_range
        span = last - first + 1
        if span == n:
            return [str(first + i) for i in range(n)], "pages field"
        if span == n - 1:  # a cover sheet in front
            return [""] + [str(first + i) for i in range(n - 1)], "pages field"
    return None, "none"


# ---------------------------------------------------------------------------
# Headings
# ---------------------------------------------------------------------------


@dataclass
class Heading:
    page: int            # 1-based PDF page
    text: str
    level: int = 1
    section: str | None = None
    y: float = 0.0


@dataclass
class Structure:
    pages: int = 0
    labels: list[str] | None = None
    labels_from: str = "none"
    headings: list[Heading] = field(default_factory=list)
    headings_from: str = "none"
    book_like: bool = False
    note: str = ""

    def to_json(self) -> dict:
        return asdict(self)


BOOK_TYPES = {"book", "thesis", "report", "manuscript", "bookSection"}


_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00ad]")
#: Bookmarks for the figures and tables, not sections ("Table 1:", "Figures").
_FLOAT_MARK_RE = re.compile(r"^(?:tables?|figures?|figs?\.?|tabel(?:len)?|figuren?)(?:\s*[\dIVX]+\b.*)?\s*[.:]?$", re.I)


def headings_from_toc(toc: list, pages: int, book_like: bool) -> list[Heading]:
    """Bookmarks, when they describe the document's own sections."""
    entries = [(lvl, _INVISIBLE_RE.sub("", title).strip(), page) for lvl, title, page in toc if title and page >= 1]
    entries = [(lvl, t, page) for lvl, t, page in entries if t and not _FLOAT_MARK_RE.match(t)]
    # A single top bookmark holding all others is the document's title: its
    # children are the sections ("Title > 1 Introduction, 2 Methods ...").
    if len(entries) > 3 and entries[0][0] < min(lvl for lvl, _t, _p in entries[1:]) \
            and canonical_section(entries[0][1]) is None:
        entries = entries[1:]
    if len(entries) < 3:
        return []
    # Bookmarks that stop halfway (only the introduction and method) leave the rest
    # unlabelled: better to ask Gemini or the rules.
    last_page = max(page for _l, _t, page in entries)
    if pages and last_page < 0.5 * pages and not any(canonical_section(t) in ("References", "Back matter")
                                                       for _l, t, _p in entries):
        return []
    named = {canonical_section(t) for _l, t, _p in entries} - {None}
    if not book_like and len(named) < 2:
        return []
    if book_like:
        top = [e for e in entries if e[0] == 1]
        spread = len({p for _l, _t, p in top})
        if spread < 2 and len(entries) < 5:
            return []
    min_level = min(lvl for lvl, _t, _p in entries)
    return [Heading(page=min(p, pages) if pages else p, text=t[:200], level=lvl - min_level + 1,
                    section=canonical_section(t)) for lvl, t, p in entries if lvl - min_level < 4]


def headings_from_rules(candidates: list[dict], book_like: bool) -> list[Heading]:
    """Without an API: candidates that are known section names or chapter headings."""
    out = []
    for c in candidates:
        section = canonical_section(c["text"])
        if section == "Abstract" and c["p"] > 2 and not book_like:
            section = None  # a "Summary" near the end is part of the discussion
        strong = c["alone"] or c["gap"] or "run-in" in c["feats"]
        if section and strong and len(c["text"].split()) <= 8:
            out.append(Heading(page=c["p"] + 1, text=c["text"], level=2 if book_like else 1,
                               section=section, y=c["y"]))
        elif book_like and _CHAPTER_RE.match(c["text"]):
            out.append(Heading(page=c["p"] + 1, text=c["text"], level=1, y=c["y"]))
    if book_like:
        return out
    main = {"Introduction", "Methods", "Results", "Discussion", "Conclusion"}
    # A structured abstract ("Background: ... Methods: ...") is not the paper's
    # sections: early headings whose section comes back later are dropped.
    later_pages = {}
    for h in out:
        if h.section:
            later_pages.setdefault(h.section, []).append(h.page)
    keep = []
    for h in out:
        if h.section in main and h.page <= 2 and any(p > h.page for p in later_pages.get(h.section, [])):
            continue
        keep.append(h)
    # After the references or back matter, "Methodology:" is an author's role
    # (CRediT), not a section.
    out, closed = [], False
    for h in keep:
        if closed and h.section in main:
            continue
        if h.section in ("References", "Back matter"):
            closed = True
        out.append(h)
    return out


SECTION_GUIDE = """Sections: Abstract, Introduction, Methods, Results, Discussion, Conclusion, References, Appendix,
Back matter, Other. Sub-sections inherit the section of their parent ("Participants" under Methods is Methods).
- Many APA-style papers have no "Introduction" heading (the paper's title may be repeated above the
  introduction); then that repeated title opens the Introduction. Papers that do have an "Introduction" heading
  use it. Topical headings between the introduction and the first Method heading ("Risky play and
  development", "The present study") are part of the introduction.
- Papers with several studies or experiments: a heading that names a study ("Study 1", "Experiment 2") is
  Other; that study's own Method, Results and Discussion headings carry those sections. A General Discussion
  is Discussion.
- Systematic, scoping and other reviews and meta-analyses: search strategy, eligibility, study selection,
  data extraction, quality or risk-of-bias assessment and synthesis methods are Methods; study
  characteristics, the synthesis itself, themes and effect sizes are Results.
- Qualitative studies: Findings and Themes are Results.
- Theoretical, narrative or position papers without a Method section: topical sections are Other, apart from
  an opening introduction and a closing conclusion or discussion.
- Books and theses: chapters are Other unless the chapter is itself an introduction, method, results,
  discussion or conclusion chapter."""

GEMINI_PROMPT = """You find the heading structure of one scholarly document from a list of candidate lines.

Document: {kind}, {pages} PDF pages. Title: {title}
Body text: {body}

{contents}{running}Candidate lines, in reading order. Each: id | PDF page | printed page | style | text.
Styles are relative to the body text (larger, bold, italic, font = another typeface, colour, caps, numbered,
alone = on its own line, gap = space above, centered, run-in = bold/italic opening of a paragraph).
{candidates}

Return the candidates that are real headings of this document's own structure: {what}.
Leave out: the document title, author names and affiliations, running headers, figure and table captions,
table cells, list items, equations, page furniture of the journal, the labels inside a structured
abstract (such as "Background:" or "Methods:" within the abstract itself), and boxed summaries beside the text
("Practice points", "Key points", "Highlights", "What this paper adds").
For each heading give its level ({levels}) and the section it opens. If the paper's title is repeated above
an APA introduction and is a candidate, return it with section Introduction.
{guide}
{contents_rule}Answer only with the JSON."""


BOOKMARK_PROMPT = """These are the bookmarks (outline) of one scholarly document. Check them.

Document: {kind}, {pages} PDF pages. Title: {title}

Bookmarks, in order. Each: id | level | PDF page | text.
{bookmarks}

Return the bookmarks that are headings of this document's own structure, each with its level ({levels}) and
the section it opens. Leave out bookmarks for the title, figures, tables, author information, journal or
publisher pages, and labels inside a structured abstract ("Background", "Methods" under the Abstract).
{guide}
Answer only with the JSON."""


def _candidate_line(c: dict, labels: list[str] | None) -> str:
    style = list(c["feats"])
    for flag in ("numbered", "alone", "gap", "centered"):
        if c.get(flag):
            style.append(flag)
    printed = labels[c["p"]] if labels and c["p"] < len(labels) and labels[c["p"]] else "-"
    return f"{c['id']} | {c['p'] + 1} | {printed} | {c['size']}pt {' '.join(style)} | {c['text'][:140]}"


def gemini_headings(scan: dict, labels: list[str] | None, book_like: bool, title: str,
                    ask: Callable[[str], str]) -> tuple[list[Heading], str]:
    """Ask Gemini (through ``ask``) which candidates are headings; checked before use."""
    cands = scan.get("candidates") or []
    if not cands:
        return [], "no candidates"
    by_id = {c["id"]: (i, c) for i, c in enumerate(cands)}
    body = scan.get("body") or {}
    contents = f"Printed table of contents:\n{scan['contents']}\n\n" if scan.get("contents") else ""
    running = ""
    if book_like and scan.get("running"):
        rows = [f"PDF pages {a + 1}-{b + 1}: {t}" for a, b, t in scan["running"][:120]]
        running = "Running headers (often the chapter title):\n" + "\n".join(rows) + "\n\n"
    prompt = GEMINI_PROMPT.format(
        kind="book, thesis or report" if book_like else "journal article or paper",
        pages=scan.get("pages"), title=title or "(unknown)",
        body=f"{body.get('size')}pt {'one font throughout (a scan)' if body.get('uniform_font') else ''}".strip(),
        contents=contents, running=running,
        candidates="\n".join(_candidate_line(c, labels) for c in cands),
        what=("its parts and chapters, and the main sections inside chapters" if book_like
              else "its sections and sub-sections"),
        levels="1 = part or chapter, 2 = section, 3 = sub-section" if book_like
        else "1 = section such as Methods, 2 = sub-section, 3 = sub-sub-section",
        contents_rule=("The chapters should match the printed table of contents; a chapter missing from the "
                       "candidates is simply left out.\n" if contents else ""),
        guide=SECTION_GUIDE,
    )
    try:
        raw = ask(prompt)
        data = json.loads(raw)
    except Exception as e:
        return [], f"gemini failed ({type(e).__name__}: {str(e)[:160]})"
    items = data.get("headings") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return [], "gemini answer not understood"
    picked = []
    for h in items:
        if not isinstance(h, dict) or h.get("id") not in by_id:
            continue
        idx, c = by_id[h["id"]]
        try:
            level = max(1, min(4, int(h.get("level") or 1)))
        except (TypeError, ValueError):
            level = 1
        section = h.get("section") if h.get("section") in SECTIONS else None
        if is_study_heading(c["text"]):
            section = None      # "Study 1": its own Method and Results headings say which part is which
        picked.append((idx, Heading(page=c["p"] + 1, text=c["text"], level=level, section=section, y=c["y"])))
    picked.sort(key=lambda x: x[0])  # reading order, whatever order the answer used
    headings = [h for _i, h in picked]
    if len(headings) < 2:
        return [], "gemini found fewer than 2 headings"
    note = f"gemini picked {len(headings)} of {len(cands)} candidates"
    if scan.get("contents") and book_like:
        toc = fold(scan["contents"])
        matched = sum(1 for h in headings if h.level == 1 and fold(h.text)[:30] in toc)
        top = sum(1 for h in headings if h.level == 1)
        note += f"; {matched}/{top} chapters found in the printed contents"
    return headings, note


#: Longer outlines (a whole book) are used as they are.
MAX_BOOKMARKS_TO_CHECK = 150


def gemini_check_bookmarks(headings: list[Heading], pages: int, book_like: bool, title: str,
                           ask: Callable[[str], str]) -> tuple[list[Heading], str]:
    """Gemini's check of the bookmarks: which are headings, their level and section.
    The text and page stay the bookmark's own; a section named by the heading itself
    ("Methods") is kept when Gemini gives none."""
    rows = [f"b{i} | {h.level} | {h.page} | {h.text[:160]}" for i, h in enumerate(headings)]
    prompt = BOOKMARK_PROMPT.format(
        kind="book, thesis or report" if book_like else "journal article or paper", pages=pages,
        title=title or "(unknown)", bookmarks="\n".join(rows), guide=SECTION_GUIDE,
        levels="1 = part or chapter, 2 = section, 3 = sub-section" if book_like
        else "1 = section such as Methods, 2 = sub-section, 3 = sub-sub-section")
    try:
        data = json.loads(ask(prompt))
    except Exception as e:
        return [], f"gemini failed ({type(e).__name__}: {str(e)[:160]})"
    items = data.get("headings") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return [], "gemini answer not understood"
    by_id = {f"b{i}": h for i, h in enumerate(headings)}
    kept: dict[str, Heading] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("id") not in by_id or item["id"] in kept:
            continue
        h = by_id[item["id"]]
        try:
            level = max(1, min(4, int(item.get("level") or h.level)))
        except (TypeError, ValueError):
            level = h.level
        section = item.get("section") if item.get("section") in SECTIONS else None
        if is_study_heading(h.text):
            section = None
        elif section is None:
            section = h.section
        kept[item["id"]] = Heading(page=h.page, text=h.text, level=level, section=section, y=h.y)
    out = [kept[f"b{i}"] for i in range(len(headings)) if f"b{i}" in kept]
    if len(out) < 2:
        return [], "gemini kept fewer than 2 bookmarks"
    return out, f"gemini kept {len(out)} of {len(headings)} bookmarks"


def gemini_asker(model: str, embedding_config: dict | None = None, thinking: str | None = "low",
                 usage_key: str | None = None) -> Callable[[str], str]:
    """A function sending one heading prompt to Gemini and returning its JSON text."""
    from zotero_mcp.gemini_util import json_asker

    schema = {
        "type": "OBJECT",
        "properties": {"headings": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"id": {"type": "STRING"}, "level": {"type": "INTEGER"},
                           "section": {"type": "STRING", "enum": list(SECTIONS) + ["Other"]}},
            "required": ["id", "level", "section"]}}},
        "required": ["headings"],
    }
    return json_asker(model, schema, embedding_config, thinking=thinking, usage_key=usage_key)


def file_signature(path: str | Path) -> str:
    p = Path(path)
    st = p.stat()
    return hashlib.sha1(f"{p.name}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]


def analyse(scan: dict, item_type: str = "", pages_field: str = "", title: str = "",
            ask: Callable[[str], str] | None = None) -> Structure:
    """Printed pages and headings for one scanned PDF."""
    book_like = item_type in BOOK_TYPES or (scan.get("pages") or 0) > 80
    labels, labels_from = page_labels(scan, pages_field, item_type)
    st = Structure(pages=scan.get("pages") or 0, labels=labels, labels_from=labels_from, book_like=book_like)
    headings = headings_from_toc(scan.get("toc") or [], st.pages, book_like)
    if headings:
        st.headings, st.headings_from = headings, "bookmarks"
        if ask is not None and len(headings) > MAX_BOOKMARKS_TO_CHECK:
            # A whole book's outline (hundreds of chapters and sections): exact as it is,
            # and too long to check in one answer.
            st.note = f"{len(headings)} bookmarks, used without a Gemini check"
        elif ask is not None:
            # Bookmarks are exact about text and page; Gemini checks which are headings
            # and what part of the paper each opens ("Research design", "Case studies").
            checked, note = gemini_check_bookmarks(headings, st.pages, book_like, title, ask)
            st.note = note
            if checked:
                st.headings, st.headings_from = checked, "bookmarks + gemini"
        return st
    if ask is not None:
        headings, note = gemini_headings(scan, labels, book_like, title, ask)
        st.note = note
        if headings:
            st.headings, st.headings_from = headings, "gemini"
            return st
    headings = headings_from_rules(scan.get("candidates") or [], book_like)
    if headings:
        st.headings, st.headings_from = headings, "rules"
    return st


# ---------------------------------------------------------------------------
# Reference lists by their shape
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r"\(\s*(?:1[89]|20)\d{2}[a-z]?\s*\)|\b(?:1[89]|20)\d{2}[a-z]?[;.]\s|\b(?:1[89]|20)\d{2}[a-z]?,\s*\d")
_INITIALS_RE = re.compile(r"\b[A-Z][A-Za-zÀ-ÿ'’\-]+,\s(?:[A-Z]\.\s?-?){1,3}|\b[A-Z][a-zà-ÿ'’\-]+\s[A-Z]{1,3}[,.](?=\s)")
_REFMARK_RE = re.compile(r"doi[:.]|doi\.org|https?://|\b\d+\s*\(\d+\)\s*[,:]\s*\d+|\bpp?\.\s*\d+|\b\d+:\d+[-–]\d+")


def looks_like_references(text: str) -> bool:
    """A passage that is mostly a reference list."""
    text = text or ""
    if len(text) < 300:
        return False
    per_k = 1000 / len(text)
    years = len(_YEAR_RE.findall(text)) * per_k
    names = len(_INITIALS_RE.findall(text)) * per_k
    marks = len(_REFMARK_RE.findall(text)) * per_k
    return years >= 3 and names >= 4 and (names + marks) >= 7


# ---------------------------------------------------------------------------
# Applying a structure to an item's indexed passages
# ---------------------------------------------------------------------------


def _page_floor(chunks: list[dict]) -> dict[int, int]:
    """For each page, an offset its text cannot start before: the start of the
    last passage that begins on the page before (passages overlap pages)."""
    last_start: dict[int, int] = {}
    for ch in chunks:
        page = ch["meta"].get("page")
        if page is not None:
            last_start[int(page)] = int(ch["meta"].get("char_start", 0))
    return {page + 1: start for page, start in last_start.items()}


_HEADING_PREFIX_RE = re.compile(r"[\s#*_>|]*(?:(?:\d{1,2}(?:\.\d{1,2}){0,3}|[IVXivx]{1,5}|[A-Ha-h])[.)]?\s*)?[*_]*")


def _locate(heading: Heading, chunks: list[dict], after: int = -1,
            floors: dict[int, int] | None = None) -> int | None:
    """Character offset of a heading in the indexed text: on its page (or the
    passage running into it), after the previous heading, preferring an
    occurrence that stands on its own line over the same words in a sentence."""
    words = re.findall(r"[^\W_]+", heading.text)
    if not words:
        return None
    pattern = re.compile(r"[\W_]*".join(re.escape(w) for w in words[:12]), re.I)
    floor = (floors or {}).get(heading.page, -1)
    best: tuple[int, int] | None = None     # (score, position)
    for ch in chunks:
        page = ch["meta"].get("page")
        if page is None or not (heading.page - 1 <= page <= heading.page):
            continue
        body = ch["body"]
        base = int(ch["meta"].get("char_start", 0))
        for m in pattern.finditer(body):
            pos = base + m.start()
            if pos <= after:
                continue
            line_start = body.rfind("\n", 0, m.start()) + 1
            line_end = body.find("\n", m.end())
            rest = body[m.end(): line_end if line_end >= 0 else len(body)]
            alone_before = bool(_HEADING_PREFIX_RE.fullmatch(body[line_start:m.start()]))
            alone_after = not rest.strip(" *_") or bool(re.match(r"[*_]*\s*[.:]", rest))
            score = 2 * alone_before + alone_after - (3 if pos < floor else 0)
            if best is None or score > best[0] or (score == best[0] and pos < best[1]):
                best = (score, pos)
    return best[1] if best else None


def label_passages(chunks: list[dict], st: Structure | None, item_type: str = "") -> tuple[list[dict], dict]:
    """New metadata for each passage: printed page, heading path, chapter, section.

    ``chunks``: [{"id", "meta", "body"}] in passage order. Returns the metadata
    dicts (complete, ready for a metadata-only update) and a small report.
    """
    report = {"headings": len(st.headings) if st else 0, "located": 0, "references": 0}
    marks: list[tuple[int, Heading]] = []
    if st and st.headings:
        page_start: dict[int, int] = {}
        for ch in chunks:
            page = ch["meta"].get("page")
            if page is not None and page not in page_start:
                page_start[page] = int(ch["meta"].get("char_start", 0))
        floors = _page_floor(chunks)
        last = -1
        # Headings come in reading order: each is looked for after the previous one,
        # so a "Results" in the abstract or a sentence cannot pull it forward.
        for h in st.headings:
            pos = _locate(h, chunks, last, floors)
            if pos is not None:
                report["located"] += 1
            else:
                later = [p for p in page_start if p >= h.page]
                pos = page_start[min(later)] if later else None
                if pos is not None and pos <= last:
                    pos = None      # its page is already past: leave this heading out
            if pos is not None:
                marks.append((pos, h))
                last = pos
        # Headings mostly not found in the indexed text: another attachment
        # than the one indexed, or text too different to trust.
        if len(st.headings) >= 4 and report["located"] < 0.3 * len(st.headings):
            marks = []
            report["mismatch"] = True
        marks.sort(key=lambda m: m[0])

    # Everything before the first Method heading that no heading names is the
    # introduction: APA papers start it without a heading, and many papers put it
    # under topical headings ("Risky play and development") before Method. It
    # starts at an "Introduction" heading when the paper has one, else after the
    # abstract (at most about 300 words).
    intro_start = intro_end = None
    if marks and not (st and st.book_like):
        intro_end = next((pos for pos, h in marks if h.section == "Methods"), None)
        if intro_end is not None:
            intro_start = next((pos for pos, h in marks if pos < intro_end and _INTRO_WORD_RE.search(fold(h.text))),
                               None)
            if intro_start is None:
                intro_start = next((pos + 2500 for pos, h in marks if h.section == "Abstract"), 0)
    # Where an abstract has certainly ended (about 300 words after its heading).
    abstract_over = next((pos + 2500 for pos, h in marks if h.section == "Abstract"), None)
    if abstract_over is None:
        abstract_over = float("inf")

    n = len(chunks)
    out = []
    for idx, ch in enumerate(chunks):
        meta = dict(ch["meta"])
        for key in ("heading", "chapter", "page_label", "section_2"):
            meta.pop(key, None)
        start = int(meta.get("char_start", 0))
        end = int(meta.get("char_end", start))
        probe = start + (end - start) // 4
        path: dict[int, Heading] = {}
        for pos, h in marks:
            if pos > probe:
                break
            path = {lvl: x for lvl, x in path.items() if lvl < h.level}
            path[h.level] = h
        chain = [path[lvl] for lvl in sorted(path)]
        if marks and not (st and st.book_like) and probe >= abstract_over:
            # The abstract is over: headings after it (an APA introduction's level-2
            # "The Present Study" when the repeated title was not found) are not its parts.
            chain = [h for h in chain if h.section != "Abstract"]
        if marks:
            # Under an Abstract, Appendix or References heading, its sub-headings
            # ("Methods" in a structured abstract) do not change the section.
            enclosing = next((h.section for h in chain if h.section in ("Abstract", "Appendix", "References",
                                                                        "Back matter")), None)
            owner = None if enclosing else next((h for h in reversed(chain) if h.section), None)
            section = enclosing or (owner.section if owner else None)
            if section:
                meta["section"] = section
            else:
                meta.pop("section", None)
            meta.pop("section_2", None)
            if owner and (other := second_section(owner.text)):
                meta["section_2"] = other
            if chain:
                meta["heading"] = " › ".join(h.text for h in chain)[:200]
                if st and st.book_like and chain[0].level == 1:
                    meta["chapter"] = chain[0].text[:150]
        if intro_end is not None and idx and intro_start <= probe < intro_end \
                and meta.get("section") in (None, "Abstract"):
            meta["section"] = "Introduction"
        page = meta.get("page")
        if st and st.labels and page is not None and 1 <= int(page) <= len(st.labels) and st.labels[int(page) - 1]:
            meta["page_label"] = st.labels[int(page) - 1]
        late = idx >= 0.4 * n or (st is not None and st.book_like)
        if late and idx and looks_like_references(ch["body"]):
            if meta.get("section") != "References":
                report["references"] += 1
            meta["section"] = "References"
        meta["structure_v"] = STRUCTURE_VERSION
        if st:
            meta["structure_from"] = st.headings_from if marks else "none"
        out.append(meta)
    return out, report


_COVER_RE = re.compile(
    r"downloaded from|this content downloaded|terms (?:and conditions )?of use|all use subject to|"
    r"for personal use only|researchgate|jstor is a not-for-profit|citation for published version|"
    r"general rights|take ?down policy|repository|cover page|this article was downloaded by|"
    r"please scroll down for article|full terms & conditions|reproduced with permission", re.I)


def is_cover_page(text: str) -> bool:
    """A cover sheet a repository or database puts in front of the document."""
    hits = len(set(m.group(0).lower() for m in _COVER_RE.finditer(text or "")))
    return hits >= 2 or (hits == 1 and len(text or "") < 1500)


def first_pages(path: str, n: int = 4, scan_more: int = 0) -> dict:
    """Text of the first ``n`` pages (a cover sheet in front is read but not
    counted), plus (books) pages within the first ``scan_more`` that carry a
    copyright or ISBN line."""
    import pymupdf

    out = {"pages": 0, "texts": [], "covers": []}
    with pymupdf.open(path) as doc:
        out["pages"] = doc.page_count
        taken = 0
        for pno in range(min(doc.page_count, max(n + 3, scan_more))):
            text = doc[pno].get_text() or ""
            if taken < n:
                out["texts"].append([pno + 1, text[:8000]])
                if taken == 0 and is_cover_page(text):
                    # A cover sheet often carries the DOI and the citation:
                    # read it, but do not count it as one of the n pages.
                    out["covers"].append(pno + 1)
                    continue
                taken += 1
            elif re.search(r"©|\bcopyright\b|\bISBN\b", text, re.I):
                out["texts"].append([pno + 1, text[:8000]])
    return out


def read_first_pages(path: str | Path, n: int = 4, scan_more: int = 0, timeout: int = 90) -> dict | None:
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-m", "zotero_mcp.structure", "pages", str(path), str(n), str(scan_more)],
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        return None


def _main(argv: list[str]) -> int:  # pragma: no cover - child process
    if len(argv) >= 2 and argv[0] == "scan":
        # ASCII-only JSON: a Windows console encoding cannot print every character.
        print(json.dumps(scan_pdf(argv[1])))
        return 0
    if len(argv) >= 2 and argv[0] == "pages":
        n = int(argv[2]) if len(argv) > 2 else 4
        more = int(argv[3]) if len(argv) > 3 else 0
        print(json.dumps(first_pages(argv[1], n, more)))
        return 0
    print("usage: python -m zotero_mcp.structure scan <file.pdf>", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv[1:]))

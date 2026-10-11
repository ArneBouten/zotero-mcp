"""The passages of one paper that answer a question, instead of the whole text.

Reading twenty papers for one answer at 60,000 characters each does not fit in
a conversation. Given a question, the text is cut into passages of a few
sentences, scored by keyword match (BM25), then ordered by the re-ranker set up
for semantic search (Voyage, Cohere, ... or a local cross-encoder) when there is
one. The best few are returned in reading order, each with its page.

Everything runs on this computer except the optional hosted re-ranker, which
costs a fraction of a cent per paper. Running heads, page numbers and the
reference list are left out: they match a question's words without answering it.
"""

from __future__ import annotations

import math
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

PAGE_SEPARATOR = "\f"
#: A passage is cut after the sentence that takes it past this length.
TARGET_CHARS = 900
#: Passages handed to the re-ranker (by keyword score, then reading order).
RERANK_POOL = 200
DEFAULT_PASSAGES = 8
MAX_PASSAGES = 30

_STOP = frozenset(
    """a about above after again against all also am an and any are as at be because been before
    being below between both but by can could did do does doing down during each few for from
    further had has have having he her here hers him his how i if in into is it its itself just me
    more most my no nor not now of off on once only or other our out over own same she should so
    some such than that the their them then there these they this those through to too under until
    up very was we were what when where which while who whom why will with would you your
    paper study article author authors find found report reported show shows et al
    de het een en van in op te dat die voor met zijn is niet aan er ook als bij door""".split()
)
_WORD = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'\-]*[A-Za-zÀ-ÿ]|\d+(?:[.,]\d+)?")
_SENTENCE_END = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"”’)\]]))\s+(?=[\"“(\[]?[A-Z0-9])")
_HYPHEN_BREAK = re.compile(r"(?<=[a-z])-\n(?=[a-z])")
_REF_HEADING = re.compile(
    r"^\s*(?:\d+\.?\s*)?(references|bibliography|literature cited|works cited|reference list|"
    r"referenties|literatuur|literatuurlijst|bibliografie)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_YEAR_CITE = re.compile(r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)|\b(?:19|20)\d{2}[a-z]?\)\.|doi\.org/|\bdoi:", re.IGNORECASE)
_PAGE_NUMBER = re.compile(r"^\s*(?:page\s*)?\d{1,4}\s*$", re.IGNORECASE)


@dataclass
class Passage:
    index: int
    text: str
    first_page: int
    last_page: int
    score: float = 0.0
    keyword: float = 0.0

    @property
    def pages(self) -> str:
        if self.first_page <= 0:
            return ""
        if self.last_page > self.first_page:
            return f"pp. {self.first_page}–{self.last_page}"
        return f"p. {self.first_page}"


@dataclass
class Excerpts:
    question: str
    passages: list[Passage]
    total: int
    pages: int
    ranked_by: str
    note: str = ""
    dropped_references: bool = False
    extra: list[str] = field(default_factory=list)

    def markdown(self) -> str:
        if not self.passages:
            return (f"## Passages for: {self.question}\n\nNo passage of the text matches this question"
                    f"{' (' + self.note + ')' if self.note else ''}. Ask it in other words, or read the "
                    "whole text.")
        where = f" from {self.pages} pages" if self.pages > 1 else ""
        lines = [
            f"## Passages for: {self.question}",
            "",
            (f"The {len(self.passages)} passages of {self.total}{where} that best answer it "
             f"({self.ranked_by}), in reading order." if len(self.passages) > 1 else
             f"The passage of {self.total}{where} that best answers it ({self.ranked_by}).")
            + (" The reference list was left out." if self.dropped_references else "")
            + (f" {self.note}." if self.note else ""),
        ]
        for block in _merge_adjacent(self.passages):
            label = _page_label(block)
            lines += ["", f"**{label}**" if label else "**·**", "", " ".join(p.text for p in block)]
        return "\n".join(lines)


def _merge_adjacent(passages: list[Passage]) -> list[list[Passage]]:
    blocks: list[list[Passage]] = []
    for p in sorted(passages, key=lambda p: p.index):
        if blocks and blocks[-1][-1].index == p.index - 1:
            blocks[-1].append(p)
        else:
            blocks.append([p])
    return blocks


def _page_label(block: list[Passage]) -> str:
    first = block[0].first_page
    last = max(p.last_page for p in block)
    if first <= 0:
        return ""
    return f"p. {first}" if last <= first else f"pp. {first}–{last}"


# --- Cutting the text --------------------------------------------------------------------------


def _norm_line(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip().lower())


def _running_heads(pages: list[str]) -> set[str]:
    """Lines repeated at the top or bottom of many pages: journal names, running titles."""
    if len(pages) < 4:
        return set()
    seen: Counter[str] = Counter()
    for page in pages:
        lines = [ln for ln in page.splitlines() if ln.strip()]
        edge = {_norm_line(ln) for ln in lines[:3] + lines[-3:] if len(ln.strip()) < 120}
        seen.update(edge)
    floor = max(3, int(len(pages) * 0.3))
    return {line for line, n in seen.items() if n >= floor and line}


_YEAR = re.compile(r"\b(?:19|20)\d{2}[a-z]?\b")


def _references_start(text: str) -> int | None:
    """Offset of the closing reference list's heading, or None.

    Only the last such heading in the second half of the text counts, and only when what
    follows it reads as a reference list (a year every few hundred characters): in a book the
    last "References" may close a chapter that other chapters follow.
    """
    hits = [m.start() for m in _REF_HEADING.finditer(text)]
    hits = [h for h in hits if h > len(text) * 0.5]
    if not hits:
        return None
    tail = text[hits[-1]:]
    if len(_YEAR.findall(tail)) * 400 < len(tail):
        return None
    return hits[-1]


def _sentences(page: str, heads: set[str]) -> list[str]:
    lines = []
    for line in page.splitlines():
        stripped = line.strip()
        if not stripped or _PAGE_NUMBER.match(stripped) or _norm_line(stripped) in heads:
            lines.append("")
            continue
        lines.append(stripped)
    body = _HYPHEN_BREAK.sub("", "\n".join(lines))
    out: list[str] = []
    for para in re.split(r"\n\s*\n", body):
        para = re.sub(r"\s*\n\s*", " ", para).strip()
        if para:
            out.extend(s.strip() for s in _SENTENCE_END.split(para) if s.strip())
    return out


def split_passages(text: str, target: int = TARGET_CHARS, keep_references: bool = False
                   ) -> tuple[list[Passage], bool]:
    """The text as passages of whole sentences, each with the page it starts and ends on.

    Pages are separated by form feeds (as the extractors write them); text without them is one
    page, and its passages have no page number. Returns the passages and whether a reference
    list was left out.
    """
    dropped = False
    if not keep_references:
        cut = _references_start(text)
        if cut is not None:
            text, dropped = text[:cut], True
    pages = text.split(PAGE_SEPARATOR)
    numbered = len(pages) > 1
    heads = _running_heads(pages)
    passages: list[Passage] = []
    buf: list[str] = []
    size = 0
    start_page = 0

    def flush(end_page: int) -> None:
        nonlocal buf, size
        if buf:
            passages.append(Passage(len(passages), " ".join(buf), start_page, end_page))
        buf, size = [], 0

    for number, page in enumerate(pages, start=1):
        page_no = number if numbered else 0
        for sentence in _sentences(page, heads):
            if not buf:
                start_page = page_no
            # A very long "sentence" (a table, a list without full stops) is cut on its own.
            while len(sentence) > target * 2:
                cut = sentence.rfind(" ", 0, target)
                cut = cut if cut > target // 2 else target
                buf.append(sentence[:cut])
                flush(page_no)
                start_page = page_no
                sentence = sentence[cut:].strip()
            buf.append(sentence)
            size += len(sentence) + 1
            if size >= target:
                flush(page_no)
    flush(len(pages) if numbered else 0)
    passages = [p for p in passages if len(p.text) >= 40]
    for i, p in enumerate(passages):
        p.index = i
    return passages, dropped


# --- Scoring -----------------------------------------------------------------------------------


def _stem(word: str) -> str:
    for suffix in ("ically", "ations", "ation", "ness", "ment", "ings", "ing", "ies", "ied", "ers",
                   "ed", "es", "er", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> list[str]:
    return [_stem(w.lower()) for w in _WORD.findall(text) if w.lower() not in _STOP and len(w) > 1]


def bm25(question: str, passages: list[Passage], k1: float = 1.4, b: float = 0.75) -> None:
    """Set each passage's keyword score."""
    query = set(tokens(question))
    if not query or not passages:
        return
    docs = [Counter(tokens(p.text)) for p in passages]
    avg = sum(sum(d.values()) for d in docs) / len(docs) or 1.0
    n = len(docs)
    df = {t: sum(1 for d in docs if t in d) for t in query}
    for p, d in zip(passages, docs, strict=True):
        length = sum(d.values()) or 1
        score = 0.0
        for t in query:
            f = d.get(t, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * length / avg))
        # A passage that is mostly citations matches names and years, not the question.
        if len(_YEAR_CITE.findall(p.text)) >= 4:
            score *= 0.3
        p.keyword = score


def configured_reranker(config_path: str | None = None):
    """The re-ranker set up for semantic search, or None. Never raises."""
    try:
        from zotero_mcp.config_light import load_reranker_config

        if config_path is None:
            from pathlib import Path

            config_path = str(Path.home() / ".config" / "zotero-mcp" / "config.json")
        cfg = load_reranker_config(config_path)
        if not cfg.get("enabled"):
            return None
        model = cfg.get("model") or "cross-encoder/ms-marco-MiniLM-L-6-v2"
        loaded = sys.modules.get("zotero_mcp.semantic_search")
        if loaded is not None:  # in the server: share its warm cache
            return loaded.get_cached_reranker(model, cfg)
        from zotero_mcp.rerank import APIReranker, CrossEncoderReranker

        if cfg.get("provider"):
            return APIReranker(model_name=model, provider=cfg["provider"], base_url=cfg.get("base_url"),
                               api_key_env=cfg.get("api_key_env"), instruction=cfg.get("instruction"),
                               timeout=float(cfg.get("timeout", 30.0)))
        return CrossEncoderReranker(model_name=model)
    except Exception:
        return None


def find(question: str, text: str, passages: int = DEFAULT_PASSAGES, reranker: object = "configured",
         keep_references: bool = False, log: Callable[[str], None] = lambda m: None) -> Excerpts:
    """The ``passages`` passages of ``text`` that best answer ``question``.

    ``reranker``: "configured" (the semantic-search re-ranker, if any), None (keywords only) or
    an object with ``rerank_detailed(query, documents, top_k)``.
    """
    question = " ".join(question.split())
    text = unicodedata.normalize("NFKC", text)  # "ﬁ" ligatures and the like, as plain letters
    want = max(1, min(int(passages or DEFAULT_PASSAGES), MAX_PASSAGES))
    keep_references = keep_references or bool(re.search(r"\b(reference|cite|citation|bibliograph)",
                                                        question, re.IGNORECASE))
    pages = text.count(PAGE_SEPARATOR) + 1 if PAGE_SEPARATOR in text else 0
    pool, dropped = split_passages(text, keep_references=keep_references)
    if not pool:
        return Excerpts(question, [], 0, pages, "", note="the text is empty")
    bm25(question, pool)
    by_keyword = sorted(pool, key=lambda p: (-p.keyword, p.index))
    candidates = by_keyword[:RERANK_POOL]
    if reranker == "configured":
        reranker = configured_reranker()
    ranked_by = "ranked by keyword match"
    note = ""
    chosen: list[Passage] = []
    if reranker is not None:
        try:
            pairs, error = reranker.rerank_detailed(question, [p.text for p in candidates], want)
        except Exception as e:  # a local model failing to load, say
            pairs, error = [], str(e)
        if error is None and pairs:
            ranked_by = f"ranked by {getattr(reranker, 'label', '') or 'the re-ranker'}"
            for i, score in pairs:
                candidates[i].score = score
                chosen.append(candidates[i])
        else:
            note = f"the re-ranker failed ({error}); ranked by keyword match"
            log(note)
    if not chosen:
        chosen = [p for p in by_keyword[:want] if p.keyword > 0]
        for p in chosen:
            p.score = p.keyword
    return Excerpts(question, chosen[:want], len(pool), pages, ranked_by, note=note, dropped_references=dropped)

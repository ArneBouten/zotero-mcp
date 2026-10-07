"""Find and attach full-text PDFs for items already in the library.

The steps follow what a researcher does by hand, cheapest first, and stop at
the first PDF that passes the checks:

1. ``open-access``: Unpaywall, OpenAlex, Semantic Scholar, Europe PMC, arXiv,
   CORE (with a key) and OAPEN for books. Free, no browser.
2. ``publisher``: the DOI's landing page and its ``citation_pdf_url``, plus
   the PDF address patterns of the large publishers. Subscribed papers come
   through here when the computer is on a network the library recognises.
3. ``scholar``: Google Scholar through SerpApi (key needed): the result's
   [PDF] link, then the other versions in its cluster.
4. ``web``: a web search through Tavily (key needed) for the exact title.
   Public pages that block plain requests (ResearchGate) can go through the
   ZenRows unblocker when a key is set.

Every PDF is checked before it is attached: a real PDF and not a cover page
or paywall stub, with the item's title and first author on its first pages.
Its version (published, accepted manuscript, preprint) is recorded in the
attachment's title. Captchas are never solved: a page that shows one is
recorded as such and skipped.

Only copies the user can open themselves are fetched: open-access copies,
public downloads, and subscriptions reached through the user's own network.
No shadow libraries.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

STEPS = ("open-access", "publisher", "scholar", "web", "browser")
#: Steps a run uses unless told otherwise. The browser step opens a visible
#: Chrome window, so it runs only when asked for (``--browser``).
DEFAULT_STEPS = ("open-access", "publisher", "scholar", "web")

#: Item types worth fetching a file for.
FETCHABLE_TYPES = {
    "journalArticle", "preprint", "conferencePaper", "book", "bookSection",
    "thesis", "report", "manuscript", "magazineArticle", "document",
}
BOOK_TYPES = {"book", "bookSection", "thesis", "report"}

TAG_NOT_FOUND = "fulltext/not-found"
TAG_FETCHED = "fulltext/fetched"
VERSION_TAGS = {"accepted": "fulltext/accepted-manuscript", "preprint": "fulltext/preprint"}
VERSION_LABELS = {
    "published": "published version",
    "accepted": "accepted manuscript",
    "preprint": "preprint",
    None: "version unknown",
}

KEY_ENV = {
    "openalex": "OPENALEX_API_KEY",
    "serpapi": "SERPAPI_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "zenrows": "ZENROWS_API_KEY",
    "core": "CORE_API_KEY",
    "semantic_scholar": "SEMANTIC_SCHOLAR_API_KEY",
}

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
API_UA = "zotero-mcp fetch-fulltext (+https://github.com/54yyyu/zotero-mcp)"
MAX_PDF_BYTES = 150 * 1024 * 1024
MIN_PDF_BYTES = 5 * 1024

_PAYWALL_MARKERS = (
    "purchase this article", "buy this article", "access through your institution",
    "log in to your account", "preview of subscription content", "rent this article",
    "get access", "check access", "subscribe to journal", "this is a preview",
    "you do not have access",
)
_CAPTCHA_MARKERS = (
    "captcha", "cf-chl", "just a moment...", "are you a robot", "verify you are human",
    "unusual traffic", "challenge-platform",
)
_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "onto", "that", "this", "their",
    "between", "among", "about", "over", "under", "within", "without", "through",
    "its", "are", "was", "were", "how", "what", "when", "why", "who", "does", "not",
}


def config_dir() -> Path:
    return Path.home() / ".config" / "zotero-mcp"


def state_dir() -> Path:
    return config_dir() / "fulltext"


# ---------------------------------------------------------------------------
# Settings, keys and budgets
# ---------------------------------------------------------------------------


@dataclass
class Settings:
    """What the fetcher may use. Read from the environment and config.json.

    Keys come from the environment first, then from ``keys.env`` beside the
    config (one ``KEY=value`` per line), then from ``client_env`` in
    ``~/.config/zotero-mcp/config.json``. Limits come from the
    ``fulltext_fetch`` section of the same file.
    """

    keys: dict[str, str] = field(default_factory=dict)
    email: str = ""
    serpapi_monthly: int = 250
    tavily_monthly: int = 1000
    zenrows_monthly_credits: int = 5000
    zenrows_cost: int = 25
    openalex_content_daily: int = 100
    host_delay: float = 6.0
    retry_days: int = 30
    max_candidates: int = 12
    unblocker_hosts: tuple[str, ...] = ("researchgate.net", "academia.edu")
    #: Browser step: optional library proxy prefix (e.g. an EZproxy login
    #: URL ending in ``?url=``), Chrome channel and the pause between papers.
    proxy_prefix: str = ""
    browser_channel: str = "chrome"
    browser_delay: tuple[float, float] = (10.0, 20.0)
    #: ResearchGate flags a network that opens many of its pages; the browser
    #: step opens at most this many per run, slower than other sites.
    researchgate_per_run: int = 20
    researchgate_delay: tuple[float, float] = (25.0, 45.0)

    @classmethod
    def load(cls, config_path: Path | None = None) -> Settings:
        path = config_path or (config_dir() / "config.json")
        cfg: dict[str, Any] = {}
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        client_env = dict(cfg.get("client_env") or {})
        client_env.update(read_keys_file(path.with_name("keys.env")))
        section = cfg.get("fulltext_fetch") or {}
        keys = {}
        for name, env in KEY_ENV.items():
            value = os.environ.get(env) or client_env.get(env) or ""
            if value:
                keys[name] = str(value).strip()
        email = (
            os.environ.get("UNPAYWALL_EMAIL")
            or client_env.get("UNPAYWALL_EMAIL")
            or section.get("email")
            or ""
        )
        s = cls(keys=keys, email=str(email).strip())
        for name in (
            "serpapi_monthly", "tavily_monthly", "zenrows_monthly_credits", "zenrows_cost",
            "openalex_content_daily", "retry_days", "max_candidates",
        ):
            if isinstance(section.get(name), int):
                setattr(s, name, section[name])
        if isinstance(section.get("host_delay"), (int, float)):
            s.host_delay = float(section["host_delay"])
        if isinstance(section.get("unblocker_hosts"), list):
            s.unblocker_hosts = tuple(str(h) for h in section["unblocker_hosts"])
        if isinstance(section.get("researchgate_per_run"), int):
            s.researchgate_per_run = section["researchgate_per_run"]
        if isinstance(section.get("proxy_prefix"), str):
            s.proxy_prefix = section["proxy_prefix"].strip()
        if isinstance(section.get("browser_channel"), str):
            s.browser_channel = section["browser_channel"].strip()
        delay = section.get("browser_delay")
        if isinstance(delay, list) and len(delay) == 2:
            s.browser_delay = (float(delay[0]), float(delay[1]))
        return s

    def has(self, name: str) -> bool:
        return bool(self.keys.get(name))


def read_keys_file(path: Path) -> dict[str, str]:
    """``KEY=value`` lines from a plain text file (``keys.env``). Lines that
    are empty or start with ``#`` are ignored; quotes around a value are
    dropped. A missing file is an empty result."""
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            out[key] = value
    return out


class Budget:
    """Monthly (or daily) counters for the services with a free allowance."""

    def __init__(self, path: Path | None = None):
        self.path = path or (state_dir() / "budget.json")
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}

    @staticmethod
    def _period(daily: bool) -> str:
        today = _dt.date.today()
        return today.isoformat() if daily else today.strftime("%Y-%m")

    def used(self, service: str, daily: bool = False) -> int:
        return int(self.data.get(service, {}).get(self._period(daily), 0))

    def allows(self, service: str, limit: int, cost: int = 1, daily: bool = False) -> bool:
        return self.used(service, daily) + cost <= limit

    def spend(self, service: str, cost: int = 1, daily: bool = False) -> None:
        bucket = self.data.setdefault(service, {})
        period = self._period(daily)
        bucket[period] = int(bucket.get(period, 0)) + cost
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Items and text matching
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    try:
        from unidecode import unidecode

        text = unidecode(text)
    except Exception:
        pass
    text = re.sub(r"<[^>]+>", " ", text)  # Zotero rich-text markup in titles
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def title_similarity(a: str, b: str) -> float:
    fa, fb = _fold(a), _fold(b)
    if not fa or not fb:
        return 0.0
    ratio = difflib.SequenceMatcher(None, fa, fb).ratio()
    main_a = _fold(re.split(r"[:?.]\s", a, maxsplit=1)[0])
    if len(main_a) >= 20 and (main_a == fb or fb.startswith(main_a)):
        ratio = max(ratio, 0.95)
    return ratio


def _significant_words(title: str) -> list[str]:
    words = [w for w in _fold(title).split() if len(w) >= 3 and w not in _STOPWORDS]
    return words[:14]


def _haystack(text: str) -> str:
    """Folded text, plus a copy without digits: affiliation and footnote
    markers stick to names and words ("Kucsko1", "motivation2")."""
    folded = re.sub(r"\s+", " ", _fold(text))
    return f" {folded} {re.sub(r'[0-9]+', ' ', folded)} "


def title_in_text(title: str, text: str) -> float:
    """Share of the title's significant words found in ``text``."""
    words = _significant_words(title)
    if not words:
        return 1.0
    hay = _haystack(text)
    hay_nospace = hay.replace(" ", "")
    found = 0
    for w in words:
        # Extraction sometimes drops the spaces in italic runs; fall back to
        # a substring of the space-free text for longer words.
        if f" {w} " in hay or (len(w) >= 6 and w in hay_nospace):
            found += 1
    return found / len(words)


@dataclass
class ItemInfo:
    key: str
    item_type: str
    title: str
    authors: list[str]
    year: str
    doi: str = ""
    isbn: str = ""
    url: str = ""
    pages: str = ""
    arxiv: str = ""

    @classmethod
    def from_zotero(cls, item: dict) -> ItemInfo:
        data = item.get("data", item)
        authors = []
        for c in data.get("creators") or []:
            last = c.get("lastName") or c.get("name") or ""
            if last and c.get("creatorType", "author") in ("author", "editor", "contributor"):
                authors.append(last)
        doi = (data.get("DOI") or "").strip()
        extra = data.get("extra") or ""
        if not doi:
            m = re.search(r"^DOI:\s*(10\.\S+)", extra, re.IGNORECASE | re.MULTILINE)
            if m:
                doi = m.group(1)
        doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi, flags=re.IGNORECASE).strip()
        arxiv = ""
        m = re.search(r"arXiv:\s*(\d{4}\.\d{4,5})", extra + " " + (data.get("url") or ""), re.IGNORECASE)
        if m:
            arxiv = m.group(1)
        m = re.search(r"(\d{4})", str(data.get("date") or ""))
        return cls(
            key=item.get("key") or data.get("key", ""),
            item_type=data.get("itemType", ""),
            title=re.sub(r"<[^>]+>", "", data.get("title") or "").strip(),
            authors=authors,
            year=m.group(1) if m else "",
            doi=doi,
            isbn=re.sub(r"[^0-9Xx]", "", (data.get("ISBN") or "").split()[0]) if data.get("ISBN") else "",
            url=data.get("url") or "",
            pages=str(data.get("pages") or ""),
            arxiv=arxiv,
        )

    @property
    def first_author(self) -> str:
        return self.authors[0] if self.authors else ""

    @property
    def label(self) -> str:
        who = self.first_author or "Anon."
        if len(self.authors) > 2:
            who += " et al."
        elif len(self.authors) == 2:
            who += f" & {self.authors[1]}"
        return f"{who} ({self.year or 'n.d.'}) {self.title[:70]}"

    def expected_pages(self) -> int | None:
        m = re.match(r"\s*(\d+)\s*[-–—]\s*(\d+)\s*$", self.pages)
        if not m:
            return None
        first, last = int(m.group(1)), int(m.group(2))
        if last < first:  # "1123-35"
            last = int(str(first)[: len(str(first)) - len(str(last))] + str(last))
        span = last - first + 1
        return span if 0 < span < 3000 else None


# ---------------------------------------------------------------------------
# HTTP with an SSRF guard and per-host pacing
# ---------------------------------------------------------------------------


def _public_host(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or None)
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            return False
    return bool(infos)


@dataclass
class Fetched:
    status: int
    content_type: str
    body: bytes
    url: str
    error: str = ""

    @property
    def is_pdf(self) -> bool:
        return self.body[:5] == b"%PDF-" or self.body.lstrip()[:5] == b"%PDF-"

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class Http:
    """Small HTTP layer: browser-like for pages, polite to each host."""

    def __init__(self, settings: Settings, session=None, sleep=time.sleep):
        import requests

        self.settings = settings
        self.session = session or requests.Session()
        self.sleep = sleep
        self._last_hit: dict[str, float] = {}
        #: item key -> candidates that a plain request could not get
        self.blocked: dict[str, list] = {}
        #: the browser session, started by the browser step when first needed
        self.browser = None
        self.log: Callable[[str], None] = lambda m: None

    def _pace(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        base = ".".join(host.split(".")[-2:])
        last = self._last_hit.get(base)
        now = time.monotonic()
        if last is not None and now - last < self.settings.host_delay:
            self.sleep(self.settings.host_delay - (now - last))
        self._last_hit[base] = time.monotonic()

    def api_json(self, url: str, *, params=None, headers=None, method="GET", body=None, timeout=25):
        """GET/POST a JSON API. Returns (status, data or None)."""
        h = {"User-Agent": API_UA, "Accept": "application/json"}
        h.update(headers or {})
        for attempt in range(3):
            try:
                if method == "POST":
                    resp = self.session.post(url, params=params, headers=h, json=body, timeout=timeout)
                else:
                    resp = self.session.get(url, params=params, headers=h, timeout=timeout)
            except Exception:
                return 0, None
            if resp.status_code == 429 and attempt < 1:
                # One short wait. A service that has used up its daily
                # allowance (OpenAlex without a key) keeps refusing, and
                # waiting longer only holds up the paper.
                try:
                    wait = float(resp.headers.get("Retry-After") or 3)
                except ValueError:
                    wait = 3.0
                if wait > 5:
                    return 429, None
                self.sleep(wait)
                continue
            try:
                return resp.status_code, resp.json()
            except Exception:
                return resp.status_code, None
        return 429, None

    def fetch(self, url: str, *, max_bytes: int = MAX_PDF_BYTES, referer: str | None = None) -> Fetched:
        """GET a page or file, re-checking every redirect.

        Publishers often refuse clients that do not look like a browser, while
        some bot shields (AWS WAF on figshare, for one) challenge a browser
        identity that cannot run JavaScript but let a plain client through.
        So a refusal with the browser identity is retried once as zotero-mcp.
        """
        got = self._fetch(url, max_bytes=max_bytes, referer=referer, ua=BROWSER_UA)
        if not got.error and not got.is_pdf and got.status in (202, 403, 429):
            plain = self._fetch(url, max_bytes=max_bytes, referer=referer, ua=API_UA)
            if plain.is_pdf or (not plain.error and plain.status == 200):
                return plain
        return got

    def _fetch(self, url: str, *, max_bytes: int, referer: str | None, ua: str) -> Fetched:
        current = url
        headers = {
            "User-Agent": ua,
            "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9,nl;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        for _ in range(8):
            if not _public_host(current):
                return Fetched(0, "", b"", current, "address not allowed")
            self._pace(current)
            try:
                resp = self.session.get(current, headers=headers, timeout=40, stream=True, allow_redirects=False)
            except Exception as e:
                return Fetched(0, "", b"", current, f"connection failed ({type(e).__name__})")
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                current = urljoin(current, resp.headers["Location"])
                resp.close()
                continue
            chunks, size = [], 0
            try:
                for chunk in resp.iter_content(65536):
                    size += len(chunk)
                    if size > max_bytes:
                        resp.close()
                        return Fetched(resp.status_code, resp.headers.get("Content-Type", ""), b"", current, "file too large")
                    chunks.append(chunk)
            except Exception as e:
                return Fetched(resp.status_code, "", b"", current, f"download interrupted ({type(e).__name__})")
            return Fetched(resp.status_code, resp.headers.get("Content-Type", ""), b"".join(chunks), current)
        return Fetched(0, "", b"", current, "too many redirects")

    def fetch_unblocked(self, url: str, budget: Budget) -> Fetched | None:
        """Fetch a public page through ZenRows. None when not allowed or no budget."""
        s = self.settings
        if not s.has("zenrows"):
            return None
        if not budget.allows("zenrows", s.zenrows_monthly_credits, s.zenrows_cost):
            return None
        budget.spend("zenrows", s.zenrows_cost)
        try:
            resp = self.session.get(
                "https://api.zenrows.com/v1/",
                params={"apikey": s.keys["zenrows"], "url": url, "js_render": "true", "premium_proxy": "true"},
                timeout=60,
            )
        except Exception as e:
            return Fetched(0, "", b"", url, f"unblocker failed ({type(e).__name__})")
        return Fetched(resp.status_code, resp.headers.get("Content-Type", ""), resp.content, url)


# ---------------------------------------------------------------------------
# Candidates from each source
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    url: str
    source: str
    version: str | None = None      # published / accepted / preprint
    by_identifier: bool = False     # found by DOI/ISBN, not by a title search
    unblock: bool = False           # fetch through the unblocker
    referer: str | None = None
    #: Gets the file itself instead of a plain download (the browser step).
    #: Returns a Fetched, or None with the reason in ``fetcher.reason``.
    fetcher: Callable[[], Fetched | None] | None = field(default=None, repr=False, compare=False)


_UNPAYWALL_VERSIONS = {
    "publishedVersion": "published", "acceptedVersion": "accepted", "submittedVersion": "preprint",
}


def _norm_version(v: str | None) -> str | None:
    return _UNPAYWALL_VERSIONS.get(v or "", None)


def src_unpaywall(item: ItemInfo, http: Http, settings: Settings, budget: Budget) -> Iterator[Candidate]:
    if not item.doi:
        return
    email = settings.email or "zotero-mcp@users.noreply.github.com"
    status, data = http.api_json(f"https://api.unpaywall.org/v2/{quote(item.doi, safe='/')}", params={"email": email})
    if status != 200 or not data:
        return
    locations = [data.get("best_oa_location") or {}] + list(data.get("oa_locations") or [])
    for loc in locations:
        url = loc.get("url_for_pdf")
        if url:
            yield Candidate(url, f"Unpaywall ({loc.get('host_type') or 'oa'})",
                            _norm_version(loc.get("version")), by_identifier=True,
                            referer=loc.get("url_for_landing_page"))


def _openalex_params(settings: Settings, extra: dict | None = None) -> dict:
    params = dict(extra or {})
    if settings.has("openalex"):
        params["api_key"] = settings.keys["openalex"]
    if settings.email:
        params["mailto"] = settings.email
    return params


def _openalex_work(item: ItemInfo, http: Http, settings: Settings) -> dict | None:
    if item.doi:
        status, data = http.api_json(
            f"https://api.openalex.org/works/doi:{quote(item.doi, safe='/')}", params=_openalex_params(settings)
        )
        return data if status == 200 and data else None
    if not item.title:
        return None
    filt = f"publication_year:{int(item.year) - 1}-{int(item.year) + 1}" if item.year.isdigit() else None
    params = {"search": item.title[:250], "per-page": "5"}
    if filt:
        params["filter"] = filt
    status, data = http.api_json("https://api.openalex.org/works", params=_openalex_params(settings, params))
    if status != 200 or not data:
        return None
    for work in data.get("results") or []:
        if title_similarity(item.title, work.get("title") or "") >= 0.9:
            return work
    return None


def src_openalex(item: ItemInfo, http: Http, settings: Settings, budget: Budget) -> Iterator[Candidate]:
    work = _openalex_work(item, http, settings)
    if not work:
        return
    by_id = bool(item.doi)
    seen = set()
    for loc in [work.get("best_oa_location") or {}] + list(work.get("locations") or []):
        url = loc.get("pdf_url")
        if url and url not in seen:
            seen.add(url)
            name = ((loc.get("source") or {}).get("display_name")) or "repository"
            yield Candidate(url, f"OpenAlex ({name})", _norm_version(loc.get("version")),
                            by_identifier=by_id, referer=loc.get("landing_page_url"))
    content = (work.get("content_urls") or {}).get("pdf")
    if content and (work.get("has_content") or {}).get("pdf") and settings.has("openalex"):
        if budget.allows("openalex_content", settings.openalex_content_daily, daily=True):
            budget.spend("openalex_content", daily=True)
            yield Candidate(f"{content}?api_key={settings.keys['openalex']}", "OpenAlex (cached copy)",
                            None, by_identifier=by_id)


def src_semantic_scholar(item: ItemInfo, http: Http, settings: Settings, budget: Budget) -> Iterator[Candidate]:
    headers = {"x-api-key": settings.keys["semantic_scholar"]} if settings.has("semantic_scholar") else {}
    fields = "title,openAccessPdf,externalIds"
    if item.doi:
        status, data = http.api_json(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(item.doi, safe='/')}",
            params={"fields": fields}, headers=headers,
        )
        by_id = True
    elif item.title:
        status, data = http.api_json(
            "https://api.semanticscholar.org/graph/v1/paper/search/match",
            params={"query": item.title[:250], "fields": fields}, headers=headers,
        )
        if status == 200 and data and data.get("data"):
            data = data["data"][0]
        by_id = False
        if not data or title_similarity(item.title, data.get("title") or "") < 0.9:
            return
    else:
        return
    if status != 200 or not data:
        return
    url = (data.get("openAccessPdf") or {}).get("url")
    if url:
        yield Candidate(url, "Semantic Scholar", None, by_identifier=by_id)
    arxiv = (data.get("externalIds") or {}).get("ArXiv")
    if arxiv:
        yield Candidate(f"https://arxiv.org/pdf/{arxiv}", "arXiv (via Semantic Scholar)", "preprint", by_identifier=by_id)


def src_europepmc(item: ItemInfo, http: Http, settings: Settings, budget: Budget) -> Iterator[Candidate]:
    if item.doi:
        query = f'DOI:"{item.doi}"'
    elif item.title:
        query = f'TITLE:"{item.title[:200]}"'
    else:
        return
    status, data = http.api_json(
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        params={"query": query, "format": "json", "resultType": "lite", "pageSize": "3"},
    )
    if status != 200 or not data:
        return
    for hit in (data.get("resultList") or {}).get("result") or []:
        if not item.doi and title_similarity(item.title, hit.get("title") or "") < 0.9:
            continue
        pmcid = hit.get("pmcid")
        if pmcid and hit.get("isOpenAccess") == "Y":
            yield Candidate(f"https://europepmc.org/articles/{pmcid}?pdf=render", "Europe PMC",
                            None, by_identifier=bool(item.doi))
            yield Candidate(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/", "PubMed Central",
                            None, by_identifier=bool(item.doi))
        break


def src_arxiv(item: ItemInfo, http: Http, settings: Settings, budget: Budget) -> Iterator[Candidate]:
    if item.arxiv:
        yield Candidate(f"https://arxiv.org/pdf/{item.arxiv}", "arXiv", "preprint", by_identifier=True)
        return
    if not item.title or item.item_type in BOOK_TYPES:
        return
    words = " ".join(_significant_words(item.title)[:8])
    if not words:
        return
    try:
        resp = http.session.get(
            "https://export.arxiv.org/api/query",
            params={"search_query": f"ti:({words})", "max_results": "5"},
            headers={"User-Agent": API_UA}, timeout=25,
        )
        feed = resp.text if resp.status_code == 200 else ""
    except Exception:
        return
    for entry in re.findall(r"<entry>(.*?)</entry>", feed, re.S):
        t = re.search(r"<title>(.*?)</title>", entry, re.S)
        i = re.search(r"<id>https?://arxiv.org/abs/([^<]+)</id>", entry)
        if t and i and title_similarity(item.title, re.sub(r"\s+", " ", t.group(1))) >= 0.9:
            yield Candidate(f"https://arxiv.org/pdf/{i.group(1)}", "arXiv", "preprint")
            return


def src_core(item: ItemInfo, http: Http, settings: Settings, budget: Budget) -> Iterator[Candidate]:
    if not settings.has("core"):
        return
    q = f'doi:"{item.doi}"' if item.doi else f'title:"{item.title[:200]}"'
    status, data = http.api_json(
        "https://api.core.ac.uk/v3/search/works", params={"q": q, "limit": "5"},
        headers={"Authorization": f"Bearer {settings.keys['core']}"},
    )
    if status != 200 or not data:
        return
    for hit in data.get("results") or []:
        if not item.doi and title_similarity(item.title, hit.get("title") or "") < 0.9:
            continue
        url = hit.get("downloadUrl")
        if url:
            yield Candidate(url, "CORE", None, by_identifier=bool(item.doi))


def src_oapen(item: ItemInfo, http: Http, settings: Settings, budget: Budget) -> Iterator[Candidate]:
    if item.item_type not in ("book", "bookSection"):
        return
    query = f"isbn:{item.isbn}" if item.isbn else f'"{item.title[:150]}"'
    status, data = http.api_json(
        "https://library.oapen.org/rest/search", params={"query": query, "expand": "bitstreams,metadata"},
    )
    if status != 200 or not isinstance(data, list):
        return
    for hit in data[:5]:
        name = hit.get("name") or ""
        if not item.isbn and title_similarity(item.title, name) < 0.85:
            continue
        for bit in hit.get("bitstreams") or []:
            if (bit.get("mimeType") or "").endswith("pdf") and bit.get("retrieveLink"):
                yield Candidate(urljoin("https://library.oapen.org", bit["retrieveLink"]), "OAPEN",
                                "published", by_identifier=bool(item.isbn))


def src_osf(item: ItemInfo, http: Http, settings: Settings, budget: Budget) -> Iterator[Candidate]:
    """Preprints on OSF: PsyArXiv, SocArXiv, EdArXiv, MetaArXiv and the rest."""
    if not item.title or item.item_type in ("book", "bookSection"):
        return
    main = re.split(r"[:?]\s", item.title, maxsplit=1)[0][:150]
    status, data = http.api_json(
        "https://api.osf.io/v2/preprints/",
        params={"filter[title]": main, "page[size]": "10"},
    )
    if status != 200 or not isinstance(data, dict):
        return
    for hit in data.get("data") or []:
        attrs = hit.get("attributes") or {}
        if title_similarity(item.title, attrs.get("title") or "") < 0.85:
            continue
        rel = (hit.get("relationships") or {})
        file_id = ((rel.get("primary_file") or {}).get("data") or {}).get("id")
        if not file_id:
            href = (((rel.get("primary_file") or {}).get("links") or {}).get("related") or {}).get("href") or ""
            m = re.search(r"/files/([^/]+)/?$", href)
            file_id = m.group(1) if m else None
        provider = (((rel.get("provider") or {}).get("data")) or {}).get("id") or "OSF"
        if file_id:
            yield Candidate(f"https://osf.io/download/{file_id}/", f"OSF ({provider})", "preprint")


def _query_title(item: ItemInfo) -> str:
    """The main title, without quotes or Solr/Lucene operators."""
    main = re.split(r"[:?]\s", item.title, maxsplit=1)[0]
    return re.sub(r'[\\"():\[\]{}^~*?!+\-/]', " ", main)[:150].strip()


def src_zenodo(item: ItemInfo, http: Http, settings: Settings, budget: Budget) -> Iterator[Candidate]:
    if not item.title:
        return
    q = f'doi:"{item.doi}"' if item.doi else f'title:("{_query_title(item)}")'
    status, data = http.api_json("https://zenodo.org/api/records", params={"q": q, "size": "5"})
    if status != 200 or not isinstance(data, dict):
        return
    for hit in (data.get("hits") or {}).get("hits") or []:
        meta = hit.get("metadata") or {}
        if title_similarity(item.title, meta.get("title") or "") < 0.85:
            continue
        for f in hit.get("files") or []:
            name = (f.get("key") or "").lower()
            link = (f.get("links") or {}).get("self")
            if name.endswith(".pdf") and link:
                yield Candidate(link, "Zenodo", None, by_identifier=bool(item.doi))


def src_hal(item: ItemInfo, http: Http, settings: Settings, budget: Budget) -> Iterator[Candidate]:
    if not item.title:
        return
    q = f'doiId_s:"{item.doi}"' if item.doi else f'title_t:"{_query_title(item)}"'
    status, data = http.api_json(
        "https://api.archives-ouvertes.fr/search/",
        params={"q": q, "fl": "title_s,fileMain_s", "rows": "5", "wt": "json"},
    )
    if status != 200 or not isinstance(data, dict):
        return
    for doc in (data.get("response") or {}).get("docs") or []:
        titles = doc.get("title_s") or [""]
        if not item.doi and max(title_similarity(item.title, t) for t in titles) < 0.85:
            continue
        if doc.get("fileMain_s"):
            yield Candidate(doc["fileMain_s"], "HAL", None, by_identifier=bool(item.doi))


#: PDF address patterns by publisher host, for when the landing page has no
#: citation_pdf_url (or blocks plain requests but serves the file).
_PUBLISHER_PATTERNS = {
    "onlinelibrary.wiley.com": ["https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"],
    "tandfonline.com": ["https://www.tandfonline.com/doi/pdf/{doi}"],
    "journals.sagepub.com": ["https://journals.sagepub.com/doi/pdf/{doi}"],
    "link.springer.com": ["https://link.springer.com/content/pdf/{doi}.pdf"],
    "pubs.acs.org": ["https://pubs.acs.org/doi/pdf/{doi}"],
    "journals.lww.com": [],
    "psycnet.apa.org": [],
}
_DOI_PREFIX_HOSTS = {
    "10.1111": "onlinelibrary.wiley.com", "10.1002": "onlinelibrary.wiley.com",
    "10.1080": "tandfonline.com", "10.1177": "journals.sagepub.com",
    "10.1007": "link.springer.com",
}


def src_publisher(item: ItemInfo, http: Http, settings: Settings, budget: Budget) -> Iterator[Candidate]:
    if not item.doi:
        if item.url.startswith("http"):
            page = http.fetch(item.url, max_bytes=8 * 1024 * 1024)
            if page.is_pdf:
                yield Candidate(page.url, "item's URL field", None, by_identifier=True)
            elif page.status == 200:
                pdf = _pdf_link_in_page(page)
                if pdf:
                    yield Candidate(pdf, "item's URL field, PDF link on that page", None, by_identifier=True, referer=page.url)
        return
    doi = item.doi
    page = http.fetch(f"https://doi.org/{quote(doi, safe='/')}", max_bytes=8 * 1024 * 1024)
    refresh = _meta_refresh(page)
    if refresh:  # Elsevier's linkinghub, some society platforms
        page = http.fetch(refresh, max_bytes=8 * 1024 * 1024, referer=page.url)
    host = urlparse(page.url).hostname or ""
    if page.is_pdf:
        yield Candidate(page.url, f"publisher ({host})", "published", by_identifier=True)
        return
    if page.status == 200:
        pdf = _pdf_link_in_page(page)
        if pdf:
            yield Candidate(pdf, f"publisher ({host})", "published", by_identifier=True, referer=page.url)
    if not host:
        host = _DOI_PREFIX_HOSTS.get(doi.split("/")[0], "")
    for pattern_host, patterns in _PUBLISHER_PATTERNS.items():
        if host.endswith(pattern_host):
            for p in patterns:
                yield Candidate(p.format(doi=doi), f"publisher ({pattern_host})", "published",
                                by_identifier=True, referer=page.url)


def _meta_refresh(page: Fetched) -> str | None:
    if page.is_pdf or page.status != 200:
        return None
    m = re.search(
        r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]+content=[\"'][^\"']*?url=[\"']?([^\"'>\s]+)",
        page.text[:20000], re.IGNORECASE,
    )
    if not m:
        return None
    target = urljoin(page.url, m.group(1).strip("'\""))
    return target if target != page.url else None


def _pdf_link_in_page(page: Fetched) -> str | None:
    if "html" not in page.content_type.lower() and not page.text.lstrip().lower().startswith("<"):
        return None
    try:
        from zotero_mcp.html_metadata import extract_embedded_metadata

        pdf = extract_embedded_metadata(page.text).pdf_url
    except Exception:
        pdf = ""
    if pdf:
        return urljoin(page.url, pdf)
    return None


def _scholar_search(params: dict, http: Http, settings: Settings, budget: Budget) -> dict | None:
    if not budget.allows("serpapi", settings.serpapi_monthly):
        return None
    budget.spend("serpapi")
    status, data = http.api_json(
        "https://serpapi.com/search.json",
        params={"engine": "google_scholar", "api_key": settings.keys["serpapi"], **params},
        timeout=60,
    )
    return data if status == 200 and data else None


def _scholar_pdf_links(result: dict) -> Iterator[str]:
    for res in result.get("resources") or []:
        if (res.get("file_format") or "").upper() == "PDF" and res.get("link"):
            yield res["link"]
    link = result.get("link") or ""
    if link.lower().endswith(".pdf"):
        yield link


def src_scholar(item: ItemInfo, http: Http, settings: Settings, budget: Budget) -> Iterator[Candidate]:
    if not settings.has("serpapi") or not item.title:
        return
    q = f'"{item.title[:200]}"'
    if item.first_author:
        q += f" {item.first_author}"
    data = _scholar_search({"q": q, "num": "5"}, http, settings, budget)
    if not data:
        return
    cluster_ids = []
    for result in data.get("organic_results") or []:
        if title_similarity(item.title, result.get("title") or "") < 0.85:
            continue
        for link in _scholar_pdf_links(result):
            yield Candidate(link, "Google Scholar", None, unblock=_needs_unblock(link, settings))
        cid = ((result.get("inline_links") or {}).get("versions") or {}).get("cluster_id")
        if cid:
            cluster_ids.append(cid)
    for cid in cluster_ids[:1]:
        versions = _scholar_search({"cluster": cid, "num": "20"}, http, settings, budget)
        for result in (versions or {}).get("organic_results") or []:
            for link in _scholar_pdf_links(result):
                yield Candidate(link, "Google Scholar (all versions)", None,
                                unblock=_needs_unblock(link, settings))


def _needs_unblock(url: str, settings: Settings) -> bool:
    host = urlparse(url).hostname or ""
    return any(host == h or host.endswith("." + h) for h in settings.unblocker_hosts)


def src_web(item: ItemInfo, http: Http, settings: Settings, budget: Budget) -> Iterator[Candidate]:
    if not settings.has("tavily") or not item.title:
        return
    if not budget.allows("tavily", settings.tavily_monthly):
        return
    budget.spend("tavily")
    query = f'"{item.title[:200]}" {item.first_author} pdf'.strip()
    status, data = http.api_json(
        "https://api.tavily.com/search", method="POST",
        headers={"Authorization": f"Bearer {settings.keys['tavily']}"},
        body={"query": query, "max_results": 10, "search_depth": "basic"},
        timeout=60,
    )
    if status != 200 or not data:
        return
    for res in data.get("results") or []:
        url = res.get("url") or ""
        text = f"{res.get('title') or ''} {res.get('content') or ''}"
        if title_in_text(item.title, text) < 0.6:
            continue
        lower = url.lower()
        if lower.endswith(".pdf") or "/download" in lower or "pdf" in urlparse(lower).path:
            yield Candidate(url, "web search", None, unblock=_needs_unblock(url, settings))
        elif "researchgate.net/publication/" in lower:
            yield Candidate(url, "ResearchGate", None, unblock=True)


#: Display names, and the key each paid source needs.
SOURCE_NAMES = {
    "src_unpaywall": "Unpaywall", "src_openalex": "OpenAlex", "src_semantic_scholar": "Semantic Scholar",
    "src_europepmc": "Europe PMC", "src_arxiv": "arXiv", "src_core": "CORE", "src_oapen": "OAPEN",
    "src_publisher": "publisher page (via DOI, or the item's URL)",
    "src_scholar": "Google Scholar (via SerpApi)", "src_web": "web search (via Tavily)",
    "src_osf": "OSF preprints (PsyArXiv and others)", "src_zenodo": "Zenodo", "src_hal": "HAL", "src_browser": "your browser (ResearchGate, Academia.edu, publisher logins)",
}
SOURCE_KEYS = {"src_scholar": "serpapi", "src_web": "tavily", "src_core": "core"}
_BUDGETS = {"serpapi": "serpapi_monthly", "tavily": "tavily_monthly"}


def source_status(source, settings: Settings, budget: Budget) -> str | None:
    """Why a source will not run, or None when it will."""
    key = SOURCE_KEYS.get(source.__name__)
    if key and not settings.has(key):
        return f"skipped (no {KEY_ENV[key]})"
    limit_attr = _BUDGETS.get(key or "")
    if limit_attr and not budget.allows(key, getattr(settings, limit_attr)):
        return f"skipped (free monthly allowance of {getattr(settings, limit_attr)} used up)"
    return None


def describe_setup(settings: Settings, budget: Budget, steps: Iterable[str] = DEFAULT_STEPS) -> list[str]:
    """One line per step: its services and whether each can run."""
    lines = []
    for step in steps:
        parts = []
        for source in SOURCES.get(step, []):
            name = SOURCE_NAMES.get(source.__name__, source.__name__)
            why = source_status(source, settings, budget)
            key = SOURCE_KEYS.get(source.__name__)
            if why:
                parts.append(f"{name}: {why.removeprefix('skipped ').strip('()')}")
            elif key in _BUDGETS:
                limit = getattr(settings, _BUDGETS[key])
                parts.append(f"{name}: {budget.used(key)}/{limit} used this month")
            else:
                parts.append(name)
        lines.append(f"  {step}: " + "; ".join(parts))
    extras = []
    if settings.has("openalex"):
        extras.append("OpenAlex key found")
    if settings.has("zenrows"):
        extras.append(f"ZenRows for ResearchGate: {budget.used('zenrows')}/{settings.zenrows_monthly_credits} credits used")
    extras.append(f"Unpaywall email: {'set' if settings.email else 'default (set UNPAYWALL_EMAIL)'}")
    lines.append("  " + "; ".join(extras))
    return lines


SOURCES: dict[str, list[Callable[..., Iterator[Candidate]]]] = {
    "open-access": [src_unpaywall, src_openalex, src_semantic_scholar, src_europepmc, src_arxiv, src_osf,
                    src_core, src_zenodo, src_hal, src_oapen],
    "publisher": [src_publisher],
    "scholar": [src_scholar],
    "web": [src_web],
    "browser": [],  # filled in below, from fulltext_browser
}


# ---------------------------------------------------------------------------
# Checking a PDF
# ---------------------------------------------------------------------------


def probe_pdf(path: str | Path, pages: int = 4, timeout: int = 90) -> dict | None:
    """Page count and first pages' text, read in a separate process.

    PyMuPDF can crash on damaged files; a separate process keeps a bad file
    from taking the whole run down. None when the file cannot be read at all.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-m", "zotero_mcp.fulltext_fetch", "probe", str(path), str(pages)],
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


def _probe_main(path: str, pages: int) -> int:
    import pymupdf

    with pymupdf.open(path) as doc:
        n = doc.page_count
        texts = []
        for i in range(min(n, pages)):
            texts.append(doc[i].get_text())
        if n > pages:
            texts.append(doc[n - 1].get_text())
        meta = doc.metadata or {}
    text = "\n".join(texts)
    print(json.dumps({
        "pages": n,
        "text": text[:40000],
        "meta_title": meta.get("title") or "",
        "meta_author": meta.get("author") or "",
    }))
    return 0


def detect_version(text: str, hint: str | None, doi: str) -> str | None:
    low = text.lower()
    if re.search(r"\barxiv:\s*\d{4}\.\d{4,5}", low) or "psyarxiv" in low or "biorxiv" in low or "medrxiv" in low:
        return "preprint"
    if "not peer reviewed" in low or "not been peer reviewed" in low or re.search(r"\bpreprint\b", low[:3000]):
        return "preprint"
    if re.search(r"accepted (author )?manuscript|author accepted|postprint|author'?s? (final )?manuscript", low):
        return "accepted"
    if hint:
        return hint
    if doi and doi.lower() in low:
        return "published"
    return None


@dataclass
class Check:
    ok: bool
    reason: str
    version: str | None = None


def check_pdf(item: ItemInfo, probe: dict | None, cand: Candidate, size: int) -> Check:
    if size < MIN_PDF_BYTES:
        return Check(False, "too small to be the paper")
    if probe is None:
        return Check(False, "not a readable PDF")
    pages = int(probe.get("pages") or 0)
    text = probe.get("text") or ""
    meta_text = f"{probe.get('meta_title', '')} {probe.get('meta_author', '')}"
    low = text.lower()
    expected = item.expected_pages()
    if expected and expected >= 4 and pages < max(2, expected // 2):
        return Check(False, f"only {pages} of about {expected} pages (preview or cover page)")
    if pages <= 2 and any(m in low for m in _PAYWALL_MARKERS):
        return Check(False, "a paywall or preview page")
    if len(text.strip()) < 200:
        # A scan without a text layer cannot be checked. Accept it only when
        # the address came from the item's own identifier.
        if cand.by_identifier:
            return Check(True, "scan without text; accepted because it was found by DOI/ISBN", cand.version)
        return Check(False, "no text to check it against the item (scan)")
    share = max(title_in_text(item.title, text), title_in_text(item.title, meta_text))
    need = 0.6 if item.item_type in BOOK_TYPES else 0.75
    if share < need:
        return Check(False, f"title not found on its first pages ({share:.0%} of the words)")
    if item.first_author:
        author = _fold(item.first_author)
        hay = _haystack(text + " " + meta_text)
        if author and f" {author.split()[-1]} " not in hay:
            if not cand.by_identifier or item.item_type not in BOOK_TYPES:
                return Check(False, f"first author ({item.first_author}) not found on its first pages")
    return Check(True, "matches the item", detect_version(text, cand.version, item.doi))


# ---------------------------------------------------------------------------
# Writing to Zotero
# ---------------------------------------------------------------------------


class _Ctx:
    """The bits of an MCP context the write helpers call."""

    def __init__(self, log: Callable[[str], None] | None = None):
        self._log = log

    def info(self, msg: str) -> None:
        if self._log:
            self._log(msg)

    def warning(self, msg: str) -> None:
        self.info(msg)

    error = warning
    debug = info


def _safe_filename(item: ItemInfo) -> str:
    who = item.first_author or "Anon"
    title = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", item.title)[:80].strip() or item.key
    name = f"{who} {item.year or 'n.d.'} - {title}.pdf"
    return re.sub(r"\s+", " ", name)


class ZoteroWriter:
    """Attaches files and edits tags through zotero-mcp's write path."""

    def __init__(self, log: Callable[[str], None] | None = None):
        from zotero_mcp.tools import _helpers

        self._helpers = _helpers
        self.ctx = _Ctx(log)
        _read, self.zot, self.mode = _helpers.resolve_write_client(self.ctx, op_description="attaching full texts")

    def attach_pdf(self, item: ItemInfo, path: str, title: str, note: str) -> str | None:
        ok, detail, att_key = self._helpers._attach_and_verify(
            self.zot, title, path, item.key, self.ctx, content_type="application/pdf"
        )
        if not ok:
            raise RuntimeError(detail)
        if att_key and note:
            try:
                att = self.zot.item(att_key)
                att["data"]["note"] = note
                att["data"]["title"] = title
                self.zot.update_item(att)
            except Exception as e:
                self.ctx.info(f"Could not add the source note to {att_key}: {e}")
        return att_key

    def set_tags(self, key: str, add: Iterable[str] = (), remove: Iterable[str] = ()) -> None:
        add, remove = list(add), set(remove)
        try:
            item = self.zot.item(key)
            tags = [t for t in item["data"].get("tags") or [] if t.get("tag") not in remove]
            have = {t.get("tag") for t in tags}
            tags += [{"tag": t} for t in add if t not in have]
            if tags == item["data"].get("tags"):
                return
            item["data"]["tags"] = tags
            self.zot.update_item(item)
        except Exception as e:
            self.ctx.info(f"Could not update the tags of {key}: {e}")


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class Attempt:
    source: str
    url: str
    outcome: str


@dataclass
class ItemResult:
    key: str
    label: str
    status: str                 # attached / found / not found / skipped / error
    source: str = ""
    url: str = ""
    version: str | None = None
    attachment_key: str = ""
    saved_to: str = ""
    reason: str = ""
    attempts: list[Attempt] = field(default_factory=list)


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def _redact(url: str) -> str:
    return re.sub(r"(api_key|apikey)=[^&]+", r"\1=…", url)


def _remember_blocked(http, item: ItemInfo, cand: Candidate) -> None:
    blocked = getattr(http, "blocked", None)
    if isinstance(blocked, dict):
        blocked.setdefault(item.key, []).append(cand)


def _try_key(cand: Candidate) -> str:
    """A link tried plainly may still be worth one try in the browser."""
    return ("browser:" if cand.fetcher is not None else "") + cand.url


def _ask_sources(runnable, item, http, settings, budget, parallel: bool = True) -> dict:
    """Each source's links (or the exception it raised), keyed by name.

    The sources of one step are independent lookups at different services,
    so they are asked at the same time: a paper no service has takes as long
    as the slowest answer, not the sum of ten. Downloads stay one at a time,
    in the sources' order.
    """
    def ask(source):
        try:
            return list(source(item, http, settings, budget))
        except Exception as e:  # reported per source by the caller
            return e

    if not parallel or len(runnable) < 2:
        return {name: ask(source) for source, name in runnable}
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(10, len(runnable))) as pool:
        futures = {name: pool.submit(ask, source) for source, name in runnable}
        return {name: f.result() for name, f in futures.items()}


def find_pdf_for(
    item: ItemInfo,
    http: Http,
    settings: Settings,
    budget: Budget,
    steps: Iterable[str] = DEFAULT_STEPS,
    log: Callable[[str], None] = lambda m: None,
    workdir: str | None = None,
) -> tuple[str | None, Candidate | None, Check | None, list[Attempt]]:
    """Walk the steps until a PDF passes the checks. Returns its local path."""
    attempts: list[Attempt] = []
    tried: set[str] = set()
    workdir = workdir or tempfile.mkdtemp(prefix="zmcp-fulltext-")
    for step in steps:
        n = 0  # the cap is per step, so a long open-access list never starves Scholar
        runnable = []
        for source in SOURCES.get(step, []):
            name = SOURCE_NAMES.get(source.__name__, source.__name__.removeprefix("src_"))
            why = source_status(source, settings, budget)
            if why:
                log(f"  [{step}] {name}: {why}")
            else:
                runnable.append((source, name))
        found = _ask_sources(runnable, item, http, settings, budget, parallel=(step != "browser"))
        for source, name in runnable:
            result = found[name]
            if isinstance(result, Exception):
                attempts.append(Attempt(name, "", f"source failed ({type(result).__name__})"))
                log(f"  [{step}] {name}: failed ({type(result).__name__})")
                continue
            candidates = [c for c in result if _try_key(c) not in tried]
            if not candidates:
                log(f"  [{step}] {name}: no copy")
                continue
            log(f"  [{step}] {name}: {len(candidates)} link(s)")
            for cand in candidates:
                if _try_key(cand) in tried:
                    continue
                if n >= settings.max_candidates and cand.fetcher is None:
                    log(f"  [{step}] stopped after {n} links (max_candidates)")
                    break
                tried.add(_try_key(cand))
                n += 1
                via = " via ZenRows" if cand.unblock and settings.has("zenrows") else ""
                log(f"      {_host_of(cand.url)}{via} ({cand.source}): {_redact(cand.url)[:100]}")
                path, check, outcome = _try_candidate(item, cand, http, settings, budget, workdir)
                attempts.append(Attempt(cand.source, _redact(cand.url), outcome))
                if path and check and check.ok:
                    log(f"        accepted: {check.reason} ({VERSION_LABELS.get(check.version, 'version unknown')})")
                    return path, cand, check, attempts
                log(f"        rejected: {outcome}")
    return None, None, None, attempts


#: The article id: the first all-digit path part after /articles/, whether
#: the link is to the page or to one of its files (…/9544121/files/17174…).
_FIGSHARE_RE = re.compile(r"/articles/(?:[^/]+/)*?(\d{6,})(?:/|$)")


def _figshare_pdf(url: str, http: Http) -> str | None:
    """The file behind a figshare page (figshare.com and the many university
    repositories built on it), whose landing pages refuse plain requests."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    m = _FIGSHARE_RE.search(parsed.path)
    if not m or not ("figshare" in host or "repository" in host):
        return None
    status, data = http.api_json(f"https://api.figshare.com/v2/articles/{m.group(1)}")
    if status != 200 or not isinstance(data, dict):
        return None
    for f in data.get("files") or []:
        if (f.get("mimetype") or "").endswith("pdf") or (f.get("name") or "").lower().endswith(".pdf"):
            return f.get("download_url")
    return None


def _try_candidate(item, cand, http, settings, budget, workdir) -> tuple[str | None, Check | None, str]:
    fig = _figshare_pdf(cand.url, http)
    if fig:
        cand = Candidate(fig, cand.source + ", file via the figshare API", cand.version,
                         cand.by_identifier, False, cand.url)
    if cand.fetcher is not None:
        got = cand.fetcher()
        if got is None:
            return None, None, getattr(cand.fetcher, "reason", "") or "the browser got no PDF"
    else:
        got = http.fetch_unblocked(cand.url, budget) if cand.unblock else None
        if got is None:
            if _needs_unblock(cand.url, settings):
                # ResearchGate and Academia.edu refuse plain requests, and
                # repeated ones get the whole network flagged. Leave them to
                # the browser step (or the unblocker, when there is a key).
                _remember_blocked(http, item, cand)
                return None, None, "left for the browser step (site refuses plain downloads)"
            got = http.fetch(cand.url, referer=cand.referer)
    if got.error:
        return None, None, got.error
    if not got.is_pdf and cand.fetcher is None and got.status in (202, 401, 403, 429):
        # Worth another try in the browser step, with the user's logins.
        _remember_blocked(http, item, cand)
    low = got.body[:20000].decode("utf-8", errors="ignore").lower()
    if not got.is_pdf:
        if any(m in low for m in _CAPTCHA_MARKERS):
            if cand.fetcher is None:
                _remember_blocked(http, item, cand)
            return None, None, "captcha or bot check (not solved)"
        if got.status in (401, 403):
            if not cand.unblock and _needs_unblock(cand.url, settings) and settings.has("zenrows"):
                retry = http.fetch_unblocked(cand.url, budget)
                if retry is not None and retry.is_pdf:
                    got = retry
                else:
                    return None, None, f"blocked ({got.status})"
            else:
                return None, None, f"no access ({got.status})"
        elif got.status != 200:
            return None, None, f"HTTP {got.status}"
    if not got.is_pdf:
        # A landing page: follow its citation_pdf_url once.
        pdf = _pdf_link_in_page(got)
        if pdf and pdf != cand.url:
            inner = http.fetch_unblocked(pdf, budget) if cand.unblock else None
            if inner is None:
                inner = http.fetch(pdf, referer=got.url)
            if inner.is_pdf:
                got = inner
            else:
                return None, None, "page links a PDF that did not download"
        else:
            return None, None, "a web page, not a PDF"
    path = os.path.join(workdir, f"{item.key}-{abs(hash(cand.url)) % 10**8}.pdf")
    with open(path, "wb") as f:
        f.write(got.body)
    check = check_pdf(item, probe_pdf(path), cand, len(got.body))
    if not check.ok:
        return None, check, check.reason
    return path, check, "ok"


def _load_state() -> dict:
    try:
        return json.loads((state_dir() / "state.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        (state_dir() / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError:
        pass


def _append_log(result: ItemResult) -> None:
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        with open(state_dir() / "log.jsonl", "a", encoding="utf-8") as f:
            rec = asdict(result)
            rec["time"] = _dt.datetime.now().isoformat(timespec="seconds")
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _has_file(children: list[dict]) -> bool:
    for child in children or []:
        d = child.get("data", child)
        if d.get("itemType") != "attachment" or d.get("deleted"):
            continue
        if d.get("linkMode") == "linked_url":
            continue
        ctype = (d.get("contentType") or "").lower()
        name = (d.get("filename") or d.get("path") or "").lower()
        if "pdf" in ctype or "epub" in ctype or name.endswith((".pdf", ".epub")):
            return True
    return False


def select_items(
    *, keys: list[str] | None = None, collection: str | None = None, limit: int | None = None,
    retry: bool = False, backend=None, settings: Settings | None = None,
) -> tuple[list[ItemInfo], list[ItemResult]]:
    """Items without a PDF or EPUB, as ItemInfo, plus those skipped and why."""
    from zotero_mcp import library as _library

    backend = backend or _library.get_library_backend()
    if keys:
        found = backend.get_items(keys)
        items = [found[k] for k in keys if k in found]
        missing = [k for k in keys if k not in found]
    elif collection:
        items = [i for i in (backend.collection_items(collection) or []) if i.get("data", {}).get("itemType") not in ("attachment", "note", "annotation")]
        missing = []
    else:
        items = backend.list_items("-attachment", limit=100000)
        missing = []
    skipped = [ItemResult(k, k, "skipped", reason="no such item") for k in missing]
    item_keys = [i.get("key") or i.get("data", {}).get("key") for i in items]
    children = backend.get_children([k for k in item_keys if k]) if item_keys else {}
    state = _load_state()
    cutoff = _dt.datetime.now() - _dt.timedelta(days=(settings or Settings.load()).retry_days)
    chosen: list[ItemInfo] = []
    for raw in items:
        info = ItemInfo.from_zotero(raw)
        data = raw.get("data", raw)
        if info.item_type not in FETCHABLE_TYPES:
            if keys:
                skipped.append(ItemResult(info.key, info.label, "skipped", reason=f"item type {info.item_type}"))
            continue
        if _has_file(children.get(info.key, [])):
            if keys:
                skipped.append(ItemResult(info.key, info.label, "skipped", reason="already has a PDF or EPUB"))
            continue
        if not info.title:
            skipped.append(ItemResult(info.key, info.label, "skipped", reason="no title"))
            continue
        if not keys and not retry:
            tags = {t.get("tag") for t in data.get("tags") or []}
            last = (state.get(info.key) or {}).get("last_attempt")
            if TAG_NOT_FOUND in tags and last:
                try:
                    if _dt.datetime.fromisoformat(last) > cutoff:
                        continue
                except ValueError:
                    pass
        chosen.append(info)
    if limit:
        chosen = chosen[:limit]
    return chosen, skipped


@dataclass
class RunReport:
    results: list[ItemResult]
    dry_run: bool
    started: str
    report_path: str = ""

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out

    def markdown(self, limit: int | None = None) -> str:
        c = self.counts()
        head = "Dry run, nothing attached. " if self.dry_run else ""
        lines = [
            f"# Full-text fetch ({self.started})",
            "",
            head + ", ".join(f"{v} {k}" for k, v in sorted(c.items())) if c else "No items to fetch.",
            "",
        ]
        order = {"attached": 0, "found": 1, "not found": 2, "error": 3, "skipped": 4}
        rows = sorted(self.results, key=lambda r: order.get(r.status, 9))
        for r in rows[:limit] if limit else rows:
            if r.status in ("attached", "found"):
                where = f" — saved to {r.saved_to}" if r.saved_to else ""
                host = _host_of(r.url) if r.url else ""
                lines.append(f"- **{r.status}** {r.label} [{r.key}]: {r.source}"
                             f"{' at ' + host if host else ''}, {VERSION_LABELS.get(r.version, r.version)}{where}")
            else:
                lines.append(f"- **{r.status}** {r.label} [{r.key}]: {r.reason}")
                for a in r.attempts[-6:]:
                    lines.append(f"    - {a.source}: {a.outcome}")
        if limit and len(rows) > limit:
            lines.append(f"- … {len(rows) - limit} more in the report file")
        if self.report_path:
            lines += ["", f"Full report: {self.report_path}"]
        return "\n".join(lines)


def _not_found_reason(attempts: list[Attempt]) -> str:
    if not attempts:
        return "no source had a copy"
    outcomes = [a.outcome for a in attempts]
    for key, text in (
        ("captcha", "a captcha blocked the only copies found"),
        ("no access", "copies found, but behind access control"),
        ("blocked", "copies found, but the sites blocked the download"),
        ("title not found", "copies found, but none matched the item"),
    ):
        if any(key in o for o in outcomes):
            return text
    return "copies found, but none downloaded as a matching PDF"


def run(
    *,
    keys: list[str] | None = None,
    collection: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    save_dir: str | None = None,
    steps: Iterable[str] = DEFAULT_STEPS,
    retry: bool = False,
    log: Callable[[str], None] = print,
    settings: Settings | None = None,
    http: Http | None = None,
    writer_factory: Callable[[], Any] | None = None,
    backend=None,
) -> RunReport:
    """Fetch full texts for the selected items. See the module docstring."""
    settings = settings or Settings.load()
    budget = Budget()
    http = http or Http(settings)
    http.log = log
    steps = [s for s in steps if s in STEPS]
    started = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    items, results = select_items(
        keys=keys, collection=collection, limit=limit, retry=retry, backend=backend, settings=settings
    )
    log("Steps and services:")
    for line in describe_setup(settings, budget, steps):
        log(line)
    log(f"{len(items)} item(s) to fetch{' (dry run)' if dry_run else ''}.")
    writer = None
    if not dry_run and not save_dir and items:
        writer = (writer_factory or (lambda: ZoteroWriter(log=None)))()
    state = _load_state()
    with tempfile.TemporaryDirectory(prefix="zmcp-fulltext-") as workdir:
        for i, item in enumerate(items, 1):
            ident = f"DOI {item.doi}" if item.doi else (f"ISBN {item.isbn}" if item.isbn else "no DOI")
            log(f"[{i}/{len(items)}] {item.label} [{item.key}, {ident}]")
            res = ItemResult(item.key, item.label, "not found")
            try:
                path, cand, check, attempts = find_pdf_for(item, http, settings, budget, steps, log, workdir)
                res.attempts = attempts
                if path and cand and check:
                    res.source, res.url, res.version = cand.source, _redact(cand.url), check.version
                    label = VERSION_LABELS.get(check.version, "version unknown")
                    title = f"Full Text PDF ({label})"
                    if save_dir:
                        os.makedirs(save_dir, exist_ok=True)
                        dest = os.path.join(save_dir, f"{item.key} - {_safe_filename(item)}")
                        with open(path, "rb") as src, open(dest, "wb") as out:
                            out.write(src.read())
                        res.status, res.saved_to = "found", dest
                    elif dry_run:
                        res.status = "found"
                    else:
                        named = os.path.join(workdir, _safe_filename(item))
                        os.replace(path, named)
                        note = (
                            f"<p>Fetched by zotero-mcp fetch-fulltext on {_dt.date.today().isoformat()} "
                            f"from {cand.source}: {_redact(cand.url)}. Version: {label}. "
                            f"Check: {check.reason}.</p>"
                        )
                        res.attachment_key = writer.attach_pdf(item, named, title, note) or ""
                        tags = [TAG_FETCHED] + ([VERSION_TAGS[check.version]] if check.version in VERSION_TAGS else [])
                        writer.set_tags(item.key, add=tags, remove=[TAG_NOT_FOUND])
                        res.status = "attached"
                    log(f"  -> {res.status}: {cand.source} at {_host_of(cand.url)}, {label}"
                        + (f" -> {res.saved_to}" if res.saved_to else ""))
                else:
                    res.reason = _not_found_reason(attempts)
                    if writer:
                        writer.set_tags(item.key, add=[TAG_NOT_FOUND])
                    log(f"  -> not found: {res.reason}")
            except Exception as e:
                res.status, res.reason = "error", f"{type(e).__name__}: {e}"
                log(f"  -> error: {res.reason}")
            if not dry_run:
                state[item.key] = {"last_attempt": _dt.datetime.now().isoformat(timespec="seconds"), "status": res.status}
                _save_state(state)
                _append_log(res)
            results.append(res)
    if getattr(http, "browser", None) is not None:
        try:
            http.browser.close()
        except Exception:
            pass
        http.browser = None
    report = RunReport(results, dry_run, started)
    try:
        runs = state_dir() / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        path = runs / f"{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        path.write_text(report.markdown(), encoding="utf-8")
        report.report_path = str(path)
    except OSError:
        pass
    return report


def src_browser(item: ItemInfo, http: Http, settings: Settings, budget: Budget) -> Iterator[Candidate]:
    """The browser step; lives in ``fulltext_browser``, imported when used."""
    from zotero_mcp.fulltext_browser import src_browser as _src

    yield from _src(item, http, settings, budget)


SOURCES["browser"] = [src_browser]


def scholar_search_url(item: ItemInfo) -> str:
    return "https://scholar.google.com/scholar?q=" + quote(f'"{item.title}" {item.first_author}'.strip())


# ---------------------------------------------------------------------------
# Background runs (for the MCP tool, whose calls must answer within a minute)
# ---------------------------------------------------------------------------


def _run_paths(run_id: str) -> dict[str, Path]:
    base = state_dir() / "runs" / f"bg-{run_id}"
    return {"log": base.with_suffix(".log"), "report": base.with_suffix(".md"), "done": base.with_suffix(".done")}


def start_background_run(options: dict) -> str:
    """Start a fetch in its own process; returns the run id."""
    run_id = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + f"{os.getpid() % 1000:03d}"
    paths = _run_paths(run_id)
    paths["log"].parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "zotero_mcp.fulltext_fetch", "bg-run", run_id, json.dumps(options)],
                     **kwargs)
    return run_id


def background_status(run_id: str, tail: int = 25) -> tuple[bool, str]:
    """(finished, text): the report when finished, else the latest log lines."""
    paths = _run_paths(run_id)
    if paths["done"].exists():
        try:
            return True, paths["report"].read_text(encoding="utf-8")
        except OSError:
            return True, paths["done"].read_text(encoding="utf-8")
    try:
        lines = paths["log"].read_text(encoding="utf-8").splitlines()
    except OSError:
        return False, "starting ..."
    return False, "\n".join(lines[-tail:])


def _bg_main(run_id: str, raw: str) -> int:
    opts = json.loads(raw)
    paths = _run_paths(run_id)
    with open(paths["log"], "a", encoding="utf-8", buffering=1) as log_file:
        def log(msg: str) -> None:
            log_file.write(msg + "\n")

        try:
            from zotero_mcp.cli import setup_zotero_environment

            setup_zotero_environment()
            report = run(keys=opts.get("keys"), collection=opts.get("collection"), limit=opts.get("limit"),
                         dry_run=bool(opts.get("dry_run")), steps=opts.get("steps") or DEFAULT_STEPS, log=log)
            paths["report"].write_text(report.markdown(limit=40), encoding="utf-8")
            paths["done"].write_text("ok", encoding="utf-8")
            return 0
        except Exception as e:
            log(f"Error: {type(e).__name__}: {e}")
            paths["report"].write_text(f"Error fetching full texts: {e}", encoding="utf-8")
            paths["done"].write_text("error", encoding="utf-8")
            return 1


if __name__ == "__main__":  # pragma: no cover - child processes
    if len(sys.argv) >= 4 and sys.argv[1] == "bg-run":
        sys.exit(_bg_main(sys.argv[2], sys.argv[3]))
    if len(sys.argv) >= 3 and sys.argv[1] == "probe":
        try:
            sys.exit(_probe_main(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 4))
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            sys.exit(1)
    sys.exit(2)

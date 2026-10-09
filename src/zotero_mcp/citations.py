"""A citation graph of the library, from OpenAlex, kept on disk.

Each paper's reference list (OpenAlex's ``referenced_works``) is fetched once and kept in
``~/.config/zotero-mcp/citations.json``; papers added later are added by the maintenance run
(the import action, Check & complete). Questions about the graph are then answered from that
file: what a paper builds on, which papers in the library cite it, which papers share its
references, and which works a collection cites often that are not in the library.

Costs: papers are looked up 50 DOIs per request (a list call, $0.10 per 1,000 with a free
OpenAlex key, which has $1 a day); single works by ID or DOI are free. Papers without a DOI
are looked up by title (a search, $1 per 1,000) and only accepted when title, year and first
author match. Papers OpenAlex does not know are tried again after 90 days.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import quote

API = "https://api.openalex.org/works"
SELECT = "id,doi,title,publication_year,cited_by_count,referenced_works,authorships"
WORK_SELECT = "id,doi,title,publication_year,cited_by_count,authorships"
BATCH = 50
#: A paper OpenAlex did not know is looked up again after this many days.
MISSING_RETRY_DAYS = 90
#: A stored reference list is fetched again after this many days (OpenAlex adds references).
REFRESH_DAYS = 365
_SKIP_TYPES = {"attachment", "note", "annotation"}


def _path() -> Path:
    from zotero_mcp.fulltext_fetch import shared_dir

    return shared_dir() / "citations.json"


def _local(name: str) -> Path:
    """Lock files stay on this computer: a synced lock would hold up the other one."""
    from zotero_mcp.fulltext_fetch import config_dir

    return config_dir() / name


def _empty() -> dict:
    return {"papers": {}, "works": {}}


def load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("papers", {})
    data.setdefault("works", {})
    return data


@contextlib.contextmanager
def _locked(name: str = "citations.lock", wait: float = 20.0) -> Iterator[bool]:
    """A lock file beside the store, so the import action and Claude do not write at once.
    A lock older than two minutes is left over from a crash and taken over."""
    lock = _local(name)
    lock.parent.mkdir(parents=True, exist_ok=True)
    end = time.monotonic() + wait
    fd = None
    while fd is None:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 120:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() > end:
                break
            time.sleep(0.2)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)
            with contextlib.suppress(OSError):
                lock.unlink()


def save(store: dict) -> None:
    """Merge into what is on disk (another process may have added papers meanwhile; the newer
    entry wins) and replace the file in one step."""
    with _locked():
        disk = load()
        for key, entry in store["papers"].items():
            old = disk["papers"].get(key)
            if old is None or str(entry.get("fetched", "")) >= str(old.get("fetched", "")):
                disk["papers"][key] = entry
        for wid, info in store["works"].items():
            disk["works"][wid] = {**disk["works"].get(wid, {}), **info}
        path = _path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(disk, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
        store["papers"], store["works"] = disk["papers"], disk["works"]


def _short(wid: str | None) -> str:
    return str(wid or "").rsplit("/", 1)[-1]


def norm_doi(doi: str | None) -> str:
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", str(doi or "").strip(), flags=re.I)
    return doi.lower()


def _today() -> str:
    return _dt.date.today().isoformat()


def _age(entry: dict) -> int:
    try:
        return (_dt.date.today() - _dt.date.fromisoformat(entry.get("fetched", ""))).days
    except ValueError:
        return 10**6


def _due(entry: dict | None, doi: str) -> bool:
    if not entry:
        return True
    if entry.get("doi", "") != doi:                 # a DOI added or corrected since
        return True
    return _age(entry) >= (MISSING_RETRY_DAYS if entry.get("missing") else REFRESH_DAYS)


def _first_author(work: dict) -> str:
    for a in work.get("authorships") or []:
        name = ((a.get("author") or {}).get("display_name") or "").strip()
        if name:
            return name.split()[-1]
    return ""


def _work_info(work: dict) -> dict:
    return {"title": re.sub(r"<[^>]+>", "", work.get("title") or "").strip(),
            "year": work.get("publication_year") or "", "author": _first_author(work),
            "doi": norm_doi(work.get("doi")), "cited_by": work.get("cited_by_count") or 0}


def _entry(work: dict, doi: str, by: str = "doi") -> dict:
    return {"doi": doi, "id": _short(work.get("id")), "by": by,
            "refs": [_short(r) for r in work.get("referenced_works") or []],
            "cited_by": work.get("cited_by_count") or 0, "fetched": _today()}


# ---------------------------------------------------------------------------
# Library items
# ---------------------------------------------------------------------------


def _get_backend(backend):
    if backend is None:
        from zotero_mcp import library

        backend = library.get_library_backend()
    return backend


def library_items(backend, keys: list[str] | None = None, collection: str | None = None) -> dict[str, dict]:
    """Regular items (no notes, attachments or trashed items) by key."""
    if keys:
        raws = list((backend.get_items(list(keys)) or {}).values())
    elif collection:
        raws = backend.collection_items(collection, include_subcollections=True) or []
    else:
        raws = backend.list_items("-attachment", limit=100000) or []
    out = {}
    for raw in raws:
        data = raw.get("data", raw)
        key = raw.get("key") or data.get("key")
        if key and data.get("itemType") not in _SKIP_TYPES and not data.get("deleted"):
            out[key] = raw
    return out


def _settings_http(settings, http):
    from zotero_mcp import fulltext_fetch as ff

    settings = settings or ff.Settings.load()
    return settings, http or ff.Http(settings)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def update(keys: list[str] | None = None, *, collection: str | None = None, backend=None, http=None,
           settings=None, refresh: bool = False, log: Callable[[str], None] = print,
           sleep: Callable[[float], None] = time.sleep, deadline: float | None = None,
           items: dict[str, dict] | None = None) -> dict:
    """Add the papers that are not in the graph yet (or whose DOI changed, or that OpenAlex did
    not know 90 days ago). ``keys``/``collection`` narrow it; by default the whole library.
    ``deadline`` (time.monotonic()) stops early; what is left is counted in "left"."""
    from zotero_mcp import fulltext_fetch as ff

    backend = _get_backend(backend)
    items = items if items is not None else library_items(backend, keys, collection)
    store = load()
    papers = store["papers"]
    by_doi: dict[str, list[str]] = {}
    by_title: list[tuple[str, ff.ItemInfo]] = []
    for key, raw in items.items():
        info = ff.ItemInfo.from_zotero(raw)
        doi = norm_doi(info.doi)
        if not refresh and not _due(papers.get(key), doi):
            continue
        if doi:
            by_doi.setdefault(doi, []).append(key)
        elif info.title and info.year:
            by_title.append((key, info))
    totals = {"added": 0, "not_found": 0, "left": 0, "stopped": ""}
    if not by_doi and not by_title:
        return totals
    settings, http = _settings_http(settings, http)

    def late() -> bool:
        return deadline is not None and time.monotonic() > deadline

    def put(keys_: list[str], entry: dict, work: dict | None) -> None:
        for k in keys_:
            papers[k] = dict(entry)
        if work is not None and entry.get("id"):
            store["works"][entry["id"]] = {**_work_info(work), "key": keys_[0]}
        totals["added" if work is not None else "not_found"] += len(keys_)

    dois = list(by_doi)
    done = 0
    for i in range(0, len(dois), BATCH):
        if late() or totals["stopped"]:
            break
        chunk = dois[i:i + BATCH]
        plain = [d for d in chunk if not re.search(r"[,|]", d)]
        found: dict[str, dict] = {}
        if plain:
            status, data = http.api_json(API, params=ff._openalex_params(settings, {
                "filter": "doi:" + "|".join(f"https://doi.org/{d}" for d in plain),
                "select": SELECT, "per-page": "100"}))
            if status != 200 or data is None:
                totals["stopped"] = _refusal(status)
                break
            for work in data.get("results") or []:
                found[norm_doi(work.get("doi"))] = work
            sleep(0.15)
        for doi in chunk:
            work = found.get(doi)
            if work is None:
                # Not matched in the list (an unusual DOI, or OpenAlex has it under another
                # record): a single lookup by DOI is free.
                status, work = http.api_json(f"{API}/doi:{quote(doi, safe='/')}", params=ff._openalex_params(settings, {"select": SELECT}))
                sleep(0.1)
                if status not in (200, 404):
                    totals["stopped"] = _refusal(status)
                    break
                work = work if status == 200 and work else None
            put(by_doi[doi], _entry(work, doi) if work else {"doi": doi, "missing": True, "fetched": _today()}, work)
            done += 1
        if (i // BATCH) % 10 == 9:
            save(store)
    totals["left"] += sum(len(by_doi[d]) for d in dois[done:])

    for j, (key, info) in enumerate(by_title):
        if late() or totals["stopped"]:
            totals["left"] += len(by_title) - j
            break
        work = ff._openalex_work(info, http, settings)
        sleep(0.15)
        if work and _same_first_author(info, work):
            put([key], _entry(work, "", by="title"), work)
        else:
            put([key], {"doi": "", "missing": True, "fetched": _today()}, None)
    save(store)
    if totals["added"] or totals["not_found"]:
        log(f"Citation graph: {totals['added']} paper(s) added"
            + (f", {totals['not_found']} not in OpenAlex" if totals["not_found"] else "") + ".")
    if totals["stopped"]:
        log(f"Citation graph: stopped, {totals['stopped']}. The rest follows at the next run.")
    return totals


def _refusal(status: int) -> str:
    if status == 429:
        return "OpenAlex's daily allowance is used up (a free key in keys.env, OPENALEX_API_KEY, raises it)"
    return "OpenAlex did not answer" if not status else f"OpenAlex answered {status}"


def _same_first_author(info, work: dict) -> bool:
    from zotero_mcp.fulltext_fetch import _fold

    if not info.authors:
        return True
    theirs = _fold(_first_author(work))
    return bool(theirs) and any(_fold(a) and (_fold(a) in theirs or theirs in _fold(a)) for a in info.authors[:3])


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


class Graph:
    """The stored graph, with the library's items for names and for recognising works that
    OpenAlex lists twice (a preprint and the article, a book without DOI)."""

    def __init__(self, store: dict, items: dict[str, dict], http=None, settings=None):
        from zotero_mcp import fulltext_fetch as ff

        self.store, self.items = store, items
        self.http, self.settings = http, settings
        self.info = {k: ff.ItemInfo.from_zotero(raw) for k, raw in items.items()}
        self.papers = {k: e for k, e in store["papers"].items() if k in items}
        self.key_of: dict[str, str] = {}
        for key, entry in self.papers.items():
            if entry.get("id"):
                self.key_of.setdefault(entry["id"], key)
        for wid, w in store["works"].items():
            if w.get("key") in items:
                self.key_of.setdefault(wid, w["key"])
        self._titles: list[tuple[str, str, str, str]] | None = None
        self.fetched_works = False

    # -- names ------------------------------------------------------------
    def label(self, key: str) -> str:
        info = self.info.get(key)
        return f"{info.label} [{key}]" if info else key

    def work_label(self, wid: str) -> str:
        w = self.store["works"].get(wid) or {}
        who = w.get("author") or "Anon."
        text = f"{who} ({w.get('year') or 'n.d.'}) {w.get('title') or wid}"
        return text + (f" — doi:{w['doi']}" if w.get("doi") else f" — openalex:{wid}")

    def describe(self, wids: list[str]) -> None:
        """Fetch titles of works not described yet (single lookups by ID are free) and note
        the ones that are in the library after all."""
        todo = [w for w in wids if not self.store["works"].get(w, {}).get("title")]
        if not todo:
            return
        if self.http is None:
            self.settings, self.http = _settings_http(self.settings, self.http)
        from zotero_mcp import fulltext_fetch as ff

        for wid in todo:
            status, work = self.http.api_json(f"{API}/{wid}", params=ff._openalex_params(self.settings,
                                                                                           {"select": WORK_SELECT}))
            time.sleep(0.1)
            if status == 200 and work:
                self.store["works"][wid] = {**self.store["works"].get(wid, {}), **_work_info(work)}
                self.fetched_works = True
                key = self._in_library(self.store["works"][wid])
                if key:
                    self.store["works"][wid]["key"] = key
                    self.key_of.setdefault(wid, key)
            elif status not in (404,):
                break

    def _in_library(self, w: dict) -> str | None:
        from zotero_mcp.fulltext_fetch import title_similarity

        doi = w.get("doi")
        if self._titles is None:
            self._titles = [(k, i.title, i.year, norm_doi(i.doi)) for k, i in self.info.items()]
        for key, title, year, own_doi in self._titles:
            if doi and own_doi == doi:
                return key
            if (title and w.get("title") and title_similarity(title, w["title"]) >= 0.9
                    and (not year or not w.get("year") or abs(int(year) - int(w["year"])) <= 1)):
                return key
        return None

    def save(self) -> None:
        if self.fetched_works:
            save(self.store)

    # -- questions --------------------------------------------------------
    def refs(self, key: str) -> list[str]:
        return list((self.papers.get(key) or {}).get("refs") or [])

    def ids_of(self, key: str) -> set[str]:
        return {w for w, k in self.key_of.items() if k == key}

    def citing(self, key: str) -> list[str]:
        ids = self.ids_of(key)
        return [k for k in self.papers if k != key and ids.intersection(self.refs(k))] if ids else []

    def related(self, key: str) -> list[tuple[str, int]]:
        mine = set(self.refs(key))
        if not mine:
            return []
        shared = [(k, len(mine.intersection(self.refs(k)))) for k in self.papers if k != key]
        return sorted([s for s in shared if s[1] >= 2], key=lambda s: -s[1])


def _graph(backend=None, http=None, settings=None) -> Graph:
    backend = _get_backend(backend)
    return Graph(load(), library_items(backend), http=http, settings=settings)


def _ensure(graph: Graph, keys: list[str], backend, http, settings, seconds: float) -> str:
    """Add the papers the question needs, as far as time allows; the rest in the background."""
    missing = [k for k in keys if k in graph.items and _due(graph.store["papers"].get(k),
                                                            norm_doi(graph.info[k].doi))]
    if not missing:
        return ""
    if _background_running():
        return (f"\n\n({len(missing)} paper(s) are not in the citation graph yet; they are being added in the "
                "background, ask again in a few minutes.)")
    totals = update(backend=backend, http=http, settings=settings, log=lambda m: None,
                    deadline=time.monotonic() + seconds, items={k: graph.items[k] for k in missing})
    fresh = Graph(load(), graph.items, http=graph.http, settings=graph.settings)
    graph.__dict__.update(fresh.__dict__)
    if totals["stopped"]:
        return f"\n\n(Not complete: {totals['stopped']}.)"
    if totals["left"]:
        started = start_background()
        return (f"\n\n({totals['left']} paper(s) are not in the citation graph yet"
                + ("; they are being added in the background, ask again in a few minutes." if started else ".") + ")")
    return ""


def resolve(target: str, graph: Graph) -> tuple[str | None, str | None]:
    """A library key, a DOI or an OpenAlex ID -> (library key or None, OpenAlex ID or None)."""
    t = str(target or "").strip()
    if t in graph.items:
        entry = graph.papers.get(t) or {}
        return t, entry.get("id")
    m = re.match(r"^(?:https?://openalex\.org/)?(W\d+)$", t, re.I)
    if m:
        wid = m.group(1).upper()
        return graph.key_of.get(wid), wid
    doi = norm_doi(t)
    if doi.startswith("10."):
        for key, info in graph.info.items():
            if norm_doi(info.doi) == doi:
                return key, (graph.papers.get(key) or {}).get("id")
        for wid, w in graph.store["works"].items():
            if w.get("doi") == doi:
                return graph.key_of.get(wid), wid
        if graph.http is None:
            graph.settings, graph.http = _settings_http(graph.settings, graph.http)
        from zotero_mcp import fulltext_fetch as ff

        status, work = graph.http.api_json(f"{API}/doi:{quote(doi, safe='/')}", params=ff._openalex_params(graph.settings,
                                                                                            {"select": WORK_SELECT}))
        if status == 200 and work:
            wid = _short(work.get("id"))
            graph.store["works"][wid] = _work_info(work)
            graph.fetched_works = True
            return None, wid
    return None, None


# ---------------------------------------------------------------------------
# Answers (plain text, for Claude and the command line)
# ---------------------------------------------------------------------------


def references(key: str, *, backend=None, http=None, settings=None, limit: int = 25, seconds: float = 25) -> str:
    backend = _get_backend(backend)
    g = _graph(backend, http, settings)
    if key not in g.items:
        return f"No item {key} in the library."
    note = _ensure(g, [key], backend, http, settings, seconds)
    entry = g.papers.get(key) or {}
    if entry.get("missing") or not entry:
        return f"{g.label(key)}: not in OpenAlex, so its references are unknown.{note}"
    refs = g.refs(key)
    if not refs:
        return f"{g.label(key)}: OpenAlex lists no references for it.{note}"
    outside = [w for w in refs if w not in g.key_of]
    g.describe(outside[:limit])
    inside = sorted({g.key_of[w] for w in refs if w in g.key_of})
    outside = [w for w in refs if w not in g.key_of]
    lines = [f"{g.label(key)} cites {len(refs)} work(s) (OpenAlex); {len(inside)} in your library."]
    if inside:
        lines += ["", "In your library:"] + [f"- {g.label(k)}" for k in inside]
    if outside:
        lines += ["", f"Not in your library ({len(outside)}"
                      + (f", first {limit}" if len(outside) > limit else "") + "):"]
        lines += [f"- {g.work_label(w)}" for w in outside[:limit]]
    g.save()
    return "\n".join(lines) + note


def cited_by(target: str, *, backend=None, http=None, settings=None, limit: int = 10, seconds: float = 25) -> str:
    """Which papers in the library cite ``target``, and (from OpenAlex) the most-cited works outside."""
    from zotero_mcp import fulltext_fetch as ff

    backend = _get_backend(backend)
    g = _graph(backend, http, settings)
    note = _ensure(g, list(g.items), backend, http, settings, seconds)
    key, wid = resolve(target, g)
    if not wid:
        what = g.label(key) if key else target
        return f"{what}: not found in OpenAlex, so its citations are unknown.{note}"
    citing = g.citing(key) if key else [k for k in g.papers if wid in g.refs(k)]
    name = g.label(key) if key else g.work_label(wid)
    total = (g.papers.get(key) or {}).get("cited_by") if key else (g.store["works"].get(wid) or {}).get("cited_by")
    lines = [f"{name}: cited by {len(citing)} paper(s) in your library"
             + (f" ({total} in all, OpenAlex)." if total else ".")]
    lines += [f"- {g.label(k)}" for k in sorted(citing, key=lambda k: g.info[k].year or "0")]
    if limit and total:
        g.settings, g.http = _settings_http(g.settings, g.http)
        status, data = g.http.api_json(API, params=ff._openalex_params(g.settings, {
            "filter": f"cites:{wid}", "sort": "cited_by_count:desc", "per-page": str(min(limit + 10, 50)),
            "select": WORK_SELECT}))
        if status == 200 and data:
            outside = []
            for work in data.get("results") or []:
                w = _short(work.get("id"))
                g.store["works"][w] = {**g.store["works"].get(w, {}), **_work_info(work)}
                g.fetched_works = True
                if w not in g.key_of and not g._in_library(g.store["works"][w]):
                    outside.append(w)
            if outside:
                lines += ["", "Most cited of the papers citing it outside your library:"]
                lines += [f"- {g.work_label(w)} (cited {g.store['works'][w].get('cited_by', 0)}x)"
                          for w in outside[:limit]]
    g.save()
    return "\n".join(lines) + note


def related(key: str, *, backend=None, http=None, settings=None, limit: int = 10, seconds: float = 25) -> str:
    """Library papers that share references with ``key`` (bibliographic coupling)."""
    backend = _get_backend(backend)
    g = _graph(backend, http, settings)
    if key not in g.items:
        return f"No item {key} in the library."
    note = _ensure(g, list(g.items), backend, http, settings, seconds)
    pairs = g.related(key)[:limit]
    if not g.refs(key):
        return f"{g.label(key)}: no references known, so nothing to compare.{note}"
    if not pairs:
        return f"No paper in your library shares two or more references with {g.label(key)}.{note}"
    lines = [f"Papers in your library sharing references with {g.label(key)}:"]
    lines += [f"- {g.label(k)}: {n} shared" for k, n in pairs]
    return "\n".join(lines) + note


def overview(*, keys: list[str] | None = None, collection: str | None = None, backend=None, http=None,
             settings=None, limit: int = 15, seconds: float = 25) -> str:
    """For a set of papers (a collection, chosen keys, or the whole library): the papers the set
    cites most among themselves, and the works it cites most that are not in the library."""
    backend = _get_backend(backend)
    g = _graph(backend, http, settings)
    chosen = list(library_items(backend, keys, collection)) if (keys or collection) else list(g.items)
    chosen = [k for k in chosen if k in g.items]
    if not chosen:
        return "No papers to look at."
    note = _ensure(g, chosen, backend, http, settings, seconds)
    known = [k for k in chosen if g.refs(k)]
    inside: Counter = Counter()
    outside: Counter = Counter()
    edges = 0
    chosen_set = set(chosen)
    for k in known:
        for w in set(g.refs(k)):
            target = g.key_of.get(w)
            if target in chosen_set and target != k:
                inside[target] += 1
                edges += 1
            elif target is None:
                outside[w] += 1
    top_out = [w for w, n in outside.most_common(limit * 2) if n >= 2]
    g.describe(top_out)
    outside = Counter({w: n for w, n in outside.items() if w not in g.key_of})
    scope = "these papers" if (keys or collection) else "your library"
    lines = [f"{len(known)} of {len(chosen)} paper(s) have references in OpenAlex; {edges} citation(s) "
             f"between them."]
    if inside:
        lines += ["", f"Most cited within {scope}:"]
        lines += [f"- {g.label(k)}: by {n}" for k, n in inside.most_common(limit)]
    gaps = [(w, n) for w, n in outside.most_common(limit) if n >= 2]
    if gaps:
        lines += ["", "Cited often, not in your library:"]
        lines += [f"- {g.work_label(w)}: by {n}" for w, n in gaps]
    g.save()
    return "\n".join(lines) + note


# ---------------------------------------------------------------------------
# In the background
# ---------------------------------------------------------------------------


def _background_running() -> bool:
    lock = _local("citations-update.lock")
    try:
        return lock.exists() and time.time() - lock.stat().st_mtime < 3600
    except OSError:
        return False


def start_background() -> bool:
    """Add the rest of the library in a separate low-priority process (once at a time)."""
    import subprocess

    if _background_running():
        return True
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                                   | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000))
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, "-m", "zotero_mcp.citations"], **kwargs)
        return True
    except Exception:
        return False


def _main() -> int:
    lock = _local("citations-update.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")
    from zotero_mcp.cli import setup_zotero_environment

    setup_zotero_environment()
    try:
        update(log=lambda m: None)
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(_main())

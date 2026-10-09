"""Find and attach full-text PDFs for items that have none."""

from __future__ import annotations

from zotero_mcp import fulltext_fetch as _ff
from zotero_mcp._app import mcp
from zotero_mcp._context import Context

#: Each item can take up to a minute (several sources, polite pauses between
#: requests to one site), so a tool call handles a few; the CLI does bulk runs.
_MAX_TOOL_ITEMS = 10


def _as_list(value) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    keys = [str(v).strip() for v in value if str(v).strip()]
    return keys or None


#: Tool calls that reach the PC through Cowork must answer within about a
#: minute. The fetch runs in its own process; the call waits this long for it.
_WAIT_SECONDS = 40


@mcp.tool(
    name="zotero_fetch_fulltext",
    description=(
        "Find and attach a PDF for items that have none, the way you would by "
        "hand: open-access copies (Unpaywall, OpenAlex, Semantic Scholar, "
        "Europe PMC, arXiv, OSF, Zenodo, HAL, OAPEN), the publisher's page "
        "(subscriptions work on the university network), Google Scholar's PDF "
        "links and versions, and a web search. Each PDF is checked (title and "
        "first author on its first pages, not a preview) before it is "
        "attached; its version is in the attachment title. Items not found "
        "are tagged fulltext/not-found. Never solves captchas, never uses "
        "shadow libraries. The fetch runs in the background: the call returns "
        "the report if it finishes within ~40 s, otherwise a run_id — then "
        "call zotero_fetch_fulltext_status(run_id) a minute later (one paper "
        "takes 10 s to 2 min). "
        "item_keys: items to fetch (list or comma-separated); omit to take "
        f"items without a PDF from collection_key or the whole library. "
        f"limit: items per run (default 5, max {_MAX_TOOL_ITEMS}). "
        "dry_run: find and check, attach nothing. "
        "steps: subset of open-access, publisher, scholar, web; 'browser' "
        "(opens a visible Chrome window with the user's logins) only when the "
        "user asks for it."
    ),
)
def fetch_fulltext(
    item_keys: list[str] | str | None = None,
    collection_key: str | None = None,
    limit: int | str | None = 5,
    dry_run: bool = False,
    steps: list[str] | str | None = None,
    *,
    ctx: Context,
) -> str:
    import time

    try:
        keys = _as_list(item_keys)
        try:
            n = int(limit) if limit not in (None, "") else 5
        except (TypeError, ValueError):
            n = 5
        n = max(1, min(n, _MAX_TOOL_ITEMS))
        chosen = [s for s in (_as_list(steps) or list(_ff.DEFAULT_STEPS)) if s in _ff.STEPS] or list(_ff.DEFAULT_STEPS)
        run_id = _ff.start_background_run({
            "keys": keys[:_MAX_TOOL_ITEMS] if keys else None,
            "collection": collection_key,
            "limit": None if keys else n,
            "dry_run": bool(dry_run),
            "steps": chosen,
        })
        deadline = time.monotonic() + _WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(2)
            finished, text = _ff.background_status(run_id)
            if finished:
                return text
        _finished, text = _ff.background_status(run_id, tail=8)
        return (
            f"Still running in the background (run_id {run_id}). Call "
            f"zotero_fetch_fulltext_status(run_id='{run_id}') in a minute for the result.\n\n"
            f"Latest progress:\n{text}"
        )
    except Exception as e:
        ctx.error(f"Full-text fetch failed: {e}")
        return f"Error fetching full texts: {e}"


@mcp.tool(
    name="zotero_fetch_fulltext_status",
    description=(
        "Progress or result of a zotero_fetch_fulltext run that was still "
        "running: the report when it has finished, else its latest log lines."
    ),
)
def fetch_fulltext_status(run_id: str, *, ctx: Context) -> str:
    finished, text = _ff.background_status(str(run_id).strip())
    if finished:
        return text
    return f"Still running (run_id {run_id}). Latest progress:\n{text}"


@mcp.tool(
    name="zotero_read_paper",
    description=(
        "Read the FULL TEXT of a paper that is not in the user's Zotero library, without adding "
        "it. Use it whenever an answer leans on a paper found elsewhere (web search, Consensus, "
        "Scite, PubMed, Scholar) of which you have only the abstract or a snippet, or when the "
        "user asks to read a paper they do not want to keep. Same search and checks as zotero_fetch_fulltext "
        "(open access, the publisher via the university network, Google Scholar, a web search; "
        "each PDF checked to be this work), but the result is the paper's text. "
        "doi (best) or title (with author and year if known). save_to: optional folder to also "
        "save the PDF in. max_chars: text returned (default 60000; the whole text is in a .txt "
        "file whose path is given). Runs in the background: if it takes longer than ~40 s the call "
        "returns a run_id; then call zotero_fetch_fulltext_status(run_id) a minute later. Use "
        "zotero_fetch_fulltext instead for papers in the library (that attaches the PDF)."
    ),
)
def read_paper(
    doi: str | None = None,
    title: str | None = None,
    author: str | None = None,
    year: str | int | None = None,
    save_to: str | None = None,
    max_chars: int | str | None = None,
    *,
    ctx: Context,
) -> str:
    import time

    try:
        if not (doi or title):
            return "Give a DOI or a title (with author and year if you know them)."
        try:
            limit = int(max_chars) if max_chars not in (None, "") else 60_000
        except (TypeError, ValueError):
            limit = 60_000
        run_id = _ff.start_background_run({
            "mode": "read", "doi": doi or "", "title": title or "", "author": author or "",
            "year": str(year or ""), "save_to": save_to or "", "max_chars": max(2_000, limit),
            "steps": list(_ff.DEFAULT_STEPS),
        })
        deadline = time.monotonic() + _WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(2)
            finished, text = _ff.background_status(run_id)
            if finished:
                return text
        _finished, text = _ff.background_status(run_id, tail=6)
        return (
            f"Still searching in the background (run_id {run_id}). Call "
            f"zotero_fetch_fulltext_status(run_id='{run_id}') in a minute for the text.\n\n"
            f"Latest progress:\n{text}"
        )
    except Exception as e:
        ctx.error(f"Reading the paper failed: {e}")
        return f"Error reading the paper: {e}"

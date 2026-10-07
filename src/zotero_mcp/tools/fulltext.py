"""Find and attach full-text PDFs for items that have none."""

from __future__ import annotations

from zotero_mcp import fulltext_fetch as _ff
from zotero_mcp._app import mcp
from zotero_mcp._context import Context

#: Each item can take up to a minute (several sources, polite pauses between
#: requests to one site), so a tool call handles a few; the CLI does bulk runs.
_MAX_TOOL_ITEMS = 15


def _as_list(value) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    keys = [str(v).strip() for v in value if str(v).strip()]
    return keys or None


@mcp.tool(
    name="zotero_fetch_fulltext",
    description=(
        "Find and attach a PDF for items that have none, the way you would by "
        "hand: open-access copies (Unpaywall, OpenAlex, Semantic Scholar, "
        "Europe PMC, arXiv, OAPEN), the publisher's page (subscriptions work "
        "on the university network), Google Scholar's PDF links and versions "
        "(SerpApi key), and a web search (Tavily key). Each PDF is checked "
        "(title and first author on its first pages, not a preview) before "
        "it is attached; its version is in the attachment title. Items not "
        "found are tagged fulltext/not-found with the reason in the report. "
        "Never solves captchas, never uses shadow libraries. "
        "item_keys: items to fetch (list or comma-separated); omit to take "
        "items without a PDF from collection_key or the whole library. "
        f"limit: items per call (default 5, max {_MAX_TOOL_ITEMS}); larger "
        "runs: `zotero-mcp fetch-fulltext` in a terminal. "
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
    try:
        keys = _as_list(item_keys)
        try:
            n = int(limit) if limit not in (None, "") else 5
        except (TypeError, ValueError):
            n = 5
        n = max(1, min(n, _MAX_TOOL_ITEMS))
        chosen = [s for s in (_as_list(steps) or list(_ff.DEFAULT_STEPS)) if s in _ff.STEPS] or list(_ff.DEFAULT_STEPS)
        report = _ff.run(
            keys=keys[:_MAX_TOOL_ITEMS] if keys else None,
            collection=collection_key,
            limit=None if keys else n,
            dry_run=bool(dry_run),
            steps=chosen,
            log=ctx.info,
        )
        return report.markdown(limit=40)
    except Exception as e:
        ctx.error(f"Full-text fetch failed: {e}")
        return f"Error fetching full texts: {e}"

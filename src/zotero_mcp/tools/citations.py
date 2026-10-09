"""Questions about the library's citation graph (stored from OpenAlex)."""

from __future__ import annotations

from zotero_mcp import citations as _cit
from zotero_mcp._app import mcp
from zotero_mcp._context import Context

_ACTIONS = ("references", "cited_by", "related", "overview")


@mcp.tool(
    name="zotero_citations",
    description=(
        "The library's citation graph, from OpenAlex reference lists stored on the user's "
        "computer (new papers are added when they are imported). Fast; use it instead of "
        "searching the web for these questions. "
        "action='references' + item_key: what the paper cites, split into papers in the "
        "library and works not in it. "
        "action='cited_by' + item_key, or target (a DOI or OpenAlex ID W...): which papers in "
        "the library cite it, plus the most-cited citing papers outside the library. "
        "action='related' + item_key: library papers sharing the most references with it. "
        "action='overview' (collection_key, item_keys, or neither for the whole library): the "
        "papers cited most within the set, and works the set cites often that are not in the "
        "library (gaps). "
        "limit: how many outside works to list. Coverage: papers with a DOI, and others "
        "OpenAlex finds by title; OpenAlex's reference lists are incomplete for some books "
        "and older papers."
    ),
)
def citations(
    action: str = "references",
    item_key: str | None = None,
    target: str | None = None,
    collection_key: str | None = None,
    item_keys: list[str] | str | None = None,
    limit: int | str | None = None,
    *,
    ctx: Context,
) -> str:
    try:
        action = str(action or "").strip().lower().replace("-", "_").replace(" ", "_")
        if action not in _ACTIONS:
            return f"Unknown action {action!r}; use one of: {', '.join(_ACTIONS)}."
        try:
            n = int(limit) if limit not in (None, "") else None
        except (TypeError, ValueError):
            n = None
        key = (item_key or "").strip() or None
        if action == "overview":
            keys = item_keys
            if isinstance(keys, str):
                keys = [k.strip() for k in keys.replace(";", ",").split(",") if k.strip()]
            return _cit.overview(keys=keys or None, collection=collection_key, limit=n or 15)
        if action == "cited_by":
            if not (key or target):
                return "Give item_key (a paper in the library) or target (a DOI or OpenAlex ID)."
            return _cit.cited_by(key or str(target), limit=10 if n is None else n)
        if not key:
            return f"action={action!r} needs item_key."
        if action == "references":
            return _cit.references(key, limit=n or 25)
        return _cit.related(key, limit=n or 10)
    except Exception as e:
        ctx.error(f"Citation graph failed: {e}")
        return f"Error reading the citation graph: {e}"

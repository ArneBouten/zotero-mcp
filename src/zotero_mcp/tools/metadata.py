"""Check item metadata against the registries; review what needs a decision."""

from __future__ import annotations

from zotero_mcp import library as _library
from zotero_mcp import metadata_audit as _ma
from zotero_mcp._app import mcp
from zotero_mcp._context import Context

#: An item takes one to a few seconds; calls through Cowork must answer
#: within a minute. Larger runs: `zotero-mcp metadata-audit` in a terminal.
_MAX_ITEMS = 15


def _as_list(value) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    keys = [str(v).strip() for v in value if str(v).strip()]
    return keys or None


@mcp.tool(
    name="zotero_metadata_audit",
    description=(
        "Check items' metadata against Crossref/DataCite (by DOI), Open Library "
        "(by ISBN) or OpenAlex (by title, first author and year), and against "
        "PubMed and the item's own PDF. Empty fields are filled; a filled field "
        "is corrected only when two independent sources agree (volume, issue, "
        "pages, DOI, year, journal name, publisher); titles, author lists and "
        "unconfirmed values become proposals for the user (tag metadata/review "
        "plus a note). Every change is tagged and noted with its source. "
        "apply=false (default) only reports. "
        f"item_keys or collection_key; at most {_MAX_ITEMS} items per call — "
        "whole library: `zotero-mcp metadata-audit` in a terminal."
    ),
)
def metadata_audit(
    item_keys: list[str] | str | None = None,
    collection_key: str | None = None,
    apply: bool = False,
    *,
    ctx: Context,
) -> str:
    try:
        keys = _as_list(item_keys)
        report = _ma.run(keys=keys[:_MAX_ITEMS] if keys else None, collection=collection_key,
                         limit=_MAX_ITEMS, apply=bool(apply), log=ctx.info)
        return report.markdown(limit=_MAX_ITEMS)
    except Exception as e:
        ctx.error(f"Metadata audit failed: {e}")
        return f"Error checking metadata: {e}"


@mcp.tool(
    name="zotero_metadata_review",
    description=(
        "The metadata review list: proposals the audit could not apply on its "
        "own (titles, authors, values only one source gives). "
        "action='list' shows them (all items tagged metadata/review, or "
        "item_keys); 'accept' applies an item's proposals (only `fields` if "
        "given, e.g. ['volume','pages']; the rest are discarded); 'reject' "
        "discards them. Ask the user before accepting or rejecting."
    ),
)
def metadata_review(
    action: str = "list",
    item_keys: list[str] | str | None = None,
    fields: list[str] | str | None = None,
    *,
    ctx: Context,
) -> str:
    try:
        keys = _as_list(item_keys)
        writer = _ma.MetadataWriter()
        backend = _library.get_library_backend()
        if action == "list":
            entries = _ma.review_list(writer, backend, keys)
            if not entries:
                return "Nothing waiting for review."
            lines = [f"# Metadata review ({len(entries)} item(s))", ""]
            for raw, changes in entries[:40]:
                info = _ma.ff.ItemInfo.from_zotero(raw)
                lines.append(f"## {info.label} [{info.key}]")
                for c in changes:
                    label = _ma.FIELD_LABELS.get(c.field, c.field)
                    lines.append(f"- {label}: {c.old[:150] or '(empty)'} → {c.new[:150]} "
                                 f"({', '.join(c.sources)}){' — ' + c.why if c.why else ''}")
                lines.append("")
            return "\n".join(lines)
        if action not in ("accept", "reject"):
            return "action must be 'list', 'accept' or 'reject'."
        if not keys:
            return "Give the item_keys to accept or reject."
        found = backend.get_items(keys)
        state = _ma._load_state()
        done = []
        for k in keys:
            if k in found:
                n = _ma.decide(writer, found[k], action == "accept", state, _as_list(fields), log=ctx.info)
                done.append(f"{k}: {n} change(s) applied" if action == "accept" else f"{k}: discarded")
        _ma._save_state(state)
        return "\n".join(done) or "No such items."
    except Exception as e:
        ctx.error(f"Metadata review failed: {e}")
        return f"Error in the metadata review: {e}"

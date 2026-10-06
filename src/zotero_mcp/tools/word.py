"""Word citations: turn citation markers in a .docx into live Zotero fields."""

from __future__ import annotations

import os
from typing import Any

from zotero_mcp import client as _client
from zotero_mcp import library as _library
from zotero_mcp import utils as _utils
from zotero_mcp import word_citations as _wc
from zotero_mcp._app import mcp
from zotero_mcp._context import Context
from zotero_mcp.client import with_zotero_api_lock
from zotero_mcp.local_db import PERSONAL_LIBRARY_GROUP_ID, get_local_zotero_reader
from zotero_mcp.tools import _helpers

#: Item types that have no citation of their own.
_UNCITABLE = {"attachment", "note", "annotation"}
_BATCH = 50


def _library_uri_prefix(group_id: int, account: dict[str, Any]) -> str:
    """The URI prefix Zotero uses for items of this library in documents."""
    if group_id != PERSONAL_LIBRARY_GROUP_ID:
        return f"http://zotero.org/groups/{group_id}"
    user_id = account.get("userID")
    if not user_id:
        env_id = os.getenv("ZOTERO_LIBRARY_ID", "")
        if env_id.isdigit() and int(env_id) > 0 and os.getenv("ZOTERO_LIBRARY_TYPE", "user") == "user":
            user_id = int(env_id)
    if user_id:
        return f"http://zotero.org/users/{user_id}"
    local_key = account.get("localUserKey")
    if local_key:
        return f"http://zotero.org/users/local/{local_key}"
    raise _wc.WordCitationError(
        "Cannot tell which Zotero account this library belongs to, so citations "
        "could not be linked to it. Run in local mode with Zotero installed, or "
        "set ZOTERO_LIBRARY_ID."
    )


def _fetch_csl(zot, keys: list[str]) -> dict[str, dict]:
    """CSL JSON for *keys* as Zotero itself exports it ({} on failure)."""
    out: dict[str, dict] = {}
    try:
        resp = zot._retrieve_data(
            f"/{zot.library_type}/{zot.library_id}/items",
            params={"itemKey": ",".join(keys), "format": "csljson", "limit": len(keys)},
        )
        body = resp.json()
    except Exception:
        return out
    records = body.get("items") if isinstance(body, dict) else body
    for rec in records or []:
        if isinstance(rec, dict) and rec.get("id") is not None:
            out[str(rec["id"]).rsplit("/", 1)[-1]] = rec
    return out


def build_resolver(ctx: Context, style: str):
    """A resolver for word_citations.convert_docx backed by this library."""

    def resolve(marker_keys: list[str]) -> dict[str, _wc.ResolvedItem]:
        backend = _library.get_library_backend()
        to_item_key: dict[str, str] = {}
        for k in marker_keys:
            if _wc.ITEM_KEY_RE.match(k):
                to_item_key[k] = k
                continue
            try:
                item = backend.find_by_citation_key(k)
            except Exception:
                item = None
            if item:
                key = item.get("key") or (item.get("data") or {}).get("key")
                if key:
                    to_item_key[k] = key

        item_keys = sorted(set(to_item_key.values()))
        if not item_keys:
            return {}

        group_id = _client.get_active_group_id()
        account: dict[str, Any] = {}
        item_ids: dict[str, int] = {}
        reader = get_local_zotero_reader()
        if reader is not None:
            with reader:
                account = reader.account_info()
                item_ids = reader.item_ids_for_keys(item_keys, group_id)
        prefix = _library_uri_prefix(group_id, account)

        zot = _helpers._get_bibliography_client(ctx)
        short_style = _wc.style_short_name(_wc.style_id(style))
        rows: dict[str, dict] = {}
        csl: dict[str, dict] = {}
        for i in range(0, len(item_keys), _BATCH):
            chunk = item_keys[i:i + _BATCH]
            fetched = zot.items(
                itemKey=",".join(chunk), limit=len(chunk) + 50,
                include="data,citation,bib", style=short_style,
            )
            # The local API answers an itemKey filter with extra items; keep
            # only what was asked for.
            for row in fetched or []:
                if isinstance(row, dict) and row.get("key") in chunk:
                    rows[row["key"]] = row
            csl.update(_fetch_csl(zot, chunk))

        resolved: dict[str, _wc.ResolvedItem] = {}
        for marker_key, key in to_item_key.items():
            row = rows.get(key)
            if not row:
                continue
            data = row.get("data") or {}
            if data.get("itemType") in _UNCITABLE:
                continue
            item_id = item_ids.get(key)
            item_data = csl.get(key) or _wc.zotero_data_to_csl(data, item_id or key)
            resolved[marker_key] = _wc.ResolvedItem(
                key=key,
                uri=f"{prefix}/items/{key}",
                item_data=item_data,
                citation=_utils.clean_html(row.get("citation") or "", collapse_whitespace=True),
                bibliography=_utils.clean_html(row.get("bib") or "", collapse_whitespace=True),
                item_id=item_id,
            )
        return resolved

    return resolve


@mcp.tool(
    name="zotero_insert_word_citations",
    description=(
        "Turn citation markers in a Word .docx into live Zotero citations — the "
        "same fields the Zotero Word plugin writes, so Refresh, style changes "
        "and the bibliography keep working. Use after drafting text with "
        "markers, instead of typing citations by hand. "
        "Markers (Pandoc syntax), each key a Zotero item key or a Better BibTeX "
        "citekey: [@KEY]; [@KEY1; @KEY2]; [@KEY, p. 12] (also pp., ch., sec., "
        "para., fig.); [see @KEY, pp. 3-5]; [-@KEY] omits the author. A "
        "paragraph containing only {{bibliography}} becomes the bibliography. "
        "Existing Zotero citations are left alone. "
        "docx_path: absolute path to the .docx on this computer. "
        "output_path: where to write; default '<name> (Zotero).docx' beside it. "
        "in_place=True overwrites the input instead, keeping '<name>.bak.docx'; "
        "refused while the file is open in Word. "
        "style: CSL style short name for a document without Zotero settings "
        "(default 'apa'); a document that has them keeps its own. "
        "dry_run=True only reports what would change. "
        "Needs Zotero running (it renders the citations). Unknown keys are "
        "listed and left as typed. Afterwards: open in Word, Zotero tab, "
        "Refresh. "
        "Example: zotero_insert_word_citations(docx_path='C:/Users/me/paper.docx')."
    ),
)
@with_zotero_api_lock
def insert_word_citations(
    docx_path: str,
    output_path: str | None = None,
    in_place: bool = False,
    style: str | None = None,
    locale: str | None = None,
    dry_run: bool = False,
    *,
    ctx: Context,
) -> str:
    """Convert citation markers in a .docx into live Zotero citation fields."""
    try:
        resolver = build_resolver(ctx, style or "apa")
        report = _wc.convert_docx(
            docx_path,
            resolver,
            output_path=output_path,
            in_place=in_place,
            style=style,
            locale=locale,
            dry_run=dry_run,
        )
    except _wc.WordCitationError as e:
        return f"Error: {e}"
    except Exception as e:
        ctx.error(f"Word citation conversion failed: {e}")
        return (
            f"Error converting citations: {e}\n\n"
            "Rendering uses Zotero's CSL engine: in local mode, check that "
            "Zotero is running with the local API enabled."
        )
    return format_report(report)


def format_report(report: _wc.ConversionReport) -> str:
    verb = "Would convert" if report.dry_run else "Converted"
    lines = ["# Word citations", ""]
    lines.append(
        f"{verb} {report.citations} citation marker(s) citing {report.items} item(s), "
        f"style {report.style}."
    )
    if report.bibliography:
        lines.append(("Would insert" if report.dry_run else "Inserted") + " the bibliography at {{bibliography}}.")
    if report.existing_citations:
        lines.append(f"{report.existing_citations} existing Zotero citation(s) left as they were.")
    if report.unresolved:
        lines.append(
            "Not found in the library (markers left as typed): "
            + ", ".join(f"`{k}`" for k in report.unresolved)
        )
    if report.skipped:
        shown = ", ".join(f"`{s}`" for s in report.skipped[:10])
        more = f" and {len(report.skipped) - 10} more" if len(report.skipped) > 10 else ""
        lines.append(f"Left unchanged: {shown}{more}")
    if report.dry_run:
        lines.append("")
        lines.append("Dry run: nothing was written.")
        return "\n".join(lines)
    lines.append("")
    lines.append(f"Saved: {report.output_path}")
    if report.prefs_written:
        lines.append("Zotero document preferences were added (field type: Fields).")
    if report.citations or report.bibliography:
        lines.append(
            "Open it in Word and click Refresh in the Zotero tab once: Zotero then "
            "applies whole-document formatting (ordering, disambiguation, et al.)."
        )
    return "\n".join(lines)

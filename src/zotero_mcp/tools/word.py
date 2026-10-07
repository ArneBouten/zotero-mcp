"""Word citations: turn citation markers in a .docx into live Zotero fields."""

from __future__ import annotations

import os
from typing import Any

from zotero_mcp import client as _client
from zotero_mcp import library as _library
from zotero_mcp import utils as _utils
from zotero_mcp import word_citations as _wc
from zotero_mcp import word_edit as _we
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


# ---------------------------------------------------------------------------
# Inspecting and editing citations in an existing document
# ---------------------------------------------------------------------------


def library_names() -> dict[str, str]:
    """Readable names for this computer's libraries, keyed as in citation URIs."""
    names: dict[str, str] = {}
    reader = get_local_zotero_reader()
    if reader is None:
        return names
    try:
        with reader:
            account = reader.account_info()
            if account.get("userID"):
                names[f"users/{account['userID']}"] = "your library"
            if account.get("localUserKey"):
                names[f"users/local/{account['localUserKey']}"] = "your library"
            try:
                rows = reader._get_connection().execute("SELECT groupID, name FROM groups").fetchall()
            except Exception:
                rows = []
            for gid, name in rows:
                names[f"groups/{gid}"] = f"group \"{name}\""
    except Exception:
        pass
    return names


def _library_label(lib: str, names: dict[str, str]) -> str:
    if lib in names:
        return names[lib]
    if lib.startswith("users/local/"):
        return f"someone else's unsynced library ({lib.rsplit('/', 1)[-1]})"
    if lib.startswith("users/"):
        return f"another Zotero user's library ({lib.rsplit('/', 1)[-1]})"
    if lib.startswith("groups/"):
        return f"a group library you're not in ({lib.rsplit('/', 1)[-1]})"
    return "unknown library"


def format_inventory(inv: _we.Inventory, limit: int = 400, names: dict[str, str] | None = None) -> str:
    names = names or {}
    mixed = len(inv.libraries) > 1
    lines = [f"# Citations in {os.path.basename(inv.path)}", ""]
    lines.append(
        f"Style: {inv.style or 'no Zotero settings yet'} · {len(inv.citations)} Zotero citation(s) · "
        f"{len(inv.bibliographies)} Zotero bibliograph{'y' if len(inv.bibliographies) == 1 else 'ies'} · "
        f"{len(inv.reference_lists)} typed reference list(s) · "
        f"{len(inv.plain_citations)} possible plain-text citation(s) · {len(inv.markers)} marker(s)"
    )
    lines.append("Ids (C = citation, B = bibliography, R = typed reference list, P = plain-text "
                 "citation, D/F/E = paragraph in body/footnotes/endnotes) are what "
                 "zotero_edit_word_citations takes; they hold until the document changes.")
    if inv.citations:
        lines += ["", "## Zotero citations"]
        for c in inv.citations[:limit]:
            items = "; ".join(
                f"{it.key} {it.label}".strip()
                + (f", {it.locator_label or 'page'} {it.locator}" if it.locator else "")
                + (" [author suppressed]" if it.suppress_author else "")
                for it in c.items
            ) or "(no items readable)"
            flag = " · edited by hand in Word" if c.hand_edited else ""
            if mixed:
                libs = sorted({_library_label(it.library, names) for it in c.items})
                flag += " · from " + ", ".join(libs)
            lines.append(f"- **{c.id}** · {c.paragraph} · `{c.text}` → {items}{flag}")
            if c.context:
                lines.append(f"  > {c.context}")
        if len(inv.citations) > limit:
            lines.append(f"- … and {len(inv.citations) - limit} more")
    if inv.libraries:
        lines += ["", "## Where the cited items come from"]
        for lib, n in sorted(inv.libraries.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {n} from {_library_label(lib, names)}")
        if mixed:
            lines.append("Items from a library someone else owns stay linked to it: their copy "
                         "stored in the citation keeps them working on Refresh. To cite such a "
                         "work again without a second bibliography entry, use [@C5] (the item "
                         "of citation C5) or [@C5.2] (its second item).")
    if inv.duplicates:
        lines += ["", "## The same work cited as different items (two bibliography entries)"]
        for group in inv.duplicates:
            label = group[0][1].label
            where = "; ".join(f"{cid} ({_library_label(it.library, names)})" for cid, it in group)
            lines.append(f"- {label}: {where}")
        lines.append("Fix: the merge_duplicates edit points each work's citations to one item.")
    if inv.bibliographies:
        lines += ["", "## Zotero bibliographies"]
        for b in inv.bibliographies:
            lines.append(f"- **{b.id}** · {b.paragraph}–{b.end_paragraph} · {b.text}")
    if inv.reference_lists:
        lines += ["", "## Typed reference lists (not linked to Zotero)"]
        for r in inv.reference_lists:
            lines.append(f"- **{r.id}** · heading {r.heading} \"{r.heading_text}\" · {len(r.entries)} entries")
            for pid, text in r.entries[:limit]:
                lines.append(f"  - {pid}: {text[:200]}")
    if inv.plain_citations:
        lines += ["", "## Possible plain-text citations (typed, not linked)"]
        for p in inv.plain_citations[:limit]:
            lines.append(f"- **{p.id}** · {p.paragraph} · `{p.text}`")
            lines.append(f"  > {p.context}")
    if inv.markers:
        lines += ["", "## Markers not yet converted"]
        for m in inv.markers[:limit]:
            lines.append(f"- {m.paragraph} · `{m.text}`")
    if inv.bibliography_markers:
        lines.append("")
        lines.append("{{bibliography}} placeholder in: " + ", ".join(inv.bibliography_markers))
    return "\n".join(lines)


@mcp.tool(
    name="zotero_inspect_word_citations",
    description=(
        "List what a Word .docx cites, read-only: its live Zotero citations "
        "(with the Zotero item key, author and year behind each, locator, and "
        "the sentence around it), Zotero bibliographies, typed reference lists, "
        "citations typed as plain text such as '(Smith, 2020)', and unconverted "
        "[@KEY] markers. Each gets an id (C3, B1, R1, P2; paragraphs D14, F2, E1) "
        "for zotero_edit_word_citations. It also says which library each "
        "cited item comes from (yours, a group, a co-author's) and flags the "
        "same work cited as different items, which gives two bibliography "
        "entries. Use it first when asked to check, fix "
        "or reformat the citations or references in a document; to check "
        "whether a source supports a claim, read the item's full text "
        "(zotero_get_item_fulltext) with the key listed here. "
        "docx_path: absolute path to the .docx on this computer. "
        "Works without Zotero running."
    ),
)
def inspect_word_citations(docx_path: str, *, ctx: Context) -> str:
    """List the citations, bibliographies and reference lists in a .docx."""
    try:
        return format_inventory(_we.inspect_docx(docx_path), names=library_names())
    except _wc.WordCitationError as e:
        return f"Error: {e}"
    except Exception as e:
        ctx.error(f"Reading Word citations failed: {e}")
        return f"Error reading {docx_path}: {e}"


_WRITE_MODE_QUESTION = (
    "Before writing, ask the user how to save the changes (unless they already said):\n"
    "- new_file: a new document '<name> (Zotero).docx' beside the original, which stays untouched;\n"
    "- tracked_changes: in the original, as tracked changes they accept or reject in Word;\n"
    "- overwrite: in the original, without tracking.\n"
    "Both of the last two first copy the original to a 'Zotero backups' folder beside it. "
    "Then call zotero_edit_word_citations again with write_mode set."
)


@mcp.tool(
    name="zotero_edit_word_citations",
    description=(
        "Change the citations and references in a Word .docx, writing the "
        "same live fields the Zotero Word plugin uses. Run "
        "zotero_inspect_word_citations first for the ids. ASK THE USER which "
        "write_mode they want unless they said: 'new_file' (new '<name> "
        "(Zotero).docx'), 'tracked_changes' (in the original, tracked), or "
        "'overwrite'; the last two back up the original first. "
        "edits: a list of objects, applied together:\n"
        "{op:'replace_citation', citation:'C3', marker:'[@KEY1; @KEY2, p. 4]'};\n"
        "{op:'delete_citation', citation:'C3'};\n"
        "{op:'replace_text', paragraph:'D12', find:'(Smith, 2020)', with:'[@KEY]', occurrence:1} — "
        "'with' may hold markers and words;\n"
        "{op:'comment', citation:'C3' | paragraph:'D12' (+ find:'exact words'), text:'…'};\n"
        "{op:'insert_bibliography', after:'D40'} (no 'after': end of document);\n"
        "{op:'rebuild_bibliography', bibliography:'B1'}; {op:'delete_bibliography', bibliography:'B1'};\n"
        "{op:'replace_reference_list', reference_list:'R1'} — typed list to Zotero bibliography;\n"
        "{op:'merge_duplicates', keep:'C3'?} — one item per work cited as several.\n"
        "Citation ops take expect:'<visible text>' as a guard. Markers and "
        "{{bibliography}} placeholders already in the text are converted too. "
        "Keys: Zotero item keys or Better BibTeX citekeys; '@C5' reuses the "
        "item of citation C5. A work the document already cites is reused "
        "automatically. dry_run=True reports without writing. Needs "
        "Zotero running. Afterwards: open in Word, accept tracked changes, "
        "Zotero tab, Refresh."
    ),
)
@with_zotero_api_lock
def edit_word_citations(
    docx_path: str,
    write_mode: str | None = None,
    edits: list[dict] | str | None = None,
    output_path: str | None = None,
    author: str | None = None,
    style: str | None = None,
    locale: str | None = None,
    dry_run: bool = False,
    *,
    ctx: Context,
) -> str:
    """Edit citations, comments and bibliographies in a .docx."""
    if not write_mode and not dry_run:
        return _WRITE_MODE_QUESTION
    try:
        resolver = build_resolver(ctx, style or "apa")
        report = _we.edit_docx(
            docx_path,
            resolver,
            write_mode=write_mode or "new_file",
            edits=edits,
            output_path=output_path,
            author=author,
            style=style,
            locale=locale,
            dry_run=dry_run,
        )
    except _wc.WordCitationError as e:
        return f"Error: {e}"
    except Exception as e:
        ctx.error(f"Word citation edit failed: {e}")
        return (
            f"Error editing citations: {e}\n\n"
            "Rendering uses Zotero's CSL engine: in local mode, check that "
            "Zotero is running with the local API enabled."
        )
    return format_edit_report(report)


def format_edit_report(report: _we.EditReport) -> str:
    lines = ["# Word citation edits", ""]
    done = [r for r in report.results if r.status == "done"]
    skipped = [r for r in report.results if r.status != "done"]
    verb = "Would apply" if report.dry_run else "Applied"
    lines.append(f"{verb} {len(done)} of {len(report.results)} edit(s); "
                 f"{report.markers_converted} marker(s) converted; style {report.style}.")
    for r in done:
        lines.append(f"- ✓ {r.op} {r.target}" + (f": {r.detail}" if r.detail else ""))
    for r in skipped:
        lines.append(f"- ✗ {r.op} {r.target}: {r.detail}")
    if report.bibliography_written:
        lines.append("Bibliography " + ("would be written." if report.dry_run else "written from the citations in the document."))
    if report.unresolved:
        lines.append("Not found in the library: " + ", ".join(f"`{k}`" for k in report.unresolved))
    if report.skipped_markers:
        lines.append("Markers left as typed: " + ", ".join(f"`{m}`" for m in report.skipped_markers[:10]))
    for n in report.notes:
        lines.append(f"Note: {n}")
    for w in report.warnings:
        lines.append(f"Warning: {w}")
    if report.dry_run:
        lines += ["", "Dry run: nothing was written."]
        return "\n".join(lines)
    if not done and not report.markers_converted and not report.bibliography_written:
        lines += ["", "Nothing changed; the document was not written."]
        return "\n".join(lines)
    lines.append("")
    mode = {"new_file": "New file", "tracked_changes": "Tracked changes in the original",
            "overwrite": "Overwrote the original"}[report.write_mode]
    lines.append(f"{mode}: {report.output_path}")
    if report.backup_path:
        lines.append(f"Backup of the original: {report.backup_path}")
    if report.write_mode == "tracked_changes":
        lines.append(f"Changes are tracked under the name \"{report.author}\".")
    if report.comments:
        lines.append(f"{report.comments} comment(s) added.")
    lines.append(
        "Open it in Word"
        + (", accept the tracked changes you want," if report.write_mode == "tracked_changes" else "")
        + " and click Refresh in the Zotero tab: Zotero then applies the final formatting "
        "(ordering within citations, disambiguation, the bibliography's exact rendering)."
    )
    return "\n".join(lines)

# Checking and fixing item metadata

`zotero-mcp metadata-audit` compares each item with the registries and fills, corrects or proposes changes. Without `--apply` it only writes a report; nothing in Zotero changes.

## What it compares against

| Item has | Reference record | Second sources (to confirm a difference) |
|---|---|---|
| a DOI | Crossref (DataCite when Crossref does not know it) | PubMed (via NCBI and Europe PMC), the item's own PDF, OpenAlex's journal record (journal name only) |
| an ISBN | Open Library | the item's PDF |
| neither | OpenAlex, matched by title (≥90 % similar), first author and year (±1) | PubMed, the item's PDF |

OpenAlex copies most of its data from Crossref, so it does not count as a second source, except for the journal name, which it takes from its own journal list. The PDF counts for a year only when the year stands on a copyright line or in a "(year)" citation line, not anywhere on the page.

Checked item types: journal articles, conference papers, preprints, books, book chapters, theses and reports.

## The rules

- **Empty fields are filled** from the reference record: volume, issue, pages, DOI, ISSN/ISBN, publisher, place, journal or book title, abstract (OpenAlex when the registry has none), and first names where only initials are given.
- **A filled field is corrected only when two independent sources agree** on a different value. This applies to volume, issue, pages, DOI, year, journal name and publisher. The year is the issue year (Crossref's print date before its online date), as APA wants. An abbreviated or misspelt journal name is replaced by the full one; the abbreviation moves to *Journal Abbr*. For a book chapter, only the chapter's own page range is compared.
- **If the second source agrees with your value, nothing changes**; the report notes it.
- **Titles and author lists are never changed on their own.** Differences in case or punctuation are ignored. Real wording differences, other surnames or another author order become proposals.
- **Everything only one source gives becomes a proposal.**

Every change is logged on the item: a child note "Metadata changes by zotero-mcp (date)" lists each field, the old value, the new value and the sources, and the item gets the tag `auto-enriched` (something filled) and/or `auto-corrected` (something overwritten). Undo by hand from the note.

## The review list

Items with proposals get the tag `metadata/review` and a note "Proposed metadata changes (date)" listing them. The first `--apply` run that makes a proposal creates the saved search **Metadata to review** in the library sidebar.

To decide:

- **In Zotero:** add the tag `metadata/accept` (apply all proposals of that item) or `metadata/reject` (discard them). The next `metadata-audit --apply` or `metadata-audit --process-review` carries this out, removes the review tag and the proposal note.
- **Through Claude:** "show my metadata review list", "accept the volume and pages for item X, reject the title". This uses `zotero_metadata_review`.

A rejected value is remembered (`~/.config/zotero-mcp/metadata/state.json`) and not proposed again. Accepting only some fields counts the rest as rejected.

## Running it

```powershell
# Report only: 50 items, then the whole library
py -3.12 -m zotero_mcp.cli metadata-audit --limit 50
py -3.12 -m zotero_mcp.cli metadata-audit

# One collection or a few items
py -3.12 -m zotero_mcp.cli metadata-audit --collection ABCD1234
py -3.12 -m zotero_mcp.cli metadata-audit --items KEY1,KEY2

# Write the changes, notes, tags and review list
py -3.12 -m zotero_mcp.cli metadata-audit --apply

# Only carry out metadata/accept and metadata/reject tags
py -3.12 -m zotero_mcp.cli metadata-audit --process-review
```

`--workers N` sets how many items are checked at once (default 4). Each item takes one to a few seconds; a library of a few thousand items takes about an hour. Every run writes its report to `~/.config/zotero-mcp/metadata/runs/<date-time>.md`.

Writing goes the same way as the other write tools (the Zotero web API key). The OpenAlex and Unpaywall settings come from the full-text fetcher's keys (`keys.env`); without an OpenAlex key, title matching is slow and may be rate-limited.

From Claude, `zotero_metadata_audit` checks up to 15 items per call (`apply` false by default); use the command line for whole collections or the library.

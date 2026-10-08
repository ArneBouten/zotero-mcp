# Keeping the library complete: metadata, PDFs, labels

`zotero-mcp maintain` runs the steps in the order that helps each next one:

1. **Metadata** (`metadata-audit --apply`): empty fields filled, corrections two sources agree on, the rest to the review list (saved search "Metadata to review"). A DOI found here is what the fetcher finds PDFs by. The **attached PDFs** are checked at the same time (below).
2. **PDFs** for the items still without one (`fetch-fulltext`, the normal steps; the browser step stays a button in the progress window).
3. **Metadata again** for the items no registry knew that now have a PDF: a DOI printed on it, or Gemini's reading of its first pages, is a source now.

The search index and the passage labels follow at the next index update.

```powershell
py -3.12 -m zotero_mcp.cli maintain --items KEY1,KEY2
py -3.12 -m zotero_mcp.cli maintain --collection COLLKEY --window
py -3.12 -m zotero_mcp.cli maintain --since 2026-10-01      # items added since then
py -3.12 -m zotero_mcp.cli maintain --collection COLLKEY --report-only
```

**In Zotero:** the right-click action "Check metadata and fetch PDFs" does this for the selected papers or a collection (`zotero-actions/Check metadata and fetch PDFs (Actions & Tags).js`, which starts `~/.config/zotero-mcp/zotero-maintain.ps1`).

**Automatically for new items:** with

```json
"maintenance": {"new_items": true}
```

in config.json, every start of Claude Desktop first maintains the items added since the last start, then updates the index. The first start only remembers the time.

## The attached PDF

For every item with a PDF, the first pages are compared with the item (free; Gemini only reads a PDF when the rules find neither the item's DOI nor its title there):

| Problem | Found by | With `--apply` |
|---|---|---|
| **Another work** | the item's DOI and title are not on the first pages; another DOI is, or Gemini reads another title | If the PDF belongs to another item in the library that has no PDF, it is moved there. Otherwise the right PDF is searched for; once found and checked it replaces the wrong one, which goes to Zotero's trash. |
| **Accepted manuscript or preprint** of a published article | the PDF says so | Replaced by the published version once that is found; until then it stays. |
| **Proof** | page numbers "000-000", "uncorrected proof", volume "XX" | As a manuscript. |
| **Whole book** attached to a chapter | far more pages than the chapter's page range | The chapter's pages are cut out (by the book's printed page numbers) and attached as the chapter's own PDF; the book stays. |

Such items get the tag `fulltext/check-pdf` and a note saying what was found, until the problem is solved. When one of an item's PDFs matches it, its other PDFs (supplements) are not questioned. A scan without text is left alone. `metadata-audit --no-attachments` skips the check.

## Saved web pages

An item's HTML snapshot carries the publisher's citation data in its head (`citation_title`, `citation_doi`, `citation_volume` ...). The audit reads it: its DOI finds the registry record for an item without a DOI, and for an item no registry knows it gives proposals, like a PDF's first pages.

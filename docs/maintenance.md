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
py -3.12 -m zotero_mcp.cli maintain --items KEY1,KEY2 --no-fetch  # metadata only
```

**Metadata only** (`--no-fetch`) is step 1: no PDFs are downloaded, for instance when Zotero's file storage is nearly full. The attached PDFs are still checked; a wrong one gets its tag and note but is not replaced, and a stray PDF still moves to the item it belongs to. Gemini still reads attached PDFs where it is needed.

**The progress window** (`--window`) shows the steps (Metadata › PDFs › Metadata again) and every paper with its metadata (✎ filled or corrected, ⚑ to review, ⚠ wrong PDF, ✓ OK) and its full text. Coloured counts sit above the list: hover over one for what it means, click it to select its papers. When something is left for you, a **To do** panel lists it, each with its button where there is one (open papers behind a bot check in your own browser, search the ones not found with the browser); hover over an ⓘ for what to do. Double-click a paper to open it in Zotero.

## The right-click actions in Zotero

| Action | What it runs | When |
|---|---|---|
| **Check & complete** | metadata → PDFs → metadata again | The default |
| **Check metadata only** | metadata, no PDF downloads | When you want no new PDFs |
| **Fetch PDF only** | PDFs, no metadata changes | Quick, when the metadata is fine |
| **Check PDF (browser only)** | only the browser step, starting with the links an earlier run could not download | Papers an earlier run did not find |

Each works on the selected papers (item menu) or a whole collection (collection menu). To add all four at once: Zotero → Settings → Actions & Tags → Import, and choose `zotero-actions/zotero-mcp-actions.yml` (importing it again later updates them). For the order above, set "Sort menu by" to Name in the same settings. The separate scripts are in `zotero-actions/` too. They start `~/.config/zotero-mcp/zotero-maintain.ps1` or `zotero-fetch.ps1`.

**Automatically for new items:** with

```json
"maintenance": {"new_items": true}
```

in config.json, every start of Claude Desktop first maintains the items added since the last start, then updates the index. The first start only remembers the time. Add `"fetch": false` for metadata only.

## The attached PDF

For every item with a PDF, the first pages are compared with the item (free; Gemini only reads a PDF when the rules find neither the item's DOI nor its title there). Two kinds of findings:

**Wrong PDF: another paper.** The item's DOI and title are not on the first pages; another DOI is, or Gemini reads another title. If the PDF belongs to another item in the library that has no PDF, it is moved there. Otherwise the item gets the tag `fulltext/check-pdf` and a note, and the right PDF is searched for; once found and checked it replaces the wrong one, which goes to Zotero's trash.

**The right paper in another form.** Nothing to check, so no note and no warning, only a tag:

| Form | Found by | Tag | What happens |
|---|---|---|---|
| Accepted manuscript or preprint of a published article | the PDF says so outright ("This is an Accepted Manuscript of ...") | `fulltext/accepted-manuscript`, `fulltext/preprint` | A fetch swaps in the published version when it finds it. |
| Proof | page numbers "000-000", "uncorrected proof", volume "XX" | `fulltext/proof` | As a manuscript. |
| Whole book attached to a chapter | far more pages than the chapter's page range | `fulltext/whole-book` if the chapter cannot be cut out | The chapter's pages are cut out (by the book's printed page numbers) and attached as the chapter's own PDF; the book stays. |

A publisher's cover page ("To cite this article", "Journal homepage") counts as the published version, also when its licence text mentions "the Accepted Manuscript". When one of an item's PDFs matches it, its other PDFs (supplements) are not questioned. A scan without text is left alone. An earlier finding that no longer holds is cleared, with a note. `metadata-audit --no-attachments` skips the check.

## Saved web pages

An item's HTML snapshot carries the publisher's citation data in its head (`citation_title`, `citation_doi`, `citation_volume` ...). The audit reads it: its DOI finds the registry record for an item without a DOI, and for an item no registry knows it gives proposals, like a PDF's first pages.

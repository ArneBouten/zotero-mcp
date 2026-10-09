# Keeping the library complete: metadata, PDFs, labels

`zotero-mcp maintain` runs the steps in the order that helps each next one:

1. **Metadata** (`metadata-audit --apply`): empty fields filled, corrections two sources agree on, the rest to the review list (saved search "Metadata to review"). A DOI found here is what the fetcher finds PDFs by. The **attached PDFs** are checked at the same time (below).
2. **PDFs** for the items still without one (`fetch-fulltext`, the normal steps; the browser step stays a button in the progress window).
3. **Metadata again** for the items no registry knew that now have a PDF: a DOI printed on it, or Gemini's reading of its first pages, is a source now.

The search index and the passage labels follow at the next index update. Papers not yet in the [citation graph](citations.md) are added at the end (a large first run continues in the background after two minutes).

```powershell
py -3.12 -m zotero_mcp.cli maintain --items KEY1,KEY2
py -3.12 -m zotero_mcp.cli maintain --collection COLLKEY --window
py -3.12 -m zotero_mcp.cli maintain --since 2026-10-01      # items added since then
py -3.12 -m zotero_mcp.cli maintain --collection COLLKEY --report-only
py -3.12 -m zotero_mcp.cli maintain --items KEY1,KEY2 --no-fetch  # metadata only
```

**Metadata only** (`--no-fetch`) is step 1: no PDFs are downloaded, for instance when Zotero's file storage is nearly full. The attached PDFs are still checked; a wrong one gets its tag and note but is not replaced, and a stray PDF still moves to the item it belongs to. Gemini still reads attached PDFs where it is needed.

**The progress window** (`--window`) shows the steps (Metadata › PDFs › Metadata again) and every paper with its metadata (✎ filled or corrected, ⚑ to review, ⚠ wrong PDF, ✓ OK) and its full text. Coloured counts sit above the list: hover over one for what it means, click it to show only those papers (click again, or "Show all", for all). Click a column heading to sort by it (again: reverse); Metadata and Full text put the most pressing first. When something is left for you, a **To do** panel lists it, each with its button where there is one (review the suggested changes, open papers behind a bot check in your own browser, search the ones not found with the browser); hover over an ⓘ for what to do. Double-click a paper to open it in Zotero.

## The right-click actions in Zotero

| Action | What it runs | When |
|---|---|---|
| **Check & complete** | metadata → PDFs → metadata again | The default |
| **Check metadata only** | metadata, no PDF downloads | When you want no new PDFs |
| **Fetch PDF only** | PDFs, no metadata changes | Quick, when the metadata is fine |
| **Check PDF (browser only)** | only the browser step, starting with the links an earlier run could not download | Papers an earlier run did not find |
| **Merge certain duplicates** (Tools menu) | Zotero's own merge for papers that are certainly the same | See below |
| **Review suggested metadata** (item, collection and Tools menu) | a window with the suggested changes: accept or reject each | See [metadata-audit.md](metadata-audit.md#the-review-list) |
| **Reports** (Tools menu) | the last 10 checks with their result; double-click one to open its report | Any time, also after closing the window |

The first four work on the selected papers (item menu) or a whole collection (collection menu). To add them all at once: Zotero → Settings → Actions & Tags → Import, and choose [`extras/zotero-actions/zotero-mcp-actions.yml`](../extras/zotero-actions/zotero-mcp-actions.yml) (importing it again later updates them). For the order above, set "Sort menu by" to Name in the same settings. The separate scripts are in the same folder. They start `~/.config/zotero-mcp/zotero-maintain.ps1` or `zotero-fetch.ps1`; copy those two from [`extras/scripts/`](../extras/scripts/) to that folder once.

## New papers, automatically

The action **Check & complete new papers** (in `zotero-mcp-actions.yml`, event "Create Item", no menu entry) runs when papers are added to My Library: by the Zotero Connector, an import, "Add by identifier", or sync from another computer. Claude Desktop does not have to be open.

1. It waits until no paper has been added for a minute (the Connector attaches the page's PDF a little after the item), and checks the papers added together in one run.
2. Metadata, then a PDF if Zotero did not attach one (a PDF that Zotero attaches while the search runs is kept, no second copy), then the metadata again.
3. Then the search index is updated for the new papers, with their passage labels, as at Claude Desktop's start. The index's update lock keeps it from running at the same time as Claude Desktop's own update (then the next start catches up), and a running Claude Desktop sees the new passages at its next search.

The window stays minimised in the taskbar. It comes forward at the end only when something is left for you (a bot check, changes to review, another paper attached); otherwise it closes by itself. To stop it, disable the action in Actions & Tags.

`maintain --index` adds the index update to a run in a terminal; `--quiet` (with `--window`) is the minimised window.

## Checking many papers again

Select papers (or a whole collection, or all of My Library) and run **Check & complete** whenever you like. Papers that need nothing are passed over quickly, so this costs little:

- **Checked fully** (metadata, attached PDF, a PDF if missing, metadata again): papers never checked; papers changed in Zotero since their last check, including a PDF added or replaced; papers checked under older rules (raised when the checks improve enough to be worth it); and recent articles that still lacked volume or pages a month after their last check, since the registries fill those in once an online-first article is in an issue.
- **Unchanged papers**: only Crossref's retraction and correction notices, at most once a month per paper. A new retraction gets the tag `retracted` and a note; a correction, erratum or expression of concern from the last year gets a note.
- **PDFs put online later**: a paper searched in vain (no PDF, or no published version of an attached manuscript) is searched again when the last search is more than 30 days old, since authors and publishers upload PDFs later. Within those 30 days it is not, so Scholar and web-search credits are not spent twice.

If every paper you chose is unchanged, a small window says when it was last checked, with **Check again** and **Cancel**. Otherwise the progress window opens; unchanged papers show "Unchanged, checked <date>", the button **Check all unchanged anyway** checks them all, and right-clicking a paper (**Check anyway**), or a selection of several (**Check these N unchanged anyway**), checks only those. In a terminal, `zotero-mcp maintain --all` checks every paper fully.

**At Claude Desktop's start instead** (older option, without the import action): with `"maintenance": {"new_items": true}` in config.json, every start first maintains the items added since the last start, then updates the index. Add `"fetch": false` for metadata only.

## Two computers

Everything works the same on two computers (a PC and a laptop) that sync the same Zotero library: the same right-click actions, and new papers checked on import on whichever computer you add them. What the checks change is in Zotero and syncs. The record of the checks (which papers were checked when, searches in vain, rejected suggestions, what was learned from your decisions, the citation graph, the free-tier counters) is kept in a folder both computers sync:

```powershell
py -3.12 -m zotero_mcp.cli share-state "$env:USERPROFILE\OneDrive - UGent\Academic\Software\zotero-mcp\state"
```

Run it once on each computer with the same folder. The first copies its records there; the next uses what is there. It sets `"state_dir"` in config.json (with a backup); the old records stay in `.config\zotero-mcp` as a backup. The search index, keys, the fetcher's Chrome profile and lock files stay on each computer.

- **New papers on import** are checked on the computer where you add them, laptop or PC. When the paper then arrives on the other computer by sync, that one leaves it alone (the action skips synced items), so a paper is never checked twice.
- Avoid running a check of the same papers on both computers at the same moment: OneDrive would keep a second copy of a record file, and one computer's record of that run would be lost (the papers are then simply checked again later).
- Free allowances (SerpApi, Tavily, ZenRows) are counted together.

## Duplicates

**Tools → Merge certain duplicates** merges papers that are in My Library more than once and certainly the same work: the same DOI (or, for books, the same ISBN), the same item type, and titles that agree (a missing subtitle is fine). It first shows what it will merge, with **Merge** and **Cancel**.

- It uses Zotero's own merge, as the "Merge items" button does: PDFs, notes, tags and collections move to the copy that stays, the others go to the trash (recoverable), and Word documents citing a merged copy keep working.
- The copy that stays is the one zotero-mcp already checked, else the oldest. Its empty fields are filled from the other copies, and the merged papers then get Check & complete (minimised), which settles fields where the copies disagreed.
- Papers with the same DOI but another type or a different title (a book and its chapter, an article and its erratum) are not merged; they get the tag `duplicate/check`.

Duplicates without a DOI or ISBN are left to Zotero's own "Duplicate Items" view.

## The attached PDF

For every item with a PDF, the first pages are compared with the item (free; Gemini only reads a PDF when the rules find neither the item's DOI nor its title there). Two kinds of findings:

**Wrong PDF: another paper.** The item's DOI or title is not printed at the top of the first page (the title's words further down are not enough: a review or a later paper by the same author has them too); another DOI is, or Gemini reads another title. If the PDF belongs to another item in the library that has no PDF, it is moved there. Otherwise the item gets the tag `fulltext/check-pdf` and a note, and the right PDF is searched for; once found and checked it replaces the wrong one, which goes to Zotero's trash.

**The right paper in another form.** Nothing to check, so no note and no warning, only a tag:

| Form | Found by | Tag | What happens |
|---|---|---|---|
| Accepted manuscript or preprint of a published article | the PDF says so outright ("This is an Accepted Manuscript of ...") | `fulltext/accepted-manuscript`, `fulltext/preprint` | A fetch swaps in the published version when it finds it. |
| Proof | page numbers "000-000", "uncorrected proof", volume "XX" | `fulltext/proof` | As a manuscript. |
| Whole book attached to a chapter | far more pages than the chapter's page range | `fulltext/whole-book` if the chapter cannot be cut out | The chapter's pages are cut out (by the book's printed page numbers) and attached as the chapter's own PDF; the book stays. |

A publisher's cover page ("To cite this article", "Journal homepage") counts as the published version, also when its licence text mentions "the Accepted Manuscript". When one of an item's PDFs matches it, its other PDFs (supplements) are not questioned. A scan without text is left alone. An earlier finding that no longer holds is cleared, with a note. `metadata-audit --no-attachments` skips the check.

## Saved web pages

An item's HTML snapshot carries the publisher's citation data in its head (`citation_title`, `citation_doi`, `citation_volume` ...). The audit reads it: its DOI finds the registry record for an item without a DOI, and for an item no registry knows it gives proposals, like a PDF's first pages.

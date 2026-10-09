# Checking and fixing item metadata

`zotero-mcp metadata-audit` compares each item with the registries and fills, corrects or proposes changes. Without `--apply` it only writes a report; nothing in Zotero changes.

## Where the data comes from

**The item's record** (one per item, first found wins):

| Item has | Source |
|---|---|
| a DOI | Crossref; DataCite when Crossref does not know it; other DOI agencies (mEDRA, JaLC, KISTI…) through doi.org |
| an ISBN (books) | Open Library; Google Books when Open Library has nothing |
| neither, or nothing found | a title search in Crossref (free), then OpenAlex, then Semantic Scholar; if an OpenAlex or Semantic Scholar match has a DOI, that DOI's Crossref record, when it too is clearly the item |
| no record anywhere | the item's own PDF, read by Gemini; without a readable PDF, Google Scholar's "Cite" (through SerpApi, two searches per item, at most the monthly allowance minus 60 kept for the full-text fetcher). Both only give proposals: Scholar's data is extracted automatically and often has errors. |

**Second sources**, to confirm a difference before anything is overwritten:

- PubMed (the NCBI finds the PubMed ID, Europe PMC gives the record): independent of Crossref, health and medical journals only.
- OpenAlex's journal record, for the journal name only (OpenAlex copies the rest from Crossref).
- Google Books, for books (year, publisher).
- The item's own PDF: first by rules (DOI, volume, issue, pages, a copyright or citation-line year), then, when the rules find nothing, read by Gemini (below).

**The item's PDF read by Gemini.** The first four pages (a repository or database cover sheet in front is read too but not counted), and for books, theses and reports also any page within the first 15 with a © or ISBN line, which is usually the copyright page. Gemini returns what is printed there: title, all authors in byline order, year, journal, volume, issue, pages, DOI, book title and editors, publisher, ISBN, university, and whether the PDF is the published version, an accepted manuscript or a preprint. A manuscript or preprint never confirms volume, issue, pages or year. Answers are cached per file (`~/.config/zotero-mcp/metadata/pdf-cache/`), so a second run costs nothing. It is asked only when a difference still has no second source, for author proposals, and for items no registry knows: a few hundred items, a few cents. `--no-gemini` checks the PDF by rules only. It uses the search index's Gemini key; the model is `semantic_search.structure.gemini_model` in config.json (default `gemini-3.8-flash`).

## Is the record really this item?

Before anything is compared:

- **A record found by DOI** must describe the item. Nothing is compared, and the report says why, when the DOI's record is:
  - a correction or retraction notice;
  - a table, figure or supplement;
  - the whole book (for a chapter);
  - a CHOICE review;
  - a book, chapter or dataset (for a journal article);
  - a work with another title (character similarity below 0.5);
  - a work with a fairly different title (below 0.75) and another first author;
  - a work from more than three years apart with a different title.
- **A preprint's DOI** on a published article becomes a proposal to use the published version's DOI, when Crossref links it.
- **A title match** (Crossref, OpenAlex, Semantic Scholar) needs:
  - a title at least 90 % similar, or equal main titles (before the colon, four words or more) when only a subtitle differs;
  - the same first author;
  - a year within one year;
  - not a dataset, erratum or peer review.

  Short or generic titles ("Editorial", "Introduction", "Book review") must match exactly. A different work type is otherwise only a soft signal, because OpenAlex often calls a chapter an article.
- **Similarity** is character-based (difflib's ratio on lower-cased, accent-free words), after removing series notes, editions and "Chapter 4:".

## Retractions and corrections

Crossref's record includes Retraction Watch data. A retracted item is flagged `RETRACTED (date, notice DOI)` in the report and, with `--apply`, tagged `retracted`. Expressions of concern and published corrections are noted.

## The rules

- **Empty fields are filled** from the reference record:
  - volume, issue, DOI, ISSN/ISBN, publisher, and journal or book title;
  - pages; an article without page numbers gets its article number, as APA wants;
  - the university (theses), the institution (reports);
  - the abstract;
  - first names where only initials are given.

  When the registry has no abstract, PubMed's is used for articles, then OpenAlex's. Either only if it shares at least three content words with the title, is 200–6,000 characters long, is not mostly copyright boilerplate and is not in another language than the title. The place of publication is left alone: APA 7 does not use it.
- **A filled field is corrected only when two independent sources agree** on a different value. This applies to volume, issue, pages, DOI, year and journal name. A publisher is only filled in, never changed.
  - The year is the issue year (Crossref's print date before its online date), as APA wants. When yours is the online year, the proposal says so.
  - An abbreviated or misspelt journal name, or one with clutter ("(Auckland, N.Z.)", " - ELEM SCH J", "&amp;"), is replaced by the plain name; the abbreviation moves to *Journal Abbr*. A registry name that only adds a subtitle to yours is not a difference.
  - A year more than two years off is never corrected, only proposed: it usually means a wrong DOI.
  - A DOI that works is never replaced by another (Crossref aliases).
- **If the second source agrees with your value, nothing changes**; the report notes it as a registry error.
- **Never a change for the worse**, not even as a proposal: an abbreviated journal name, a shorter name that drops its first words ("Advances in Neural ..." → "Neural ..."), a lost accent, a lost supplement ("27 Suppl 3" → "27"), a year as the volume, or pages that are not page numbers ("Article # 3"). Semantic Scholar never changes a journal name: its venue names are normalised, not the journal's own title. A volume or issue with a leading zero ("04") is the same as without.
- **Without a second source, nothing changes**: the difference becomes a proposal. Publisher differences (imprint, parent company, spelling) are not proposed at all. A one-page value is completed to the registry's range ("68" → "68–78") but a range is never shortened to a first page.
- **Titles and author lists are never changed on their own.**
  - **Ignored:** differences in case or punctuation, series notes, editions, a subtitle the registry left out, ISBN hyphens and different valid ISSNs. Also ignored: name suffixes ("Jr."), stray initials, degrees in names ("Lambiase MS") and garbled accents from the registry ("JÃ¤ger").
  - **Kept:** a registry that lists fewer authors than you have never leads to a proposal to drop yours.
  - **The PDF decides author proposals:** if the PDF's author list agrees with the registry, the proposal says so; if it agrees with yours, nothing is proposed. An author proposal keeps your fuller first names where the registry has initials.
- **Items no registry knows** (theses, reports, unpublished work) get proposals from their own PDF, when its title matches the item's.

Every change is logged on the item: a child note "Metadata changes by zotero-mcp (date)" lists each field, the old value, the new value and the sources, and the item gets the tag `auto-enriched` (something filled) and/or `auto-corrected` (something overwritten). Undo by hand from the note.

## The review list

Items with proposals get the tag `metadata/review` and a note "Proposed metadata changes (date)" listing them. The first `--apply` run that makes a proposal creates the saved search **Metadata to review** in the library sidebar.

To decide:

- **With a click (easiest):** the progress window's **Review** button (under "To do"), or in Zotero right-click › **Review suggested metadata** (selected papers, a collection, or from the Tools menu all papers waiting). A window lists each suggestion under its paper, with yours, the suggested value and why. **Accept** changes the field in Zotero at once; **Reject** keeps yours. Select a paper row to decide all its suggestions together; undecided ones stay waiting. In a terminal: `zotero-mcp metadata-review`.
- **With a tag** on the paper or on its suggestions note: `metadata/accept` (apply all of that paper's suggestions) or `metadata/reject` (discard them). This is carried out at the next check of any paper (Check & complete, Check metadata only, or a new paper's import), not at once.
- **Through Claude:** "show my metadata review list", "accept the volume and pages for item X, reject the title". This uses `zotero_metadata_review`.

A rejected value is remembered (`~/.config/zotero-mcp/metadata/state.json`) and not proposed again. With the tag or through Claude, accepting only some fields counts the rest as rejected.

**Learning from your decisions.** Each proposal has a kind: its field plus its reason, such as "Year | yours is the online year; APA uses the issue year". Once you have decided at least 10 proposals of one kind the same way at least 90 % of the time, later runs decide that kind for you. The change then says "you accepted 12 of 12 like this"; a kind you keep rejecting is no longer proposed. The counts are in `state.json` under `_learned`.

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

**When a registry does not answer.** A service that refuses (HTTP 429, for instance OpenAlex's free daily allowance of about 1,000 searches, which resets at midnight UTC), fails or cannot be reached is not taken as "no record": the item is reported as *not checked this time* and counted separately, without proposals from its PDF or Google Scholar. A service that refuses three times is not asked again in that run. Run the audit again later for those items.

`--workers N` sets how many items are checked at once (default 4). With an OpenAlex key, the whole library (about 2,200 items) takes a few minutes. Every run writes its report to `~/.config/zotero-mcp/metadata/runs/<date-time>.md`: a summary, then what needs you (suggestions to review, wrong PDFs, papers not checked or unknown to every registry, other findings, grouped), then what was changed automatically.

Writing goes the same way as the other write tools (the Zotero web API key). The OpenAlex, Semantic Scholar and Unpaywall keys, and an optional `GOOGLE_BOOKS_API_KEY`, come from the full-text fetcher's `keys.env`.

From Claude, `zotero_metadata_audit` checks up to 15 items per call (`apply` false by default); use the command line for whole collections or the library.

## The attached PDF, and DOIs on the PDF or web page

The audit also checks the attached PDF (another work, an accepted manuscript or proof of a published article, a whole book on a chapter) and, with `--apply`, has it replaced or the chapter cut out: see [maintenance.md](maintenance.md). For an item without a DOI, a DOI printed on its PDF's first pages or in its saved web page's citation data is looked up first (free, and surer than a title search); Gemini's reading of the PDF can give it too.

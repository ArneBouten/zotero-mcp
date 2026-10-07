# Printed pages, headings and chapters for search passages

Every passage in the search index can carry, besides its text:

| Label | What it is | Example |
|---|---|---|
| `page_label` | the page number as printed, for citing | `379` (PDF page 3) |
| `section` | the part of the paper | `Methods`, `References` |
| `heading` | the heading path it falls under | `Study 2 › Method › Participants` |
| `chapter` | for books, theses and reports: the chapter | `3 Risky play and development` |

Search results show them in the **Location** line: `Methods, under "Participants", p. 379 (PDF p. 3)`. Without a printed page number the line says `PDF p. 3`, never a guess.

Labels are metadata beside the passage: they never change what is embedded, so adding or correcting them costs no embedding money.

## Printed page numbers

From the most reliable source the PDF offers:

1. **The PDF's own page labels.** Not used when they only count the PDF's pages (1, 2, 3…) while the item's Pages field says 275–288.
2. **The numbers printed in the header or footer**, by majority vote. A stretch of pages counts only when most of its numbered pages agree, so the following don't move it:
   - a cover sheet;
   - a chapter opener without a number;
   - a year in the footer;
   - an OCR misread.

   Roman front matter is recognised, and a page before the article's first page (a cover) gets no number.
3. **The item's Pages field**, when the PDF has exactly that many pages (or one more, a cover in front).

In a test on 40 random articles from this library, 35 got their printed pages and 5 kept PDF pages. In those 5, nothing in the PDF or the item gave the printed numbers: a preprint, or a PDF with a different page count.

## Headings and sections

1. **Bookmarks** (the PDF's outline), when they name the document's own sections. A third of the articles here have them, and they are exact.
2. **Gemini judging candidate lines** (optional, `--gemini`).
   - **Candidates:** short lines set apart from the body text by size, weight, typeface, colour, capitals, numbering or space, including run-in headings ("**Participants.** Children…").
   - **Left out:** running headers, captions and lowercase list items.
   - **What Gemini gets:** the candidates, each with its page and style, plus the printed table of contents for books and the running headers.
   - **What it returns:** which candidates are headings, their level and the section each opens. It can only pick candidates, never invent a heading; the answer is put back in reading order and cached per file.
3. **Rules**, without an API: candidates whose text is a section name. The names are known in English, Dutch, French, Spanish, Portuguese, Italian and German ("Inleiding", "Résultats", "Literatur"). Two kinds of false headings are dropped:
   - a structured abstract ("Background: … Methods: …");
   - author roles after the references ("Methodology: A.B.").

A section heading's sub-headings inherit its section ("Participants" under Method is Methods). Sub-headings of an Abstract, Appendix or References heading do not change the section.

Headings are then located in the indexed text near their page. When most of an item's headings cannot be found there (another attachment than the one indexed), its headings are not used.

## Reference lists

A passage that is mostly references (many years, author initials, volume and page numbers, DOIs) is labelled `References` even where the heading was missed. Reference passages are left out of normal searches: a list of titles matches a topic query without saying anything about it. A search that filters on `section` keeps them; to switch this off, set `semantic_search.chunking.exclude_references_from_search` to `false`.

## Running it

```powershell
# Try it: 40 items, nothing written, with Gemini for headings
py -3.12 -m zotero_mcp.cli relabel-index --dry-run --limit 40 --gemini

# Label the whole index (items labelled before are skipped)
py -3.12 -m zotero_mcp.cli relabel-index --gemini

# Again from scratch, or a few items
py -3.12 -m zotero_mcp.cli relabel-index --force
py -3.12 -m zotero_mcp.cli relabel-index --items KEY1,KEY2
```

Each run writes a report with examples to check to `~/.config/zotero-mcp/structure/runs/`.

**New and re-indexed items** get labels after each index update when this is in config.json:

```json
"semantic_search": {
  "structure": {"enabled": true, "gemini": true, "gemini_model": "gemini-flash-latest"}
}
```

**Cost with Gemini:** only items whose bookmarks do not name their sections are sent, about two thirds of articles and most books. An article is about 4,000 tokens, a book up to 20,000: roughly 1 cent per book and 0.5 cent per article at Gemini Flash prices, once, because answers are cached.

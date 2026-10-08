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

1. **The PDF's own page labels.** Not used when they only count the PDF's pages (1, 2, 3…) while the item's Pages field says 275–288. Nor when they cannot be printed numbers: "image 1", a page 0, numbers running backwards, or roman numerals throughout an article.
2. **The numbers printed in the header or footer**, by majority vote. A stretch of pages counts only when most of its numbered pages agree, so the following don't move it:
   - a cover sheet;
   - a chapter opener without a number;
   - a year in the footer;
   - an OCR misread.

   Roman front matter is recognised, and a page before the article's first page (a cover) gets no number.
3. **The item's Pages field**, when the PDF has exactly that many pages (or one more, a cover in front).

In a test on 40 random articles from this library, 35 got their printed pages and 5 kept PDF pages. In those 5, nothing in the PDF or the item gave the printed numbers: a preprint, or a PDF with a different page count.

## Headings and sections

1. **Bookmarks** (the PDF's outline), when they name the document's own sections. A third of the articles here have them, and they are exact. A top bookmark that only holds the title is dropped, as are bookmarks for tables and figures. Bookmarks that stop halfway (only up to the method, say) are not used. With `--gemini`, Gemini checks the bookmarks: which are headings, their level, and the part of the paper each opens, also when the heading does not name it ("Research design" is Methods, "Case studies" Results). Text and page stay the bookmark's own.
2. **Gemini judging candidate lines** (optional, `--gemini`).
   - **Candidates:** short lines set apart from the body text by size, weight, typeface, colour, capitals, numbering or space, including run-in headings ("**Participants.** Children…").
   - **Left out:** running headers, captions and lowercase list items.
   - **What Gemini gets:** the candidates, each with its page and style, plus the printed table of contents for books and the running headers.
   - **What it returns:** which candidates are headings, their level and the section each opens. It can only pick candidates, never invent a heading; the answer is put back in reading order and cached per file.
3. **Rules**, without an API: candidates whose text is a section name. The names are known in English, Dutch, French, Spanish, Portuguese, Italian and German ("Inleiding", "Résultats", "Literatur"). Two kinds of false headings are dropped:
   - a structured abstract ("Background: … Methods: …");
   - author roles after the references ("Methodology: A.B.").

**Introductions, with or without a heading.** In papers with a Method heading, text before Method that no heading names is labelled Introduction. This covers:

- APA papers without an "Introduction" heading: the introduction starts after the abstract;
- topical headings before Method ("Risky play and development"): they are part of the introduction;
- papers that do have an "Introduction" heading: the introduction starts there, and keywords or highlights above it keep their own label.

"Background", "Literature review" and "The present study" open the Introduction section themselves, whether or not an "Introduction" heading came before them.

**Combined headings** such as "Results and Discussion" get both: `section` Results and `section_2` Discussion, as the JATS standard publishers use allows. A search filtered on Discussion finds them too, and the Location line shows "Results & Discussion".

A section heading's sub-headings inherit its section ("Participants" under Method is Methods). Sub-headings of an Abstract, Appendix or References heading do not change the section.

Headings are then located in the indexed text on their page, in reading order: each one after the previous, preferring the line where it stands alone over the same words in a sentence. A "Methods:" in a structured abstract or a second "Phase 1" under Results cannot pull a heading to the wrong place. A heading that names a study ("Study 1", "Experiment 2") has no section of its own; its Method, Results and Discussion do. When most of an item's headings cannot be found in the indexed text (another attachment than the one indexed), its headings are not used.

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

Each run writes a report with examples to check to `~/.config/zotero-mcp/structure/runs/`, and says what Gemini used: calls, input and output tokens (thinking included) and the cost at list price.

**When Gemini fails** (a daily limit, a spending cap, no network), a run waits out a per-minute limit and otherwise stops asking Gemini after 10 failures in a row. Those items get labels from their bookmarks or the rules, and the next run asks Gemini for them again.

**New and re-indexed items** get labels after each index update when this is in config.json:

```json
"semantic_search": {
  "structure": {"enabled": true, "gemini": true, "gemini_model": "gemini-3.8-flash"}
}
```

**Kinds of papers.** Gemini is told how sections work in APA papers (no Introduction heading), papers with several studies ("Study 2" has its own Method and Results; a General Discussion is Discussion), systematic and scoping reviews (search, eligibility, screening, data extraction and risk of bias are Methods; study characteristics and the synthesis are Results), qualitative studies (Findings and Themes are Results), theoretical papers (topical sections are Other) and books. The rules know the review terms too.

**Cost with Gemini:** a bookmark check is a few hundred tokens (a third of articles). The other items send their candidate lines: an article is about 4,000 tokens, a book up to 20,000: about 0.5 cent per article and 2 cents per book with Gemini 3.8 Flash (2026 prices; Google doubles them in January 2027), once, because answers are cached. The model is fixed (`gemini-3.8-flash`) rather than the `gemini-flash-latest` alias, which Google moves to each new model.

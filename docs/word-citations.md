# Word citations

`zotero_insert_word_citations` turns citation markers in a Word document into
live Zotero citations: the same fields the Zotero Word plugin inserts with
*Add/Edit Citation*. After conversion the document behaves as if every
citation had been inserted by hand — *Refresh* updates them, *Document
Preferences* switches the style, and the bibliography follows the citations.

It is meant for drafting with an assistant: the assistant finds the sources
(`zotero_semantic_search`, `zotero_search_items`), writes the text with
markers, and converts them, so no citation is ever typed as plain text.

Needs the `[word]` extra (`lxml`), Zotero running (it renders the citations
with its own CSL engine), and the Zotero Word plugin to refresh afterwards.

## Markers

Pandoc's citation syntax. A key is a Zotero item key (8 characters, as in
`zotero_search_items` results) or a Better BibTeX citation key.

| Marker | Becomes (APA) |
|---|---|
| `[@SOEN2009]` | (Soenens et al., 2009) |
| `[@SOEN2009; @GROL2009]` | (Soenens et al., 2009; Grolnick & Pomerantz, 2009) |
| `[@SOEN2009, p. 190]` | (Soenens et al., 2009, p. 190) |
| `[@SOEN2009, pp. 190-192]`, `[@KEY, 12]` | page ranges; a bare number is a page |
| `[@KEY, ch. 3]`, `sec.`, `para.`, `fig.`, `vol.` | other locators |
| `[see @SOEN2009]` | (see Soenens et al., 2009) |
| `[-@SOEN2009]` | (2009) — for "Soenens et al. (2009) showed …" |
| `{{bibliography}}` on its own paragraph | the bibliography |

A marker may be split across differently formatted runs (as Word does when
you type it); the field takes the formatting of the marker's first
character. Markers in footnotes and endnotes are converted too.

## What it writes

- By default a new file, `<name> (Zotero).docx`, beside the original.
  `output_path` chooses another; `in_place=True` overwrites the original and
  keeps it as `<name>.bak.docx`. Overwriting is refused while Word has the
  file open.
- Each marker becomes an `ADDIN ZOTERO_ITEM CSL_CITATION` field linking the
  item by its Zotero URI (`http://zotero.org/users/<id>/items/<KEY>`), with
  the item's CSL JSON as Zotero exports it, locator, prefix and
  suppress-author flags.
- The visible text is Zotero's own rendering of each item, so it already
  reads correctly. Click **Refresh** in Word's Zotero tab once: Zotero then
  applies what needs the whole document — ordering within a citation,
  disambiguation, "et al." rules, numbering in numeric styles.
- A document with no Zotero settings gets them (style `apa` unless `style`
  says otherwise, fields rather than bookmarks). A document that already has
  Zotero citations keeps its own style and settings; its existing citations
  are not touched.
- Keys not found in the library are listed and their markers left as typed,
  so nothing is silently dropped.

`dry_run=True` reports what would be converted without writing anything.

## Checking and fixing an existing document

Two more tools work on documents that already have citations, whether
inserted with the Word plugin, by this tool, or typed by hand.

`zotero_inspect_word_citations` reads a document without changing it and
lists, each with a short id:

| Id | What |
|---|---|
| `C3` | a live Zotero citation, with the item key, author and year behind it, any page locator, and the sentence around it; flagged when its text was edited by hand in Word |
| `B1` | a Zotero bibliography |
| `R1` | a typed reference list (a "References" heading and the entries under it), not linked to Zotero |
| `P2` | a citation typed as plain text, such as "(Smith, 2020)" or "Smith et al. (2020)" |
| `D14`, `F2`, `E1` | paragraph 14 of the body, footnote paragraph 2, endnote paragraph 1 |

It also lists markers not yet converted. It needs no Zotero running. To
check whether a source supports a claim, read the cited item's full text
(`zotero_get_item_fulltext`) with the key listed.

`zotero_edit_word_citations` applies a list of edits in one go:

| Edit | Does |
|---|---|
| `{"op": "replace_citation", "citation": "C3", "marker": "[@KEY1; @KEY2, p. 4]"}` | points a citation to other items, adds or removes items, changes locators |
| `{"op": "delete_citation", "citation": "C3"}` | removes it, and the space it leaves before punctuation |
| `{"op": "replace_text", "paragraph": "D12", "find": "(Smith, 2020)", "with": "[@KEY]"}` | replaces exact text; markers in `with` become live citations; `occurrence` picks a later match |
| `{"op": "comment", "citation": "C3", "text": "…"}` | a Word comment on a citation; with `paragraph` (and optionally `find`) on text instead |
| `{"op": "insert_bibliography", "after": "D40"}` | a new bibliography; without `after`, at the end of the document |
| `{"op": "rebuild_bibliography", "bibliography": "B1"}` | rewrites it from the citations in the document |
| `{"op": "delete_bibliography", "bibliography": "B1"}` | removes it |
| `{"op": "replace_reference_list", "reference_list": "R1"}` | replaces a typed list with a Zotero bibliography |
| `{"op": "merge_duplicates", "keep": "C3"}` | points all citations of a work cited as several items to one item (`keep` optional) |

Citation edits accept `"expect": "<visible text>"`: the edit is skipped if
the citation no longer reads like that. Markers already in the text and
`{{bibliography}}` placeholders are converted as well. A bibliography lists
every item the document cites once the edits are made, in alphabetical
order; Zotero's Refresh renders it exactly.

### Shared documents

A citation stores a link to the item in the library it came from, plus a copy
of the item's details. Anyone can open the document, add citations and click
Refresh: items from a library they cannot reach (a co-author's own library,
or a group they are not in) are shown from that copy. They cannot edit those
items, though, and Zotero does not merge them with their own copy of the same
work.

So when two people cite the same paper from different libraries, Zotero sees
two items, and the bibliography lists the paper twice. Zotero matches items
by the library link only, never by DOI or title, so this is not fixed by
anyone adding the paper to their own library.

The tools prevent and repair this inside the document:

- When an edit cites a work the document already cites (same DOI, or same
  title and year), the new citation reuses that item, whoever's library it
  came from, so the work stays one bibliography entry. For the person who
  owns that item it is an ordinary citation from their own library, which
  they can keep editing. `reuse_cited=False` turns this off.
- `{"op": "merge_duplicates"}` points every citation of a work cited as
  several items to one of them: the item cited most often, or the one in
  the citation given as `"keep": "C3"`. Only the links change; the visible
  text stays until Refresh.
- `[@C5]` (or `[@C5.2]` for its second item) cites the item of citation C5
  explicitly.

The inspect tool shows where each cited item comes from and lists works
cited as several items. The lasting fix for a shared paper is a Zotero group
library that all authors cite from.

### How the result is saved

`write_mode` is required, and the assistant asks which you want before
writing:

- `new_file`: a new document, `<name> (Zotero).docx` (or `output_path`);
  the original stays as it was.
- `tracked_changes`: in the original, as tracked changes, with comments
  where the edits added them. Accept or reject each change in Word, then
  click **Refresh**. Changes and comments go under the name in
  `ZOTERO_MCP_WORD_AUTHOR` if set, otherwise the document's *last modified
  by* (skipping names that assistants and libraries leave there, such as
  "Claude" or "python-docx"), otherwise its creator.
- `overwrite`: in the original, without tracking.

Both of the last two first copy the original to a `Zotero backups` folder
next to it, named with the date and time. Writing is refused while Word has
the document open.

`dry_run=True` reports what each edit would do without writing.

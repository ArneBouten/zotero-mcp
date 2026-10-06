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

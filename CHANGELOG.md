# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.15.2+arne.70] - 2026-11-01

### Changed

- **Upstream 0.15.2 merged in** (from 0.15.1). New here: `zotero-mcp repair-db` for an index ChromaDB cannot read (#747), clearer reports of a damaged index in the status and in `update-db`, the status read from `chroma.sqlite3` instead of ChromaDB's `count()`, opt-in idle unloading of ChromaDB (`semantic_search.idle_unload_minutes`, off by default), `ZOTERO_API_URL`, `zotero-cli edit --field`, `link_mode="linked"` for `zotero_attach_file`, and the HuggingFace model loaded once per process (#726). This build keeps its own section labels, passage pool, study-context headers, startup update and two-computer code; the hosted re-ranker (#722) and section labels (#721) went upstream from here, so the re-rank line now says "candidates" as upstream does.
- Upstream's `repair-db` module is `zotero_mcp.repair_db` here, because this build's `zotero_mcp.index_repair` already holds `db-check` and `db-rebuild-vectors`. Both kinds of repair stay: `db-rebuild-vectors` for duplicated or missing HNSW labels, `repair-db` for a `chroma.sqlite3` ChromaDB refuses to read.
- **Minimum versions for two advisories:** `fastmcp>=3.4.10` (GHSA-7xgj-8392-5q4r) and, for the `pdf` extra, `pymupdf>=1.26.7` (PYSEC-2026-3001). Neither path is used here (no authorization transforms, no PyMuPDF command line), but the PC had both affected versions installed.

## [0.15.2] - 2026-10-31

### Added

- **`zotero-mcp repair-db` rebuilds an unreadable semantic index without re-embedding it** (#747). An `update-db` cut off part way through can leave `chroma.sqlite3` in a state ChromaDB refuses to read, and the only way out was `--force-rebuild`, which re-embeds the whole library: hours and a full API bill for a large full-text index. `repair-db` reads the vectors from the index's HNSW files and the documents and metadata from a copy of `chroma.sqlite3` (full table scans, so damaged indexes do not matter; a header that undercounts its pages is corrected in the copy), and writes every item whose chunks all have a document, a vector and matching metadata into a fresh index. The rest are left out and listed in a report; `update-db --fulltext` embeds them. The old index is kept as `chroma_db.broken-<timestamp>`. `--dry-run` reports what would be kept without changing anything. On a 1.16-million-chunk OpenAI index damaged this way it kept 36,421 of 36,442 items.
- **Each indexed passage carries the paper section it falls in** (#721, split from #547). With chunking on, passages get a `section` label (Abstract, Introduction, Methods, Results, Discussion, Conclusion, References, Appendix, Back matter), shown first in the Location line and usable in `filters`. Headings are matched by shape; unrecognised headings end a label rather than letting it run on. Existing indexes get labels after a rebuild.
- **Re-ranking through a hosted endpoint** (#722, split from #547). `reranker.provider` set to `voyage`, `cohere`, `openrouter` or `contextual` (or `base_url` + `api_key_env` for any compatible endpoint) re-ranks through that API instead of a local cross-encoder; the query and candidate passages are sent to the provider. A failed call falls back to embedding order and the search output says why. With any re-ranker, Relevance is now its score, with the embedding similarity alongside. Default unchanged (local cross-encoder, off).
- **`zotero-cli edit --field NAME=VALUE`** sets any Zotero field that has no dedicated flag, such as `seriesNumber` or `versionNumber` (#728, #753). Repeatable; `NAME=` clears a field; the item-type schema check applies as for other edits.
- **`embedding_config.max_input_tokens`** caps the text sent per document for OpenAI-compatible and Ollama providers (#735), for local servers that run a model with a shorter context than OpenAI's 8000 tokens. Unset, nothing changes.
- **Paste or drop a picture into the Zotero Agent's message box** (#757). A screenshot from the clipboard or a dropped image file becomes an image card for the next message, redrawn as PNG at most 1568 px on the long edge. An agent that takes no images now says so in a notice instead of silently leaving the picture out.
- **Callout cards in the Zotero Agent panel** (#719). Definitions, theorems, lemmas, proofs, notes and the like, written as `> [!definition]` blockquotes or LaTeX `\begin{theorem}`, render as numbered cards, and `\ref{thm:x}` links back to one.
- **`zotero_attach_file` takes `link_mode="linked"`** (#754) to link a local file in place instead of importing a copy.
- **`ZOTERO_API_URL` points the client at a self-hosted Zotero Web API server** (#734). Unset, nothing changes.
- **A Nix flake** for `nix run` and `nix profile install` (#733), maintained by its contributor; CI does not build it.

### Changed

- **The MCP server can free the semantic index's memory when semantic search is idle (opt-in).** ChromaDB loads a collection's whole vector index on the first query and keeps it until the process exits; nothing in its API releases it, and the 1.x bindings ignore `chroma_memory_limit_bytes`. A server that ran one semantic search therefore held the index for as long as it ran, often days: about 6 GB for a million OpenAI passages. With `semantic_search.idle_unload_minutes` set (it is off by default; 10 is a good value), the server runs ChromaDB in a worker process that starts at the first semantic call and exits after that many idle minutes; the next call starts a new worker and reloads the index. Only ChromaDB moves: fetching the Zotero items for the hits, the active library and the result formatting stay in the server. `0`, the default, keeps the previous in-process behavior. The command-line tools are unchanged, since their process ends with the command. See "Semantic search memory" in `docs/configuration.md`.
- **The search database status reads its counts from the database file.** `zotero_get_search_database_status` called ChromaDB's `count()`, which loads the whole vector index just to count it, and on a damaged index made ChromaDB retry its pending writes into the file. It now reads `chroma.sqlite3` read-only, reports writes ChromaDB has logged but not applied, and recognizes the on-disk signature of an interrupted write (a file longer than its header's page count) as an unreadable index without opening it.
- **Selected areas and pasted pictures are small tiles in the Zotero Agent's message box**, in a row under the chips, with × on the corner on hover and a click for a large preview, instead of full-width cards.
- **Context already sent in a chat leaves the Zotero Agent's message box.** The open paper's chip goes out with the first message and comes back only when you switch papers; a selection or area comes back when it changes. The agent still gets a one-line reminder of what is open.
- **The Zotero Agent answers in the language you write in**, and "Explain better" keeps the language of the last answer.
- **`zotero-cli batch` and `tags` address tag names that contain commas** (a JSON array), reaches attachments when items are selected by tag or key, takes `--item-keys`, and pages `--limit` up to 1000, saying so when more items matched (#752).

### Fixed

- **A damaged semantic index is reported as damaged, not as empty** (#747). When ChromaDB could not read the index, `zotero_get_search_database_status` and `db-status` showed `Document Count: 0` with ChromaDB's error, and a collection that failed to open was reported as "not initialized". Both now say the index is unreadable and show its size on disk, the last successful update and when the index was last written (a write after the last successful update points to an interrupted update), with the two ways out: `repair-db`, or `update-db --force-rebuild`. `update-db` and `zotero_semantic_search` stop with the same message instead of ChromaDB's error alone. `--force-rebuild` on an unreadable index renames it to `chroma_db.broken-<timestamp>` and starts a new one, rather than deleting the collection through ChromaDB, the same code that cannot read it.
- **The HuggingFace embedding provider loads each model once per process** (#726). ChromaDB rebuilds the embedding function from the persisted collection config on every upsert and query, which reloaded the `SentenceTransformer` each time and, on CUDA, exhausted GPU memory during `update-db`.
- **`zotero_search_by_citation_key` finds keys in web mode that the API's quick search misses** (#740). The web API's search indexes neither `citationKey` nor Extra, so a key that appeared nowhere else was reported as not found. The lookup now falls back to scanning top-level items, which is slower on large libraries only when the quick search misses.
- **Japanese text in the Zotero Agent panel uses Japanese glyph forms** (#755, from #756). The font stack named Chinese fonts ahead of the system's own, so shared characters such as 骨 and 直 took Chinese forms; CJK text now falls back by the system's locale.
- **The Zotero Agent installs its bridge on Windows when Node and the profile path both contain spaces** (#730, #737). Node's own `npm.cmd` is now run through `node` instead of `cmd.exe`.
- **While an answer streams, the chat lets go of the bottom at your first upward scroll.**
- **Local mode says Zotero is not running** when a write fails with a refused connection, instead of a bare `[Errno 61] Connection refused` (#744, #745).
- **No more leaked database copies on Windows** (#736). The local SQLite snapshot is cleaned up when the process that made it is gone.
- **A dry run of batch annotations reads metadata from the selected group library** in local-only mode (#731, #732).
- **Highlights on rotated pages land in the right place, and a quote that repeats on a page is highlighted once** (#720).
- **Batch Extra edits fetch each item once** instead of twice: a separately built read client made every item look like it came from another backend.
- **`docs/cli.md` examples that did not parse** (`coll list`, `tags list`, `library info`) now name the real commands.

### Deprecated

- **`zotero-cli tags` is deprecated in favor of `zotero-cli batch`**, which takes the same selection and does the same tag edits with `--add-tags` / `--remove-tags`, plus Extra edits. `tags` still works and prints a notice on stderr; it will be removed in 0.17.

## [0.15.1+arne.69] - 2026-10-31

### Changed

- **Upstream 0.15.1 merged in** (from 0.13.2). Among others: `config.json` is never rewritten from a file that cannot be parsed, and always written atomically (#646); temp copies of `zotero.sqlite` are named per process and cleaned up (#645; 52 were left in Temp, 1.8 GB); the DOI duplicate check reads SQLite in local mode instead of pinning Zotero's CPU (#704); notes keep `<`, `>` and `&` (#664); bibliography exports include every item (#662); SQLite quick search ignores accents (#663); PubMed IDs for `zotero_add_item`; OpenAlex repository copies in the open-access PDF cascade. This build keeps its own startup update (`background_update`) and its two-computer code; the Windows update lock is upstream's, released explicitly before closing.

### Fixed

- **Relabelling no longer rewrites the whole library at every update.** ChromaDB 1.x merges metadata on `upsert` and `update`, so a key left out of the new metadata kept its old value. The relabeller removes `structure_gemini` after a successful Gemini retry and headings that no longer apply by leaving them out, so they never went away: all 1,693 labelled papers kept `structure_gemini: "failed"` from an earlier Gemini outage, and each index update relabelled all of them, rewrote about 125,000 passages and published them to the shared log. Upserts now replace a record's metadata (a re-indexed passage no longer inherits the old passage's labels either), and the relabeller's metadata updates replace too.
- **The shared index log stays small.** A paper is not published again when its newest publication already holds exactly its entries; the monthly file moves on past 200 MB; `zotero-mcp shared-store compact` keeps only the newest version of each paper (old files to `~\.config\zotero-mcp\_archive\index-log-<time>\`), and updates compact by themselves once a log is over 1 GB and mostly superseded. The other computer rereads the folder when files disappear. (The PC's log had grown to 8.5 GB in 11 days, with the whole library published 26 times.)
- **Semantic search works while another process writes the index.** ChromaDB is not process-safe: during the startup update or `maintain --index`, the server's cached vector index stopped matching the stored records and searches failed with "Error executing plan: Internal error: Error finding id". The search now reloads the index and retries once.
- A test left `ZOTERO_LOCAL` set for the rest of the run, which made `test_webdav` fail depending on order.

## [0.13.2+arne.68] - 2026-10-26

### Changed

- **Each computer writes its own record files in the shared folder** (`zotero_mcp/computer_files.py`; `docs/maintenance.md`, "Two computers"). The fetcher's state, log and free-tier counters, the metadata audit's state, the maintenance record and the citation graph were one file each in `state_dir`, written by both computers, so OneDrive kept conflict copies or one computer's changes were lost when both ran. Now each computer writes `<name>-<COMPUTER>.<ext>` (for example `fulltext\state-DESKTOP-A.json`) and reads every computer's copy: per paper the newer record wins (entries carry an `_updated` time), dates such as the last maintenance run take the latest, the log is read in time order and the counters are added up. The old shared files are still read, never written. Without `state_dir` nothing changes.

## [0.13.2+arne.67] - 2026-10-19

### Added

- **One search index on two computers** (`zotero_mcp/index_sync.py`; `docs/two-computers.md`, "One index on both computers"). With the shared store on, every paper whose index entries change is published whole to a log in the shared folder (`state\index\`: ids, texts and metadata; embeddings in the store), and before each update a computer copies what the other published more recently. Newest wins, both ways. Before reading a new or changed paper a computer claims it; the other leaves a paper claimed in the last 30 minutes to its peer and copies it once published (an unfinished claim runs out), and leaves a paper whose file Zotero has not downloaded yet to the computer that has it, for up to a day. A search waits for the update when the other computer published something new. New commands: `shared-store publish [--all]`, `pull`, `mirror` (replace this index by an exact copy from the log, without reading PDFs or calling an API; the old index goes to `_archive`), `compare [--fix]`; `status` reports the log. Writes made while copying are not published back; files are per computer and append-only, and a half-synced line waits for the rest. Index entries are now noted per paper on every write (add, upsert, delete, prune, metadata update, reset) so they can be published.

## [0.13.2+arne.66] - 2026-10-19

### Changed

- **Gemini's cached answers in a few files instead of thousands.** With the shared store on, Gemini's readings of a PDF's structure (headings, sections, printed pages) and the metadata audit's readings of a PDF are kept like the embeddings: each computer appends to its own file per month (`state\structure\answers\<COMPUTER>-<YYYY-MM>.jsonl`, `state\metadata\pdf-answers\…`), one line per answer. arne.65 wrote one small file per answer (about 3,400 so far), which OneDrive syncs slowly. `zotero-mcp shared-store pack` puts those into pack files and moves the loose ones out of the shared folder to `~\.config\zotero-mcp\_archive\`; answers kept one file each are still read. A half-synced line waits for the rest; two processes on one computer take turns writing (a lock file outside the synced folder), for the embeddings too.

### Fixed

- `shared-store status` named a computer by the part of its name before the first hyphen ("DESKTOP" for DESKTOP-A); `--coverage` reports its progress.

## [0.13.2+arne.65] - 2026-10-19

### Added

- **A shared store of embeddings and section labels for two computers** (`zotero-mcp shared-store setup`, `zotero-mcp shared-store status [--coverage]`; `docs/two-computers.md`). Each computer keeps its own search index, but before a passage is sent to the embedding API the store in the shared folder (`state_dir`, see `share-state`) is asked for it, and what one computer embeds is added for the other. Gemini's answers about a PDF's structure (headings, sections, printed pages) are kept there too, so both computers label the same PDF the same way and pay for it once. `setup` turns it on (`semantic_search.shared_store.enabled` in config.json, with a backup) and copies this computer's embeddings and structure answers into the shared folder without any API call. Files are per computer and only appended to (`<COMPUTER>-<YYYY-MM>.vec`; exports in parts of about 200 MB), so a synced folder never has two writers for one file; a record the sync has only half delivered waits until the rest arrives. Vectors are kept as float32, as ChromaDB keeps them, so nothing is lost. Any failure of the store falls back to the API. Index updates report how many passages came from the store. Off unless turned on; `ZOTERO_MCP_SHARED_STORE=0` turns it off for one run.
- **`scripts/sync.cmd` and `scripts/install_build.py`**: comparing the working copy with GitHub and the installed build with the working copy, on opening the folder in VS Code (`extras/vscode/`).

## [0.13.2+arne.64] - 2026-10-18

### Fixed

Three fixes taken from upstream (0.13.3 and 0.14.0):

- **Book chapters and conference papers added by DOI keep their book or proceedings title.** CrossRef's container title went to `publicationTitle`, which those item types do not have, so it was dropped (upstream #638).
- **Name particles and suffixes survive CSL JSON import.** "van der Maaten" no longer becomes "Maaten"; suffixes such as "Jr." are kept (upstream #639).
- **Inlined `data:` images are removed from HTML snapshots before extraction**, so a saved web page with embedded images no longer floods the full text and the index with base64 (upstream #670).

## [0.13.2+arne.63] - 2026-10-11

### Added

- **Only the passages that answer a question.** `zotero_read_paper` and `zotero_get_item_fulltext` take a `question` (and `passages`, default 8): the whole document is searched and only the passages that answer it come back, about 1,000 characters each with their pages, after the metadata and abstract. Ranked on your computer by keyword match, then by the re-ranker set up for semantic search (Voyage here). Running heads, page numbers and the reference list are left out. For reading many papers for one answer without filling the conversation.
- **Parallel as a second web search** in the fetcher's web step (`PARALLEL_API_KEY` in `keys.env`; paid, about half a cent a search, capped by `parallel_monthly`, 500 by default). Tested on the 147 papers no source had found: Parallel found a right PDF for 13 (author copies on personal and society pages, repositories), Tavily for 2.

### Fixed

- **Shadow libraries are never used.** The fetcher promised it, but nothing enforced it: a Sci-Hub link from a web search would have been downloaded if its title matched. Links to Sci-Hub, Library Genesis, Z-Library, Anna's Archive and the like are now refused at every step, redirects included. (None was ever attached.)
- **Four kinds of wrong PDF that passed the checks** (found in the test above):
  - the main title followed by another subtitle ("Adolescents as active managers of their own psychological needs: The role of …" for "…: Examining the role of …");
  - a registry or PDF title that only shares the words before the colon ("Materialistic values and goals" for "Materialistic values: Their causes and consequences");
  - a near match meant for a scan's misread letters that swaps a word ("The validation of the Norwegian … Scale" for "Manual of the … Scale");
  - slides about the paper, and a book's later edition (every copyright year well after the item's).
  Replayed on the 51 PDFs the fetcher attached by title that are still in the library: none is newly rejected.

## [0.13.2+arne.62] - 2026-10-09

### Changed

- `zotero_read_paper` says when to use it: whenever an answer leans on a paper found outside the library (web search, Consensus, Scite, PubMed, Scholar) of which only the abstract or a snippet was seen. Claude then reaches for the full text on its own instead of settling for abstracts.

## [0.13.2+arne.61] - 2026-10-09

### Added

- **Read a paper without adding it to Zotero** (`zotero_read_paper` in Claude). Give a DOI, or a title with author and year: the paper is found with the same sources and checks as the PDF fetcher (open access, the publisher on the university network, Google Scholar, the web search; the visible browser step is left out), and Claude gets its text to read. For a paper you are only curious about, one for someone else's project, or one not worth keeping. Nothing is written to the library or to the shared records; the whole text is kept in a `.txt` file in the temp folder, and `save_to` also saves the PDF to a folder you name.

## [0.13.2+arne.60] - 2026-10-09

### Fixed

- **103 papers stayed "not checked" at every run.** A paper without a DOI counted as not checked whenever Semantic Scholar refused (it often does without an API key), even when Crossref and OpenAlex had answered. Now Semantic Scholar alone refusing no longer holds a paper back: the paper is checked, and when neither main registry knows it, it is reported as unknown. Only Crossref or OpenAlex not answering leaves a paper for the next run.

## [0.13.2+arne.59] - 2026-10-09

### Fixed

- **Wrong PDFs that still got through** (found by checking every PDF of the 09-10-2026 run by hand): a chapter that cites the book ("Bandura, A. (1986). Social foundations of …"), a paper whose title merely contains a short title ("What is conscientiousness …" for the chapter "Conscientiousness"), another author's chapter with the same title, a book's front matter, a publisher's preview, a supplement, a three-page book review and a reading list. Now:
  - a title right after a year is a citation, not the title;
  - a book's or chapter's title, and any title of five words or fewer, must stand on lines of its own near the top;
  - an article's first author must be in the byline at the top, not only cited further down (affiliation letters stuck to the name still count);
  - previews, front matter, tables of contents and sample chapters (by their address), supplements, and "books" of fewer than 10 pages are never attached;
  - a PDF the item's URL field points to directly is checked like one found by title.
  Replayed on all 93 PDFs the fetcher attached: every wrong one is rejected, every right one still passes.
- A PDF the fetcher attached and you moved to the trash is not attached again: its link is remembered as wrong at the next run.
- When a wrong PDF is replaced, its "PDF to check" note goes too; a `fulltext/check-pdf` finding whose PDF is gone is cleared at the next check.
- **The chapter cutter cuts only when it is sure of the pages.** At least 80 % of the chapter's page numbers must be printed in the book, and the chapter's title must stand at the top of the cut's first page (or of the page before it, a title page of its own, which is then included). Otherwise nothing is cut and the item is tagged `fulltext/whole-book`. Before, another edition of a handbook gave another chapter on the same page numbers (Nakamura & Csikszentmihalyi got Van Allen's chapter, three times), a wrong Pages field gave a chapter cut mid-way (Ritchie & Spencer), and a chapter starting on an unnumbered title page lost that page (Renninger & Hidi). Tried on those books: each one is now cut right or left alone.
- Metadata check: BMJ's habit of registering an article as "<title>: Table 1" no longer reads as a table's DOI, and an Oxford Handbooks chapter (registered as a "book" under the chapter's own title) no longer reads as the whole book's DOI. A DOI ending in `.supp`, `.t001` and the like is still a part's.

## [0.13.2+arne.58] - 2026-10-09

### Changed

- **Only the chapter stays on a chapter's item.** Once the chapter is cut out of a whole book, the book's PDF moves to the book's own item when the library has one without a PDF, otherwise to Zotero's trash (recoverable). A book PDF with your annotations stays. Chapters cut earlier, with the book still beside them, are tidied the same way at their next check.

## [0.13.2+arne.57] - 2026-10-09

### Fixed

- A scanned thesis whose OCR misreads letters of its title ("Pensistence") counted as another work in `fetch-fulltext --recheck`. A title nearly letter for letter at the top (90 %) now counts too.

## [0.13.2+arne.56] - 2026-10-09

### Fixed

- **The metadata check's attachment check was as lenient as the web search** (most of the title's words on the first pages counted as "this paper"). Every attached PDF, whoever attached it, now counts as this work only with its title printed at the top of the first page or its DOI there. Otherwise Gemini reads its first page (once; cached), or a different DOI on top shows it is another work: tag `fulltext/check-pdf`, a note, and the right PDF is searched for.
- The checking rules' version is raised, so the next Check & complete checks every paper's PDF again with these rules.

## [0.13.2+arne.55] - 2026-10-09

### Fixed

- **The web search attached other works** (a review with the same words, a protocol, a later paper by the same author, a CV, a publisher's preview): a PDF was accepted when most of the title's words and the first author were on its first pages. A copy found by its title (web search, Scholar, ResearchGate, your own browser) now needs its title printed as such at the top of the first page, or the item's DOI there; another DOI on top, or a preview, rejects it. Checked against every PDF attached so far: the Google Scholar ones pass, the wrong web-search ones do not.
- `fetch-fulltext --recheck [--since DATE] [--dry-run]` checks the PDFs attached by their title again with these rules: another work goes to Zotero's trash, its paper loses the fetched tags and is searched again; that link is not tried again.

### Added

- **Reports** (Actions & Tags, Tools menu; `zotero-mcp reports`): a small window with the last 10 checks (when, what, result), newest first; double-click one to open its report and the PDF search's report of the same run, also after the progress window was closed or Zotero restarted. All reports are kept, in the shared `state` folder. The review window is available any time through Tools › Review suggested metadata.

### Changed

- **A preprint or manuscript the fetcher attaches is replaced by the published version later.** It stays tagged `fulltext/preprint` or `fulltext/accepted-manuscript`; the published version is looked for again after the retry period (30 days, not at every run, to spare the search credits) and replaces it when found. Before, only a preprint the metadata check recognised by its text was followed up.
- **The review window fills while it loads**: each paper appears as soon as its suggestions are read ("loading 8 more…"), so you can start at once.

## [0.13.2+arne.54] - 2026-10-09

### Changed

- **The review window shows only what a suggestion changes**: "Keer, Hilde → Van Keer, Hilde", "+ Parker, Philip", "2023 → 2025", '+ ": A Systematic Review"', instead of both full values. The selected suggestion's full values below are marked: what goes struck through in red, what comes in green.

## [0.13.2+arne.53] - 2026-10-09

### Added

- `extras/`: the Zotero actions (`zotero-mcp-actions.yml` and each script) and the PowerShell scripts they start, kept with the code they call.

### Changed

- The actions no longer name a user folder: without `USERPROFILE` they use the system's home folder. The actions file has no author. The maintain script no longer has the removed `-Monthly` switch.

## [0.13.2+arne.52] - 2026-10-09

### Added

- **Two computers**: `zotero-mcp share-state FOLDER` keeps the record of the checks (what was checked when, searches in vain, rejected suggestions, learned decisions, the citation graph, free-tier counters) in a folder both computers sync, such as OneDrive (`"state_dir"` in config.json). Each computer then skips what the other already checked. Record files are written in one step, so a synced folder never holds half a file.

### Changed

- The import action runs on the computer where a paper is added, laptop or PC, and skips papers that arrive there by sync from the other one, so a paper is checked once. The removed monthly action is no longer in `zotero-mcp-actions.yml`.

## [0.13.2+arne.51] - 2026-10-09

### Changed

- **The online year becomes the issue year by rule**, without a review: when Crossref's record, found by the item's DOI, has both dates and the item's volume and issue, and the item has the online year. Tagged `auto-corrected` with a note, like other corrections. A large year gap, or another volume or issue, stays a suggestion.
- A paper whose suggestions are all settled at its next check (by this rule or by the registry) leaves the review list; its old suggestions note goes to the trash.
- The checking rules' version is raised, so papers checked before are checked once more at the next Check & complete.

## [0.13.2+arne.50] - 2026-10-09

### Fixed

- **Checking the whole library seemed to check only the unchanged papers.** The window listed the unchanged papers first and then spent minutes on their retraction check, with no progress shown ("0 of 0"), before the papers that needed a check began. Now the papers to check come first in the list and are checked first; the unchanged ones follow, with "Retractions and corrections · 12 of 316" as progress.
- Papers checked fully no longer get a second retraction check the same month: the check records the notices it read.
- A wrong-PDF finding that no longer holds now takes its note with it (to the trash) instead of adding a "Checked again" note.

## [0.13.2+arne.49] - 2026-10-09

### Fixed

- A `metadata/accept` or `metadata/reject` tag put on the suggestions note, rather than on the paper, was ignored; it now counts for the paper.

## [0.13.2+arne.48] - 2026-10-09

### Added

- **Review suggested metadata with a click**: a window lists each suggested change under its paper (yours, suggested, why); Accept changes the field in Zotero at once, Reject keeps yours. Opened by the progress window's Review button, its right-click menu, or the Actions & Tags action "7 Review suggested metadata" (item, collection and Tools menu); `zotero-mcp metadata-review` in a terminal. Undecided suggestions stay waiting.
- The report lists the papers still waiting for review from earlier checks.

### Changed

- Progress window: clicking a count shows only those papers (again, or "Show all": all); clicking a column heading sorts by it.
- `metadata/accept` and `metadata/reject` tags are also carried out by a run in which every paper was unchanged.

## [0.13.2+arne.47] - 2026-10-09

### Added

- **Citation graph**: each paper's references from OpenAlex, kept in `citations.json`. Claude's tool `zotero_citations` answers what a paper cites, which papers in the library cite it, which share its references, and which works a collection cites often that are not in the library. New papers are added on import and by Check & complete; `zotero-mcp citations --update` adds the rest of the library. See `docs/citations.md`.
- **Merge certain duplicates** (Actions & Tags, Tools menu): merges papers with the same DOI or ISBN, type and title with Zotero's own merge, after a preview; keeps the checked or oldest copy and fills its empty fields. Other pairs with the same DOI get the tag `duplicate/check`.

### Changed

- The fetcher's Chrome window stays minimised and comes forward, with a sound, only when a captcha or login needs you (`"browser_minimized": false` keeps it visible).
- An item with several PDFs (a supplement, or a manuscript beside the published version) is fine when one of them is the right paper in its published form.

## [0.13.2+arne.46] - 2026-10-09

### Changed

- One button, "Check all unchanged anyway"; a single paper, or several selected, from the right-click menu ("Check anyway", "Check these N unchanged anyway").

## [0.13.2+arne.45] - 2026-10-09

### Changed

- Unchanged papers are not checked again, also when selected on their own (arne.44 always checked up to 5). If every paper chosen is unchanged, a small window says when it was last checked, with "Check again" and "Cancel". Otherwise the progress window shows "Unchanged, checked 09-10-2026" for those papers, a button "Check N unchanged again" ("Check N selected again" when you select some), and "Check again" when you right-click one.

## [0.13.2+arne.44] - 2026-10-09

### Changed

- Up to 5 selected papers were always checked fully; in a larger run the window said which papers were unchanged, with a "Check anyway" button.

## [0.13.2+arne.43] - 2026-10-09

### Changed

- **No monthly check** (arne.41-42): a manual run does its work instead. Check & complete on many papers checks fully only what needs it: papers never checked, changed in Zotero (the paper or an attachment), checked under older rules, or recent articles still without volume or pages a month later (online first). Unchanged papers get only a retraction and correction check, at most monthly. `maintain --all` checks everything.
- Papers searched in vain, for a PDF or for the published version of a manuscript, are searched again once the last search is more than 30 days old (PDFs put online later), and not before.
- The "Monthly check" action is disabled in `zotero-mcp-actions.yml`; delete it in Actions & Tags if you like.

## [0.13.2+arne.42] - 2026-10-09

### Fixed

- The monthly check counts a PDF added or replaced by hand as a change to its paper (Zotero does not change the paper's own modified time then), so that PDF is checked too.

## [0.13.2+arne.41] - 2026-10-09

### Added

- **The monthly check**, without Claude: the Actions & Tags action "Monthly check" (event Main Window Load) starts `maintain --monthly` five minutes after Zotero opens, at most once a day; it stops at once unless due (30 days, or papers left over). Papers changed since their last check get Check & complete (at most 400 per run, the rest the next day); the others with a DOI only a check for new retractions (tag `retracted`, note) and recent corrections (note); then the search index. Papers searched in vain recently are not searched again.

### Fixed

- A paper that was not checked because a registry did not answer is no longer remembered as checked.

## [0.13.2+arne.40] - 2026-10-09

### Added

- **New papers are checked when they are added**, without Claude: the Actions & Tags action "Check & complete new papers" (event Create Item) waits until no paper has been added for a minute, then checks the metadata, fetches a PDF if Zotero did not attach one, checks again, and updates the search index with passage labels. Papers added together share one run; the window stays minimised unless something is left for you.
- `maintain --index` (the index update as a last step, under the index's update lock) and `--quiet` (the minimised window that closes itself when nothing is left).

### Fixed

- No second PDF when Zotero attaches one while the fetcher is still searching (the Connector saves the page's PDF a little after the item).

## [0.13.2+arne.39] - 2026-10-09

### Changed

- The `zotero_metadata_audit` tool (Claude in a chat) describes the new attachment findings (wrong PDF against the right paper in another form) and no longer fetches replacement PDFs within the call, which has to answer within a minute; `zotero_fetch_fulltext` or "Check & complete" does that.

## [0.13.2+arne.38] - 2026-10-09

### Changed

- **A wrong PDF and the right paper in another form are told apart.** Only another paper attached is a wrong PDF (tag `fulltext/check-pdf`, a note, replaced when the right one is found). An accepted manuscript, preprint or proof gets a version tag (`fulltext/accepted-manuscript`, `fulltext/preprint`, `fulltext/proof`) and no note; a fetch quietly swaps in the published version when it finds it. A whole book attached to a chapter has its chapter cut out, or gets the tag `fulltext/whole-book` when the pages cannot be found. Earlier `fulltext/check-pdf` tags on such items are replaced by these at the next check.
- In the progress window they are a grey "◐ other form" count, not a warning, and not in "To do"; the report lists them as "Right paper, other form".

## [0.13.2+arne.37] - 2026-10-09

### Changed

- **The metadata report** is ordered by what needs you: a summary, then suggestions to review, wrong PDFs, papers not checked or unknown to every registry, other findings grouped ("Article without a DOI (8): ..."), and last what was changed automatically. Added abstracts are listed as "Abstract added" instead of their first 120 characters. Dates as dd-mm-yyyy.
- The terminal says "filled" and "corrected" when the changes were made, and the right-click actions print the number of papers instead of their keys.
- The progress window no longer scrolls by itself.
- The right-click actions start the PowerShell window minimised from the first moment, so no terminal flashes up before the progress window (if Zotero cannot start it hidden, it opens as before and minimises itself).
- The browser action is now called "Check PDF (browser only)" and comes last in the menu (Actions & Tags: "Sort menu by" Name).

### Fixed

- **Published PDFs reported as accepted manuscripts.** Taylor & Francis's open-access licence ("allow the posting of the Accepted Manuscript in a repository"), an old journal's "Accepted manuscript received ...", and a repository's general cover text no longer count; a publisher's cover page ("To cite this article", "Journal homepage") means the published version unless the PDF says outright that it is a manuscript. Of the 10 PDFs flagged in the first run, 5 were published versions (Sæther 2025, Thomas 2025, Sando 2025, Wood 1976, Pyke 2025); 4 are real accepted manuscripts and 1 a proof.
- The note on a paper with a wrong PDF is shorter and says which action replaces it; notes are dated dd-mm-yyyy.
- A chapter's PDF that opens with its book's title page was reported as another work (Ellis 1984, Chatzipanteli 2022); a whole book attached to a chapter (Nakamura 2009) is now reported as the whole book, so the chapter is cut out. An earlier wrong finding is cleared: the tag `fulltext/check-pdf` is removed, with a note.
- A PDF whose title is the item's main title ("Unmasking the face" for "Unmasking the face: A guide ...") was reported as another work.
- A book series was proposed as a chapter's book title ("Evolutionary Psychology"); "Oxford handbook of ..." against "The Oxford Handbook of ..." is no longer a difference.
- First names in capitals from Crossref ("JOSJE M.") are filled as "Josje M.".
- Abstracts that are not abstracts are no longer filled: a citation line ("HARTER, SUSAN. Pleasure derived ..."), a table of contents ("Preface ... Foreword"), or text that lost its first letters ("e authors argue"). A heading glued to the text ("IntroductionSELF-DETERMINATION") is removed.
- "an erratum", and no nested brackets in sources ("Gemini, reading its PDF").

## [0.13.2+arne.36] - 2026-10-09

### Added

- **Progress window for `maintain`** (`--window`, used by the right-click actions): the steps (1 Metadata › 2 PDFs › 3 Metadata again) and every paper with its metadata (filled, corrected, to review, wrong PDF, OK) and its full text. Before, the metadata steps only showed in the terminal.
- Both progress windows are quieter: short statuses, coloured counts that explain themselves on hover and select their papers on click, and a **To do** panel (only when something is left for you) with the button for each task next to it.
- **Metadata only**: `maintain --no-fetch` and the right-click action "Check metadata only": no PDFs downloaded; wrong PDFs are tagged, not replaced. `"maintenance": {"new_items": true, "fetch": false}` does the same for new items at startup.
- Four right-click actions with clearer names: **Check & complete**, **Check metadata only**, **Fetch PDF only**, **Fetch PDF in browser** (scripts in `zotero-actions/`).

### Fixed

- A browser-only run searched several papers at once in the one Chrome window; it now takes one at a time.

## [0.13.2+arne.35] - 2026-10-09

### Added

- **Bot checks only your own browser passes** (Cloudflare's "Performing security verification" on Taylor & Francis and others): the fetcher's Chrome no longer waits five minutes on a check that restarts after every attempt. After a few seconds the paper is marked "needs your browser" (not tagged not found) and the site is not opened again in that run. The progress window's **Open N in my own browser** opens the links as ordinary tabs in your default browser and attaches the PDFs you save to Downloads once they match the paper (title, first author, DOI). Without the window: `fetch-fulltext --from-downloads --items ...`.
- Links that refused a plain download are remembered, so a later browser-only run starts with them.

### Fixed

- A browser-only run (`--steps browser`, the window's browser button) tried each paper with the browser twice.
- The "Fetch full text with browser" action runs only the browser step; it also ran all the other steps first (change in `zotero-fetch.ps1`).
- The attachment check no longer reports "another work" when Gemini reads a different title but the item's DOI, or its first author and year: a translated or reworded title of the same work.

## [0.13.2+arne.34] - 2026-10-09

### Added

- **Attachment check** in the metadata audit: another work, an accepted manuscript, preprint or proof of a published article, or a whole book attached to a chapter. Found by the first pages (free); Gemini only reads a PDF when neither the item's DOI nor its title is there. With `--apply`: tag `fulltext/check-pdf` and a note; a stray PDF moves to its own item when that has none; otherwise the right PDF (for a manuscript or proof: the published version) is fetched and replaces the wrong one, which goes to Zotero's trash; a chapter is cut out of an attached book. `--no-attachments` skips it.
- **`maintain`**: metadata (with the attachment check), then PDFs for the items without one, then the metadata again for the items no registry knew, now with their PDF. For `--items`, `--collection`, `--since` or `--new`; with `"maintenance": {"new_items": true}` in config.json it runs for new items before the index update at every start of Claude Desktop. Right-click action "Check metadata and fetch PDFs".
- The audit looks up **a DOI printed on the item's PDF or in its saved web page** (citation tags) before a title search, and Gemini's reading of the PDF can give the DOI too. A saved web page's citation data gives proposals for items no registry knows.
- The fetcher replaces PDFs marked wrong, and for a manuscript or proof accepts only the published version.

## [0.13.2+arne.33] - 2026-10-09

### Added

- **Progress window for fetching full text** (`fetch-fulltext --window`, used by the right-click actions): every paper with its status, like Zotero's Find Full Text; a button to search the papers not found with the browser, available as soon as one is not found and at the end; a summary and the report at the end; double-click shows the paper in Zotero. The terminal opens minimised with the details.
- **Several papers at once** (`--workers`, default 4). Requests to one site stay spaced out; the browser step runs after the other steps, one paper at a time, and a paper is only tagged not found after it.

## [0.13.2+arne.32] - 2026-10-09

### Fixed

- A whole book's outline (more than 150 bookmarks) is no longer sent to Gemini for checking: in the model comparison the 947 bookmarks of an Oxford handbook took minutes and failed for two of three models. Such outlines are used as they are.

## [0.13.2+arne.31] - 2026-10-09

### Added

- **`compare-gemini`**: runs several Gemini models and thinking levels (default: 3.8 Flash low, 3.6 Flash minimal, 3.5 Flash-Lite minimal) on the same random items, for both Gemini tasks (headings and PDF reading), and reports quality (agreement with the reference model and with the item's Zotero fields) and cost (tokens, thinking, seconds, dollars per 1,000 items), with a side-by-side view per item. Answers are cached.
- `semantic_search.structure.gemini_thinking` sets the thinking level (default `low`) for the passage labels and the metadata audit.

## [0.13.2+arne.30] - 2026-10-09

### Added

- **Gemini usage per run.** `relabel-index` and `metadata-audit` print, and the relabel report records, the Gemini calls, input and output tokens (thinking included, which is billed as output) and the cost at list price.

### Fixed

- **A full `relabel-index` run on 7 October had 792 Gemini failures** (after about 370 successes): the items fell back to bookmarks or rules and were marked done. Now a per-minute limit is waited out, a run stops asking Gemini after 10 failures in a row and says why, and items labelled without Gemini because it failed are asked again in the next run.
- The note for a failed Gemini call includes the error text, not only its type.

### Changed

- Passage labels are version 3, so the next run labels every item again with arne.29's bookmark check and section guide. Gemini answers are cached per file, prompt and model only (no longer per label version), so a later version change does not ask Gemini again for unchanged prompts.

## [0.13.2+arne.29] - 2026-10-07

### Added

- **Passage labels: Gemini checks the bookmarks** (with `--gemini`): which bookmarks are headings, their level, and the part of the paper each opens, also when the heading does not name it ("Research design" is Methods, "Case studies" Results). Text and page stay the bookmark's own; without an answer the bookmarks are used as before. A short prompt: a few hundred tokens per paper.
- **Sections by kind of paper**, in both Gemini prompts: APA papers without an Introduction heading, papers with several studies ("Study 2" has its own Method and Results; a General Discussion is Discussion), systematic and scoping reviews (search, eligibility, screening, data extraction and risk of bias are Methods; study characteristics and the synthesis are Results), qualitative studies (Findings, Themes), theoretical papers and books. The rules know the review terms too.

### Changed

- Gemini answers for passage labels are cached per file and exact prompt, so the bookmark check and the candidate list of one file never share an answer.

## [0.13.2+arne.28] - 2026-10-07

### Fixed

- **Metadata audit: a registry that does not answer is no longer "no record".** The third full run of the day found no record for 207 items instead of 147: OpenAlex's free daily allowance (about 1,000 title searches) was used up and its HTTP 429 answers were taken for "not found". Such items are now reported as *not checked this time* and counted separately, and a service that refuses three times is not asked again in that run.
- **Metadata audit: title searches start with Crossref**, which is free, before OpenAlex and Semantic Scholar.
- **Passage labels: headings are located in reading order on their own page**, each after the previous one, preferring a line where the heading stands alone. A "Methods" in a structured abstract, a "Phase 1" under Results or two-column text no longer put headings in the wrong place.
- Bookmarks: a top bookmark holding only the title is dropped, bookmarks for tables and figures are ignored, and bookmarks that stop halfway are not used (Gemini or the rules take over). Invisible characters are removed from bookmark text.
- PDF page labels that cannot be printed numbers ("image 1", a page 0, numbers running backwards, roman numerals throughout an article) are not used.

### Changed

- Metadata audit: a publisher is only filled in, never changed ("Association for Computing Machinery" was about to become "ACM"). Semantic Scholar no longer changes journal names. No correction or proposal makes a field worse: an abbreviated journal, a name without its first words ("Advances in Neural ..."), a lost accent or supplement, a year as the volume, or pages such as "Article # 3". "04" and "4" are the same volume.
- Passage labels: "Abstract: ..." lines and "Appendix 2: ..." headings get their section; "Study 1" and "Experiment 2" headings have no section of their own. Gemini is told to leave out boxed summaries ("Practice points", "Key points"). Labels are version 2, so items labelled before are labelled again.

## [0.13.2+arne.27] - 2026-10-07

### Changed

- **Gemini model fixed at `gemini-3.8-flash`** (passage labels and the metadata audit's PDF reading) instead of the `gemini-flash-latest` alias, which Google moves to each new Flash model; cached answers would otherwise mix models. 3.8 Flash also costs half of 3.5 Flash. Set `semantic_search.structure.gemini_model` to choose another.
- **No `temperature` in Gemini requests** (deprecated by Google in July 2026; newer models reject it) and thinking set to `low`: these are reading tasks, and thinking tokens are billed as output. A model without thinking levels is asked again without one.

## [0.13.2+arne.26] - 2026-10-07

### Fixed

- An APA introduction's level-2 headings ("The Present Study") are no longer nested under the Abstract when the repeated title was not found as a heading.

## [0.13.2+arne.25] - 2026-10-07

### Changed

- **Passage labels: introductions with or without a heading.** In papers with a Method heading, text before Method that no heading names is labelled Introduction. APA papers without an "Introduction" heading start it after the abstract; papers that do have one start it there, so keywords or highlights above it keep their label. Topical headings before Method ("Risky play and development") count as introduction. Gemini's prompt describes both styles.
- **Combined headings carry both sections**, as in the JATS standard: "Results and Discussion" is `section` Results and `section_2` Discussion. A search filtered on Discussion finds it, and the Location line shows "Results & Discussion".
- Section names in Portuguese and Italian for the rules fallback.
- Gemini's heading answers are cached per prompt wording, so a changed prompt is asked again.

## [0.13.2+arne.24] - 2026-10-07

### Fixed

- **`relabel-index` and the metadata audit found "no PDF" for almost every item while Zotero was open.** Zotero's database is then read from a snapshot whose connection may only be used by the thread that opened it; the parallel workers got an error that was taken for a missing PDF. All database calls of these two commands now run on one dedicated thread (`SerialReader`).

## [0.13.2+arne.23] - 2026-10-07

### Fixed

- **`relabel-index` and the metadata audit could not read most PDFs on Windows.** The child process that reads a PDF printed its result with characters a Windows console cannot encode ("ﬁ", "≥"), and crashed. Its output is now plain ASCII JSON. A trial run reported such PDFs as "no PDF"; the report now says "PDF unreadable" when the file exists but could not be read.
- `relabel-index` reads the attachment that was indexed (its key is in the passage metadata) rather than the item's first PDF.
- Adding a book by ISBN uses the optional Google Books key from `keys.env`.
- Gemini's "automatic function calling" warning no longer appears.

## [0.13.2+arne.22] - 2026-10-07

### Added

- **Metadata audit: Google Scholar's "Cite" as a last resort.** For items no registry knows and without a readable PDF. It goes through SerpApi (two searches per item, keeping 60 a month for the full-text fetcher), and only gives proposals.

## [0.13.2+arne.21] - 2026-10-07

### Added

- **Printed page numbers, headings and chapters for search passages** (`zotero-mcp relabel-index`).
  - Each passage gets its page number as printed, from the PDF's page labels, the numbers in its header or footer (by majority vote) or the item's Pages field. It also gets its heading path and, for books, theses and reports, its chapter.
  - Headings come from the PDF's bookmarks or, with `--gemini`, from Gemini judging candidate lines (set apart by size, weight, typeface, colour, capitals, numbering or space) against the printed table of contents. Rules are the fallback.
  - Search results show "p. 379 (PDF p. 3)" and the heading in the Location line.
  - Metadata only: nothing is re-embedded. New items are labelled after each index update with `semantic_search.structure.enabled`.
  - See [Printed pages, headings and chapters](docs/passage-labels.md).
- **Reference lists are recognised by their shape** and left out of normal searches; a search filtering on `section` keeps them.
- **Metadata audit:**
  - **More sources:** other DOI agencies (mEDRA, JaLC…) through doi.org; Semantic Scholar as a second title search; Google Books for books.
  - **The item's PDF read by Gemini** when rules find nothing. It confirms differences, decides author proposals (full author list), and gives proposals for items no registry knows.
  - **Retractions** from Crossref's Retraction Watch data are flagged and tagged `retracted`.
  - **Learning:** a kind of proposal you decide the same way 10 times is decided for you from then on.

### Changed

- **Metadata audit: the DOI is no longer trusted blindly.** Nothing is compared when the DOI's record is one of these:
  - a correction or retraction notice;
  - a table or figure;
  - the whole book of a chapter;
  - a CHOICE review;
  - a work with another title, or with a different title and other authors.

  A preprint DOI on a published article becomes a proposal for the published DOI.
- **Metadata audit: title matches** need the same first author and year, and generic titles ("Editorial") an exact match. Main titles are compared when only a subtitle differs, and a different work type is only a soft signal.
- **Metadata audit: fewer false proposals** from the first full-library run:
  - ISSN hyphens and print/online ISSNs; journal names with clutter ("(Auckland, N.Z.)", " - ELEM SCH J"), a subtitle the registry adds, ", The".
  - Page ranges shortened to a first page; publisher variants.
  - Edition notes and "Chapter 4:" in titles; name suffixes, stray initials and garbled accents.
  - A registry that lists fewer authors.
  - A DOI alias is left alone; a year more than two years off is only proposed. When yours is the online year, the proposal says so.
  - An author proposal keeps your fuller first names.
- Abstracts: PubMed's first for articles; any abstract must share three content words with the title, be 200–6,000 characters, not mostly boilerplate and not in another language.
- The audit's rules read the first four PDF pages (a cover sheet in front is read but not counted) instead of two.

## [0.13.2+arne.20] - 2026-10-07

### Fixed

- **Metadata audit: thesis title pages taken for abstracts.** OpenAlex's "abstract" for some theses is their title page ("A Doctoral Thesis. Submitted in partial fulfilment…"). Every OpenAlex abstract, also for items matched by title, is now checked: long enough, sharing words with the title, and not a title page or citation line. Line breaks inside OpenAlex abstracts are removed.

## [0.13.2+arne.19] - 2026-10-07

### Fixed

- **The metadata audit filled nothing when it read items from the local database.** That database leaves empty fields out of an item, and the audit took a missing field for one the item type does not have. Empty fields are now filled whichever way the item was read.
- **False proposals from registry quirks.** HTML entities in Crossref names (`Accident Analysis &amp; Prevention`), subtitles Crossref keeps in a separate field, series notes in book titles ("(Routledge Revivals)"), ISBN hyphens and degrees inside author names ("Lambiase MS") no longer count as differences.
- **Wrong abstracts.** OpenAlex's abstract is used only for articles, conference papers and preprints, and only when it shares words with the title: it had given a book chapter another work's abstract, and an old article a citation line.

### Changed

- An article without page numbers gets its article number in Pages (e.g. `e70024`), as APA asks and as most of the library already does.
- The place of publication is no longer filled: APA 7 does not use it, and Crossref's version is often a whole imprint line.

## [0.13.2+arne.18] - 2026-10-07

### Added

- **Metadata audit.** `zotero-mcp metadata-audit` checks items against Crossref or DataCite (by DOI), Open Library (by ISBN) or OpenAlex (by title, first author and year), with PubMed and the item's own PDF as second sources. Without `--apply` it only reports. With `--apply` it fills empty fields (volume, issue, pages, DOI, ISSN/ISBN, publisher, abstract, first names given as initials) and corrects a filled field only when two independent sources agree on another value (volume, issue, pages, DOI, year, journal name, publisher); an abbreviated journal name is replaced and kept as the abbreviation. Every change is listed in a note on the item with the old value and its sources, and tagged `auto-enriched` or `auto-corrected`. Titles, author lists and values only one source gives become proposals: tag `metadata/review`, a note, and the saved search "Metadata to review". Tag an item `metadata/accept` or `metadata/reject` to decide; rejected values are not proposed again. Each run writes a report to `~/.config/zotero-mcp/metadata/runs/`. See [Checking and fixing metadata](docs/metadata-audit.md).
- Tools `zotero_metadata_audit` (up to 15 items per call) and `zotero_metadata_review` (list, accept or reject proposals).

## [0.13.2+arne.17] - 2026-10-07

### Fixed

- **`zotero_fetch_fulltext` timed out in Cowork.** Tool calls that reach the PC through Cowork must answer within a minute, and one paper can take longer. The fetch now runs in its own process: the call returns the report if it finishes within about 40 seconds, otherwise a run id, and the new `zotero_fetch_fulltext_status` tool gives the progress or the result. The ZenRows unblocker now gives up after 60 seconds instead of 120.
- **Links to a file inside a figshare repository were not recognised** (Loughborough's `…/articles/<title>/9544121/files/…`); only page links were. Both now go through the figshare API.

### Changed

- **`ocr-pdfs` reports every file.** A line per PDF as soon as it is checked (pages, OCR'd pages and time, running minutes), in the order they finish rather than list order, so a long scan no longer hides the progress of the others; "OCR: <file>, N pages" shows while a scan is worked on. What it has checked is saved every 25 files, so a run stopped with Ctrl+C does not start over.

## [0.13.2+arne.16] - 2026-10-07

### Changed

- **Faster lookups.** The services of one step (Unpaywall, OpenAlex, Semantic Scholar, OSF, Zenodo and the rest) are now asked at the same time instead of one after another; downloads stay one at a time and paced. A service that answers "too many requests" gets one short retry instead of up to a minute of waiting. A paper that no open source has now takes a few seconds in that step instead of up to a minute.
- `zotero_fetch_fulltext` handles at most 10 papers per call, to stay within the client's time limit; use the command line for larger batches.

## [0.13.2+arne.15] - 2026-10-07

### Fixed

- **ResearchGate could flag the whole network.** Links to ResearchGate and Academia.edu are no longer requested plainly; they go straight to the browser step. The browser step opens at most 20 ResearchGate pages per run (`researchgate_per_run`), 25–45 seconds apart, and stops using ResearchGate for the run when it shows its "unusual activity" page.
- **"Sign in with Google" was refused in the login window.** `--browser-login` now opens Chrome as an ordinary program with the fetcher's profile, so every sign-in method works. The automated window keeps Chrome's sandbox on.

## [0.13.2+arne.14] - 2026-10-07

### Added

- **The fetcher's browser step.** `zotero-mcp fetch-fulltext --browser` drives its own Chrome window, with logins you set up once (`--browser-login`): it retries the links that blocked a plain download (ResearchGate, Academia.edu, SSRN, bot-protected publishers), searches ResearchGate by title and clicks "Download", and opens the publisher's page with your institutional login or library proxy. It works at a human pace and waits for you when a captcha or login page appears; it never solves captchas. Needs Playwright and Google Chrome.
- **More open sources:** OSF preprints (PsyArXiv, SocArXiv, EdArXiv and others), Zenodo and HAL.

### Changed

- The cap on links tried (`max_candidates`) now counts per step, so many dead open-access links no longer keep a paper from reaching Scholar, the web search or the browser.

## [0.13.2+arne.13] - 2026-10-07

### Added

- **Searchable PDFs.** With `extraction.ocr.write_text_layer: true`, the text OCR recognises is also written into the PDF as an invisible layer behind the page images, so Zotero's own search and reader see it and it syncs with the file to other computers, which then skip the OCR. `zotero-mcp ocr-pdfs` does the same for the scans already in the library. Only files in Zotero's storage folder are changed; each is checked before it replaces the original. See [Configuration](docs/configuration.md#searchable-pdfs).
- **`keys.env` for the fetcher's keys.** One `KEY=value` per line in `~/.config/zotero-mcp/keys.env`, edited in any text editor.

## [0.13.2+arne.12] - 2026-10-07

### Changed

- **Index updates say which file they are reading.** Every attachment extracted gets a line with its file name, item key, how much text came out and how long it took. A file still being worked on after 30 seconds is listed every 30 seconds with its running time, so a long OCR no longer looks like an update stuck at "Extracting text: 20/51".

## [0.13.2+arne.11] - 2026-10-07

### Changed

- **`fetch-fulltext` says what it is doing.** A run starts by listing each step's services and whether each can run (key found, allowance used this month). For every item it prints its key and DOI, each service it asks and what came back, every link it tries with the site's name, and why a PDF was accepted or rejected.

### Fixed

- **PDFs on figshare and repositories built on it (Loughborough and others) were never downloaded.** Their pages refuse plain requests; the file is now taken through the figshare API. A download refused with a browser identity is also retried once as a plain client, which some bot shields let through.

## [0.13.2+arne.10] - 2026-10-07

### Added

- **Fetch full texts for items without a PDF.** `zotero-mcp fetch-fulltext` and the `zotero_fetch_fulltext` tool search open-access sources (Unpaywall, OpenAlex, Semantic Scholar, Europe PMC, arXiv, CORE, OAPEN), the publisher's page (subscriptions work on the university network), Google Scholar through SerpApi and a web search through Tavily, cheapest first. Every PDF is checked against the item (title and first author on its first pages, not a preview) before it is attached, with its version (published, accepted manuscript, preprint) in the attachment title and its source in a note. Items get `fulltext/fetched` or `fulltext/not-found`; each run writes a report and a log of every attempt. Free allowances are counted and never exceeded. Captchas are never solved. See [Fetching full texts](docs/fulltext-fetch.md).

## [0.13.2+arne.9] - 2026-10-07

### Fixed

- **The server could stop answering while it caught up on new papers.** The startup update extracted and OCR'd new attachments inside the server process, and a few dozen of them kept it too busy to answer: Claude Desktop reported "Request timed out", killed the server, and the next start began the same catch-up again. The startup update now runs in a separate background process at low priority, and the server answers straight away. `ZOTERO_MCP_UPDATE_IN_PROCESS=1` restores the old behaviour.
- **Papers indexed by another process were missing from a running server's searches.** Passages added by the startup update or a manual `zotero-mcp update-db` while the server was running were counted but not found until a restart. The server now notices the change and loads them before its next search.

### Changed

- **Long OCR runs are announced.** OCR of a document of 10 pages or more prints the file name and page count when it starts and how long it took when it ends, so a long scan no longer looks like a hung update.

## [0.13.2+arne.8] - 2026-10-07

### Added

- **Check and fix the citations in an existing Word document.** `zotero_inspect_word_citations` lists a document's live Zotero citations (with the items behind them and the sentence around each), its bibliographies, typed reference lists, citations typed as plain text, and unconverted markers. `zotero_edit_word_citations` then replaces or deletes citations, turns plain-text citations into live ones, adds Word comments, and inserts, rebuilds or deletes bibliographies, including replacing a typed reference list. It writes to a new file, as tracked changes in the original, or over the original, and asks which first; the last two keep a backup. For shared documents it shows which library each cited item comes from, flags the same work cited as different items (two bibliography entries), reuses the item already cited when an edit cites the same work again, and merges such duplicates on request (`merge_duplicates`). See [Word citations](docs/word-citations.md#checking-and-fixing-an-existing-document).

### Fixed

- **Text after a citation marker could disappear.** When a marker ended exactly where its run of text ended (for example right before a change of formatting), `zotero_insert_word_citations` removed everything after it in the paragraph.

## [0.13.2+arne.7] - 2026-10-07

### Added

- **OCR for scanned PDFs while indexing.** Run `zotero-mcp ocr-setup` once (downloads Tesseract language data, about 15 MB for English); from then on scans are recognised when indexed, once each, and the scans that failed before are retried. See [Configuration](docs/configuration.md#ocr-for-scanned-pdfs).

## [0.13.2+arne.6] - 2026-10-06

### Fixed

- **A rebuilt index could miss the best passages.** `db-rebuild-vectors` copied paper by paper with ChromaDB's default graph settings, and searches got stuck among one paper's near-identical passages. It now copies in random order and builds a denser graph (32 neighbours, ef_construction 200, ef_search 400). Run `zotero-mcp db-rebuild-vectors --force` once if you rebuilt with arne.4 or arne.5.

## [0.13.2+arne.5] - 2026-10-06

### Added

- **The status tool reports whether the vector index is intact** ("Index Integrity"), checked from the index files directly so it answers even when the index is damaged.

## [0.13.2+arne.4] - 2026-10-06

### Added

- **`zotero-mcp db-check` and `zotero-mcp db-rebuild-vectors`.** The check finds passages whose vector the index no longer holds reliably — the damage two processes writing the index at once leave behind. The rebuild copies every intact passage into a fresh index without re-embedding, keeps the old one, and leaves damaged items for the next update to index again. See [Troubleshooting](docs/troubleshooting.md).

### Fixed

- **A damaged index made updates and searches hang.** An index read that fails is now an error that stops the update, instead of being taken for "not indexed" — which sent items back for embedding into the same index, whose next read then hung.

## [0.13.2+arne.3] - 2026-10-06

### Fixed

- **An index update could wait forever on text-extraction workers.** On Windows the server's six worker processes stalled at start-up and the startup update never finished, holding the update lock. The MCP server now extracts in-process by default (`extraction.server_workers` opts back in), and a pool that produces nothing for 10 minutes is stopped and its work finished in-process.
- **Adding tags to an existing item failed with HTTP 412 in hybrid mode** (`if_exists="file"`): the local copy's version lags the web API's until Zotero syncs. The tags are now written to a freshly fetched copy, with a retry on a version conflict.

## [0.13.2+arne.2] - 2026-10-06

Arne's build: upstream 0.13.2 plus the changes below.

### Added

- **Section labels on indexed passages** (Abstract, Introduction, Methods, Results, Discussion, Conclusion, References, Appendix, Back matter), shown first in the Location line and filterable with `filters={"section": ...}`. A chapter, part or unrecognised all-caps heading ends a label.
- **Hosted re-ranking** through Voyage, Cohere, OpenRouter, Contextual or any compatible endpoint (`reranker.provider`). Search results say *"Ranked by voyage/rerank-3 over N candidate passages"*, or why re-ranking failed.
- **Optional study context for passages** (`chunking.context_header`): passages after the first are embedded with their paper's title, year, authors and abstract opening. Results show the passage without it.
- **`zotero_insert_word_citations`**: Pandoc-style markers in a .docx (`[@KEY]`, `[@KEY, p. 12]`, `[-@KEY]`, `{{bibliography}}`) become live Zotero Word-plugin citations and a bibliography. See [Word citations](docs/word-citations.md). New `[word]` extra (lxml).
- **Status shows the server version and whether the index is current.**

### Fixed

- **Voyage re-ranking never ran**: its response lists results under `data`, not `results`, so every call fell back silently to retrieval order.
- **A passage index returned fewer papers than asked for**: the top passages all came from one or two papers. Search now takes a 200-passage pool and keeps at most two passages per paper before ranking.
- **New papers were only searchable after a restart, or one search late.** A library fingerprint now skips the index scan when nothing changed and, when something did, the search waits (up to 25 s) for the update.
- **Index updates were not locked on Windows**, so two could run at once.
- **Extractions that failed under an older extractor are retried once**, so scanned PDFs the OCR-layer fallback can now read are indexed.
- **Keyword search on the SQLite backend matched the whole query against each field**, so "Author Year" found nothing; words are now matched independently, as in Zotero's quick search.
- **`zotero_update_search_database` reported a skipped run as an update of 0 items**; it now says another update holds the lock.
## [0.15.1] - 2026-10-16

### Added

- **Add items by PubMed ID** (#692, #714). `zotero_add_item` takes `source_type="pmid"` (a bare number is not auto-detected, so name the source type), and `zotero-cli add pmid 12345678` takes one or several. One PubMed `efetch` request returns the full record, mapped the way Zotero's PubMed translator does it, so the item keeps its PMID and PMCID and carries the abstract, MeSH headings, keywords and language that a DOI import through Crossref lacks. Dedup, collections, tags and `if_exists` work as for the other sources. Thanks @rizakardas.

### Fixed

- **The Zotero Agent panel no longer cuts off a PDF read at a fixed zoom.** Opening or widening the panel narrows the reader; Page Width, Auto and Page Fit already refit, but a fixed zoom such as 120% kept its page width and spilled out of the view. A fixed zoom that no longer fits now becomes Page Width; one that still fits is left as set.
- **BibTeX and CSL JSON imports write the native PMID and PMCID fields** (#692, #713). Zotero 7.1+ has both on journal articles, but the converters put them in Extra, so an imported item could not be found with `PMID is ...`. They go to the native field when the schema has it and to Extra otherwise. CSL JSON also maps `container-title-short` to Journal Abbr and `source` to Library Catalog, as Zotero's own CSL mapping does. Thanks @rizakardas.
- **`.yaml` and `.yml` files get a text content type on the default upload path too** (#715, follow-up to #703). pyzotero asks `mimetypes` for the type, which says `application/octet-stream` on Python 3.10 to 3.13 and `application/yaml` on 3.14, neither of which Zotero full-text indexes; the 0.15.0 fix only covered the WebDAV and two-step paths. Thanks @lots-o.
- **Chat text can be selected and copied in the Zotero Agent panel** (#716). Zotero's window chrome can set `user-select: none`, which the panel inherited; answers, your messages and the other readable parts of the chat now turn it back on. Thanks @IceGiraffe.
- **Passages are sized to fit the embedding model.** With `semantic_search.chunking` on, the default `chunk_size` of 1500 characters (about 350 tokens) is longer than all-MiniLM-L6-v2 reads (256 tokens), so the tail of every passage was cut off before embedding. `chunk_size` is now capped at about 85% of the model's token limit (4 characters per token, 3 for OpenAI), roughly 870 characters for the default model, and the change is logged once. Models whose limit already fits keep the configured size. The new "Passage chunking" section in `docs/configuration.md` also recommends raising `max_chunks_per_item` to cover whole papers.
- **`update-db --fulltext` warns when the full text cannot be searched.** Without chunking each item is one vector cut at the model's input limit, 256 tokens for the default model, so the extracted PDF body was dropped without a word. A user found only the abstract indexed by reading the ChromaDB collection. `update-db --fulltext` now prints one warning saying so and how to fix it (enable `semantic_search.chunking`, rebuild with `--force-rebuild`), and `db-status` shows the limit next to the chunking line. The Batch API path keeps its own warning.

## [0.15.0] - 2026-10-15

### Added

- **The open-access PDF cascade asks OpenAlex for repository copies.** When `zotero_add_item` or a batch import attaches a PDF for a DOI, it now tries every `pdf_url` OpenAlex lists after Unpaywall and before the publisher's `citation_pdf_url`, open-access locations first. OpenAlex knows institutional repository copies that Unpaywall does not count as open access. For Maraun 2013 in J. Climate, DOI 10.1175/jcli-d-12-00821.1, Unpaywall has nothing and the AMS page offers a 2-page preview, while OpenAlex lists the full 7-page PDF in the GEOMAR repository. A URL already tried under one source is not downloaded again under the next. Thanks @phil-c137.
- **Markdown and YAML files can be attached** (#703). `zotero-cli attach` and `add file` accept `.md`, `.yaml` and `.yml`, stored as `text/markdown` and `text/yaml` so Zotero's own full-text indexer picks them up. `.json` stays a CSL-JSON import. Requested by @rizakardas.
- **`zotero-cli --json get fulltext` says when a read was cut short** (#701). `data.truncated` is always present, and a capped read adds `pages_read` and `page_count`, so a script no longer has to parse the TRUNCATED heading out of `data.text`.

### Changed

- **Semantic search is its own toolset, and hidden on installs without the `[semantic]` extra** (#572, #689). `zotero_semantic_search` moves from core into a new `semantic` toolset that is on by default, so default installs see no change. `ZOTERO_MCP_TOOLSETS=none` now leaves it out too; use `none,semantic` to keep it. A spec that uses `none` without naming `semantic` logs one warning at startup saying so; `none,-semantic` leaves it out without the warning. `semantic` and `search-admin` are left out automatically when ChromaDB isn't installed, so a base install no longer advertises tools that can only reply with install instructions; naming them in `ZOTERO_MCP_TOOLSETS` keeps them. The connector `search` keeps its keyword fallback. Reported by @zhuziqi97; thanks @falcon1-1-code.
- **The literature-review and contradicting-evidence prompts fall back to `zotero_search_items` when semantic search is not usable** (#693). `zotero_literature_review` and `zotero_find_contradicting_evidence` always told the model to call `zotero_semantic_search`, which fails without the `[semantic]` extra and is absent when a toolset spec leaves it out. They now check at render time (ChromaDB found with `find_spec`, not imported, and the tool's toolset enabled) and otherwise name `zotero_search_items` with `qmode='everything'`, one key term per query. With semantic search usable the text is unchanged. Thanks @falcon1-1-code.
- **`zotero_advanced_search` rejects a field name it does not know** (#694). A typo such as `titel` was a valid-looking condition that matched nothing, so the tool answered "No items found matching the search criteria" and the mistake went unnoticed. It now returns an error naming the field, up to three close matches (`Did you mean: title?`) and the supported operations. Thanks @falcon1-1-code.
- **`zotero_advanced_search` accepts a single condition sent as a bare object** (#695). `{"field": "title", "operation": "contains", "value": "x"}` instead of a list of one was rejected with a raw pydantic "Input should be a valid list" error, and the same object as a JSON string answered "No search conditions provided". It is now read as a one-condition list. An empty object is still an error. Thanks @falcon1-1-code.

### Fixed

- **A startup semantic update no longer holds up the MCP handshake** (#706). With `auto_update: true` and `update_frequency: "startup"` (or a due `daily` run), the server ran the update in a worker thread of its own process, where native calls that hold the GIL (ChromaDB, loading or downloading the embedding model) and, on Windows, DLL loads stall the event loop as well: Claude Desktop's `initialize` went unanswered until the update finished, timed out, and shut the server down. The update now runs as a child `zotero-mcp update-db` process (with `--fulltext` in local mode), its output relayed to the server's log, and the server answers at once. The update lock still keeps it from racing a manual `update-db` or a second server, and an update unfinished at shutdown is stopped and resumes on the next start. Reported by @marius-ne.
- **The Zotero Agent plugin finds the agent CLIs on Windows** (#700). Detection ran `command -v` through `/bin/sh`, so on Windows every backend failed with `File at path "/bin/sh" does not exist, or is not executable`. There the plugin now searches PATH with `where.exe` (PATHEXT aware, taking `codex.cmd` rather than npm's extension-less shim), reads the environment from `cmd.exe /c set`, runs npm's `.cmd` shims by the script or `.exe` they wrap, makes its folders with Node instead of `mkdir`, and stops agents with `taskkill`. macOS and Linux are unchanged. Translating with pi is still unavailable on Windows. Reported by @patrickjcrawford. This is untested on a real Windows machine; reports welcome on #700.
- **In local mode the DOI duplicate check reads `zotero.sqlite`** (#704, #705). It ran an `everything` quick search per DOI, which against the local API is a full-text scan of the library and pinned Zotero at 100% CPU for minutes on a batch import. The DOI is now looked up in the database, about a millisecond per DOI, with the old search as the fallback when the database cannot answer. A candidate the API cannot fetch is skipped instead of failing the import. Thanks @mronkko.
- **`zotero-cli --json search` reports a failed search as an error** (#691, #696). An error came back as `ok: true, count: 0`, the same as no matches; it is now `ok: false` with exit code 1, also for a failed semantic search or a collection that does not exist. Thanks @RizgarOzan.
- **`url` conditions work in SQLite advanced search** (#701). The field was missing from the SQL map, so with `--all-libraries`, which has no API fallback, every `url` condition failed. Reported by @jstotero.
- **`zotero-cli edit --collections` help says it replaces** (#701). It read "Add to collections", but the tool replaces the item's memberships, so following the help removed the item from its other collections. The help now says so and points at `collections manage --add-to`.
- **`gemini-embedding-2` returns one vector per text** (#697, #698). The batch went out as one list, which that model reads as a single multi-part input and answers with one aggregated vector, so an index built with it was wrong. Each text is now its own `Content`. `gemini-embedding-001` is unchanged. Reported by @Faustoneiros; thanks @lots-o.
- **Restoring after an update no longer replaces the semantic index** (#699). The restore step deleted the index and copied the backup back, so a partial backup replaced a complete index, along with anything else in a shared `persist_directory`. It now copies only when the index is missing. Thanks @lots-o.
- **YAML attachments are read as text** (#703, #707). `.yaml`, `.yml` and `application/yaml` attachments were skipped by full-text extraction. Thanks @kskszq.

## [0.14.1] - 2026-10-07

### Added

- **`semantic_search.persist_directory` moves the ChromaDB index** (#617). The index was pinned to `~/.config/zotero-mcp/chroma_db`, and on Windows a user folder with non-ASCII characters (for example `C:\Users\王林澜`) makes ChromaDB write part of the index to the wrong place once it holds about 1,000 items, after which every search fails with `Error loading hnsw index`. Set the key in `config.json` to an ASCII path; the server, `db-status` and `zotero-mcp setup` all follow it, and the default location is unchanged. A non-ASCII path on Windows now logs a warning that names the setting. Thanks @lots-o. The pre-update backup follows the configured directory.

### Fixed

- **On Windows, two servers no longer run the same semantic-index update at once** (#267). The update lock used `fcntl`, which Windows lacks, so there it quietly did nothing: a client that starts the server twice (Codex does) ran two full-text indexes of the same library side by side, each holding gigabytes. The lock now uses `msvcrt.locking` on Windows, on a byte past the stored pid so the process that loses can still read who holds it. Reported by @lwz20210407.
- **`zotero-mcp setup` and `zotero-cli` no longer crash on a Windows console** (#26). Under the default cp1252 or cp936 console encoding, printing an emoji (`setup-info`, `setup`) or an item title in another script raised `UnicodeEncodeError` and aborted the command, so `setup` died before it wrote its config. Both entry points now keep the console's encoding and replace what it cannot hold.
- **`zotero-mcp setup` finds the Claude Desktop config of the Microsoft Store build on Windows** (#26). That build reads `%APPDATA%\Claude` through a package redirect, so the real file is under `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\`; setup wrote to `%APPDATA%\Claude Desktop` and reported success while Claude never saw the server. The packaged path is now probed, and written to when it exists, like the other known locations.
- **Tables without closing tags no longer flood HTML extraction with empty cells** (#687). `html.parser` nests an unterminated `<tr>`/`<td>`, and markdownify then printed a blank header row and a `| --- |` row as wide as the whole table for every nested row: `zotero_get_item_fulltext` on a 74-row datosmacro statistics snapshot returned 218,832 characters, 92% `|  |  |` and `| --- | --- |` runs (one line of 125,237 characters), around 17K of real data. The scaffolding (a header row of nothing but empty cells, the separator under it, and the runs of both inside the nested rows) now collapses to one short row, so that page extracts to 17,931 characters with every word kept; 70 of 355 snapshots in one library shrink, 1.3M characters in all. A table with a real header keeps its separator and its empty cells.
- **Deeply nested web page snapshots are extracted instead of silently replaced by Zotero's flat index text** (#688). `html.parser` leaves unterminated tags nested, so pages with many unclosed `<li>`/`<td>` tags exceeded Python's recursion limit inside markdownify ("Extraction failed ... maximum recursion depth exceeded"), and `zotero_get_item_fulltext` fell back to the index text with no structure: 7,061 characters for a datosmacro page whose Markdown is 129,328, and the same failure kept a 1.1 MB article out of the semantic index. Extraction now retries once on a worker thread with a raised recursion limit (5000, restored afterwards) and a 64 MB stack; pages nested more than about 2,000 levels deep (one converts to 29M characters) still use the fallback.
- **Voyage embeddings say whether they embed a document or a query** (#684). Through the OpenAI provider with a Voyage `base_url`, indexing now sends `input_type: "document"` and search sends `input_type: "query"`, so Voyage can tune the pair for retrieval. Other base URLs are unchanged. An existing index is not rebuilt. Thanks @sghng.
- **Annotations written by `zotero-cli` and the MCP tools sort in reading order** (#608). The sort index was `page|y|x` in PDF coordinates, where y grows upward, so a page's annotations came out bottom to top in Zotero's list. It is now Zotero's own `page|character offset|top`: the offset is the nearest page character (spaces and control characters not counted) from PyMuPDF's text, so it can differ from Zotero's by a few characters, and the top is the page height minus the rect's top edge. A two-column page now orders left column first. Thanks @dnlfis42.
- **`zotero-cli get collection-items` fails for a collection that is not in the library** (#606). A key from another library came back as `ok: true` with zero items, which reads as an empty collection. It now exits 1 with a `not_found` (or `tool_error`) envelope, or the message on stderr. A genuinely empty collection is still a success. Thanks @MohammadHijjawi97.
- **`update-db` without `--fulltext` no longer indexes PDF annotations** (#604). The API scan skipped attachments and notes but not annotations, so each highlight became its own document, while the incremental fetch and the SQLite scan already skipped them. All three now share one list of item types. Annotations already in an index stay until `update-db --force-rebuild`. Thanks @RizgarOzan.

## [0.14.0] - 2026-10-07

### Added

- **Zotero Agent, a Zotero plugin.** An AI chat panel inside Zotero for your own Claude Code, Codex or pi, with the open item, page and selection as context and `zotero-cli` for the library. The built `zotero-agent.xpi` ships in the wheel and is attached to each GitHub release; `zotero-cli plugin` prints where it is and how to install it (`--path` prints only the path). See [docs/chat-plugin.md](docs/chat-plugin.md).
- **`zotero-cli read --find "phrase"`** locates a passage and returns the matching pages with short snippets, without reading whole pages. `--start-page` is optional with `--find`.
- **`zotero-cli open`** shows a PDF page or an annotation in the Zotero reader (`open KEY --page N`, `open --annotation KEY`, `annotations create --open`).
- **`zotero-cli plugin --reveal`** shows the plugin file in the file manager; the plugin docs have a prompt that lets any agent do the install.
- **Notes take the note editor's full formatting.** `notes create` and `notes update` accept Markdown with `$math$` and a small safe tag subset (`<u> <s> <sub> <sup> <mark>`, colour and highlight colour spans) and convert it to Zotero's note HTML; `notes update --append` adds to an existing note. Existing citation, highlight and image markup survives an edit.

### Fixed

- **Note search no longer misses real matches behind markup-only hits** (#635). `search_notes_local` applied its SQL `LIMIT` before discarding notes whose match was only in HTML markup, so a query like `zotero` or `version` (present in Zotero 7 citation attributes) could fill the limit with discarded rows and return few or no notes. The limit now applies after the filter.
- **Web page snapshots no longer carry their images as base64** (#670). The Zotero Connector saves a page with every image inlined as a `data:` URI, and HTML extraction copied each one into the Markdown, so `zotero_get_item_fulltext` on an 8 MB snapshot returned 6.1 million characters (about 1.5M tokens) around 33K characters of text and failed over HTTP with "Server-sent event exceeded the 1048576 byte limit"; semantic search embedded the same base64. Images, video posters and sources, and links whose URL is a `data:` URI are now dropped: an image leaves `[image: <alt text>]` (or `[image]`), a link leaves its text. Remote images and links are unchanged.
- **SQLite free-text search ignores case and accents, like Zotero's own search** (#663). With `ZOTERO_BACKEND=sqlite`, `zotero_search_items` compared raw values with SQLite's `LIKE`, which folds ASCII case only, so `índice` and `indice` missed a title with `Índice`, and `SUCESIÓN` missed `sucesión`, while the API path found them. Title, creator, abstract, tag and note matches now go through `zsearch_norm` on both sides, as `zotero_advanced_search` already does since #417. The folding function itself is about seven times faster, because a dash regex that could never match after `unidecode` is gone.
- **A failed note update is a tool error, not a success message** (#669). `zotero_manage_note(action="update")` returned its failure as text, so MCP clients saw `isError=false` even when Zotero answered HTTP 500, and any read failure (a refused connection, a timeout) was reported as "No item found". Failures now raise a tool error that keeps the underlying cause; only a real 404 says the note is missing. Thanks @lipaopao000.
- **`zotero-cli` reports a failed fulltext read and a rejected library switch as failures** (#599, #595). A fulltext attachment error came after the metadata, and `--json` wrapped it as data, so it arrived as `ok: true` with exit status 0; the error now leads the text and `get fulltext` no longer treats it as data. `switch-library` with an unsupported library type or an unknown group, feed or personal id now starts with `Error:` like other refusals. Thanks @feiiiiii5.
- **`zotero_export_bibliography(item_keys=...)` renders every requested item** (#662). Keys were fetched from `/items`, which also returns each item's notes and attachments, so those filled the 100-row page: 60 keys gave 52 entries and 80 gave 53, with no warning, in every format. Keys now go to `/items/top` in batches of 50, Zotero's limit for one `itemKey` filter.
- **`zotero_export_bibliography` exports a whole collection or library, not the first 100 rows.** A 191-reference collection came back with 51 entries and no notice, because the fetch included each item's child attachments, which render as empty entries, used up the 100-row cap and were then dropped; the library-wide export stopped at 68 of 470 the same way, and BibTeX took one 100-row page. It now reads the top-level item endpoints and pages until exhausted for bib, citation and bibtex (191 of 191 and 470 of 470 live). A large export starts with the usual response-size warning.
- **Plain-text notes keep `<`, `>` and `&`** (#664). `zotero_create_note` wrapped plain text in `<p>` without escaping it, so `x<y and y>z` was stored as markup, Zotero read `<y and y>` as a tag, and the text inside it was lost (reading the note back gave `xz`). Plain text is now HTML-escaped. Text is treated as HTML when it contains a tag such as `<h2>`, `<ul>` or `<p class=...>`, not only the exact `<p>` or `<div>`.
- **A parent collection named ambiguously is an error, not a guess** (#665). `zotero_create_collection` and `zotero_update_collection` resolved a `parent_collection` name to the first collection with that name, so with a `Readings` subcollection under two courses the new or moved collection could land under the wrong one, and the tool reported success. Parent names now go through the same resolver as `zotero_set_item_collections` (#233): an ambiguous name returns an error listing the candidates, a `Parent/Child` path picks one, and an eight-capital name such as `PROJECTS` is read as a name unless a collection has that key.
- **`zotero_advanced_search` answers any Zotero field from SQLite.** Only seven fields (title, abstract, DOI, publication, dates, item type) were translated to SQL, so a condition on `extra`, `publisher`, `volume`, `ISBN`, `url`, `language` or any other field sent the whole query on a walk of the library over the API: about 2 s, 11 requests and 2.5 MB on a 2,500-item library, against 2 ms in SQL. Any field in the database's field table is now queried directly, resolved per item type like `title`, with the name bound as a parameter. Names the database does not know behave as before, and so do ordering operators (`isGreaterThan`, `isBefore`, ...) on these fields, which stay on the API path because it orders numeric values by magnitude where SQL would order the stored text (`volume > 9` must hold for 12).
- **`zotero_search_items` applies `tag` and `item_type` to its semantic fallback.** When every text search came up empty, the last-resort semantic search ignored both filters: `tag="a"` returned items that do not carry the tag and `item_type="book"` returned articles, each presented as a match for the filtered query. The hits are now filtered with the same syntax as the text searches (` OR `, `-tag`, `a || b` types, case and accents folded for tags), and the index is asked for more candidates so the limit survives the filter. A tag containing a wildcard, which cannot be checked here, returns no fallback hits. The fallback note also no longer sits between the title and the `with tags:` line.
- **`zotero_find_related_papers` and the Scite tools no longer block every other tool while they wait on the network** (#431). They held the shared Zotero API lock across each OpenAlex or Scite request, so with 1.5 s of OpenAlex latency a concurrent `zotero_get_recent` waited 6.3 s (2.7 s behind `scite_enrich_item`). The lock is now taken only around the reads from Zotero itself; the same test waits 0.01 s.

### Changed

- **License: the Zotero Agent plugin (`plugin/`) is AGPL-3.0-or-later**, the license Zotero itself uses. The MCP server and `zotero-cli` stay MIT, so the package is now `MIT AND AGPL-3.0-or-later` and carries both license files.
- **A new README and logo.** The red Z in a chat bubble is now the project's logo; the README shows the three ways in (MCP server, `zotero-cli` + skill, Zotero Agent) with screenshots, and the PyPI summary says what the package is for.
- **Reading a PDF in page ranges no longer re-parses it every time.** `zotero_read_pdf_pages` and `zotero_get_item_fulltext` ran pdf-inspector's whole-document Markdown pass on every call, and it costs about the same for one page as for the whole file (3.7-6 s on some 28-page papers, with the GIL held so other tools stalled), so reading a paper in chunks paid it again for each chunk. The parse of an unchanged file is now kept in a small in-process memo (3 files, 8M characters in all), so repeat and different-range reads of the same PDF return in milliseconds. Indexing is unchanged.
- **`update-db` no longer probes every item for fulltext.** For each item the indexer asked for the parent's `/fulltext` (always empty for a regular item) and then its `/children`, so a 552-item library cost 1041 requests to find text for 99 items. It now asks only for each item's PDF attachments' text, and a whole-library run lists those attachments in one paged pass first: 106 requests, same text. The docs now say plain `update-db` also indexes the text Zotero already extracted, in local mode too.

## [0.13.3] - 2026-10-06

### Security

- **HTTP transports reject requests for a foreign host (DNS rebinding).** Served over `streamable-http` or `sse`, the server accepted any `Host` and `Origin`, so a web page could rebind its hostname to 127.0.0.1 and use every tool, writes and deletes included, from the user's browser. `serve` now turns on FastMCP's Host/Origin check in `auto` mode: on a loopback bind, requests must name localhost/127.0.0.1 and come from no origin or a local one (421/403 otherwise). FastMCP's own option does not reach its SSE app, so the check is attached to `sse` as middleware. Clients that connect to localhost, including through a VPN or `mcp-remote`, are unaffected. **A tunnel or reverse proxy that forwards its own hostname (ngrok's default) now needs that hostname in `FASTMCP_HTTP_ALLOWED_HOSTS='["host.example"]'`**, or the proxy must rewrite `Host`; `FASTMCP_HTTP_HOST_ORIGIN_PROTECTION=false` turns the check off. The server prints this on start-up, and docs/troubleshooting.md covers it. A FastMCP too old to support the check logs a warning.

### Fixed

- **Linked-file attachments resolve against the right Zotero profile** (#389). With more than one Zotero profile, the local reader took `baseAttachmentPath` from whichever profile's prefs.js it found first, so `attachments:` paths could point at another profile's folder and full text was silently left out of the index. The profile whose data directory holds the database is now preferred. Thanks @feiiiiii5.
- **BibTeX imports keep every author when the author list wraps across lines** (#631). `zotero_add_by_bibtex` and `zotero-cli add bibtex` split names only on a literal ` and `, so `Smith, John and` followed by a line break and `Doe, Jane` (how exported `.bib` files commonly wrap long author fields) became a single creator with first name `Smith, John and Doe,` and last name `Jane`. Names are now separated by `and` between any whitespace.
- **Adding a book chapter or conference paper by DOI keeps the book or proceedings title** (#638). CrossRef's `container-title` was written only to `publicationTitle`, which `bookSection` and `conferencePaper` items do not have, so the container was dropped silently. It now goes to `bookTitle` / `proceedingsTitle`.
- **`zotero_batch_update` applies Extra edits to the items it selected** (#640). With both tag and Extra actions and a `tag`/`query` selector, the Extra half re-ran the search after the tag half had already edited tags, so `tag='to-read', remove_tags=['to-read'], set_keys={...}` removed the tag and then reported "No items found" for the Extra edits, which were never written. The selection is now resolved once and shared.
- **CSL JSON import keeps name particles and suffixes** (#639). Names exported with `non-dropping-particle`, `dropping-particle` or `suffix` (Better BibTeX, citation.js) lost them, so "van der Maaten" was stored as "Maaten". They are now folded into the last and first names.
- **Note search in local mode stays in the active library** (#632). `zotero_get_notes(query=...)` read `itemNotes` and `itemAnnotations` from SQLite with no library filter, so with group libraries synced, a personal-library search also returned the groups' notes and annotations (and a search after `zotero_switch_library` returned personal ones), even though the tool is documented as active-library only. The local queries are now scoped like the other local searches; a group that isn't in the local database falls back to the API.
- **PDF downloads have a size and time limit** (#644). `zotero_attach_file` with `url`, the open-access PDF step of `zotero_add_item` and the arXiv PDF step streamed the response to disk with no ceiling while holding the global API lock, so a huge or endless response filled the disk and a slow one kept every other write tool waiting. Downloads now stop at 500 MB or after 300 s, and a `Content-Length` over the limit is refused before reading.
- **Index bookkeeping no longer wipes `config.json`** (#646). The savers for the sync watermark, index schema version and backfill count, and `--db-path`, read the config with any parse error treated as `{}` and wrote the result back in place. One typo from hand-editing the file, or a read that caught another process mid-write, followed by any `update-db` erased the web API key, the local write key and the embedding settings. They now refuse to overwrite a config they cannot parse (and log why), and every write goes through a temp file and an atomic rename, so readers never see half a file. A config that is a symlink is written through, so the file it points to stays current. A config that is not valid UTF-8 is treated like invalid JSON.
- **Stopped servers no longer leave copies of the library behind** (#645). The SQLite backend reads a private copy of `zotero.sqlite` plus its WAL (#536) and deleted it only at a normal exit, so every stop by SIGTERM (launchd, systemd, Docker) or SIGKILL left a full copy of the library in the temp directory, one per restart. Copies are now named after their process, and each process removes those of processes that are gone before making its first one; unnamed copies from older versions go once they are a week old. The snapshot tests no longer leave copies in the real temp directory either. On Windows, where a process id cannot be probed safely, only copies older than a week are removed.
- **A failed batch-manifest save no longer loses the run** (#646). The manifest of an OpenAI/Gemini Batch API run, its only local record, was rewritten in place on every status refresh and import, so a crash or a full disk mid-write left truncated JSON. That run then disappeared from status and import, and an older run was treated as the newest. Manifests are now written to a temp file and renamed. The full-text cache index now uses the same writer, which also removes a fixed temp file name that concurrent writers shared.
- **`zotero-cli library switch` no longer claims the switch carries over** (#606). Each `zotero-cli` command is a new process, so a switch only lasted for that one command, while the message said "All tools now operate on this library" and the next command silently read the default library. The CLI now says the switch lasts for this command only and prints the `ZOTERO_LIBRARY_ID` / `ZOTERO_LIBRARY_TYPE` values to use instead; `library reset` says the same.
- **Merging duplicates no longer trashes the annotations on a duplicate's PDF** (#661). When a duplicate's attachment is the same file as one the keeper already has (same md5), `zotero_merge_duplicates` left it on the duplicate and trashed it, along with every highlight and note the user had made on that copy, and still reported "Merge complete". Such an attachment is now moved to the keeper whenever it has annotations or a child note, so the keeper may end up with two copies of the file but nothing is lost. Zotero's local API lists annotations only when asked for them by type, so the check asks for them explicitly.
- **Embeddings work with Voyage AI through the `openai` provider** (#667). Voyage's `/v1/embeddings` rejects `encoding_format="float"`, which the provider always sent (#348), so a `base_url` pointing at `api.voyageai.com` failed with a 400. For that host the provider now asks for `base64` and decodes the vectors itself, since the OpenAI SDK decodes base64 only when it picked the format. OpenAI and every other `base_url` still send `float`.
- **Web-mode feed tools report an error** (#601). `zotero_list_feeds` and `zotero_get_feed_items` answered "only accessible in local mode" as plain text, so `zotero-cli --json list-feeds` and `get feed-items` printed `ok: true` and exited 0. The refusal now starts with `Error:`.

## [0.13.2] - 2026-10-04

### Fixed

- **Duplicate detection agrees with itself** (#496). Duplicate grouping, the auto-merge DOI-conflict guard, the pre-add existence check and the semantic index's preprint filter now take their keys from `zotero_mcp.identifiers`, so DOIs match in canonical form (`10.1000/ABC`, `https://doi.org/10.1000/abc`) and titles fold the way Zotero's own duplicate finder folds them. A DOI field holding a placeholder such as `article` no longer groups every item that carries it, and re-adding a paper whose stored DOI differs only in case no longer creates a second copy.
- **File uploads into a group library no longer go to your personal WebDAV** (#591). Group libraries always store files in Zotero Storage, but with `ZOTERO_WEBDAV_*` configured and writes going through the Web API, `zotero_add_item` and `zotero_attach_file` sent the file only to WebDAV and left the group attachment without one. Other attach paths PUT a second copy to WebDAV, and if that failed they deleted the group attachment even though its file was already in Zotero Storage. The WebDAV steps now skip group libraries.
- **Automatic tags read as automatic in local mode** (#620). Items read from `zotero.sqlite` listed every tag without its type, so `zotero_get_item_metadata(format="json")` and other reads showed automatic tags as manual, unlike the Zotero API. They now carry `"type": 1`.
- **Exported bibliographies are numbered 1..N** (#619). `zotero_export_bibliography` numbered entries before dropping the ones that are empty once their HTML is stripped, so each dropped row used up a number and a 12-item collection came out as 4, 5, 7, 9, … Entries are now filtered first, then numbered.
- **PDFs with a scanner OCR text layer are no longer indexed metadata-only** (#611). Files from Xerox or ABBYY scanners carry their text as an invisible layer over a page image, and pdf-inspector's markdown pass returned nothing for every page; Ghostscript and PDFCreator output could make it fail outright. Extraction now falls back to pdf-inspector's plain-text pass when the markdown pass yields no text or raises. That pass cannot be split by page, so the text is attributed to the first page and a `max_pages` cap is applied as the same share of the text; an explicit page subset (as in `zotero_read_pdf_pages`) is unchanged. Reported by @timmyfaraday.
- **Percent-encoded doi.org URLs are read as the DOI they name** (#630). A DOI URL copied from a browser's address bar, such as `https://doi.org/10.1002/%28SICI%291097-4636...`, kept its `%28`/`%3C` escapes through `normalize_doi`, so the item was saved with the encoded string in its DOI field and duplicate detection and `if_exists` treated it as different from the same DOI written plainly. A URL with an encoded `%2F` was not recognised as a DOI at all. The URL path is now percent-decoded; a bare DOI is left as written.
- **Merging duplicates no longer trashes distinct linked-file attachments** (#633). `zotero_merge_duplicates` skips a duplicate's attachment when the keeper "already has" it, but the check compared only content type, filename, md5 and URL. Linked-file PDFs carry none of the last three, so any linked PDF on a duplicate matched any linked PDF on the keeper, stayed behind, and went to the Trash with the duplicate. Attachments are now compared by link mode and path as well, and one with no md5, path or URL is never treated as a duplicate.
- **Automatic tags stay automatic when tags are edited or items merged** (#618). `add_tags`/`remove_tags` on `zotero_update_item` and `zotero_update_annotation`, and the keeper's tags in `zotero_merge_duplicates`, were rebuilt from tag names alone, so automatic tags (e.g. MeSH headings from PubMed) became manual and "Delete Automatic Tags in This Library…" no longer reached them. Existing tags now keep their type, and tags copied from a duplicate keep the duplicate's. Reported by @rizakardas.
- **A read right after a write sees the write** (#228). In local mode the database copy that reads use is refreshed at most every `ZOTERO_MCP_DB_SNAPSHOT_MIN_INTERVAL` seconds, and a write tool's own read just before writing usually used up that window, so `zotero_get_item_metadata` after `zotero_set_item_collections`, or `zotero_get_annotations` after `zotero_create_annotation`, still showed the old state. After zotero-mcp writes, the next read takes one fresh copy regardless of that interval, then the interval applies again.
- **Switching to the personal library by the id `zotero_list_libraries` shows now works** (#603). In local mode the listing showed the personal library as `libraryID=1`, but `zotero_switch_library` only accepted `0` for `library_type='user'`, so the list-then-switch workflow the tool descriptions recommend always failed. Local mode now accepts `0`, `user`, or the personal library's listed libraryID (other ids are still rejected), and web mode maps `user`/`0` to the configured `ZOTERO_LIBRARY_ID` unless `ZOTERO_LIBRARY_TYPE` is `group`. Reported by @mronkko.
- **A failed re-embed no longer deletes an item's passages** (#610). New passages are now written before the stale tail is pruned, so an embedding error no longer leaves the item unsearchable. A run with items still failing after the end-of-run retry no longer advances the sync watermark, and a process that fails to take the update lock no longer truncates the holder's PID.
- **Changing an item's type keeps type-specific fields** (#636). `zotero_update_item(fields={"item_type": ...})` dropped every field the new type names differently, so turning a journal article into a conference paper lost its `publicationTitle` instead of moving it to `proceedingsTitle`, and the success report did not mention it. Values now carry over through their shared Zotero base field, as in the desktop client, and the success report lists what was carried over.

## [0.13.1] - 2026-09-23

### Added

- **Sticky-note annotations.** `zotero_create_annotation` takes `note=[x, y]`, `zotero-cli annotations create` takes `--note x,y`, and `annotations batch` plans accept `{"page", "note", "comment"}`. The note is a 22 pt square centered on that normalized point, the same geometry Zotero's reader uses, and its text is the comment.

### Fixed

- **`zotero_find_related_papers` no longer under-reports silently** (#458, items 3 to 5). A referenced work missing from OpenAlex is now replaced by the next one, so asking for 20 references returns 20; a failed OpenAlex request is reported as incomplete or failed instead of as zero results; and citations are sorted most-cited first by OpenAlex itself, so the most-cited works are no longer missed.
- **Area boxes land in the right place on rotated pages.** On a page with `/Rotate` (e.g. a landscape figure page), the normalized rect was scaled to the displayed page but stored as if the page were unrotated, so the box was drawn somewhere else. Rects are now derotated first; highlights were not affected.
- **`update-db` progress lines no longer garble the terminal on CJK titles.** Lines are clipped by display width, so wide characters count as two columns, and cleared to the real terminal width (#585, thanks @jianxing-chen).
- **A failed search request hidden behind a page of notes is reported as an error.** In `titleCreatorYear` mode, a first page made up entirely of child notes followed by a failed next page came back as "No items found": the failure check ran before the note filter emptied the results (reported on #578).

## [0.13.0] - 2026-09-21

### Changed

- **FastMCP 4 is supported** (`fastmcp>=2.14.0,<5`). With it the HTTP server answers the MCP 2026-07-28 `server/discover` handshake that the ChatGPT Secure MCP Tunnel sends; legacy `initialize` clients keep working (#573).

### Fixed

- **Tool progress and error messages now reach the client.** The tools are synchronous while FastMCP's `ctx.info` / `ctx.warning` / `ctx.error` are coroutines, so every such message was an un-awaited coroutine and was silently dropped, on FastMCP 3 as well as 4. They are now handed to the event loop from the tool's worker thread.
- **`zotero_add_item_relation` and `zotero_remove_item_relation` no longer fail after writing the forward relation** when the reverse one cannot be written: the warning path called a `ctx.warn` that does not exist.
- The MCP `serverInfo.version` reports zotero-mcp's version, not FastMCP's.

## [0.12.5] - 2026-09-21

### Fixed

- **SQLite keyword search missed titles the API backend finds, and rendered them "Untitled"** (#570, #574). Queries hardcoded a fieldID for `title`, `date` and `publicationTitle`; they now resolve by name and per item type, as Zotero does, so cases (`caseName`), statutes, emails, webpages and libraries with non-standard field IDs are found, sorted and indexed correctly. The lookup is also cheaper than before: keyword search 3.6 ms to 1.3 ms on a 700-item library.
- **Collection-scoped search on the web API ignored the query** and returned the collection's first items. Regression from 0.12.1.
- **Child notes no longer crowd papers out of small `titleCreatorYear` result sets** on the API backend: the search pages past notes it is going to drop (#542).
- **A failed metadata search is reported as an error** instead of "No items found" followed by the fallback cascade (#578). Results already found by another query variant are kept.
- **Annotations no longer render as "Untitled"**: they show Zotero's own composed title, the quoted text and comment, or the type name for image and ink annotations (#575, #576). Annotation type 6 ("text") is no longer reported as an empty string.
- **`update-db --limit N` no longer marks the whole library as indexed** (#564). If you have used `--limit`, run `update-db --fulltext` or `--force-rebuild` once to repair the index.
- **Semantic search no longer fails with `embed_query() got an unexpected keyword argument 'input'`** when the config file is missing and ChromaDB rebuilds the stored embedding function (#565). The single-text method is now `embed_query_text`.
- **`zotero_find_related_papers` reported 0 citations for every paper** after OpenAlex removed `cited_by_api_url`; it now uses the `cites:` filter (#458, #566). Its "in library" flag also recognises DOIs stored as `doi.org` URLs (#568).
- **`export --format bibtex` failed with `'BibDatabase' object is not iterable`**, and `@inproceedings` entries had no `booktitle` (#579).
- **Legacy arXiv ids with a dotted archive (`math.GT/0309136`) are recognised**, so re-adding such a paper is deduplicated (#571).
- **Local writes are probed on `127.0.0.1`**, where pyzotero 1.15.2 sends them. Where `localhost` resolves to `::1` first, writes silently fell back to the web API (#567, #569).
- **The ChatGPT connector `search` tool falls back to keyword search** when the semantic extra or index is missing, instead of returning nothing (#572).

### Changed

- **A case's title carries its reporter or court**, as in Zotero: `Marbury v. Madison (5 U.S. 137)` (#576).
- **Creating a note from Markdown-looking text warns** that Zotero stores it literally (#503, #559).

## [0.12.4] - 2026-09-14

Found by using `zotero-cli` to read "Attention Is All You Need" and annotate it end to end.

### Added

- **`zotero-cli annotations batch`** creates many highlights and area boxes from JSON Lines (or a JSON array). 48 separate `annotations create` calls took 58 s. A batch reads the attachment's metadata and fetches the PDF once, then writes the annotations 50 per request, so its cost barely grows with the number of annotations. Every spec is attempted, each outcome is listed, and the exit code is 1 if any failed. `--dry-run` needs no write access and prints the words each highlight would cover, so misses are fixed before anything is written.
- **`zotero-cli layout ATTACHMENT_KEY [--pages 3-9]`** lists figure and table boxes with captions and a paste-ready `--rect` value. Layout detection was only reachable as an MCP tool.
- `zotero-cli annotations create` takes `--rect x,y,w,h` for area boxes and `--tags`, and `--color` (here and in `batch`) accepts Zotero's color names (`yellow`, `blue`, ...).
- **`zotero_read_pdf_pages` / `zotero-cli read` flag what the extracted text garbles and can return the page instead.** Text extraction drops math symbols and runs table cells together, and nothing told the reader. Each page now ends with a note naming its display equations, figures, tables and dense inline math ("Garbled in this text: Equation (1), Table 2"), found from the page's fonts and captions in a few milliseconds. `format='image'` (`--format image` on the CLI, which writes PNG files) returns up to 10 pages as images sized for vision models, and `rect` crops one region of a page at higher magnification. This extends the existing tool rather than adding one.
- **Layout detection finds display equations**, labelled with their number ("Equation (3)"), so equations can be boxed like figures and tables.
- The zotero-cli skill has a "Reading and annotating a paper" section.

### Changed

- **PDF reads open each file once and use files in local storage in place.** Creating annotations, layout detection over several pages, the PDF outline, full text and PDF annotation extraction each copied the attachment out of Zotero's storage and reopened it per step; a file on disk is now read where it is (read-only callers only), and one open document serves a whole call. Resolving an attachment's file on disk, previously written out in three places, is one `LocalZoteroReader.resolve_attachment_file`, which also brings the filename-drift fallback (#291) to PDFs found through their parent item. Fuzzy text matching skips windows that cannot beat the best match (same results, measured identical on 108 real queries), EPUB attachments are verified from their zip structure instead of a full parse, and unused matcher parameters are gone.
- **`zotero_delete_annotation` deletes permanently instead of moving the annotation to the Trash.** Zotero's PDF reader keeps drawing a trashed annotation and offers no way to remove it there, so re-annotating a paper left the old and new annotations overlapping on the page. Zotero's own reader deletes annotations outright; the tool now does the same.
- `zotero_read_pdf_pages` / `zotero-cli read` read through the last page when the end page is past it, and say so, instead of failing.
- Advice in `zotero-cli` output names CLI commands instead of MCP tools (`zotero-cli search --mode semantic`, not `zotero_semantic_search`).

### Fixed

- **`zotero-cli --json` reported failed writes as `ok: true`.** Most tools return failures as prose, and the CLI wrapped that prose in a success envelope with exit code 0, so an agent checking `ok` believed a refused annotation write had worked. Output that opens with a failure report (`Error...`, `Failed to...`, `Could not...`, `Cannot...`) is now an `ok: false` envelope with code `tool_error`, and exits 1 in both output modes.
- **Highlights covered more than the requested text.** Passages over 100 characters and fuzzy matches were boxed span by span, and a span is usually most of a line, so a highlight spilled onto the words before and after it. Matches are now clipped to the matched characters.
- **Layout detection missed tables drawn with horizontal rules only** (booktabs style, the norm in papers), because `find_tables()` needs a grid. Groups of three or more rules with the same span are now detected as tables. **Figures made of side-by-side panels** were reported as two regions with the caption on one; captionless panels in the same band above a figure caption now join the captioned region.
- **The PDF outline tool copied or downloaded PDFs that were already on disk.** `library.attachment_path_for` looked the key up as a parent item, and both of its callers pass an attachment key, so it never found the local file.
- **Layout detection reported fragments as regions of their own** (the rule band of one table row, a partial table inside the drawing that frames it). The outer box is now kept and takes the stronger source. Detection is also about three times faster: `find_tables()` is skipped on pages with no vertical strokes, which cannot hold a grid, and several pages share one open document.
- **Table captions were attached to the wrong table** when a venue puts them below tables: a "captions sit above tables" bonus pulled each caption onto the next table down. Table captions now go by distance alone.
- **Rules around an algorithm's title bar were reported as tables.** A rule-bounded table must now hold at least two rows of text.
- **A highlight found on a neighbouring page was labelled with the requested page.** It now carries the label of the page it was found on.
- **`zotero-cli --json notes list` always returned `count: 0`.** It read note keys from the listing with the item-key pattern (`**Item Key:**`), but notes are listed as `**Key:**`, so nothing matched.
- **PyMuPDF's "Consider using the pymupdf_layout package" notice went to stdout** on the first layout detection, corrupting `--json` output and the MCP server's stdio stream. It is suppressed.

## [0.12.3] - 2026-09-14

### Added

- `scripts/profile_recent_items.py` shows where `recent_items` spends its time on the SQLite backend: per-statement timings, query plans, the sort versus the scan, the tools' path through `get_library_backend()`, and the cost of a snapshot refresh. By @mronkko in #554.

### Fixed

- **`recent_items` on the SQLite backend reads `items` in the cheaper order for the scope.** Ranking a large personal library walked the `(libraryID, key)` index and fetched every row by rowid, which @mronkko profiled at 94 ms on a 52k-of-90k-item library, against 31 ms for a plain table scan with an `itemTypeID` subquery. The query now filters on `itemTypeID` directly and uses a full scan when the library is at least a fifth of `items`, keeping the index for small scopes such as a group library inside a large database, where it is still faster. Both plans return the same items.

## [0.12.2] - 2026-09-14

### Fixed

- **`zotero_advanced_search` returned wrong items for date ranges on the API path (#551).** A `date` condition with `isAfter`, `isBefore`, `isGreaterThan` or `isLessThan` compared Zotero's display date as text, so "Nov/Dec 1990" sorted after "2024" and "papers since 2024" returned papers from 1990, while an item with no date matched "before 1900". This affected every web-mode install and local mode whenever the SQL path was not used. Range conditions now compare `meta.parsedDate` padded to `YYYY-MM-DD`, exactly the value Zotero's own SQL compares, never the display text; an item with no date matches no condition; and `year` reads the ISO year too, so "October 1, 2016" is a 2016 item on both backends. Date range and year cases are now part of the backend parity suite. Reported, with the fix design, by @mronkko.

- **`zotero_get_tags` on the SQLite backend left out tags that exist only on trashed items (#552).** The API's `/tags` endpoint counts trashed items and the SQLite query excluded them, so the tag list differed depending on which backend answered. SQLite now lists them too, which keeps 0.12.1's change a pure speed-up. The live parity test for tags also gets its own timeout, since listing tags through the API takes about 99 s on a 44k-item library. Reported by @mronkko.


## [0.12.1] - 2026-09-14

### Added

- **In local mode, read tools answer from `zotero.sqlite` instead of the Zotero API, with the API as a per-call fallback.** Every read tool used to call pyzotero, so its access patterns were shaped by HTTP: one request per item, per page of 100, per parent's children. Reads now go through one read port with two implementations. The SQLite one answers from the database file in a fixed handful of queries, and is the default whenever `ZOTERO_LOCAL=true` and the database is readable. Measured on a 44,105-item library: one item 0.4 ms vs 43.5 ms, children of 25 items 1.8 ms vs 2,476 ms, all tags 110 ms vs 103.9 s. Anything SQLite cannot express, such as a wildcard tag filter or a boolean `itemType` expression, is re-asked of the API for that call, and `ZOTERO_BACKEND=api` restores the old behaviour. It also keeps read tools working with Zotero closed, makes global search (`search_all_libraries`) available by default in local mode, and fixes four bugs the port exposed: `search_items(collection_key=...)` reporting an existing collection as missing when Zotero was closed, `search_by_citation_key` missing keys outside a 25-result window, `items(itemKey=...)` returning child notes, and a redundant probe in `switch_library`. Readers held across tool calls reopen when Zotero writes, and the WAL snapshot of a large database is refreshed at most once per `ZOTERO_MCP_DB_SNAPSHOT_MIN_INTERVAL` seconds (default 5). Designed, built and measured by @mronkko (#502).

### Fixed

- **Semantic search no longer fails outright after documents are deleted while the server is running (#545).** A server kept open while another process (a CLI update, or the deletion pass) removed documents could get hits back whose document text was `None`; the cross-encoder reranker accepts only strings, so every search failed with `Unsupported input type: NoneType` until restart. Those hits are now dropped before reranking and enrichment, with a warning, and the search returns the rest. Reported by @Aboottogo in #457.

- **`update-db` could sit silent for hours in the rate limiter (#548).** The token bucket's capacity was fixed from the initial tokens-per-minute while every 429 halved the rate, and the wait is the deficit divided by the rate, so with Gemini's default budget against a free-tier account a single request waited about 34 hours, indistinguishable from a hang; running out of API credit mid-run did the same. Capacity now follows the current rate, so a wait is at most a quarter-minute of budget, and a wait of 30 seconds or more is logged. Found and fixed by @ArneBouten.

- **`if_exists='file'` no longer creates a duplicate of an item that is already in the library.** The dedup lookup narrowed candidates with a server-side quick search on the identifier itself — `zot.items(q=<doi>, qmode='everything')`. Zotero's `q` searches titles and creator fields, and the API documentation notes that "searching of other fields will be possible in the future", so `DOI`, `url`, `archiveID` and `extra` are not searchable server side today. Measured against a 16,859-item library, six of six items selected *for having a DOI* returned zero hits when searched by their own DOI, and no spelling of a stored preprint's arXiv ID found it while a title query found it immediately. The search returned nothing, the client-side comparison never ran, the caller read that as "not in the library", and a duplicate was created — which is every `if_exists='file'` add, the `zotero-cli` default. The existing tests could not catch it: the fake client ignores `q` and hands back the whole library, so the matcher was exercised and the search never was.

  Where a title has already been fetched it is now passed alongside the identifier, and a miss on the identifier query falls back to querying the title — the one field the API does index — running the same identifier comparison over those candidates. **The identifier still decides**, so this widens the net without loosening the test: a same-title-different-paper is rejected exactly as it was. Wired into the six checks that hold metadata by the time they run — the arXiv path after its metadata fetch, the DOI batch path from the CrossRef payload, the ISBN path after the Open Library / Google Books lookup, the BibTeX/CSL-JSON batch import, which carries hundreds of entries per call and was re-creating every one of them, and the ISBN and URL checks `add_by_url` makes before creating an item from a page's embedded metadata (#515). Note the cost: because the identifier query almost never matches against the Web API, a caller that passes a title should expect two searches per check rather than one. The identifier query is kept in front of the fallback rather than skipped, because `qmode='everything'` also searches child-attachment full text and finds an item whose stored title no longer matches the one just fetched — neither of which the title query covers.

  Titles are normalized before they are used as a query. Zotero's quick search splits on whitespace and requires every token to match (measured: reordering a title's words still finds it; appending one junk word drops it to zero), so markup in the fetched title — a real `<i>`, `<sub>` or `&amp;` — is a token that matches nothing, and one italicised species name took the whole lookup to zero against an item whose stored title is clean. The CrossRef mapping's own clean-up (see "A DOI added here now reads like the same DOI saved from the browser") does not make a title a search key: it keeps the inline markup Zotero renders — `<i>`, `<b>`, `<sub>`, `<sup>`, small caps as a styled `<span>` — which is exactly the markup at issue, and titles from arXiv, Open Library, a landing page or a BibTeX/CSL-JSON entry never pass through it. Since that mapping decodes entities first, a `<` not followed by a letter is kept as text, so a title about `&lt;10 Hz` keeps the words between its `<` and a later `>`. A tag is replaced by a space rather than deleted, because every token has to occur in the stored title whether Zotero kept the markup or not: measured, `DREAM(D): …` does not find an item stored as `DREAM<sub>(D)</sub>: …`, while `DREAM (D) : …` does.

- **The dedup candidate window no longer truncates the item it is looking for.** `find_existing_items` narrowed with `limit=50`, which never mattered while the only query was on an identifier the API cannot search — that query returns nothing, so the window was never full. A title query fills it. Quick search matches each token as a substring, so a short title pulls in far more than it appears to (`Dependence` matches 91 items in a 16,861-item library; `Noise` 87), and results are sorted `dateModified` descending — an ordering that works against the caller, since the item being deduped against is by definition already in the library and therefore competing with everything touched since. Measured at `limit=50`, a book titled `Stochastic Processes` (83 matches, last modified 2024) fell outside the window and would have been duplicated. Raised to 100, the API maximum, which covers every over-50 title in that library.

- **An arXiv paper already in the library is recognized however it got there.** The arXiv matcher checked two of the four places Zotero can record an arXiv identity, and compared version-sensitively. An item saved by the browser connector stores the ID in `archiveID`, and one added by DOI carries only arXiv's DataCite DOI (`10.48550/arXiv.<id>`); neither field was checked, so a later add of the same paper by its arXiv ID saw nothing and duplicated it. Separately, `.../abs/2509.04259v1` and a stored `.../abs/2509.04259` compared unequal, so re-adding a paper at a different version duplicated it too. Matching now goes through a version-independent identity that also accepts the DataCite DOI form. `_normalize_arxiv_id` is untouched — it deliberately keeps the version because its other callers use the result to fetch a specific one — so fetch behaviour is unchanged.

- **`zotero_get_item_fulltext` says when a document was cut short.** Reads stop at `fulltext_display_max_pages` (10 by default), but the text returned for the first ten pages of a 442-page book was byte-for-byte indistinguishable from the text returned for a complete three-page note: same `## Full Text` heading, no marker anywhere, and a tool description that promised "the entire paper". An agent summarized and cited the excerpt as if it had read the whole thing. The parser already reports `page_count` and `truncated` on every extraction, so both capped paths — local storage and the last-resort download+parse — now carry them through to the heading: `## Full Text (pages 1-10 of 442 — TRUNCATED)`, followed by the config key to raise and the `zotero_read_pdf_pages` call that resumes at the first unread page. A read that fits under the cap is unchanged. Zotero's own `.zotero-ft-cache` and the transient fulltext cache are not page-capped and are never reported as truncated. `LocalZoteroReader`'s parser seam returns the whole `ExtractedDoc` instead of just its text, so page bookkeeping has one source of truth rather than being re-derived by callers (#448).

## [0.12.0] - 2026-09-13

### Added

- **`zotero_update_collection` renames a collection or moves it under another parent (#517).** The collection keeps its key, subcollections and item membership, so renaming no longer means creating a new collection, refiling every item and deleting the old one. The parent can be given by key or name, `to_top_level=True` moves a collection out of any parent, and a move under the collection itself or one of its own subcollections is refused rather than sent to Zotero. Also available as `zotero-cli collections update KEY --name ... --parent ... --top-level`.

- **The open-access PDF cascade now tries the `citation_pdf_url` the article's own landing page advertised (#229).** `html_metadata.EmbeddedMetadata` has parsed that tag since the embedded-metadata reader landed and nothing had ever read the field — grep for `pdf_url` outside `html_metadata.py` and the only hits were local variables and arXiv's separately-constructed URL. It is how the browser connector finds a PDF: OJS/PKP, Atypon, Silverchair, Highwire and Springer all publish it, and for a paper no aggregator has indexed it is the only source there is. It goes *after* Unpaywall, not before. The case for first was that it names the version of record; the case against is stronger, because it is also the URL most likely to answer with an access-denied page, a cover sheet or a first-page preview — a real PDF, of the right content type, above the size floor — and the cascade returns on the first success, so a stub would win outright over the full text Unpaywall had. Losing a known open-access copy to a paywall stub is the worse failure, and behind Unpaywall the source still does the thing it was added for. `citation_pdf_url` is stored as deposited and OJS/DSpace/EPrints all deposit it relative, so it is resolved against the page's post-redirect URL — unresolved it could never download, and, being the first URL *found*, it displaced the absolute Unpaywall URL in the "a URL was found" message users are told to try through their library. The "no open-access PDF found" message now derives its source list from the table it describes, which had already drifted: the table said "PubMed Central", the sentence said "PMC".

  Two sinks had been relying on their inputs being curated, and this is the first source that comes from arbitrary third-party HTML rather than a known API over a known host, so both are fixed here. `_attach_pdf_linked_url` never fetches, so `attach_mode="linked_url"` never reached the SSRF guard at all — a `file:///etc/passwd` in a meta tag would have been written into the library verbatim; it is scheme-checked now, which closes the same hole for the aggregator sources. `_download_and_attach_pdf` trusted `Content-Type`, and Cloudflare interstitials and "PDF viewer" endpoints — exactly what a publisher's `citation_pdf_url` points at — serve HTML under `application/pdf`, which was attached as if it were the paper; it now checks the bytes for a `%PDF` header. The download path's SSRF protection is otherwise inherited correctly: `_guarded_pdf_get` rejects non-http(s) schemes and non-global addresses, and re-validates every redirect hop.

  Scope is the DOI path only. `add_by_url`'s embedded-metadata branch creates items with no attachment at all and would benefit from the same URL, but `_add_from_embedded_metadata` runs under `@with_zotero_api_lock` on the stated grounds that it makes no third-party call, and adding a download inside it would hold the process-wide lock across a publisher fetch — the starvation #A5b exists to prevent. Doing it properly means restructuring that function to create under the lock and attach outside it, which is a separate change rather than a rider on this one.

### Changed

- **A single-DOI add now reads the article's landing page, where before it only did so for a CrossRef record that could not stand alone.** This supersedes the last sentence of the `zotero_add_by_url` entry in 0.11.0 ("The fetch is gated on that condition, so a DOI whose CrossRef record is complete never pays for it"), which was true as shipped and is no longer. The gate was sound given what "complete" was taken to mean, and 10.1006/bulm.1999.0141 is the counterexample: a title, a journal, a volume, pages and an author, passing every thinness test — while naming one of the paper's four authors and carrying no abstract. Nothing in the record says so, so the only way to find out is to look. The cost is one bounded GET (512 KB cap, 15 s timeout, outside the Zotero API lock) and every failure mode degrades to the record CrossRef gave us.

  **Callers adding more than one item are exempt**, because the cost is per item. Passing several DOIs to one call is exempt automatically; a caller that loops over single items has to say so, and `add_by_doi`/`add_by_url` take `page_check="thin_only"` for that. Two such callers exist and both pass it: `add_by_url`'s multi-URL recursion — which handles the mixed DOI/arXiv/webpage batches this project advertises, one URL at a time, so it would otherwise have turned a 200-link import into 200 publisher requests — and `add_from_file`, whose directory mode is a loop over single PDFs.

### Fixed

- **Items deleted from Zotero stayed in semantic search results indefinitely when the index was updated from the MCP tool in local mode (#457).** The pass that removes deleted items ran only on the incremental path, and the MCP tool always takes the full-scan path in local mode because it extracts fulltext. The full scan then promoted the sync watermark anyway, so a later `zotero-mcp update-db` found the library unchanged and never reconciled either. The deletion pass now runs on both paths (not on a forced rebuild, which starts empty, or a `limit` test run), with the same library scoping and mass-deletion guard; a full scan that cannot list the library's items keeps the watermark where it was. The tool's result also reports how many items were deleted and why a deletion check was skipped.

- **Text from a deleted or replaced attachment kept matching semantic searches after an incremental update (#428).** An indexed item whose text-bearing attachments had all been removed fell through to "already up to date", so its PDF-derived passages stayed in the index until a forced rebuild; a replaced PDF with the same priority was treated the same way. Such items are now re-indexed: metadata-only when no attachment is left, re-extracted when the stored attachment set differs from the current one. Documents indexed before attachment keys were recorded are not compared, so upgrading does not re-extract the whole library. Diagnosis by @feima3333.

- **Attaching a file in hybrid mode reported success even when the file could never reach this computer (#463).** With local reads and web writes, an uploaded file goes to cloud storage (Zotero's or WebDAV) and appears in `~/Zotero/storage` only when Zotero syncs files. With file syncing turned off it never did: the attachment looked fine, and every local tool that later resolved its path failed. When the upload went through the web API, the server reads locally, and every Zotero profile on the machine has file syncing off, the success message now says so and points to turning file syncing on or, on Zotero 10, `zotero-mcp authorize-local`, which writes files straight to the local library.

- **`add_by_url` could create a duplicate from a page's embedded metadata even with `if_exists='file'` or `'skip'` (#515).** When a page carried citation tags but no DOI, the item was built from those tags without checking the ISBN the page declared, and without re-checking the URL after the page fetch, so an item saved under a different URL, or one a parallel add created while the page was loading, was added again. That branch now looks for an existing item by the page's ISBN and then by URL, under the same identifier lock the plain-webpage branch uses. URLs are still compared exactly (after trimming); matching by title is left to #506.

- **`~/.config/zotero-mcp` was created with the process umask, usually `0755`, so other accounts on the machine could read the semantic index (#401).** That directory holds `chroma_db`, which stores the indexed metadata and full text of the library, next to `config.json`. It is now created owner-only (`0700`) by every code path that can be first to create it: the setup wizard, `update-db`, the semantic search client, local write authorization and the update lock. An existing directory is not changed, since its mode may be deliberate; if other users can read it, a warning says how to restrict it. Based on a PR by @michaelzehetleitner.

- **Local reads could not see anything Zotero had written since its last checkpoint, so tools reported "No PDF attachment found" for a paper added minutes earlier (#536).** Zotero keeps `zotero.sqlite` in WAL mode under an exclusive lock. The reader opened it with `immutable=1`, which gets past the lock by ignoring the `-wal` file, so new items, their attachments and recent edits stayed invisible until Zotero restarted; for linked files there was no download fallback either. When the WAL holds changes, the reader now opens a private copy of the database together with its WAL, which SQLite applies on open. The copy lives in an owner-only temp directory, is shared across readers in the process until either file changes, is retried if a checkpoint lands mid-copy, and is removed at exit. With no WAL the database is read in place as before; if a copy cannot be made the reader falls back to the in-place read, and `ZOTERO_MCP_DB_SNAPSHOT=0` turns the copy off for very large databases.

- **A `uv tool` or pipx install outside the default directories was told to `pip install` a missing extra (#534).** The installer was detected from the package's path alone (`.../uv/tools/...`, `.../pipx/venvs/...`), so an environment under a custom `UV_TOOL_DIR`, `PIPX_HOME` or relocated data directory fell through to the generic hint, which leads with `pip`. Detection now also checks for the marker each installer writes at the environment root, `uv-receipt.toml` or `pipx_metadata.json`, so those installs get the command that works for them.

- **Better BibTeX and pdfannots diagnostics were printed to stdout, which broke `zotero-cli --json annotations list` and could corrupt the MCP stdio stream (#529).** When Better BibTeX rejected a cite-key search, the client printed `Error searching for cite keys: ...` before falling back to the Zotero API, so the command's stdout was that line followed by a valid envelope, and `json.loads`/`jq` failed on a lookup that had succeeded. The pdfannots helpers on the `zotero_get_annotations` path printed install and extraction messages the same way, and on the stdio transport stdout is the JSON-RPC channel. All of these now go through `logging`, which writes to stderr.

- **`zotero-cli --json get children` with more than one parent key reported `count: 0` (#505).** Several keys render through the grouped children listing, whose `  - [KEY] Attachment: ...` lines matched none of the shapes the JSON path reads keys back from, so every child was dropped before the fetch. That shape is now recognised too, the same kind of fix #504 made for the single-parent listing.

- **Items with no text-bearing attachments were reported as "PDF extraction previously failed", and a rebuild made the count grow (#446).** Any item whose extraction pass produced no text was marked `has_fulltext="failed"`, including notes-only entries, webpage stubs and books with no file, which never had anything to extract. Every later run listed them as failed extractions, and `--force-rebuild` re-marked all of them, so the number went up (928 to 2273 in the report) while the rebuild itself said `0 errors`. Items with no text-bearing attachments now get no marker: they index metadata-only, count as up to date, and re-index when a first attachment appears. Legacy markers on such items are reported as metadata-only rather than as failures, and the run that does the extraction now says how many items had attachments that produced no text.

- **`zotero-mcp update-db --batch --openai-batch` read the config and built the embedding client before rejecting the flag combination.** The conflict is a usage error, so it is now checked first, before anything reads or writes the config. The old order also made `tests/test_generic_batch_flags.py` depend on the developer's real config and on that config's embedding provider being installed.

- **The test suite was red on a clean checkout against pyzotero >=1.15 (#511).** pyzotero 1.15 moved off httpx onto httpx2 — httpx 2.x, published under a separate distribution name, and a disjoint class hierarchy. `tests/test_rate_limit_guard.py` did `import httpx`, which resolves to the httpx 0.28 that `mcp`/`httpx-sse` pull in regardless of pyzotero, and built its 429 fixtures as `httpx.Response`. pyzotero's `_post_check` catches only `httpx2.HTTPError`, so those responses escaped it uncaught: the retry loop never saw the 429, `error_handler` never recorded the backoff, and `TooManyRetriesError` was never raised. Five tests failed — three leaking `HTTPStatusError` where they expect a retry or the typed error, two degrading to `[]` because `find_existing_items`' generic `except Exception` caught the leaked error instead of the typed clause above it. Since CI installs bare `pyzotero` against a floor with no ceiling and there is no lockfile, it resolves whatever is newest, so every PR opened after 1.15.0 reached PyPI showed the same five red checks. The fixtures are now built from whatever HTTP library the installed pyzotero speaks — asked of pyzotero rather than guessed at import time, since both libraries can be installed at once — which keeps the declared `pyzotero>=1.13.5` range green rather than narrowing it. Nothing the tests pin actually changed: 1.15.1 still retries a rate-limited read three times and still waits out a server-supplied backoff. The same mismatch has a *runtime* consequence on the local-API path, where our own httpx 0.28 
- **The test suite was red on a clean checkout against pyzotero >=1.15 (#511).** pyzotero 1.15 moved off httpx onto httpx2 — httpx 2.x, published under a separate distribution name, and a disjoint class hierarchy. `tests/test_rate_limit_guard.py` did `import httpx`, which resolves to the httpx 0.28 that `mcp`/`httpx-sse` pull in regardless of pyzotero, and built its 429 fixtures as `httpx.Response`. pyzotero's `_post_check` c
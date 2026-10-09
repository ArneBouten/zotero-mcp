# The citation graph

zotero-mcp keeps each paper's reference list from [OpenAlex](https://openalex.org) in
`~/.config/zotero-mcp/citations.json`. Questions about who cites what are then answered from
that file, at once, for the whole library:

| Ask Claude | Answer |
|---|---|
| What does this paper build on? | Its references, split into papers in your library and works not in it |
| Which papers in my library cite X? | X can be a paper in the library, a DOI or an OpenAlex ID; also the most-cited papers outside your library that cite it |
| Which papers are related to this one? | Papers in your library sharing the most references with it |
| What are the key papers in this collection? What am I missing? | The papers cited most within the collection (or the library), and works the collection cites often that are not in your library |

Claude uses the tool `zotero_citations` for these.

## Keeping it up to date

- **New papers** are added when they are imported (the action "Check & complete new papers") and whenever you run Check & complete or Check metadata only: papers not in the graph yet, or whose DOI was added or corrected, are looked up at the end of the run.
- **The rest of the library** is added the first time a question needs it, or at once with `zotero-mcp citations --update`. A large first run continues in the background, at low priority.
- Papers OpenAlex does not know are tried again after 90 days; reference lists are refreshed after a year.

## Coverage and costs

- Papers with a DOI are looked up 50 at a time. Papers without one are looked up by title and accepted only when title, year (±1) and first author agree.
- OpenAlex's reference lists are good for journal articles of the last decades; they are often missing for books, chapters and older papers. OpenAlex sometimes lists a work twice (a preprint and the article); a work whose title and year match a paper in your library counts as that paper.
- Cost with a free OpenAlex key (`OPENALEX_API_KEY` in `keys.env`, $1 a day): 50 papers by DOI cost $0.0001, a title lookup $0.001, single works are free. Filling a library of 2,000 papers once costs well under $1, most of it for papers without a DOI.

## In a terminal

```powershell
py -3.12 -m zotero_mcp.cli citations --update                 # add what is missing (whole library)
py -3.12 -m zotero_mcp.cli citations --references KEY
py -3.12 -m zotero_mcp.cli citations --cited-by KEY_OR_DOI
py -3.12 -m zotero_mcp.cli citations --related KEY
py -3.12 -m zotero_mcp.cli citations --overview --collection COLLKEY
```

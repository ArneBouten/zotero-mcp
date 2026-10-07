# Fetching full texts

`zotero-mcp fetch-fulltext` (and the `zotero_fetch_fulltext` tool) finds a PDF
for items that have none and attaches it. It repeats the steps you would take
by hand, cheapest first, and stops at the first PDF that passes its checks.

| Step | What it does | Needs |
|---|---|---|
| `open-access` | Unpaywall, OpenAlex, Semantic Scholar, Europe PMC / PubMed Central, arXiv, CORE, OAPEN (books) | Nothing; keys raise the limits |
| `publisher` | The DOI's landing page and its `citation_pdf_url`, plus the PDF addresses of Wiley, Taylor & Francis, SAGE and Springer. Items without a DOI: their URL field | A network the library recognises (eduroam, campus) for subscribed papers |
| `scholar` | Google Scholar through SerpApi: the result's [PDF] link, then the other versions in its cluster | `SERPAPI_API_KEY` (250 free searches a month) |
| `web` | A web search for the exact title through Tavily; ResearchGate pages through the ZenRows unblocker | `TAVILY_API_KEY` (1,000 free searches a month); `ZENROWS_API_KEY` optional |

Only copies you can open yourself are fetched: open-access copies, public
downloads, and subscriptions through your own network. Captchas are never
solved; a page that shows one is logged and skipped. No shadow libraries.

## What it checks before attaching

- The file is a real PDF, not a web page, a cover page or a preview: with a
  page range on the item, the PDF must have at least half those pages.
- The item's title and first author appear on its first pages. A scan without
  a text layer is accepted only when it was found by the item's DOI or ISBN.
- The version is recorded in the attachment's title: `Full Text PDF
  (published version)`, `(accepted manuscript)`, `(preprint)` or `(version
  unknown)`. A note on the attachment gives the source and address.

## Tags and logs

- `fulltext/fetched` on every item that got a PDF, plus
  `fulltext/accepted-manuscript` or `fulltext/preprint` when it is not the
  published version.
- `fulltext/not-found` on items with no usable copy. They are skipped for 30
  days unless you pass `--retry` (or name the item).
- Each run writes a report to `~/.config/zotero-mcp/fulltext/runs/`, and every
  item's attempts (source, address, outcome) are appended to
  `~/.config/zotero-mcp/fulltext/log.jsonl`.

## Running it

```
zotero-mcp fetch-fulltext --dry-run --limit 10      # find and check, attach nothing
zotero-mcp fetch-fulltext --limit 25                # attach
zotero-mcp fetch-fulltext --items ABCD1234,EFGH5678
zotero-mcp fetch-fulltext --collection KEY --steps open-access,publisher
zotero-mcp fetch-fulltext --save-dir C:\temp\pdfs   # save instead of attaching
zotero-mcp fetch-fulltext --open-missing            # open Scholar for what is left
```

Zotero must be running, with local writes authorized (`zotero-mcp
authorize-local`) or web API credentials set. Requests to one site are spaced
at least 6 seconds apart, so a run of 25 items takes several minutes.

## Keys and limits

Keys are read from the environment, then from `client_env` in
`~/.config/zotero-mcp/config.json`, so the command line and the MCP server see
the same ones:

```json
"client_env": {
  "OPENALEX_API_KEY": "…", "SERPAPI_API_KEY": "…", "TAVILY_API_KEY": "…",
  "ZENROWS_API_KEY": "…", "CORE_API_KEY": "…", "UNPAYWALL_EMAIL": "you@example.org"
}
```

Use is counted per month in `~/.config/zotero-mcp/fulltext/budget.json` and
stops at the free allowance. Change the limits in a `fulltext_fetch` section
of `config.json`: `serpapi_monthly` (250), `tavily_monthly` (1000),
`zenrows_monthly_credits` (5000), `openalex_content_daily` (100),
`host_delay` (6 seconds), `retry_days` (30), `max_candidates` (12 per item).

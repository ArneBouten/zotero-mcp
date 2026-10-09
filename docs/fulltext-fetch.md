# Fetching full texts

`zotero-mcp fetch-fulltext` (and the `zotero_fetch_fulltext` tool) finds a PDF
for items that have none and attaches it. It repeats the steps you would take
by hand, cheapest first, and stops at the first PDF that passes its checks.

| Step | What it does | Needs |
|---|---|---|
| `open-access` | Unpaywall, OpenAlex, Semantic Scholar, Europe PMC / PubMed Central, arXiv, OSF preprints (PsyArXiv and others), CORE, Zenodo, HAL, OAPEN (books) | Nothing; keys raise the limits |
| `publisher` | The DOI's landing page and its `citation_pdf_url`, plus the PDF addresses of Wiley, Taylor & Francis, SAGE and Springer. Items without a DOI: their URL field | A network the library recognises (eduroam, campus) for subscribed papers |
| `scholar` | Google Scholar through SerpApi: the result's [PDF] link, then the other versions in its cluster | `SERPAPI_API_KEY` (250 free searches a month) |
| `web` | A web search for the exact title through Tavily; ResearchGate pages through the ZenRows unblocker | `TAVILY_API_KEY` (1,000 free searches a month); `ZENROWS_API_KEY` optional |
| `browser` | Your own Chrome window with your logins: links that blocked a plain download earlier in the run (ResearchGate, Academia.edu, SSRN, bot-protected publishers), ResearchGate searched by title, and the publisher's page | Playwright and Google Chrome; only with `--browser` |

Within a step, the services are asked at the same time; the links they
return are then downloaded one at a time, in the order of the table, with
at least 6 seconds between two requests to the same site.

Only copies you can open yourself are fetched: open-access copies, public
downloads, and subscriptions through your own network. Captchas are never
solved; a page that shows one is logged and skipped. No shadow libraries.

## The browser step

Some copies only come through a browser: ResearchGate's and Academia.edu's
"Download" buttons, and publisher pages behind bot protection or an
institutional login. With `--browser`, the fetcher opens its own Chrome
window (its own profile in `~/.config/zotero-mcp/fetch-browser`, separate
from your everyday Chrome) for the papers the other steps could not find.

Once, beforehand:

```
pip install playwright
zotero-mcp fetch-fulltext --browser-login
```

The second command opens the window with ResearchGate and Academia.edu login
pages; log in there, and on any publisher site you use through your
university ("Access through your institution"). The logins are kept for
later runs.

During a run the window stays minimised in the taskbar, so it does not get in
your way; it waits 10–20 seconds between papers. When a captcha or a login
page appears, the window comes forward, maximised, with a sound, and waits up
to five minutes for you to deal with it; afterwards it goes back to the
taskbar. It never solves captchas (a check that only passes in your own
browser is handed to it, see below). `"browser_minimized": false` in the
`fulltext_fetch` section of `config.json` keeps the window visible throughout. Off campus, a library proxy
prefix can be set as `fulltext_fetch.proxy_prefix` (the part of a proxied
link before the encoded address, ending in `?url=`).

```
zotero-mcp fetch-fulltext --browser                 # all steps, browser last
zotero-mcp fetch-fulltext --retry --steps browser   # only the browser, for what is still missing
```

A run without the browser remembers the links that refused a plain download,
so a later browser-only run (the "Check PDF (browser only)" action in Zotero, or the
window's button) starts with them.

### Bot checks only your own browser passes

Some sites (Cloudflare's "Performing security verification", on Taylor &
Francis among others) check whether a script drives the browser. In the
fetcher's Chrome that check starts again after every attempt you make, while
the same link passes in your everyday browser. The fetcher does not try to hide
that it is a script. It gives such a page a few seconds; after that the paper
is marked **needs your browser** (not `fulltext/not-found`), and the site is
not opened again in that run.

The progress window then offers **Open N in my own browser**. That opens the
links (five at a time) as ordinary tabs in your default browser, with your
logins. Download the PDF there, into your Downloads folder. The window watches
Downloads for 15 minutes: a new PDF that matches one of the papers (title,
first author, DOI) is attached with a note, and tagged like any other. The tab
stays open and the downloaded file stays in Downloads; Zotero keeps its own copy.

Without the window, the end of the run lists the links and the command for
afterwards:

```
zotero-mcp fetch-fulltext --from-downloads --items KEY1,KEY2   # PDFs from the last 24 hours, then waits 15 min
```

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
zotero-mcp fetch-fulltext --collection KEY --window # with the progress window
```

Zotero must be running, with local writes authorized (`zotero-mcp
authorize-local`) or web API credentials set. Requests to one site are spaced
at least 6 seconds apart.

**Several papers at once.** Four papers are searched at the same time
(`--workers N`); requests to one site stay spaced out. The browser step comes
after the other steps, for the papers still missing, one at a time in one
Chrome window.

**The progress window** (`--window`; the right-click actions in Zotero use it)
lists every paper with its status, like Zotero's own Find Full Text: searching,
attached (and from where), no file found. As soon as one paper is not found, a
button offers the browser step for the papers not found so far; it stays there
at the end, with a summary and the run's report. Papers behind a bot check get
a button that opens them in your own browser (see above). Both buttons sit in a
"To do" panel that appears when something is left for you. Coloured counts
above the list explain themselves when you hover over them; a click selects
those papers. Double-clicking a paper shows
it in Zotero. The terminal opens minimised and keeps the details.

## Keys and limits

The simplest place for keys is a plain text file, `~/.config/zotero-mcp/keys.env`
(on Windows `C:\Users\<you>\.config\zotero-mcp\keys.env`), one per line:

```
SERPAPI_API_KEY=...
TAVILY_API_KEY=...
OPENALEX_API_KEY=...
CORE_API_KEY=...
ZENROWS_API_KEY=...
UNPAYWALL_EMAIL=you@example.org
```

Lines starting with `#` are ignored. The environment wins over this file,
and this file over `client_env` in `~/.config/zotero-mcp/config.json`:

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
`host_delay` (6 seconds), `retry_days` (30), `max_candidates` (12 links per step and paper).

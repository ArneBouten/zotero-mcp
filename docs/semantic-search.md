# Semantic search

Find research by concept and meaning, not just keywords. Requires the `[semantic]` extra:

```bash
pip install "zotero-mcp-server[semantic]"
```

- **Vector-based similarity search** over your entire research library
- **Multiple embedding models**: Default (free, local), OpenAI, Gemini, and Ollama
- **Similarity scores** with each result
- **Auto-updating database** with configurable sync schedules

## Setup

During setup or separately, configure semantic search:

```bash
# Configure during initial setup (recommended)
zotero-mcp setup

# Or configure semantic search separately
zotero-mcp setup --semantic-config-only
```

**Available embedding models:**
- **Default (all-MiniLM-L6-v2)**: Free, runs locally, good for most use cases
- **OpenAI**: Better quality, requires API key (`text-embedding-3-small` or `text-embedding-3-large`)
- **Gemini**: Better quality, requires API key (`gemini-embedding-001`)
- **Ollama**: Runs locally via Ollama API (requires model name, e.g., 'qwen3-embedding')

### Ollama

Install and start Ollama, then pull an embedding model before running `zotero-mcp update-db`:

```bash
ollama serve

# Small model: fast and lightweight
ollama pull nomic-embed-text

# Medium model: better multilingual retrieval quality
ollama pull bge-m3
```

When prompted by `zotero-mcp setup --semantic-config-only`, choose **Ollama** and use either `nomic-embed-text` or `bge-m3` as the model name. If you change embedding models later, rebuild the index:

```bash
zotero-mcp update-db --force-rebuild
```

Two `semantic_search.embedding_config` keys tune the Ollama path for slower
hardware or very large libraries:

```jsonc
"embedding_config": {
  "model_name": "bge-m3",
  "timeout": 600,            // HTTP timeout per /api/embed call (default 120s)
  "request_batch_size": 64   // documents per request (default 64)
}
```

Raise `timeout` if indexing reports `Read timed out`; lower
`request_batch_size` to make each request cover less GPU work, which usually
fixes timeouts more reliably than raising the timeout alone.

### OpenAI Batch API

When you choose OpenAI, setup also asks whether database updates should use
OpenAI Batch API. Batch updates are cheaper for large libraries, but they are
asynchronous: submit the batch, wait for completion, then import the embeddings.

### Update frequency

- **Manual**: Update only when you run `zotero-mcp update-db`
- **Auto on startup**: Update database every time the server starts
- **Daily**: Update once per day automatically
- **Every N days**: Set custom interval

With any automatic setting, an update that is due also runs in the background
when you search. In local mode the server keeps a fingerprint of your library
(item count, newest item, latest modification, trash) from the last complete
update, which makes two things possible:

- **Nothing changed, nothing scanned.** An unchanged library skips the update
  at startup and at search time, instead of re-reading every item.
- **Something changed, this search sees it.** When the library has changed —
  you just added a paper — the search waits for the update before answering,
  up to `update_config.presearch_wait_seconds` (25 by default; 0 never
  waits). If the update needs longer, the search answers anyway and says the
  newest additions may be missing; they appear in the next search.

Updates take a lock in `~/.config/zotero-mcp/update.lock`, on Windows as well
as macOS and Linux, so the startup update, a search-time update and a manual
`zotero-mcp update-db` never index the same collection at once.

The startup update runs in a separate background process, at low priority, so
the server answers requests straight away even while it extracts and OCRs a
batch of new papers. Its progress goes to the server log. If the server stops
first, the update carries on and finishes; one that is cut short anyway picks
up where it left off at the next start. Set `ZOTERO_MCP_UPDATE_IN_PROCESS=1`
to run it inside the server instead, as before 0.13.2+arne.9.

A server that is already running notices when another process (the startup
update, or a manual `zotero-mcp update-db`) has added to the index, and loads
the new passages before its next search, without a restart.

## Building and updating the index

After setup, initialize your search database:

```bash
# Build the semantic search database (fast, metadata-only)
zotero-mcp update-db

# Submit OpenAI embeddings through Batch API for this update
zotero-mcp update-db --openai-batch

# Check and import completed OpenAI Batch API embeddings
zotero-mcp openai-batch-status
zotero-mcp openai-batch-import

# Force realtime OpenAI embeddings even if Batch API is enabled in config
zotero-mcp update-db --no-openai-batch

# Build with full-text extraction (slower, more comprehensive)
zotero-mcp update-db --fulltext

# Use your custom zotero.sqlite path
zotero-mcp update-db --fulltext --db-path "/Your_custom_path/zotero.sqlite"

# If you have embedding conflicts or changed models, force a rebuild
zotero-mcp update-db --force-rebuild

# Check database status
zotero-mcp db-status
```

How much of each PDF is extracted, and which attachment is read when an item has several, is set in [Text extraction settings](configuration.md#text-extraction-settings).

## Passage indexing

By default each item is one vector: its metadata, abstract and as much full
text as the embedding model accepts. With chunking on, each item is indexed as
overlapping passages instead, so a search can land on page 14 of a paper and
quote it, and long documents are searchable past the model's input limit.

```jsonc
"semantic_search": {
  "chunking": {
    "enabled": true,
    "chunk_size": 1500,            // characters per passage
    "overlap": 200,                // characters shared by neighbouring passages
    "max_chunks_per_item": 20      // guard against very long documents
  }
}
```

Changing any of these needs a rebuild (`zotero-mcp update-db --fulltext --force-rebuild`).

### Section labels

Each passage records the section of the paper it falls in — Abstract,
Introduction, Methods, Results, Discussion, Conclusion, References, Appendix or
Back matter — and search results show it first in the **Location** line. A hit
in someone's Introduction is a citation of a finding; a hit in their Results is
the finding.

The label is ordinary chunk metadata, so it can be filtered on:

```python
zotero_semantic_search(query="...", filters={"section": {"$ne": "Introduction"}})
zotero_semantic_search(query="...", filters={"section": {"$in": ["Results", "Discussion"]}})
```

Headings are recognised by shape (a line of their own, optional numbering, an
optional colon), because extracted PDF text keeps line breaks but not styling.
A heading the parser does not recognise ends the current label rather than
letting it run on, so a passage is left unlabelled rather than mislabelled.
Two consequences:

- **An existing index gets labels without re-embedding** through
  `zotero-mcp relabel-index`, which also adds printed page numbers, heading
  paths, chapters and reference lists recognised by their shape. See
  [Printed pages, headings and chapters](passage-labels.md).
- **Books mostly come out unlabelled.** Chapter titles are not section names,
  and many book PDFs extract without any heading the parser can read.
  Unlabelled passages are still searched; only a `section` filter excludes
  them.

### Study context in passages

Passage 0 of an item opens with its title, authors and abstract; every later
passage is a bare window of the document. With `"context_header": true` under
`chunking`, each later passage is embedded with a short header naming its
study — title, year, authors and the first 500 characters of the abstract —
so a sentence from the middle of a paper is matched as part of that paper
rather than as an anonymous fragment.

The header is used for embedding and re-ranking only; results show the
passage itself. It is off by default: it costs about 130 tokens per passage,
and since it changes what every passage embeds, switching it on or off takes
full effect only after `zotero-mcp update-db --fulltext --force-rebuild`.
It also makes the passages of one paper more alike, which is one reason
search thins candidates per paper (see above).

### How many papers a search returns

A passage index returns many hits per paper, and the paper most about the
query contributes the most of them. Search therefore retrieves a wide pool of
passages, keeps at most a few per paper, and only then groups them into
results, so asking for 10 results returns 10 papers rather than 10 passages of
one book.

| Key (under `chunking`) | Default | What it does |
|---|---|---|
| `search_pool` | `200` | Passages retrieved per search before thinning. Raised automatically to 20 × `limit`, capped at 1000. |
| `max_passages_per_item` | `2` | Passages of one paper that may compete. More than one lets a re-ranker pick the passage that answers the query rather than the one nearest in embedding space. |

## Re-ranking

A re-ranker reads the query and each candidate passage together and scores how
well the passage answers the query — slower than comparing embeddings, but
more accurate, so it is applied to the shortlist the embedding search
returns. `candidate_multiplier` sets the shortlist: papers per wanted result.

A local cross-encoder runs on your machine (needs `sentence-transformers`; the
strong models want a GPU):

```jsonc
"reranker": {
  "enabled": true,
  "model": "cross-encoder/ms-marco-MiniLM-L-6-v2",
  "candidate_multiplier": 3
}
```

A hosted re-ranker answers in well under a second without a GPU. Set
`provider` to `voyage`, `cohere`, `openrouter` or `contextual`, and put the
provider's key in the environment the server runs in (`VOYAGE_API_KEY`,
`COHERE_API_KEY`, `OPENROUTER_API_KEY`, `CONTEXTUAL_API_KEY`):

```jsonc
"reranker": {
  "enabled": true,
  "provider": "voyage",
  "model": "rerank-3",
  "candidate_multiplier": 5,
  "instruction": "Prefer passages that report findings over ones that cite them."  // optional
}
```

Any other endpoint that takes `{model, query, documents, top_n}` and answers
with `{index, relevance_score}` records works with `base_url` and
`api_key_env` in place of `provider`.

**Privacy:** with a hosted provider, every search sends the query and the text
of the candidate passages — dozens of excerpts from your library — to that
provider. The local cross-encoder sends nothing anywhere.

Search results say how they were ordered: *"Ranked by voyage/rerank-3 over 80
candidate passages"*, with the re-ranker's score as **Relevance** and the
embedding similarity alongside. If a hosted call fails — a wrong model name,
an expired key, a timeout — the search still answers, in embedding order, and
says why: *"re-ranking with voyage/rerank-3 failed (HTTP 401 …)"*.

## Example queries

In your AI assistant:
- *"Find research similar to machine learning concepts in neuroscience"*
- *"Papers that discuss climate change impacts on agriculture"*
- *"Research related to quantum computing applications"*
- *"Studies about social media influence on mental health"*
- *"Find papers conceptually similar to this abstract: [paste abstract]"*

From the shell: `zotero-cli search --mode semantic "attention mechanisms"`.

Problems building or querying the index: see [Troubleshooting](troubleshooting.md#semantic-search).

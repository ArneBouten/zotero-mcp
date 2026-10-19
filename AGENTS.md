# Agent notes for Arne's zotero-mcp build

This is `arne/main` of `github.com/ArneBouten/zotero-mcp`: upstream zotero-mcp
plus Arne's changes, worked on from two computers (PC and laptop) through
GitHub. See `docs/two-computers.md`.

- At the start of a session run `scripts\sync.cmd --check` (or
  `py -3 scripts/sync.py --check`) and tell the person what it reports: commits
  on GitHub that this computer lacks (offer to pull), commits here that are not
  pushed, uncommitted changes, and whether the installed zotero-mcp (what
  Claude Desktop runs) is older than this folder. Pull only after a yes.
- After committing, ask whether to push now. Never push without a yes.
- After a change under `src/` that should reach Claude Desktop, bump
  `src/zotero_mcp/_version.py` (`0.13.2+arne.N`), commit, and offer to run
  `uv run --no-project scripts/install_build.py` (Claude Desktop must be closed).
- If the same files changed on both computers, combine the changes carefully
  and show the result before committing.
- Never run `zotero-mcp update` or `pip install --upgrade zotero-mcp-server`,
  and never `--force-rebuild` the search index unless the embedding model changes.

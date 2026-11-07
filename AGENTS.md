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
  `src/zotero_mcp/_version.py` (`<upstream version>+arne.N`, e.g. `0.15.1+arne.69`), commit, and offer to run
  `uv run --no-project scripts/install_build.py` (Claude Desktop must be closed).
- If the same files changed on both computers, combine the changes carefully
  and show the result before committing.
- To take a new upstream release: `git fetch upstream`, merge `upstream/main`
  into `arne/main` (never rebase: the history is on two computers), keep this
  build's versions of `background_update` and the two-computer code, run the
  tests, and set the version to `<new upstream version>+arne.N`.
- Claude Desktop starts this server through the extension "zotero" (since 07-11-2026; source and
  `.mcpb` in OneDrive\Personal\Software\Claude extensions\Zotero, installed with install_mcpb.py
  there), not through a claude_desktop_config.json entry. Its keys are extension settings that
  Claude Desktop stores encrypted; the CLI reads them through `claude_extension.py`. Keep the
  display name lowercase (the tools are `mcp__remote-devices__zotero__*`), never add a config
  entry back, and check on both PCs that the installed extension matches `dist\Zotero.mcpb`.
- Never run `zotero-mcp update` or `pip install --upgrade zotero-mcp-server`,
  and never `--force-rebuild` the search index unless the embedding model changes.

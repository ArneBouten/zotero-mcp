# Two computers (PC and laptop)

This build (`arne/main` of `github.com/ArneBouten/zotero-mcp`) is worked on from
two computers. GitHub is the one place where the work comes together; it is not
a live sync like OneDrive, so the steps below remind you.

| What | PC | Laptop |
|---|---|---|
| Working copy (git) | `C:\Users\ArneB\zotero-mcp`, branch `arne/main` | the same path |
| What Claude Desktop runs | `py -3.12`'s `Scripts\zotero-mcp.exe`, installed from the working copy | the same |
| Settings | `%USERPROFILE%\.config\zotero-mcp\config.json` and `keys.env` | own copy (no keys in git) |
| Search index | `%USERPROFILE%\.config\zotero-mcp\chroma_db` | own index; never shared or synced |
| Shared state (checks, citations, counters) | `state_dir` in `config.json`: the OneDrive folder's `state` ([Two computers](maintenance.md#two-computers)) | the same folder |
| Python tools | uv (`%USERPROFILE%\.local\bin\uv.exe`) and Python 3.12 from python.org | the same |

## Every day

- **Opening the folder in VS Code** runs `scripts\sync.cmd` in a terminal
  (`.vscode/tasks.json`, copied from `extras/vscode/`; VS Code only runs it in a
  trusted folder). It
  1. fetches from GitHub and shows what the other computer pushed: *Pull now? [Y/n]*;
  2. shows commits that are only on this computer: *Push now? [Y/n]*;
  3. mentions files that are changed but not committed;
  4. compares the installed zotero-mcp (what Claude Desktop runs) with the
     working copy and offers *Build and install it now? [y/N]*
     (`scripts/install_build.py`; quit Claude Desktop first).

  It changes nothing without a yes, and it stops when the same file was changed
  on both computers. Run it any time with `scripts\sync.cmd`
  (`scripts\sync.cmd --check`: report only).
- **Committing in VS Code** offers to sync right away (pull + push;
  `git.postCommitCommand` in `.vscode/settings.json`). VS Code also checks
  GitHub every few minutes: ↓ / ↑ numbers next to the branch name at the bottom
  left mean there is something to pull or push.
- **Coding tools that read `AGENTS.md`** run the check at the start and ask
  before pulling, pushing or installing.

Pushing `arne/main` starts no workflow and publishes nothing (the GitHub
workflows only run for upstream's `main` and tags), so pushing often is safe.
Fully automatic syncing (every save pushed) is still not set up: edits on both
computers at once would clash.

The installed version and commit are recorded in
`%USERPROFILE%\.config\zotero-mcp\installed-build.json`, so the check also
notices new code under an unchanged version number. Ask Claude to run
`zotero_get_search_database_status` to see the version the server reports.

## Setting up a computer

The OneDrive folder `OneDrive - UGent\Academic\Software\zotero-mcp` has
`Set up zotero-mcp on this computer.cmd`. With Claude Desktop closed, double-click
it. It installs what is missing (Git, uv, Python 3.12, with winget or uv's own
installer), clones this repository to `%USERPROFILE%\zotero-mcp` on `arne/main`
(or syncs an existing clone), copies `extras\vscode\*.json` into `.vscode\`, puts the settings template in
`%USERPROFILE%\.config\zotero-mcp\config.json` if there is none, unpacks the
search index snapshot if there is no index yet, copies the Zotero action scripts,
builds and installs zotero-mcp, and prints the path of `zotero-mcp.exe`.

By hand, the same in PowerShell:

```powershell
winget install -e --id Git.Git
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
winget install -e --id Python.Python.3.12
git clone -b arne/main https://github.com/ArneBouten/zotero-mcp.git $env:USERPROFILE\zotero-mcp
cd $env:USERPROFILE\zotero-mcp
uv run --no-project scripts/install_build.py
```

Then, once per computer:

1. **Keys:** copy `%USERPROFILE%\.config\zotero-mcp\keys.env` and the
   `client_env` block of `config.json` (OpenAlex, SerpAPI, Tavily, ZenRows) from
   the other computer (USB stick or a password manager; never into git or
   OneDrive). The template has only the Unpaywall e-mail address there.
2. **Claude Desktop:** *Settings → Developer → Edit Config*, and copy the
   `zotero` entry of `mcpServers` from the other computer, with `command` set to
   the `zotero-mcp.exe` path the installer printed (backslashes doubled). The
   `env` block (Gemini and Voyage keys, `ZOTERO_LOCAL`, `ZOTERO_MCP_WORD_AUTHOR`)
   must be the same on both.
3. **Shared state:** the settings template already points `state_dir` at the
   OneDrive folder's `state`; on a computer that had its own records, run
   `py -3.12 -m zotero_mcp.cli share-state "$env:USERPROFILE\OneDrive - UGent\Academic\Software\zotero-mcp\state"`
   instead ([Two computers](maintenance.md#two-computers)).
4. **OCR for scanned PDFs** (optional): `py -3.12 -m zotero_mcp.cli ocr-setup`.
5. **Zotero:** signed in and synced, files downloaded at sync time, Better BibTeX,
   *Settings → Advanced → Allow other applications on this computer to
   communicate with Zotero*; for the *Check & complete* and *Fetch full text*
   actions, the Actions & Tags add-on (`setup\actions-and-tags-for-zotero.xpi`
   in the OneDrive folder) and the actions in `extras/zotero-actions/`.
6. **VS Code:** open the folder once and choose *Trust*, so the sync task can run
   (by hand: `extras/vscode/README.md`).
   For tests, `uv sync --extra dev` makes `.venv`, and VS Code's Python
   extension picks it up.

The Claude skills (`zotero-library-search`, `zotero-word-citations`,
`find-full-paper`) belong to the Claude account, not to a computer: they are
the same on every computer you sign in on. They need the server tools of this
build, so a computer still on an old build gives errors for the newer tools.

## Do not

- Run `zotero-mcp update` or `pip install --upgrade zotero-mcp-server`: that
  replaces this build with the plain PyPI release.
- Copy or sync `chroma_db` while a server runs, or keep it in a synced folder:
  OneDrive corrupts it. Each computer keeps its own index.
- Work in the old copies (`C:\dev\zotero-mcp-fork`, `zotero-mcp-arne9`,
  `zotero-mcp-arne17`, `zotero-mcp-clean`, `OneDrive\Documenten\zotero-mcp`):
  they have the history from before it was rewritten and must never be pushed.

# Extras: Zotero actions and their scripts

The right-click actions in Zotero and the PowerShell scripts they start. See [Keeping the library complete](../docs/maintenance.md) for what each action does.

| Folder | What | Where it goes |
|---|---|---|
| `zotero-actions/` | `zotero-mcp-actions.yml` (all actions at once) and each action's script | Zotero → Settings → Actions & Tags → Import (needs the Actions & Tags plugin) |
| `scripts/` | `zotero-maintain.ps1` (Check & complete, Check metadata only, new papers) and `zotero-fetch.ps1` (Fetch PDF only, browser only) | `%USERPROFILE%\.config\zotero-mcp\` |

On a second computer: install zotero-mcp, copy the two scripts, import the actions file, and run `zotero-mcp share-state` with the same synced folder as the first computer ([Two computers](../docs/maintenance.md#two-computers)).

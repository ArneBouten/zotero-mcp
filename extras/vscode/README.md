# VS Code settings for this build

Copied into `.vscode\` (which git ignores, as upstream does) by
`Set up zotero-mcp on this computer.cmd`, or by hand:

```powershell
cd $env:USERPROFILE\zotero-mcp
New-Item -ItemType Directory -Force .vscode | Out-Null
Copy-Item extras\vscode\*.json .vscode\
```

- `tasks.json`: runs `scripts\sync.cmd` when the folder is opened (after you choose *Trust*), and has a task to build and install zotero-mcp for Claude Desktop.
- `settings.json`: checks GitHub every few minutes and offers to sync after each commit; pytest and the uv `.venv`.
- `extensions.json`: Python, Python Environments (uv), Ruff, GitHub Pull Requests.

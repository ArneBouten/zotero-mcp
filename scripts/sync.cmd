@echo off
rem Compares this computer with GitHub and the installed zotero-mcp with this folder
rem (scripts\sync.py). Runs by itself when the folder is opened in VS Code.
rem   scripts\sync.cmd           asks before pulling, pushing or installing
rem   scripts\sync.cmd --check   report only
cd /d "%~dp0.."
where uv >nul 2>nul
if %errorlevel%==0 (
  uv run --no-project --quiet scripts\sync.py %*
) else (
  py -3 scripts\sync.py %*
)

# Started by the "Fetch full text" actions in Zotero (Actions & Tags).
# Runs zotero-mcp's fetcher for the selected items or collection. A progress window shows each paper
# and whether its PDF was found, with a button for the browser step; this terminal keeps the details.
param([string]$Items, [string]$Collection, [string]$CollectionName, [switch]$Browser)
$Host.UI.RawUI.WindowTitle = "Zotero full-text fetch"
if ($Collection) {
    Write-Host "Fetching full text for the papers without a PDF in collection: $CollectionName" -ForegroundColor Cyan
    $cmd = @("-3.12", "-m", "zotero_mcp.cli", "fetch-fulltext", "--collection", $Collection, "--retry")
} else {
    $n = ($Items -split ",").Count
    Write-Host "Fetching full text for $n paper(s)" -ForegroundColor Cyan
    $cmd = @("-3.12", "-m", "zotero_mcp.cli", "fetch-fulltext", "--items", $Items)
}
if ($Browser) {
    # Only the browser step: the normal steps ran before (their blocked links are remembered).
    Write-Host "With the fetcher's browser only (ResearchGate, Academia.edu, publisher logins). Stay nearby for captchas." -ForegroundColor Yellow
    $cmd += "--steps", "browser"
}
# The progress window (zotero-mcp 0.13.2+arne.33 and later); older versions show the progress here.
& py -3.12 -c "import zotero_mcp.fulltext_window" 2>$null
if ($LASTEXITCODE -eq 0) {
    $cmd += "--window"
    Write-Host "Progress: see the 'Find Full Text' window. This terminal keeps the details." -ForegroundColor DarkGray
    try {
        Add-Type -Name ConsoleWin -Namespace ZoteroMcp -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern System.IntPtr GetConsoleWindow();
[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
'@
        [ZoteroMcp.ConsoleWin]::ShowWindow([ZoteroMcp.ConsoleWin]::GetConsoleWindow(), 6) | Out-Null  # minimise
    } catch {}
}
Write-Host ""
& py @cmd
Write-Host ""
Write-Host "Done. The PDFs found are attached to the items in Zotero. You can close this window." -ForegroundColor Green

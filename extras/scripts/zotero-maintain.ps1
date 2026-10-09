# Started by the "Check & complete" and "Check metadata only" actions in Zotero (Actions & Tags).
# Check & complete: 1. metadata and attached PDFs checked and fixed, 2. missing PDFs fetched,
# 3. papers no registry knew checked again with their PDF. -NoFetch: step 1 only.
# -Quiet (the "on import" action): the window starts minimised and only comes forward when something
# is left for you; afterwards the search index is updated for the new papers.
# A progress window shows the steps and each paper; this terminal keeps the details.
param([string]$Items, [string]$Collection, [string]$CollectionName, [switch]$NoFetch, [switch]$Quiet)
$what = if ($NoFetch) { "Check metadata" } else { "Check & complete" }
$Host.UI.RawUI.WindowTitle = "Zotero: $what"
if ($Collection) {
    Write-Host "${what}: the papers in collection $CollectionName" -ForegroundColor Cyan
    $cmd = @("-3.12", "-m", "zotero_mcp.cli", "maintain", "--collection", $Collection)
} else {
    $n = ($Items -split ",").Count
    Write-Host "${what}: $n paper(s)" -ForegroundColor Cyan
    $cmd = @("-3.12", "-m", "zotero_mcp.cli", "maintain", "--items", $Items)
}
if ($NoFetch) { $cmd += "--no-fetch" }
if ($Quiet) { $cmd += "--index" }
# The progress window (zotero-mcp 0.13.2+arne.33 and later); older versions show the progress here.
& py -3.12 -c "import zotero_mcp.fulltext_window" 2>$null
if ($LASTEXITCODE -eq 0) {
    $cmd += "--window"
    if ($Quiet) { $cmd += "--quiet" }
    Write-Host "Progress: see the '$what' window. This terminal keeps the details." -ForegroundColor DarkGray
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
Write-Host "Done. Changes are tagged metadata/filled or metadata/corrected; proposals are in the saved search 'Metadata to review'; PDFs to check are tagged fulltext/check-pdf. You can close this window." -ForegroundColor Green

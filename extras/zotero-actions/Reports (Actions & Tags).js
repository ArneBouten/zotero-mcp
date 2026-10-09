// Actions & Tags script: "Reports" (zotero-mcp reports), Tools menu
// A small window with the latest checks' reports (the last 10), newest first: double-click one to open
// it, also after the progress window was closed or Zotero restarted. All reports are kept.
if (item) return;   // from a menu: runs once
let python = "C:\\Windows\\pyw.exe";   // Python without a console window; py as a fallback
try {
  if (!Zotero.File.pathToFile(python).exists()) python = "C:\\Windows\\py.exe";
} catch (e) {}
const argv = ["-3.12", "-m", "zotero_mcp.cli", "reports"];
try {
  const proc = Components.classes["@mozilla.org/process/util;1"].createInstance(Components.interfaces.nsIProcess);
  proc.init(Zotero.File.pathToFile(python));
  proc.startHidden = true;
  proc.runwAsync(argv, argv.length);
} catch (e) {
  Zotero.Utilities.Internal.exec(python, argv).catch((err) => Zotero.debug("Reports: " + err));
}

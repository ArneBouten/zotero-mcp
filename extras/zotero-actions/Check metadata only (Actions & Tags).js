// Actions & Tags script: "Check metadata only" (zotero-mcp maintain --no-fetch)
// Checks and fixes the metadata and checks the attached PDFs, without downloading PDFs:
// a wrong PDF is tagged fulltext/check-pdf (with a note), not replaced.
// Works from the item menu (selected papers) and the collection menu (a whole
// collection). Opens a minimised PowerShell window (the details); the progress window shows each paper.
// From the item menu the action runs once for all selected items and once per
// item: act only on the first run.
if (item) return;
let args, what;
if (collection && !(items && items.length)) {
  if (collection.libraryID !== Zotero.Libraries.userLibraryID) return "Only collections in My Library work this way.";
  args = ["-Collection", collection.key, "-CollectionName", collection.name];
  what = `collection "${collection.name}"`;
} else {
  const keys = (items || []).filter((i) => i.isRegularItem()).map((i) => i.key);
  if (!keys.length) return "Select one or more papers first.";
  if (items.some((i) => i.libraryID !== Zotero.Libraries.userLibraryID)) {
    return "Only items in My Library work this way.";
  }
  args = ["-Items", keys.join(",")];
  what = `${keys.length} item(s)`;
}
const extra = ["-NoFetch"];
let home = "";
try {
  home = Services.env.get("USERPROFILE");
} catch (e) {}
const script = (home || Services.dirsvc.get("Home", Components.interfaces.nsIFile).path) + "\\.config\\zotero-mcp\\zotero-maintain.ps1";
const powershell = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";
const argv = ["-NoExit", "-ExecutionPolicy", "Bypass", "-File", script, ...args, ...extra];
// Start the PowerShell window minimised from the first moment (no flash of a terminal): a hidden
// PowerShell starts it with Start-Process -WindowStyle Minimized. If that is not possible, the
// window opens as before and minimises itself.
try {
  const ps = (s) => "'" + String(s).replace(/'/g, "''") + "'";
  const arg = (s) => {
    const v = String(s).replace(/"/g, "");
    return ps(/\s/.test(v) ? `"${v}"` : v);
  };
  const command = `Start-Process -FilePath ${ps(powershell)} -WindowStyle Minimized -ArgumentList ${argv.map(arg).join(",")}`;
  const proc = Components.classes["@mozilla.org/process/util;1"].createInstance(Components.interfaces.nsIProcess);
  proc.init(Zotero.File.pathToFile(powershell));
  proc.startHidden = true;
  const launcher = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", command];
  proc.runwAsync(launcher, launcher.length);
} catch (e) {
  Zotero.debug("Check metadata only action: no hidden start (" + e + "); opening PowerShell directly");
  // Not awaited: the window stays open after the run, and Zotero should not wait for it.
  Zotero.Utilities.Internal.exec(powershell, argv).catch((err) => Zotero.debug("Check metadata only action: " + err));
}
return `Checking the metadata for ${what} in a PowerShell window.`;

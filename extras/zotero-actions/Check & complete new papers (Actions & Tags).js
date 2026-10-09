// Actions & Tags script: "Check & complete new papers" (event: Create Item; zotero-mcp maintain --quiet --index)
// When papers are added (Zotero Connector, import, DOI), about a minute after the last one: their
// metadata is checked and fixed, a PDF is fetched if Zotero did not attach one, and the search index
// is updated. The window stays minimised and only comes forward when something is left for you.
// Papers added together are checked in one run. Turn it off by disabling this action.
// With two computers: a paper that arrives by sync was added (and is checked) on the other one.
if (!item || !item.isRegularItem() || item.libraryID !== Zotero.Libraries.userLibraryID) return;
if (item.synced) return;
const WAIT_MS = 60000;   // the Connector attaches the page's PDF a little after the item
const queue = Zotero.__zoteroMcpNewItems || (Zotero.__zoteroMcpNewItems = { keys: new Set(), gen: 0 });
queue.keys.add(item.key);
const gen = ++queue.gen;
Zotero.Promise.delay(WAIT_MS).then(() => {
  if (gen !== queue.gen || !queue.keys.size) return;   // more papers came: the last one starts the run
  const keys = Array.from(queue.keys);
  queue.keys.clear();
  let home = "";
  try {
    home = Services.env.get("USERPROFILE");
  } catch (e) {}
  const script = (home || Services.dirsvc.get("Home", Components.interfaces.nsIFile).path) + "\\.config\\zotero-mcp\\zotero-maintain.ps1";
  const powershell = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";
  // No -NoExit: the minimised terminal closes when the run is done.
  const argv = ["-ExecutionPolicy", "Bypass", "-File", script, "-Items", keys.join(","), "-Quiet"];
  try {
    const ps = (s) => "'" + String(s).replace(/'/g, "''") + "'";
    const command = `Start-Process -FilePath ${ps(powershell)} -WindowStyle Minimized -ArgumentList ${argv.map(ps).join(",")}`;
    const proc = Components.classes["@mozilla.org/process/util;1"].createInstance(Components.interfaces.nsIProcess);
    proc.init(Zotero.File.pathToFile(powershell));
    proc.startHidden = true;
    const launcher = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", command];
    proc.runwAsync(launcher, launcher.length);
  } catch (e) {
    Zotero.debug("Check & complete new papers: no hidden start (" + e + "); opening PowerShell directly");
    Zotero.Utilities.Internal.exec(powershell, argv).catch((err) => Zotero.debug("Check & complete new papers: " + err));
  }
}).catch((e) => Zotero.debug("Check & complete new papers: " + e));
return;   // no message: this runs for every new paper

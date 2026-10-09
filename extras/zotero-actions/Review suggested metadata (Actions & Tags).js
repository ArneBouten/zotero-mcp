// Actions & Tags script: "Review suggested metadata" (zotero-mcp metadata-review)
// Opens a window with the metadata changes zotero-mcp suggested but did not make (tag
// metadata/review): accept or reject each with a click. Item menu: the selected papers;
// collection menu: the papers in that collection; Tools menu: every paper waiting for review.
if (item) return;   // from the item menu the action also runs once per item: act only once
const TAG_REVIEW = "metadata/review";
let args = [];
if (items && items.length) {
  const keys = items.filter((i) => i.isRegularItem() && i.hasTag(TAG_REVIEW)).map((i) => i.key);
  if (!keys.length) return "None of the selected papers has suggested changes.";
  args = ["--items", keys.join(",")];
} else if (collection) {
  args = ["--collection", collection.key];
}
// pyw: Python without a console window; py as a fallback.
let python = "C:\\Windows\\pyw.exe";
try {
  if (!Zotero.File.pathToFile(python).exists()) python = "C:\\Windows\\py.exe";
} catch (e) {}
const argv = ["-3.12", "-m", "zotero_mcp.cli", "metadata-review", ...args];
try {
  const proc = Components.classes["@mozilla.org/process/util;1"].createInstance(Components.interfaces.nsIProcess);
  proc.init(Zotero.File.pathToFile(python));
  proc.startHidden = true;
  proc.runwAsync(argv, argv.length);
} catch (e) {
  Zotero.Utilities.Internal.exec(python, argv).catch((err) => Zotero.debug("Review suggested metadata: " + err));
}

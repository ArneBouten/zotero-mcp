// Actions & Tags script: "Merge certain duplicates" (Tools menu)
// Merges papers in My Library that are certainly the same work, with Zotero's own merge (the same as
// its "Merge items" button: attachments, notes, tags, collections and links to the merged copies are
// kept; the other copies go to the trash, recoverable). Certain = the same DOI, or for books the same
// ISBN, the same item type, and titles that agree. Pairs with the same DOI but a different type or
// title are not merged: they get the tag "duplicate/check" for you to look at.
// Which copy stays: the one zotero-mcp has already checked; else the oldest (Word documents most
// likely cite it). Its empty fields are filled from the other copies; afterwards the merged papers
// get Check & complete, which settles fields where the copies disagreed.
if (item) return;   // from a menu: runs once
const TAG_CHECK = "duplicate/check";
const libraryID = Zotero.Libraries.userLibraryID;

const normDoi = (v) => String(v || "").trim().toLowerCase()
  .replace(/^https?:\/\/(dx\.)?doi\.org\//, "").replace(/^doi:\s*/, "").replace(/[.,;)\]]+$/, "");
const isbn13 = (v) => {
  const d = String(v || "").toUpperCase().replace(/[^0-9X]/g, "");
  if (d.length === 13) return d;
  if (d.length !== 10) return "";
  const core = "978" + d.slice(0, 9);
  let sum = 0;
  for (let i = 0; i < 12; i++) sum += Number(core[i]) * (i % 2 ? 3 : 1);
  return core + ((10 - (sum % 10)) % 10);
};
const field = (it, name) => {
  try {
    return it.getField(name) || "";
  } catch (e) {
    return "";
  }
};
const doiOf = (it) => normDoi(field(it, "DOI") || ((field(it, "extra").match(/^DOI:\s*(\S+)/im) || [])[1]));
const words = (t) => new Set(String(t || "").toLowerCase().normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
  .replace(/[^a-z0-9 ]/g, " ").split(/\s+/).filter((w) => w.length > 2));
const sameTitle = (a, b) => {
  const A = words(a), B = words(b);
  if (!A.size || !B.size) return false;
  let shared = 0;
  for (const w of A) if (B.has(w)) shared++;
  return shared / Math.min(A.size, B.size) >= 0.8;   // a missing subtitle is still the same title
};

// The papers zotero-mcp has checked (its record of checks).
let checked = {};
try {
  const home = Services.env.get("USERPROFILE") || Services.env.get("HOME");
  const state = await IOUtils.readJSON(PathUtils.join(home, ".config", "zotero-mcp", "maintenance.json"));
  checked = state.checked_modified || {};
} catch (e) {}

const ids = await Zotero.Items.getAll(libraryID, true, false, true);
const papers = (await Zotero.Items.getAsync(ids)).filter((it) => it.isRegularItem() && !it.deleted);
const groups = new Map();
for (const it of papers) {
  const doi = doiOf(it);
  const isbn = it.itemType === "book" ? isbn13(field(it, "ISBN").split(/[\s,;]+/)[0]) : "";
  const key = doi ? "doi:" + doi : isbn ? "isbn:" + isbn : "";
  if (!key) continue;
  if (!groups.has(key)) groups.set(key, []);
  groups.get(key).push(it);
}

const toMerge = [], toCheck = [];
for (const [key, items] of groups) {
  if (items.length < 2) continue;
  const first = items[0];
  const certain = items.every((it) => it.itemTypeID === first.itemTypeID && sameTitle(field(it, "title"), field(first, "title")));
  (certain ? toMerge : toCheck).push({ key, items });
}
if (!toMerge.length && !toCheck.length) return "No duplicates with the same DOI or ISBN.";

const label = (it) => {
  const c = it.getCreators()[0];
  return `${c ? c.lastName : "Anon."} (${field(it, "date").slice(0, 4) || "n.d."}) ${field(it, "title").slice(0, 60)}`;
};
const lines = toMerge.slice(0, 12).map((g) => "• " + label(g.items[0]) + (g.items.length > 2 ? ` (${g.items.length} copies)` : ""));
if (toMerge.length > 12) lines.push(`• … and ${toMerge.length - 12} more`);
const message = (toMerge.length ? `${toMerge.length} paper(s) are in your library more than once (same DOI or ISBN, same type and title):\n\n${lines.join("\n")}\n\n`
  + "Each is merged into one with Zotero's own merge; the other copies go to the trash.\n" : "")
  + (toCheck.length ? `\n${toCheck.length} group(s) share a DOI or ISBN but differ in type or title: not merged, tagged "${TAG_CHECK}".` : "");
const ps = Services.prompt;
const choice = ps.confirmEx(Zotero.getMainWindow(), "Merge certain duplicates", message,
  ps.BUTTON_POS_0 * ps.BUTTON_TITLE_IS_STRING + ps.BUTTON_POS_1 * ps.BUTTON_TITLE_CANCEL,
  toMerge.length ? "Merge" : "Tag them", null, null, null, {});
if (choice !== 0) return "Nothing merged.";

for (const g of toCheck) {
  for (const it of g.items) {
    it.addTag(TAG_CHECK);
    await it.saveTx();
  }
}
const kept = [];
let failed = 0;
for (const g of toMerge) {
  // Keep the copy already checked by zotero-mcp, else the oldest.
  const byAge = g.items.slice().sort((a, b) => String(a.dateAdded).localeCompare(String(b.dateAdded)));
  const master = byAge.find((it) => checked[it.key]) || byAge[0];
  const others = g.items.filter((it) => it !== master);
  try {
    for (const fieldID of Zotero.ItemFields.getItemTypeFields(master.itemTypeID)) {
      const name = Zotero.ItemFields.getName(fieldID);
      if (field(master, name)) continue;
      const value = others.map((it) => field(it, name)).find((v) => v);
      if (value) master.setField(name, value);
    }
    if (!master.getCreators().length) {
      const withCreators = others.find((it) => it.getCreators().length);
      if (withCreators) master.setCreators(withCreators.getCreators());
    }
    await master.saveTx();
    await Zotero.Items.merge(master, others);
    kept.push(master.key);
  } catch (e) {
    failed++;
    Zotero.debug("Merge certain duplicates: " + label(master) + ": " + e);
  }
}

// Check & complete for the merged papers: it settles the fields where the copies disagreed.
if (kept.length) {
  let home = "";
  try {
    home = Services.env.get("USERPROFILE");
  } catch (e) {}
  const script = (home || Services.dirsvc.get("Home", Components.interfaces.nsIFile).path) + "\\.config\\zotero-mcp\\zotero-maintain.ps1";
  const powershell = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";
  const argv = ["-ExecutionPolicy", "Bypass", "-File", script, "-Items", kept.join(","), "-Quiet"];
  try {
    const q = (s) => "'" + String(s).replace(/'/g, "''") + "'";
    const command = `Start-Process -FilePath ${q(powershell)} -WindowStyle Minimized -ArgumentList ${argv.map(q).join(",")}`;
    const proc = Components.classes["@mozilla.org/process/util;1"].createInstance(Components.interfaces.nsIProcess);
    proc.init(Zotero.File.pathToFile(powershell));
    proc.startHidden = true;
    const launcher = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", command];
    proc.runwAsync(launcher, launcher.length);
  } catch (e) {
    Zotero.Utilities.Internal.exec(powershell, argv).catch((err) => Zotero.debug("Merge certain duplicates: " + err));
  }
}
return `Merged ${kept.length} paper(s)` + (failed ? `; ${failed} could not be merged (see Help > Debug Output)` : "")
  + (toCheck.length ? `; ${toCheck.length} group(s) tagged "${TAG_CHECK}" for you.` : ".");

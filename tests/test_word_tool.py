"""The zotero_insert_word_citations tool: resolving keys against the library."""

import json
import sqlite3
import zipfile

import pytest

pytest.importorskip("lxml.etree")

from conftest import DummyContext  # noqa: E402
from test_word_citations import fields, make_docx, para, read, run  # noqa: E402

from zotero_mcp import client as _client  # noqa: E402
from zotero_mcp import library as _library  # noqa: E402
from zotero_mcp.tools import _helpers  # noqa: E402
from zotero_mcp.tools import word as word_tool  # noqa: E402


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeZot:
    library_type = "users"
    library_id = "0"

    def __init__(self, items, csl_ok=True):
        self._items = items
        self.csl_ok = csl_ok
        self.calls = []

    def items(self, **kwargs):
        self.calls.append(kwargs)
        wanted = kwargs["itemKey"].split(",")
        # Like the local API: answers with the requested items plus others.
        return [self._items[k] for k in wanted if k in self._items] + [self._items["EXTRA001"]]

    def _retrieve_data(self, request=None, params=None):
        if not self.csl_ok:
            raise RuntimeError("format not supported")
        keys = params["itemKey"].split(",")
        return _Resp({"items": [
            {"id": f"7756991/{k}", "type": "article-journal", "title": self._items[k]["data"]["title"]}
            for k in keys if k in self._items
        ]})


def _row(key, title, citation, item_type="journalArticle"):
    return {
        "key": key,
        "data": {"key": key, "itemType": item_type, "title": title, "date": "2009",
                 "creators": [{"creatorType": "author", "firstName": "B.", "lastName": "Soenens"}]},
        "citation": f"<span>{citation}</span>",
        "bib": f'<div class="csl-entry">{citation[1:-1]}. {title}.</div>',
    }


ITEMS = {
    "SOEN2009": _row("SOEN2009", "Psychological control", "(Soenens et al., 2009)"),
    "NOTE0001": _row("NOTE0001", "", "", item_type="note"),
    "EXTRA001": _row("EXTRA001", "Not asked for", "(Extra, 2000)"),
}


class FakeBackend:
    def find_by_citation_key(self, citekey):
        return {"key": "SOEN2009"} if citekey == "soenens2009how" else None


def _local_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE libraries (libraryID INTEGER PRIMARY KEY, type TEXT, editable INT, filesEditable INT);
        CREATE TABLE groups (groupID INTEGER PRIMARY KEY, libraryID INT, name TEXT, description TEXT, version INT);
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INT, libraryID INT,
                            dateAdded TEXT, dateModified TEXT);
        CREATE TABLE settings (setting TEXT, key TEXT, value, PRIMARY KEY (setting, key));
        INSERT INTO libraries VALUES (1, 'user', 1, 1);
        INSERT INTO items VALUES (861, 'SOEN2009', 1, 1, '', '');
        INSERT INTO settings VALUES ('account', 'userID', 7756991);
        INSERT INTO settings VALUES ('account', 'localUserKey', 'DVh4ejoJ');
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def env(monkeypatch, tmp_path):
    from zotero_mcp.local_db import LocalZoteroReader

    db = tmp_path / "zotero.sqlite"
    _local_db(db)
    zot = FakeZot(ITEMS)
    monkeypatch.setattr(_helpers, "_get_bibliography_client", lambda ctx=None: zot)
    monkeypatch.setattr(_library, "get_library_backend", lambda zot=None: FakeBackend())
    monkeypatch.setattr(_client, "get_active_group_id", lambda: 0)
    monkeypatch.setattr(word_tool, "get_local_zotero_reader", lambda: LocalZoteroReader(db_path=str(db)))
    return zot


def test_resolver_maps_item_keys_and_citekeys(env):
    resolve = word_tool.build_resolver(DummyContext(), "apa")
    out = resolve(["SOEN2009", "soenens2009how", "MISSING1", "NOTE0001", "unknowncite"])
    assert set(out) == {"SOEN2009", "soenens2009how"}
    item = out["soenens2009how"]
    assert item.key == "SOEN2009"
    assert item.uri == "http://zotero.org/users/7756991/items/SOEN2009"
    assert item.item_id == 861
    assert item.citation == "(Soenens et al., 2009)"
    assert item.bibliography == "Soenens et al., 2009. Psychological control."
    assert item.item_data["title"] == "Psychological control"  # Zotero's CSL export
    call = env.calls[0]
    assert call["include"] == "data,citation,bib" and call["style"] == "apa"


def test_resolver_falls_back_to_its_own_csl(env):
    env.csl_ok = False
    out = word_tool.build_resolver(DummyContext(), "apa")(["SOEN2009"])
    data = out["SOEN2009"].item_data
    assert data["type"] == "article-journal"
    assert data["author"] == [{"family": "Soenens", "given": "B."}]
    assert data["id"] == 861


def test_uri_prefix_rules(monkeypatch):
    assert word_tool._library_uri_prefix(4321, {}) == "http://zotero.org/groups/4321"
    assert word_tool._library_uri_prefix(0, {"userID": 5}) == "http://zotero.org/users/5"
    assert word_tool._library_uri_prefix(0, {"localUserKey": "abc"}) == "http://zotero.org/users/local/abc"
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "99")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "user")
    assert word_tool._library_uri_prefix(0, {}) == "http://zotero.org/users/99"
    monkeypatch.delenv("ZOTERO_LIBRARY_ID")
    with pytest.raises(word_tool._wc.WordCitationError):
        word_tool._library_uri_prefix(0, {})


def test_tool_end_to_end(env, tmp_path):
    src = make_docx(
        tmp_path / "draft.docx",
        para(run("Control [@soenens2009how, p. 190] and [@MISSING1].")),
        para(run("{{bibliography}}")),
    )
    out = word_tool.insert_word_citations(docx_path=str(src), ctx=DummyContext())
    assert "Converted 1 citation marker(s) citing 1 item(s), style apa." in out
    assert "Inserted the bibliography" in out
    assert "`MISSING1`" in out
    assert "draft (Zotero).docx" in out
    assert "Refresh in the Zotero tab" in out
    xml = read(tmp_path / "draft (Zotero).docx")
    code, result = fields(xml)[0]
    data = json.loads(code.split("CSL_CITATION", 1)[1])
    assert result == "(Soenens et al., 2009, p. 190)"
    assert data["citationItems"][0]["id"] == 861


def test_tool_dry_run_and_errors(env, tmp_path):
    src = make_docx(tmp_path / "d.docx", para(run("[@SOEN2009]")))
    out = word_tool.insert_word_citations(docx_path=str(src), dry_run=True, ctx=DummyContext())
    assert "Would convert 1" in out and "nothing was written" in out
    assert not (tmp_path / "d (Zotero).docx").exists()
    assert word_tool.insert_word_citations(
        docx_path=str(tmp_path / "nope.docx"), ctx=DummyContext()
    ).startswith("Error: No such file")


def test_tool_reports_api_failure(env, tmp_path, monkeypatch):
    def broken(**kwargs):
        raise ConnectionError("Zotero is not running")

    monkeypatch.setattr(env, "items", broken)
    src = make_docx(tmp_path / "d.docx", para(run("[@SOEN2009]")))
    out = word_tool.insert_word_citations(docx_path=str(src), ctx=DummyContext())
    assert "Zotero is not running" in out and "local API" in out


def test_zip_contains_valid_word_parts(env, tmp_path):
    src = make_docx(tmp_path / "d.docx", para(run("[@SOEN2009]")))
    word_tool.insert_word_citations(docx_path=str(src), ctx=DummyContext())
    with zipfile.ZipFile(tmp_path / "d (Zotero).docx") as z:
        assert z.testzip() is None
        names = z.namelist()
    assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml", "docProps/custom.xml"} <= set(names)


# --- inspect and edit tools -----------------------------------------------------------


def test_inspect_tool_lists_ids(env, tmp_path):
    src = make_docx(tmp_path / "m.docx", para(run("Control [@SOEN2009] and (Smith, 2020).")),
                    para(run("References")), para(run("Smith, J. (2020). A paper.")))
    word_tool.insert_word_citations(docx_path=str(src), in_place=True, ctx=DummyContext())
    out = word_tool.inspect_word_citations(docx_path=str(src), ctx=DummyContext())
    assert "**C1** · D1 · `(Soenens et al., 2009)` → SOEN2009" in out
    assert "**P1** · D1 · `(Smith, 2020)`" in out
    assert "**R1** · heading D2" in out and "D3: Smith, J. (2020). A paper." in out
    assert word_tool.inspect_word_citations(
        docx_path=str(tmp_path / "nope.docx"), ctx=DummyContext()
    ).startswith("Error: No such file")


def test_edit_tool_asks_for_the_write_mode_first(env, tmp_path):
    src = make_docx(tmp_path / "m.docx", para(run("Control [@SOEN2009].")))
    before = src.read_bytes()
    out = word_tool.edit_word_citations(docx_path=str(src), edits=[], ctx=DummyContext())
    assert "ask the user" in out and "tracked_changes" in out and "new_file" in out
    assert src.read_bytes() == before and not (tmp_path / "m (Zotero).docx").exists()


def test_edit_tool_tracked_changes_end_to_end(env, tmp_path):
    src = make_docx(tmp_path / "m.docx", para(run("Control (Soenens, 2009) matters.")),
                    para(run("References")), para(run("Soenens, B. (2009). Psychological control.")))
    out = word_tool.edit_word_citations(
        docx_path=str(src), write_mode="tracked_changes",
        edits=[
            {"op": "replace_text", "paragraph": "D1", "find": "(Soenens, 2009)", "with": "[@soenens2009how]"},
            {"op": "comment", "paragraph": "D1", "find": "matters", "text": "Supported: p. 12."},
            {"op": "replace_reference_list", "reference_list": "R1"},
        ],
        ctx=DummyContext(),
    )
    assert "Applied 3 of 3 edit(s); 1 marker(s) converted" in out
    assert "Tracked changes in the original" in out and "Backup of the original" in out
    assert "1 comment(s) added." in out and "accept the tracked changes" in out
    xml = read(src)
    assert "ADDIN ZOTERO_ITEM" in xml and "ADDIN ZOTERO_BIBL" in xml and "<w:del " in xml
    assert (tmp_path / "Zotero backups").is_dir()


def test_edit_tool_dry_run_needs_no_write_mode(env, tmp_path):
    src = make_docx(tmp_path / "m.docx", para(run("Control [@SOEN2009].")))
    out = word_tool.edit_word_citations(docx_path=str(src), dry_run=True, ctx=DummyContext())
    assert "Dry run: nothing was written." in out and "1 marker(s) converted" in out

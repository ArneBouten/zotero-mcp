"""Keeping the semantic index current without scanning the library for nothing.

- ``LocalZoteroReader.library_fingerprint`` moves whenever a library's items
  change, and only then.
- ``update_database`` records the fingerprint it started from after a clean,
  complete run, and ``index_is_current`` compares against it.
- The pre-search sync skips an unchanged library, and when the library is
  known to have changed it waits briefly so the search can include the change.
- The startup update skips an unchanged library.
"""

import sqlite3
import sys
import time

import pytest

if sys.version_info >= (3, 14):
    pytest.skip(
        "chromadb relies on pydantic v1 paths incompatible with Python 3.14+",
        allow_module_level=True,
    )

from test_sync_watermark_per_library import (
    FakeChromaClient,
    FakeZoteroClient,
    _build_search,
    _saved,
    _write_config,
)

from zotero_mcp import semantic_search
from zotero_mcp.local_db import LocalZoteroReader

# --- library_fingerprint ------------------------------------------------------


def _zotero_db(path, with_client_modified=True, with_trash=True):
    conn = sqlite3.connect(path)
    cdm = ", clientDateModified TEXT" if with_client_modified else ""
    conn.executescript(
        f"""
        CREATE TABLE libraries (libraryID INTEGER PRIMARY KEY, type TEXT, editable INT, filesEditable INT);
        CREATE TABLE groups (groupID INTEGER PRIMARY KEY, libraryID INT, name TEXT, description TEXT, version INT);
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INT, libraryID INT,
                            dateAdded TEXT, dateModified TEXT{cdm});
        INSERT INTO libraries VALUES (1, 'user', 1, 1);
        INSERT INTO libraries VALUES (5, 'group', 1, 1);
        INSERT INTO groups VALUES (777, 5, 'G', '', 1);
        """
    )
    if with_trash:
        conn.execute("CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY)")
    conn.commit()
    return conn


def _add(conn, item_id, library_id=1, modified="2026-01-01 00:00:00", with_client_modified=True):
    if with_client_modified:
        conn.execute(
            "INSERT INTO items VALUES (?, ?, 1, ?, '2026-01-01', ?, ?)",
            (item_id, f"K{item_id:07d}", library_id, modified, modified),
        )
    else:
        conn.execute(
            "INSERT INTO items VALUES (?, ?, 1, ?, '2026-01-01', ?)",
            (item_id, f"K{item_id:07d}", library_id, modified),
        )
    conn.commit()


def _fp(path, group_id=0):
    with LocalZoteroReader(db_path=str(path)) as reader:
        return reader.library_fingerprint(group_id)


def test_fingerprint_moves_on_add_edit_trash_delete(tmp_path):
    path = tmp_path / "zotero.sqlite"
    conn = _zotero_db(path)
    _add(conn, 1)
    first = _fp(path)
    assert first == _fp(path)  # stable while nothing changes

    _add(conn, 2)
    added = _fp(path)
    assert added != first

    conn.execute("UPDATE items SET dateModified = '2026-02-01', clientDateModified = '2026-02-01' WHERE itemID = 1")
    conn.commit()
    edited = _fp(path)
    assert edited != added

    conn.execute("INSERT INTO deletedItems VALUES (2)")
    conn.commit()
    trashed = _fp(path)
    assert trashed != edited

    conn.execute("DELETE FROM deletedItems")
    conn.execute("DELETE FROM items WHERE itemID = 2")
    conn.commit()
    assert _fp(path) != trashed


def test_fingerprint_is_per_library(tmp_path):
    path = tmp_path / "zotero.sqlite"
    conn = _zotero_db(path)
    _add(conn, 1)
    personal = _fp(path, 0)
    group = _fp(path, 777)
    _add(conn, 2, library_id=5)
    assert _fp(path, 0) == personal
    assert _fp(path, 777) != group


def test_fingerprint_unknown_library_is_none(tmp_path):
    path = tmp_path / "zotero.sqlite"
    _zotero_db(path)
    assert _fp(path, 12345) is None


def test_fingerprint_on_a_minimal_schema(tmp_path):
    path = tmp_path / "zotero.sqlite"
    conn = _zotero_db(path, with_client_modified=False, with_trash=False)
    _add(conn, 1, with_client_modified=False)
    before = _fp(path)
    _add(conn, 2, with_client_modified=False)
    assert before and _fp(path) != before


# --- update_database records it; index_is_current reads it --------------------


def _search(monkeypatch, tmp_path, fingerprint):
    config_path = _write_config(tmp_path)
    search = _build_search(
        monkeypatch, FakeZoteroClient(["AAAA0001", "AAAA0002"], 10), FakeChromaClient(),
        config_path=config_path,
    )
    state = {"fp": fingerprint}
    monkeypatch.setattr(search, "library_fingerprint", lambda group_id=None: state["fp"])
    return search, config_path, state


def test_complete_update_records_the_fingerprint(monkeypatch, tmp_path):
    search, config_path, _ = _search(monkeypatch, tmp_path, "1:2|9|x")
    stats = search.update_database()
    assert not stats.get("error")
    assert _saved(config_path)["update_config"]["library_fingerprints"] == {"0": "1:2|9|x"}


def test_limited_update_does_not_record_it(monkeypatch, tmp_path):
    search, config_path, _ = _search(monkeypatch, tmp_path, "1:2|9|x")
    search.update_database(limit=1)
    assert "library_fingerprints" not in _saved(config_path)["update_config"]


def test_no_fingerprint_available_records_nothing(monkeypatch, tmp_path):
    search, config_path, _ = _search(monkeypatch, tmp_path, None)
    search.update_database()
    assert "library_fingerprints" not in _saved(config_path)["update_config"]


def test_index_is_current(monkeypatch, tmp_path):
    search, _, state = _search(monkeypatch, tmp_path, "A")
    assert search.index_is_current() is None  # nothing recorded yet
    search._remember_fingerprint(0, "A")
    assert search.index_is_current() is True
    state["fp"] = "B"
    assert search.index_is_current() is False
    state["fp"] = None
    assert search.index_is_current() is None  # cannot tell


# --- pre-search sync -------------------------------------------------------------


class _StubSearch:
    def __init__(self, current, update_seconds=0.0, wait=None, due=True):
        self.current = current
        self.update_seconds = update_seconds
        self.update_config = {} if wait is None else {"presearch_wait_seconds": wait}
        self.due = due
        self.updates = 0

    def should_update_database(self):
        return self.due

    def index_is_current(self):
        return self.current

    def update_database(self, extract_fulltext=False):
        self.updates += 1
        time.sleep(self.update_seconds)
        return {}


@pytest.fixture
def presearch(monkeypatch):
    from zotero_mcp.tools import search as search_module

    monkeypatch.setattr(search_module, "_last_presearch_sync_ts", 0.0)
    monkeypatch.setattr(search_module, "_presearch_thread", None)
    yield search_module
    t = search_module._presearch_thread
    if t is not None:
        t.join(5)


def test_unchanged_library_starts_nothing(presearch):
    stub = _StubSearch(current=True)
    assert presearch._maybe_fire_presearch_sync(stub) is None
    assert stub.updates == 0


def test_not_due_starts_nothing(presearch):
    stub = _StubSearch(current=False, due=False)
    assert presearch._maybe_fire_presearch_sync(stub) is None
    assert stub.updates == 0


def test_changed_library_is_waited_for(presearch):
    stub = _StubSearch(current=False, update_seconds=0.2, wait=5)
    sync = presearch._maybe_fire_presearch_sync(stub)
    assert sync == {"changed": True, "finished": True}
    assert stub.updates == 1


def test_wait_is_bounded(presearch):
    stub = _StubSearch(current=False, update_seconds=1.5, wait=0.1)
    started = time.monotonic()
    sync = presearch._maybe_fire_presearch_sync(stub)
    assert time.monotonic() - started < 1.0
    assert sync == {"changed": True, "finished": False}


def test_unknown_state_does_not_wait(presearch):
    stub = _StubSearch(current=None, update_seconds=1.0, wait=5)
    started = time.monotonic()
    sync = presearch._maybe_fire_presearch_sync(stub)
    assert time.monotonic() - started < 0.5
    assert sync["changed"] is None and sync["finished"] is False


def test_running_update_is_joined_not_duplicated(presearch):
    first = _StubSearch(current=False, update_seconds=0.6, wait=0)
    presearch._maybe_fire_presearch_sync(first)  # starts, does not wait
    second = _StubSearch(current=False, wait=5)
    sync = presearch._maybe_fire_presearch_sync(second)
    assert second.updates == 0  # joined the running update instead
    assert first.updates == 1
    assert sync["finished"] is True


def test_semantic_tool_mentions_an_unfinished_update(monkeypatch, tmp_path, presearch):
    from conftest import DummyContext

    from zotero_mcp import client as _client

    class _Sem:
        def search(self, query, limit=10, filters=None, group_id=None):
            return {"results": [{
                "item_key": "K1", "similarity_score": 0.5, "matched_passage": "p", "metadata": {},
                "zotero_item": {"key": "K1", "data": {"title": "T", "itemType": "book"}},
            }]}

    config_dir = tmp_path / ".config" / "zotero-mcp"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text("{}")
    monkeypatch.setattr(presearch.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(semantic_search, "create_semantic_search", lambda *a, **k: _Sem())
    monkeypatch.setattr(_client, "get_active_group_id", lambda: 0)
    monkeypatch.setattr(presearch, "_maybe_fire_presearch_sync", lambda s: {"changed": True, "finished": False})
    out = presearch.semantic_search(query="q", ctx=DummyContext())
    assert "update is still running" in out

    monkeypatch.setattr(presearch, "_maybe_fire_presearch_sync", lambda s: {"changed": True, "finished": True})
    assert "still running" not in presearch.semantic_search(query="q", ctx=DummyContext())


# --- startup update ------------------------------------------------------------------


def test_startup_skips_an_unchanged_library(monkeypatch, tmp_path):
    from zotero_mcp import _app

    cfg = tmp_path / ".config" / "zotero-mcp"
    cfg.mkdir(parents=True)
    (cfg / "config.json").write_text(
        '{"semantic_search": {"update_config": {"auto_update": true, "update_frequency": "startup"}}}'
    )
    monkeypatch.setattr(_app.Path, "home", lambda: tmp_path)
    stub = _StubSearch(current=True)
    monkeypatch.setattr(semantic_search, "create_semantic_search", lambda *a, **k: stub)
    _app._sync_semantic_update()
    assert stub.updates == 0

    stub.current = False
    _app._sync_semantic_update()
    assert stub.updates == 1

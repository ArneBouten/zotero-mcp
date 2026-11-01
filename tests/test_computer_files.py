"""Records in a state folder that two computers sync: each writes its own copy, readers merge."""

import json
from pathlib import Path

import pytest

from zotero_mcp import citations, maintenance
from zotero_mcp import computer_files as cf
from zotero_mcp import fulltext_fetch as ff
from zotero_mcp import metadata_audit as ma


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    folder = tmp_path / "OneDrive" / "state"
    folder.mkdir(parents=True)
    monkeypatch.setenv("ZOTERO_MCP_STATE_DIR", str(folder))
    monkeypatch.setenv("COMPUTERNAME", "DESKTOP-A")
    return folder


def on(monkeypatch, name):
    monkeypatch.setenv("COMPUTERNAME", name)


def test_each_computer_writes_its_own_copy_and_reads_both(shared, monkeypatch):
    ff._save_item_state("ITEM1", {"status": "not found", "last_attempt": "2026-10-20T10:00:00"})
    assert (shared / "fulltext" / "state-DESKTOP-A.json").is_file()
    assert not (shared / "fulltext" / "state.json").exists()

    on(monkeypatch, "LAPTOP-A")
    assert ff._load_state()["ITEM1"]["status"] == "not found"
    ff._save_item_state("ITEM1", {"status": "attached", "last_attempt": "2026-10-20T09:00:00"})
    ff._save_item_state("ITEM2", {"status": "not found"})

    on(monkeypatch, "DESKTOP-A")
    state = ff._load_state()
    # The laptop's change is newer (its stamp), even though its last_attempt is earlier.
    assert state["ITEM1"]["status"] == "attached"
    assert state["ITEM2"]["status"] == "not found"


def test_the_old_shared_file_still_counts(shared, monkeypatch):
    (shared / "fulltext").mkdir()
    (shared / "fulltext" / "state.json").write_text(
        json.dumps({"OLD": {"status": "not found", "last_attempt": "2026-10-01T00:00:00"}}), encoding="utf-8")
    assert ff._load_state()["OLD"]["status"] == "not found"
    ff._save_item_state("NEW", {"status": "attached"})
    state = ff._load_state()
    assert set(state) >= {"OLD", "NEW"}
    # The old file is read, never written again.
    assert "NEW" not in json.loads((shared / "fulltext" / "state.json").read_text(encoding="utf-8"))


def test_free_allowances_add_up_over_computers(shared, monkeypatch):
    (shared / "fulltext").mkdir()
    (shared / "fulltext" / "budget.json").write_text(json.dumps({"serpapi": {ff.Budget._period(False): 100}}),
                                                     encoding="utf-8")
    ff.Budget().spend("serpapi", 20)
    on(monkeypatch, "LAPTOP-A")
    laptop = ff.Budget()
    assert laptop.used("serpapi") == 120
    laptop.spend("serpapi", 5)
    assert not laptop.allows("serpapi", 125)
    on(monkeypatch, "DESKTOP-A")
    assert ff.Budget().used("serpapi") == 125


def test_logs_of_both_computers_are_read_in_time_order(shared, monkeypatch):
    log = shared / "fulltext" / "log.jsonl"
    log.parent.mkdir()
    (shared / "fulltext" / "log-LAPTOP-A.jsonl").write_text(json.dumps({"key": "A", "time": "2026-10-02"}) + "\n",
                                                           encoding="utf-8")
    log.write_text(json.dumps({"key": "A", "time": "2026-10-01"}) + "\n", encoding="utf-8")
    assert cf.own_path(log).name == "log-DESKTOP-A.jsonl"
    times = sorted(json.loads(x)["time"] for x in cf.read_lines(log))
    assert times == ["2026-10-01", "2026-10-02"]


def test_maintenance_keeps_the_later_watermark_and_both_checks(shared, monkeypatch):
    maintenance._save({"last_new_items": "2026-10-20T08:00:00", "checked_modified": {"A": {"date": "2026-10-20"}}})
    on(monkeypatch, "LAPTOP-A")
    state = maintenance._load()
    state["last_new_items"] = "2026-10-25T08:00:00"
    state["checked_modified"]["B"] = {"date": "2026-10-25"}
    maintenance._save(state)
    on(monkeypatch, "DESKTOP-A")
    merged = maintenance._load()
    assert merged["last_new_items"] == "2026-10-25T08:00:00"
    assert set(merged["checked_modified"]) == {"A", "B"}


def test_metadata_and_citations_merge_too(shared, monkeypatch):
    ma._save_state({"A": {"last_audit": "2026-10-20T08:00:00"}})
    citations.save({"papers": {"P1": {"refs": [], "fetched": "2026-10-20"}}, "works": {}})
    on(monkeypatch, "LAPTOP-A")
    ma._save_state({**ma._load_state(), "B": {"last_audit": "2026-10-21T08:00:00"}})
    citations.save({"papers": {"P2": {"refs": [], "fetched": "2026-10-21"}}, "works": {}})
    on(monkeypatch, "DESKTOP-A")
    assert set(ma._load_state()) >= {"A", "B"}
    assert set(citations.load()["papers"]) == {"P1", "P2"}


def test_without_a_shared_folder_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ZOTERO_MCP_STATE_DIR", raising=False)
    ff._save_item_state("ITEM1", {"status": "attached"})
    assert (tmp_path / ".config" / "zotero-mcp" / "fulltext" / "state.json").is_file()
    assert not list((tmp_path / ".config" / "zotero-mcp" / "fulltext").glob("state-*.json"))

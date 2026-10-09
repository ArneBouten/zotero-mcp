"""The records of the checks shared between computers through a synced folder."""

import json
from pathlib import Path

import pytest

from zotero_mcp import fulltext_fetch as ff
from zotero_mcp import maintenance, shared_state
from zotero_mcp import metadata_audit as ma


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ZOTERO_MCP_STATE_DIR", raising=False)
    return tmp_path


def test_two_computers_share_one_record_of_the_checks(home):
    cfg = home / ".config" / "zotero-mcp"
    cfg.mkdir(parents=True)
    (cfg / "config.json").write_text(json.dumps({"semantic_search": {"x": 1}}), encoding="utf-8")
    maintenance._save({"checked_modified": {"A": {"date": "2026-10-09"}}})
    ma._save_state({"A": {"rejected": {"title": "X"}}})
    (cfg / "fulltext" / "runs").mkdir(parents=True)
    (cfg / "fulltext" / "runs" / "bg-123.log").write_text("this computer's own", encoding="utf-8")
    assert ff.shared_dir() == cfg

    onedrive = home / "OneDrive - UGent" / "zotero-mcp" / "state"
    out = shared_state.share(onedrive, log=lambda m: None)
    assert set(out["copied"]) == {"maintenance.json", "metadata", "fulltext"}
    assert out["state_dir"] == "~/OneDrive - UGent/zotero-mcp/state"
    config = json.loads((cfg / "config.json").read_text(encoding="utf-8"))
    assert config["semantic_search"] == {"x": 1} and list(cfg.glob("config.json.bak-*"))
    assert ff.shared_dir() == onedrive
    assert maintenance._load()["checked_modified"]["A"]["date"] == "2026-10-09"
    assert ma._load_state()["A"]["rejected"] == {"title": "X"}
    assert not (onedrive / "fulltext" / "runs" / "bg-123.log").exists()

    # The second computer: what is shared already is used, not overwritten by its own records.
    maintenance._save({"checked_modified": {"A": {"date": "2026-10-10"}}})      # written to the shared folder
    (cfg / "maintenance.json").write_text(json.dumps({"checked_modified": {}}), encoding="utf-8")
    out = shared_state.share(onedrive, log=lambda m: None)
    assert "maintenance.json" in out["kept"]
    assert maintenance._load()["checked_modified"]["A"]["date"] == "2026-10-10"

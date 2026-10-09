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


def test_the_latest_reports_are_listed_with_their_pdf_report(home):
    import os
    import time

    meta, fetch = ma.meta_dir() / "runs", ff.state_dir() / "runs"
    meta.mkdir(parents=True)
    fetch.mkdir(parents=True)

    def report(path, text, age):
        path.write_text(text, encoding="utf-8")
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    old = report(meta / "a.md", "# Metadata check, 01-10-2026 10:00\n\n**1 paper checked.**\n", 86400)
    new = report(meta / "b.md", "# Metadata check, 09-10-2026 10:00\n\n**353 papers checked.**\n", 600)
    pdfs = report(fetch / "c.md", "# Full-text fetch (2026-10-09 10:05)\n\n4 attached, 2 not found\n", 300)
    alone = report(fetch / "d.md", "# Full-text fetch (2026-10-02 10:05)\n\n1 attached\n", 7 * 86400)
    report(fetch / "bg-1.md", "background", 10)
    runs = maintenance.recent_reports()
    assert [r["paths"] for r in runs] == [[new, pdfs], [old], [alone]]
    assert runs[0]["summary"] == "353 papers checked, PDFs: 4 attached, 2 not found"
    assert runs[2]["what"] == "PDF search" and runs[2]["summary"] == "1 attached"
    assert len(maintenance.recent_reports(limit=2)) == 2

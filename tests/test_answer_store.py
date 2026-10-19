"""The shared answer store (src/zotero_mcp/answer_store.py): Gemini's cached answers in a
few append-only files per computer instead of one file each, and `shared-store pack`."""

import json

import pytest

from zotero_mcp.answer_store import AnswerStore


def _store(tmp_path, computer="PC", legacy=()):
    return AnswerStore("structure", tmp_path / "shared" / "answers", legacy_dirs=legacy,
                       index_dir=tmp_path / f"idx-{computer}", computer=computer)


def test_put_then_get_and_skip_known_keys(tmp_path):
    s = _store(tmp_path)
    assert s.put("a", '{"headings": []}')
    assert not s.put("a", "other")
    assert s.get("a") == '{"headings": []}'
    assert s.get("missing") is None
    assert s.count() == 1


def test_values_with_newlines_and_unicode_survive(tmp_path):
    s = _store(tmp_path)
    value = 'line 1\nline 2 — “quotes” é ü'
    s.put("k", value)
    other = _store(tmp_path, "LAPTOP")
    other.refresh(force=True)
    assert other.get("k") == value


def test_two_computers_share_through_their_own_files(tmp_path):
    pc, laptop = _store(tmp_path, "DESKTOP-A"), _store(tmp_path, "LAPTOP-A")
    pc.put("p", "from pc")
    laptop.put("l", "from laptop")
    pc.refresh(force=True)
    laptop.refresh(force=True)
    assert laptop.get("p") == "from pc" and pc.get("l") == "from laptop"
    names = sorted(p.name.rsplit("-", 2)[0] for p in (tmp_path / "shared" / "answers").glob("*.jsonl"))
    assert names == ["DESKTOP-A", "LAPTOP-A"]


def test_a_half_synced_line_waits_for_the_rest(tmp_path):
    pc, laptop = _store(tmp_path, "PC"), _store(tmp_path, "LAPTOP")
    pc.put_many([("one", "1"), ("two", "2")])
    path = pc.own_file()
    full = path.read_bytes()
    path.write_bytes(full[:-4])
    laptop.refresh(force=True)
    assert laptop.get("one") == "1" and laptop.get("two") is None
    path.write_bytes(full)
    laptop.refresh(force=True)
    assert laptop.get("two") == "2"


def test_a_damaged_line_is_skipped(tmp_path):
    pc, laptop = _store(tmp_path, "PC"), _store(tmp_path, "LAPTOP")
    pc.put("good", "yes")
    with open(pc.own_file(), "ab") as f:
        f.write(b"not json\n")
    pc.put("after", "still fine")
    laptop.refresh(force=True)
    assert laptop.get("good") == "yes" and laptop.get("after") == "still fine"


def test_old_one_file_answers_are_read_and_packed(tmp_path):
    loose = tmp_path / "gemini-cache"
    loose.mkdir()
    for i in range(1200):
        (loose / f"k{i}.json").write_text(json.dumps({"i": i}), encoding="utf-8")
    s = _store(tmp_path, legacy=[loose])
    assert s.get("k5") == '{"i": 5}'  # read from the old file
    assert s.pack_legacy([loose]) == 1200
    assert s.pack_legacy([loose]) == 0
    files = sorted(p.name for p in s.dir.glob("*.jsonl"))
    assert files == ["PC-pack-0001.jsonl"]
    other = _store(tmp_path, "LAPTOP")
    assert other.count() == 1200 and other.get("k1199") == '{"i": 1199}'


def test_writes_from_two_stores_on_one_computer_do_not_lose_lines(tmp_path):
    a = _store(tmp_path, "PC")
    b = AnswerStore("structure", tmp_path / "shared" / "answers", index_dir=tmp_path / "idx-PC", computer="PC")
    a.put("x", "1")
    b.put("y", "2")
    a.put("z", "3")
    a.refresh(force=True)
    b.refresh(force=True)
    assert [a.get(k) for k in "xyz"] == ["1", "2", "3"]
    assert [b.get(k) for k in "xyz"] == ["1", "2", "3"]


def test_pack_moves_loose_shared_files_out_of_the_synced_folder(tmp_path, monkeypatch):
    from zotero_mcp import fulltext_fetch as ff
    from zotero_mcp import metadata_audit, relabel, shared_store

    cfg, shared = tmp_path / "cfg", tmp_path / "OneDrive" / "state"
    monkeypatch.setattr(ff, "config_dir", lambda: cfg)
    monkeypatch.setattr(ff, "shared_dir", lambda: shared)
    monkeypatch.setattr(relabel, "structure_dir", lambda: cfg / "structure")
    monkeypatch.setattr("zotero_mcp.answer_store._config_dir", lambda: cfg)
    (shared / "structure" / "gemini-cache").mkdir(parents=True)
    (shared / "structure" / "gemini-cache" / "s1.json").write_text("S1", encoding="utf-8")
    (shared / "metadata" / "pdf-cache").mkdir(parents=True)
    (shared / "metadata" / "pdf-cache" / "p1.json").write_text('{"title": "T"}', encoding="utf-8")
    (cfg / "structure" / "gemini-cache").mkdir(parents=True)
    (cfg / "structure" / "gemini-cache" / "local1.json").write_text("L1", encoding="utf-8")
    result = shared_store.pack(log=lambda m: None)
    assert result["structure answers"] == {"packed": 2, "in_store": 2}
    assert result["PDF readings"] == {"packed": 1, "in_store": 1}
    assert not (shared / "structure" / "gemini-cache").exists()
    assert not (shared / "metadata" / "pdf-cache").exists()
    assert (cfg / "structure" / "gemini-cache" / "local1.json").exists()  # local cache stays
    assert len(list((cfg / "_archive").iterdir())) == 2
    assert relabel.shared_answer_store().get("s1") == "S1"
    assert metadata_audit.pdf_answer_store().get("p1") == '{"title": "T"}'


def test_pack_needs_a_shared_folder(tmp_path, monkeypatch):
    from zotero_mcp import fulltext_fetch as ff
    from zotero_mcp import shared_store

    monkeypatch.setattr(ff, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(ff, "shared_dir", lambda: tmp_path)
    with pytest.raises(RuntimeError, match="share-state"):
        shared_store.pack(log=lambda m: None)

"""One search index on two computers (src/zotero_mcp/index_sync.py).

Two "computers" in one test: each has its own ChromaDB index, its own local state, and
the same shared folder. Covers publishing and copying both ways (newest wins), removal,
claims (one computer per paper, abandoned claims), embeddings that have not arrived yet,
half-synced lines, files Zotero has not downloaded, and compare/--fix.
"""

import time

import pytest

pytest.importorskip("chromadb")

import chromadb  # noqa: E402
from chromadb.config import Settings  # noqa: E402

from zotero_mcp import index_sync as isync  # noqa: E402
from zotero_mcp.chroma_client import ChromaClient  # noqa: E402
from zotero_mcp.embedding_store import EmbeddingStore  # noqa: E402


class _EF:
    @staticmethod
    def _prepare_document(text):
        return text


def _computer(tmp_path, name):
    db = chromadb.PersistentClient(path=str(tmp_path / name / "chroma"), settings=Settings(anonymized_telemetry=False))
    c = ChromaClient.__new__(ChromaClient)
    c.client = db
    c.collection_name = "zotero_library"
    c.collection = db.get_or_create_collection("zotero_library", embedding_function=None)
    c.embedding_function = _EF()
    c.index_sync = None
    store = EmbeddingStore("ns", root=tmp_path / "shared" / "emb", index_dir=tmp_path / name / "emb-idx",
                           computer=name)
    c.index_sync = isync.IndexSync(c, store, root=tmp_path / "shared" / "index",
                                   state_dir=tmp_path / name / "sync", computer=name)
    return c


def _paper(key, n=2, text="text", section="Methods"):
    ids = [f"{key}#{i}" for i in range(n)]
    docs = [f"{key} {text} {i}" for i in range(n)]
    metas = [{"item_key": key, "parent_item_key": key, "chunk_index": i, "section": section} for i in range(n)]
    vecs = [[float(i), float(len(key)), 0.5] for i in range(n)]
    return docs, metas, ids, vecs


def _add(c, key, **kw):
    c.upsert_embeddings(*_paper(key, **kw))


def _state(c):
    got = c.collection.get(include=["documents", "metadatas"])
    return sorted(zip(got["ids"], got["documents"], [tuple(sorted(m.items())) for m in got["metadatas"]]))


def test_what_one_indexes_the_other_copies_exactly(tmp_path):
    pc, laptop = _computer(tmp_path, "PC"), _computer(tmp_path, "LAPTOP")
    _add(pc, "AAA", n=3)
    _add(pc, "BBB")
    assert pc.index_sync.pending == 2
    assert pc.index_sync.publish() == 2
    assert pc.index_sync.pending == 0
    result = laptop.index_sync.pull()
    assert result == {"copied": 2, "deleted": 0, "waiting": 0}
    assert _state(laptop) == _state(pc)
    got = laptop.collection.get(ids=["AAA#1"], include=["embeddings"])
    assert [float(x) for x in got["embeddings"][0]] == [1.0, 3.0, 0.5]
    # Copying does not publish back.
    assert laptop.index_sync.pending == 0
    assert laptop.index_sync.pull()["copied"] == 0


def test_newest_wins_both_ways(tmp_path):
    pc, laptop = _computer(tmp_path, "PC"), _computer(tmp_path, "LAPTOP")
    _add(pc, "AAA")
    pc.index_sync.publish()
    laptop.index_sync.pull()
    # The laptop changes the paper later: the PC takes the laptop's version.
    time.sleep(0.01)
    laptop.update_metadatas(["AAA#0", "AAA#1"], [
        {"item_key": "AAA", "parent_item_key": "AAA", "chunk_index": i, "section": "Results"} for i in range(2)])
    laptop.index_sync.publish()
    assert pc.index_sync.pull()["copied"] == 1
    assert _state(pc) == _state(laptop)
    assert pc.collection.get(ids=["AAA#0"])["metadatas"][0]["section"] == "Results"
    # And back: the PC re-indexes it with fewer passages; the laptop follows.
    time.sleep(0.01)
    pc.delete_item_chunks("AAA")
    _add(pc, "AAA", n=1, text="new")
    pc.index_sync.publish()
    laptop.index_sync.pull()
    assert _state(laptop) == _state(pc)
    assert laptop.collection.count() == 1


def test_an_older_publication_does_not_overwrite_a_newer_one(tmp_path):
    pc, laptop = _computer(tmp_path, "PC"), _computer(tmp_path, "LAPTOP")
    _add(pc, "AAA", text="old")
    pc.index_sync.publish()
    time.sleep(0.01)
    _add(laptop, "AAA", text="newer")
    laptop.index_sync.publish()
    assert laptop.index_sync.pull()["copied"] == 0  # PC's is older
    assert pc.index_sync.pull()["copied"] == 1
    assert _state(pc) == _state(laptop)
    assert "newer" in pc.collection.get(ids=["AAA#0"])["documents"][0]


def test_a_removed_paper_is_removed_on_the_other_computer(tmp_path):
    pc, laptop = _computer(tmp_path, "PC"), _computer(tmp_path, "LAPTOP")
    _add(pc, "AAA")
    _add(pc, "BBB")
    pc.index_sync.publish()
    laptop.index_sync.pull()
    time.sleep(0.01)
    pc.delete_item_chunks("AAA")
    pc.index_sync.publish()
    assert laptop.index_sync.pull()["deleted"] == 1
    assert sorted(laptop.collection.get()["ids"]) == ["BBB#0", "BBB#1"]


def test_claims_one_computer_per_paper_and_abandoned_claims_run_out(tmp_path, monkeypatch):
    pc, laptop = _computer(tmp_path, "PC"), _computer(tmp_path, "LAPTOP")
    pc.index_sync.claim(["AAA", "BBB"])
    laptop.index_sync._last_refresh = 0
    assert laptop.index_sync.claimed_elsewhere(["AAA", "BBB", "CCC"]) == {"AAA", "BBB"}
    assert pc.index_sync.claimed_elsewhere(["AAA"]) == set()  # its own claim
    _add(pc, "AAA")
    pc.index_sync.publish()  # done: no longer waited for
    laptop.index_sync._last_refresh = 0
    assert laptop.index_sync.claimed_elsewhere(["AAA", "BBB"]) == {"BBB"}
    monkeypatch.setattr(isync, "CLAIM_TTL_SECONDS", 0)
    time.sleep(0.01)
    assert laptop.index_sync.claimed_elsewhere(["BBB"]) == set()  # abandoned: the laptop does it


def test_a_paper_waits_until_its_embeddings_have_arrived(tmp_path):
    pc, laptop = _computer(tmp_path, "PC"), _computer(tmp_path, "LAPTOP")
    _add(pc, "AAA")
    pc.index_sync.publish()
    emb_files = list((tmp_path / "shared" / "emb" / "ns").glob("*.vec"))
    hidden = [(p, p.read_bytes()) for p in emb_files]
    for p, _ in hidden:
        p.unlink()
    assert laptop.index_sync.pull() == {"copied": 0, "deleted": 0, "waiting": 1}
    assert laptop.collection.count() == 0
    for p, data in hidden:
        p.write_bytes(data)
    assert laptop.index_sync.pull()["copied"] == 1


def test_a_half_synced_publication_waits_for_the_rest(tmp_path):
    pc, laptop = _computer(tmp_path, "PC"), _computer(tmp_path, "LAPTOP")
    _add(pc, "AAA")
    pc.index_sync.publish()
    path = next((tmp_path / "shared" / "index").glob("PC-*.jsonl"))
    full = path.read_bytes()
    path.write_bytes(full[:-10])
    assert laptop.index_sync.has_news()
    assert laptop.index_sync.pull()["copied"] == 0
    path.write_bytes(full)
    assert laptop.index_sync.pull()["copied"] == 1
    assert not laptop.index_sync.has_news()


def test_compare_finds_and_fixes_differences(tmp_path):
    pc, laptop = _computer(tmp_path, "PC"), _computer(tmp_path, "LAPTOP")
    for k in ("AAA", "BBB", "CCC"):
        _add(pc, k)
    pc.index_sync.publish()
    laptop.index_sync.pull()
    assert laptop.index_sync.compare()["same"] == 3
    # Something went wrong on the laptop outside the log.
    with laptop.index_sync.suspended():
        laptop.collection.update(ids=["BBB#0"], metadatas=[{"item_key": "BBB", "section": "oops"}])
        laptop.collection.delete(ids=["CCC#0", "CCC#1"])
        laptop.upsert_embeddings(*_paper("ZZZ"))
    diff = laptop.index_sync.compare()
    assert diff["different"] == ["BBB"] and diff["only_in_log"] == ["CCC"] and diff["only_here"] == ["ZZZ"]
    laptop.index_sync.pull(diff["different"] + diff["only_in_log"], force=True)
    laptop.index_sync.publish(["ZZZ"])
    assert laptop.index_sync.compare()["same"] == 4
    pc.index_sync.pull()
    assert _state(pc) == _state(laptop)


def test_reset_publishes_papers_that_were_not_indexed_again_as_removed(tmp_path):
    pc, laptop = _computer(tmp_path, "PC"), _computer(tmp_path, "LAPTOP")
    _add(pc, "AAA")
    _add(pc, "BBB")
    pc.index_sync.publish()
    laptop.index_sync.pull()
    time.sleep(0.01)
    ef, pc.embedding_function = pc.embedding_function, None  # chromadb rejects the test's stand-in
    pc.reset_collection()
    pc.embedding_function = ef
    _add(pc, "AAA")
    pc.index_sync.publish()
    laptop.index_sync.pull()
    assert _state(laptop) == _state(pc)


def test_files_not_downloaded_wait_a_day_then_are_indexed_anyway(tmp_path, monkeypatch):
    pc = _computer(tmp_path, "PC")
    s = pc.index_sync
    assert s.still_waiting_for_files(["AAA"]) == {"AAA"}
    assert s.still_waiting_for_files(["AAA"]) == {"AAA"}
    monkeypatch.setattr(isync, "MISSING_FILE_WAIT_SECONDS", 0)
    assert s.still_waiting_for_files(["AAA"]) == set()
    s.files_arrived(["AAA"])
    monkeypatch.setattr(isync, "MISSING_FILE_WAIT_SECONDS", 3600)
    assert s.still_waiting_for_files(["AAA"]) == {"AAA"}  # a new wait after the file came and went


def test_publish_all_then_mirror_into_an_empty_index(tmp_path):
    pc, laptop = _computer(tmp_path, "PC"), _computer(tmp_path, "LAPTOP")
    with pc.index_sync.suspended():  # an index that existed before the log
        for k in ("AAA", "BBB", "CCC"):
            _add(pc, k, n=4)
    assert pc.index_sync.publish_all() == 3
    assert list((tmp_path / "shared" / "index").glob("PC-pack-*.jsonl"))
    laptop.index_sync.pull()
    assert _state(laptop) == _state(pc)

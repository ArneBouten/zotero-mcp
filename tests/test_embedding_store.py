"""The shared embedding store (src/zotero_mcp/embedding_store.py) and its use.

Covers: the file format and the per-computer key index, two computers sharing one
folder, half-synced records, the RemoteEmbeddingFunction hook (only missing texts go to
the API; a broken store never breaks embedding), the shared Gemini structure cache in
relabel.py, and `zotero-mcp shared-store setup` (turning it on, copying an index's
embeddings without API calls).
"""

import json
import struct
import threading

import pytest

pytest.importorskip("chromadb")

from zotero_mcp import embedding_store as es  # noqa: E402
from zotero_mcp.embeddings.base import RemoteEmbeddingFunction  # noqa: E402


def _store(tmp_path, computer="PC", root=None):
    return es.EmbeddingStore("gemini-test", root=root or tmp_path / "shared",
                             index_dir=tmp_path / f"index-{computer}", computer=computer)


# -- the store itself ------------------------------------------------------------


def test_put_then_get_returns_the_same_float32_vectors_by_position(tmp_path):
    s = _store(tmp_path)
    assert s.put_many([("a", [0.5, -1.25, 3.0]), ("b", [1.0, 2.0, 4.0])]) == 2
    got = s.get_many(["x", "b", "a"])
    assert got == {1: [1.0, 2.0, 4.0], 2: [0.5, -1.25, 3.0]}
    assert (s.hits, s.misses) == (2, 1)


def test_put_skips_texts_the_store_already_has(tmp_path):
    s = _store(tmp_path)
    s.put_many([("a", [1.0, 2.0])])
    assert s.put_many([("a", [9.0, 9.0]), ("b", [3.0, 4.0]), ("b", [3.0, 4.0])]) == 1
    assert s.get_many(["a"]) == {0: [1.0, 2.0]}
    assert s.count() == 2


def test_each_computer_writes_its_own_file_and_reads_the_others(tmp_path):
    pc, laptop = _store(tmp_path, "PC"), _store(tmp_path, "LAPTOP")
    pc.put_many([("from pc", [1.0, 0.0])])
    laptop.put_many([("from laptop", [0.0, 1.0])])
    pc.refresh(force=True)
    laptop.refresh(force=True)
    assert laptop.get_many(["from pc"]) == {0: [1.0, 0.0]}
    assert pc.get_many(["from laptop"]) == {0: [0.0, 1.0]}
    names = sorted(p.name.split("-")[0] for p in (tmp_path / "shared" / "gemini-test").glob("*.vec"))
    assert names == ["LAPTOP", "PC"]


def test_a_half_synced_record_is_ignored_until_the_rest_arrives(tmp_path):
    pc, laptop = _store(tmp_path, "PC"), _store(tmp_path, "LAPTOP")
    pc.put_many([("one", [1.0, 1.0]), ("two", [2.0, 2.0])])
    path = pc.own_file()
    full = path.read_bytes()
    path.write_bytes(full[:-5])  # the sync has not delivered the last bytes yet
    laptop.refresh(force=True)
    assert laptop.get_many(["one", "two"]) == {0: [1.0, 1.0]}
    path.write_bytes(full)
    laptop.refresh(force=True)
    assert laptop.get_many(["two"]) == {0: [2.0, 2.0]}


def test_file_format_is_header_then_key_and_float32_records(tmp_path):
    s = _store(tmp_path)
    s.put_many([("a", [1.0, 2.0, 3.0])])
    data = s.own_file().read_bytes()
    magic, dim, _ = es.HEADER.unpack(data[:es.HEADER_SIZE])
    assert (magic, dim) == (es.MAGIC, 3)
    key = data[es.HEADER_SIZE:es.HEADER_SIZE + 32]
    assert key == es.text_key("gemini-test", "a")
    assert struct.unpack("<3f", data[es.HEADER_SIZE + 32:]) == (1.0, 2.0, 3.0)


def test_a_replaced_file_is_read_again_from_the_start(tmp_path):
    pc, laptop = _store(tmp_path, "PC"), _store(tmp_path, "LAPTOP")
    pc.put_many([("a", [1.0]), ("b", [2.0])])
    laptop.refresh(force=True)
    path = pc.own_file()
    data = path.read_bytes()
    rec = 32 + 4
    path.write_bytes(data[:es.HEADER_SIZE + rec])  # shorter than before: replaced
    laptop.refresh(force=True)
    assert laptop.get_many(["a", "b"]) == {0: [1.0]}


def test_export_files_roll_over_at_their_size(tmp_path, monkeypatch):
    monkeypatch.setattr(es, "EXPORT_FILE_BYTES", 100)
    s = _store(tmp_path)
    first = s.export_file()
    s.put_many([(f"t{i}", [float(i)] * 8) for i in range(5)], path=first)
    second = s.export_file()
    assert first.name == "PC-export-0001.vec" and second.name == "PC-export-0002.vec"


def test_namespace_names_provider_model_and_size():
    class EF:
        model_name = "text-embedding-3-large"
        dimensions = 1024

        @staticmethod
        def name():
            return "openai"

    assert es.namespace_for(EF()) == "openai-text-embedding-3-large-d1024"


def test_is_enabled_reads_config_and_env_switch(monkeypatch):
    assert not es.is_enabled({})
    assert es.is_enabled({"shared_store": {"enabled": True}})
    monkeypatch.setenv("ZOTERO_MCP_SHARED_STORE", "0")
    assert not es.is_enabled({"shared_store": {"enabled": True}})


# -- the embedding function hook ---------------------------------------------------


class _CountingEF(RemoteEmbeddingFunction):
    def _embed_batch(self, texts, is_query=False):
        self.sent.extend(texts)
        return [[float(len(t)), 1.0] for t in texts]


def _plain(vectors):
    """ChromaDB's wrapper hands vectors back as numpy arrays."""
    return [[float(x) for x in v] for v in vectors]


def _ef(store=None):
    ef = _CountingEF.__new__(_CountingEF)
    ef.sent = []
    ef.request_batch_size = 10
    ef.max_parallel_requests = 1
    ef.max_retries = 0
    ef.rate_limit_rps = None
    ef.tokens_per_minute = None
    ef._shared_store = store
    return ef


def test_without_a_store_every_text_goes_to_the_api():
    ef = _ef()
    assert _plain(ef(["ab", "abc"])) == [[2.0, 1.0], [3.0, 1.0]]
    assert ef.sent == ["ab", "abc"]


def test_with_a_store_only_missing_texts_go_to_the_api_and_are_added(tmp_path):
    store = _store(tmp_path)
    store.put_many([("known", [7.0, 7.0])])
    ef = _ef(store)
    out = ef(["new one", "known", "x"])
    assert _plain(out) == [[7.0, 1.0], [7.0, 7.0], [1.0, 1.0]]
    assert ef.sent == ["new one", "x"]
    ef.sent.clear()
    ef(["new one", "x", "known"])
    assert ef.sent == []  # all three come from the store now


def test_the_other_computer_pays_nothing_for_what_one_embedded(tmp_path):
    pc_ef = _ef(_store(tmp_path, "PC"))
    pc_ef(["passage 1", "passage 2"])
    laptop_ef = _ef(_store(tmp_path, "LAPTOP"))
    laptop_ef(["passage 2", "passage 1"])
    assert laptop_ef.sent == []


class _BrokenStore:
    def get_many(self, texts):
        raise OSError("sync folder offline")


def test_a_broken_store_falls_back_to_the_api():
    ef = _ef(_BrokenStore())
    assert _plain(ef(["ab"])) == [[2.0, 1.0]]
    assert ef.sent == ["ab"]


def test_store_is_safe_with_parallel_callers(tmp_path):
    store = _store(tmp_path)
    ef = _ef(store)
    errors = []

    def work(n):
        try:
            ef([f"text {n} {i}" for i in range(20)])
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert store.count() == 120


# -- relabel's Gemini cache in the shared folder ----------------------------------------


def test_relabel_cache_shared_reads_local_fallback_and_writes_shared(tmp_path, monkeypatch):
    from zotero_mcp import relabel

    local = tmp_path / "local"
    shared = tmp_path / "shared"
    monkeypatch.setattr(relabel, "structure_dir", lambda: local / "structure")
    monkeypatch.setattr(relabel, "shared_cache_dir", lambda: shared / "structure" / "gemini-cache")
    (local / "structure" / "gemini-cache").mkdir(parents=True)
    (local / "structure" / "gemini-cache" / "old.json").write_text("from before", encoding="utf-8")
    cache = relabel._Cache(shared=True)
    assert cache.get("old") == "from before"
    cache.put("new", "answer")
    assert (shared / "structure" / "gemini-cache" / "new.json").read_text(encoding="utf-8") == "answer"
    assert not (local / "structure" / "gemini-cache" / "new.json").exists()
    assert not list((shared / "structure" / "gemini-cache").glob(".*tmp"))


def test_relabel_cache_unshared_is_unchanged(tmp_path, monkeypatch):
    from zotero_mcp import relabel

    monkeypatch.setattr(relabel, "structure_dir", lambda: tmp_path / "structure")
    cache = relabel._Cache()
    cache.put("k", "v")
    assert cache.fallback is None
    assert (tmp_path / "structure" / "gemini-cache" / "k.json").read_text(encoding="utf-8") == "v"


# -- zotero-mcp shared-store setup -------------------------------------------------------


def test_enable_sets_the_flag_with_a_backup(tmp_path):
    from zotero_mcp import shared_store

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"semantic_search": {"embedding_model": "gemini"}}), encoding="utf-8")
    assert shared_store.enable(cfg, log=lambda m: None)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["semantic_search"] == {"embedding_model": "gemini", "shared_store": {"enabled": True}}
    assert list(tmp_path.glob("config.json.bak-*"))
    assert not shared_store.enable(cfg, log=lambda m: None)  # already on: nothing to do


class _FakeCollection:
    def __init__(self, docs, vecs):
        self.docs, self.vecs = docs, vecs

    def count(self):
        return len(self.docs)

    def get(self, limit, offset, include):
        return {"documents": self.docs[offset:offset + limit], "embeddings": self.vecs[offset:offset + limit]}


class _FakeClient:
    def __init__(self, docs, vecs):
        self.collection = _FakeCollection(docs, vecs)
        self.embedding_function = _ef()


def test_export_copies_an_index_into_the_store_without_api_calls(tmp_path):
    from zotero_mcp import shared_store

    docs = [f"passage {i}" for i in range(25)] + [None]
    vecs = [[float(i), 0.5] for i in range(25)] + [None]
    client = _FakeClient(docs, vecs)
    store = _store(tmp_path)
    result = shared_store.export_embeddings(client, store, log=lambda m: None, batch=10)
    assert result == {"passages": 26, "added": 25}
    assert client.embedding_function.sent == []
    assert store.get_many(["passage 7"]) == {0: [7.0, 0.5]}
    assert all(p.name.startswith("PC-export-") for p in store.dir.glob("*.vec"))
    # Running it again adds nothing.
    assert shared_store.export_embeddings(client, store, log=lambda m: None, batch=10)["added"] == 0


def test_setup_needs_a_shared_folder_first(tmp_path, monkeypatch):
    from zotero_mcp import fulltext_fetch as ff
    from zotero_mcp import shared_store

    monkeypatch.setattr(ff, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(ff, "shared_dir", lambda: tmp_path)
    with pytest.raises(RuntimeError, match="share-state"):
        shared_store.setup(str(tmp_path / "config.json"), log=lambda m: None)


def test_export_reads_a_real_chromadb_collection(tmp_path):
    import chromadb
    from chromadb.config import Settings

    from zotero_mcp import shared_store

    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"),
                                       settings=Settings(anonymized_telemetry=False))
    col = client.create_collection("test_export", embedding_function=None)
    col.add(ids=[f"k#{i}" for i in range(30)], documents=[f"doc {i}" for i in range(30)],
            embeddings=[[float(i), 0.25, -1.0] for i in range(30)])

    class Wrapped:
        collection = col
        embedding_function = _ef()

    store = _store(tmp_path)
    result = shared_store.export_embeddings(Wrapped(), store, log=lambda m: None, batch=7)
    assert result == {"passages": 30, "added": 30}
    assert store.get_many(["doc 29"]) == {0: [29.0, 0.25, -1.0]}


def test_create_chroma_client_attaches_the_store_only_when_turned_on(tmp_path, monkeypatch):
    pytest.importorskip("google.genai")
    from zotero_mcp.chroma_client import create_chroma_client

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-used")
    monkeypatch.setenv("ZOTERO_MCP_STATE_DIR", str(tmp_path / "shared"))
    cfg = tmp_path / "config.json"
    sem = {"embedding_model": "gemini", "embedding_config": {"model_name": "gemini-embedding-001"}}
    cfg.write_text(json.dumps({"semantic_search": sem}), encoding="utf-8")
    assert create_chroma_client(str(cfg)).embedding_function._shared_store is None
    sem["shared_store"] = {"enabled": True}
    cfg.write_text(json.dumps({"semantic_search": sem}), encoding="utf-8")
    store = create_chroma_client(str(cfg)).embedding_function._shared_store
    assert store is not None
    assert store.dir == tmp_path / "shared" / "embeddings" / "gemini-gemini-embedding-001"

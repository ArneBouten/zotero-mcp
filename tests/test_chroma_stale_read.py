"""A search that hits ChromaDB's "Error finding id" reloads the index and retries once.

ChromaDB is not process-safe: while the startup update or `maintain --index` writes
the index in another process, the server's cached vector index stops matching the
stored records and every search failed until that update finished.
"""

import pytest

pytest.importorskip("chromadb")

from zotero_mcp import chroma_client as cc  # noqa: E402


class _Collection:
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def query(self, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("Error executing plan: Internal error: Error finding id")
        return {"ids": [["A#0"]]}


def _client(collection, reopen_ok=True):
    c = cc.ChromaClient.__new__(cc.ChromaClient)
    c.collection = collection
    c.embedding_function = None
    c.reopened = 0

    def reopen():
        c.reopened += 1
        return reopen_ok
    c._reopen = reopen
    return c


def test_a_stale_read_is_retried_after_reloading():
    c = _client(_Collection(fail_times=1))
    assert c.search(["q"])["ids"] == [["A#0"]]
    assert c.reopened == 1


def test_it_retries_only_once():
    c = _client(_Collection(fail_times=2))
    with pytest.raises(RuntimeError, match="Error finding id"):
        c.search(["q"])
    assert c.reopened == 1


def test_other_errors_are_not_retried():
    class Broken(_Collection):
        def query(self, **kw):
            raise RuntimeError("no such collection")
    c = _client(Broken(0))
    with pytest.raises(RuntimeError, match="no such collection"):
        c.search(["q"])
    assert c.reopened == 0


def test_reopen_drops_the_cached_system_and_reconnects(tmp_path):
    import chromadb
    from chromadb.config import Settings

    path = str(tmp_path / "chroma")
    db = chromadb.PersistentClient(path=path, settings=Settings(anonymized_telemetry=False, allow_reset=True))
    db.get_or_create_collection("zotero_library", embedding_function=None)
    c = cc.ChromaClient.__new__(cc.ChromaClient)
    c.persist_directory = path
    c.collection_name = "zotero_library"
    c.embedding_function = None
    c.client = db
    assert c._reopen()
    assert c.client is not db
    assert c.collection.name == "zotero_library"

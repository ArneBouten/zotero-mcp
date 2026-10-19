"""The local scan with the shared index log on: a paper the other computer claimed, or whose
file Zotero has not downloaded here yet, is left alone; the rest is claimed before its PDF
is read (src/zotero_mcp/semantic_search.py, phase 2 of _get_items_from_source)."""

import sys

import pytest

if sys.version_info >= (3, 14):
    pytest.skip("chromadb is incompatible with Python 3.14+", allow_module_level=True)

from test_failed_marker_retry import FakeChromaClient, FakeReader  # noqa: E402

from zotero_mcp import semantic_search  # noqa: E402

ATTACHMENTS = [("ATTKEY1", "storage:paper.pdf", "application/pdf")]


class FileReader(FakeReader):
    def __init__(self, *a, file_here=True, **kw):
        super().__init__(*a, **kw)
        self.file_here = file_here

    def get_attachment_paths(self, key):
        return [{"key": "ATTKEY1", "exists": self.file_here}]


class FakeSync:
    def __init__(self, theirs=(), waiting_files=True):
        self.theirs = set(theirs)
        self.waiting_files = waiting_files
        self.claimed, self.arrived, self.missing = [], [], []

    def claimed_elsewhere(self, keys):
        return {k for k in keys if k in self.theirs}

    def claim(self, keys):
        self.claimed += list(keys)

    def files_arrived(self, keys):
        self.arrived += list(keys)

    def still_waiting_for_files(self, keys):
        self.missing += list(keys)
        return set(keys) if self.waiting_files else set()


def _scan(monkeypatch, sync, file_here=True):
    monkeypatch.setattr(semantic_search, "get_zotero_client", lambda: object())
    monkeypatch.setattr(semantic_search, "is_local_mode", lambda: True)
    reader = FileReader(attachments=ATTACHMENTS, file_here=file_here)
    monkeypatch.setattr(semantic_search, "LocalZoteroReader", lambda *a, **kw: reader)
    chroma = FakeChromaClient(None)
    chroma.index_sync = sync
    search = semantic_search.ZoteroSemanticSearch(chroma_client=chroma)
    monkeypatch.setattr(search, "_get_items_from_api", lambda *a, **kw: pytest.fail("API fallback"))
    search._deferred_items = 0
    items = search._get_items_from_source(extract_fulltext=True, chroma_client=chroma, force_rebuild=False)
    return items, reader, search


def test_new_paper_is_claimed_then_read(monkeypatch):
    sync = FakeSync()
    items, reader, search = _scan(monkeypatch, sync)
    assert [i["key"] for i in items] == ["ITEMKEY1"]
    assert sync.claimed == ["ITEMKEY1"] and sync.arrived == ["ITEMKEY1"]
    assert reader.extract_calls == 1 and search._deferred_items == 0


def test_paper_claimed_by_the_other_computer_is_left_to_it(monkeypatch):
    sync = FakeSync(theirs={"ITEMKEY1"})
    items, reader, search = _scan(monkeypatch, sync)
    assert items == [] and reader.extract_calls == 0
    assert sync.claimed == [] and search._deferred_items == 1


def test_paper_whose_file_is_not_here_yet_waits(monkeypatch):
    sync = FakeSync()
    items, reader, search = _scan(monkeypatch, sync, file_here=False)
    assert items == [] and reader.extract_calls == 0
    assert sync.missing == ["ITEMKEY1"] and sync.claimed == [] and search._deferred_items == 1


def test_after_the_wait_a_paper_without_its_file_is_indexed_anyway(monkeypatch):
    sync = FakeSync(waiting_files=False)
    items, reader, search = _scan(monkeypatch, sync, file_here=False)
    assert [i["key"] for i in items] == ["ITEMKEY1"]
    assert sync.claimed == ["ITEMKEY1"]

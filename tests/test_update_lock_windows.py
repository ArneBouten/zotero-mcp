"""The update lock on Windows is a real lock (``msvcrt.locking``), not a no-op.

Without it the startup update, a pre-search background update and a manual
``zotero-mcp update-db`` could all index the same ChromaDB collection at once.
"""

import sys
import threading
import types

import pytest

if sys.version_info >= (3, 14):
    pytest.skip(
        "chromadb relies on pydantic v1 paths incompatible with Python 3.14+",
        allow_module_level=True,
    )

from zotero_mcp import semantic_search


class _FakeMsvcrt(types.ModuleType):
    """msvcrt.locking semantics: a byte locked by one handle refuses others."""

    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self):
        super().__init__("msvcrt")
        self.held: dict[tuple[str, int], int] = {}
        self._mutex = threading.Lock()

    def locking(self, fd, mode, nbytes):
        import os

        path = os.path.realpath(f"/proc/self/fd/{fd}") if os.path.exists("/proc/self/fd") else str(fd)
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        key = (path, pos)
        with self._mutex:
            if mode == self.LK_NBLCK:
                if key in self.held:
                    raise OSError(36, "Resource deadlock avoided")
                self.held[key] = fd
            elif mode == self.LK_UNLCK:
                self.held.pop(key, None)


@pytest.mark.skipif(not __import__("os").path.exists("/proc/self/fd"), reason="needs /proc to identify files")
def test_windows_lock_excludes_a_second_holder_and_releases(monkeypatch, tmp_path):
    fake = _FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "fcntl", None)  # import fcntl -> ImportError
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.delenv("ZOTERO_MCP_FORCE_UPDATE", raising=False)
    lock_path = tmp_path / "update.lock"

    with semantic_search._acquire_update_lock(lock_path) as first:
        assert first is True
        # The pid stays readable while the lock is held.
        holder, _ = semantic_search.read_lock_holder(lock_path)
        assert holder == __import__("os").getpid()
        with semantic_search._acquire_update_lock(lock_path) as second:
            assert second is False
    with semantic_search._acquire_update_lock(lock_path) as again:
        assert again is True
    assert fake.held == {}


def test_no_lock_primitive_at_all_still_proceeds(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(sys.modules, "msvcrt", None)
    monkeypatch.delenv("ZOTERO_MCP_FORCE_UPDATE", raising=False)
    with semantic_search._acquire_update_lock(tmp_path / "update.lock") as ok:
        assert ok is True

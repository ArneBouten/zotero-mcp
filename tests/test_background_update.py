"""The startup update runs in a child process, and the server sees its result."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from zotero_mcp import _app, background_update, index_generation

_DUE = '{"semantic_search": {"update_config": {"auto_update": true, "update_frequency": "startup"}}}'
_NOT_DUE = '{"semantic_search": {"update_config": {"auto_update": false}}}'


@pytest.fixture
def home(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".config" / "zotero-mcp").mkdir(parents=True)
    return tmp_path


def _write_config(home, text):
    (home / ".config" / "zotero-mcp" / "config.json").write_text(text)


class _FakePopen:
    calls: list = []

    def __init__(self, args, **kwargs):
        _FakePopen.calls.append((args, kwargs))


# --- spawning ------------------------------------------------------------------


def test_nothing_due_spawns_nothing(home, monkeypatch):
    _write_config(home, _NOT_DUE)
    _FakePopen.calls = []
    monkeypatch.setattr(background_update.subprocess, "Popen", _FakePopen)
    assert background_update.spawn() is None
    assert _FakePopen.calls == []


def test_no_config_spawns_nothing(home, monkeypatch):
    _FakePopen.calls = []
    monkeypatch.setattr(background_update.subprocess, "Popen", _FakePopen)
    assert background_update.spawn() is None
    assert _FakePopen.calls == []


def test_due_update_runs_in_a_child_that_cannot_touch_the_protocol(home, monkeypatch):
    _write_config(home, _DUE)
    _FakePopen.calls = []
    monkeypatch.setattr(background_update.subprocess, "Popen", _FakePopen)
    assert isinstance(background_update.spawn(), _FakePopen)
    (args, kwargs), = _FakePopen.calls
    assert args == [sys.executable, "-m", "zotero_mcp.background_update"]
    # stdin and stdout carry MCP: the child must read neither and write
    # nothing to the server's stdout.
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] not in (None, 1, sys.stdout)


def test_in_process_opt_out(monkeypatch):
    ran = []
    monkeypatch.setenv(background_update.IN_PROCESS_ENV, "1")
    monkeypatch.setattr(_app, "_sync_semantic_update", lambda: ran.append(True))
    monkeypatch.setattr(background_update, "spawn", lambda: pytest.fail("spawned"))
    assert _app._start_background_update() is None
    assert ran == [True]


def test_spawn_failure_falls_back_to_in_process(monkeypatch):
    ran = []
    monkeypatch.delenv(background_update.IN_PROCESS_ENV, raising=False)
    monkeypatch.setattr(_app, "_sync_semantic_update", lambda: ran.append(True))

    def boom():
        raise OSError("no python")

    monkeypatch.setattr(background_update, "spawn", boom)
    assert _app._start_background_update() is None
    assert ran == [True]


@pytest.mark.asyncio
async def test_lifespan_answers_while_the_child_runs(monkeypatch):
    """The server must keep serving while the update process is busy."""
    released = threading.Event()
    waited = threading.Event()

    class _Proc:
        def wait(self):
            waited.set()
            released.wait(timeout=5)
            return 0

    monkeypatch.setattr(_app, "_start_background_update", lambda: _Proc())
    async with _app.server_lifespan(None) as ctx:
        assert ctx == {}
        t0 = time.monotonic()
        await asyncio.sleep(0.1)
        assert time.monotonic() - t0 < 1.0
        assert waited.is_set()
    released.set()


def test_child_with_nothing_to_do_exits_cleanly_and_silently(tmp_path):
    """The real entry point: no config, so exit 0 with nothing on stdout."""
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    proc = subprocess.run(
        [sys.executable, "-m", "zotero_mcp.background_update"],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_child_reports_whether_it_updated(home, monkeypatch):
    from zotero_mcp import semantic_search

    class _Stub:
        update_config: dict = {}

        def __init__(self, stats):
            self.stats = stats

        def should_update_database(self):
            return True

        def index_is_current(self):
            return False

        def update_database(self, extract_fulltext=False):
            return self.stats

    _write_config(home, _DUE)
    monkeypatch.setattr(semantic_search, "create_semantic_search",
                        lambda *a, **k: _Stub({"processed_items": 3}))
    assert background_update.sync_semantic_update() is True
    monkeypatch.setattr(semantic_search, "create_semantic_search",
                        lambda *a, **k: _Stub({"skipped_reason": "another_update_in_progress"}))
    assert background_update.sync_semantic_update() is False


# --- the server sees what the child wrote --------------------------------------


@pytest.fixture
def fresh_generations(monkeypatch):
    monkeypatch.setattr(index_generation, "_seen", {})


def test_first_look_and_own_writes_do_not_reload(tmp_path, fresh_generations):
    db = tmp_path / "chroma_db"
    assert index_generation.changed_elsewhere(db) is False  # nothing recorded
    assert index_generation.changed_elsewhere(db) is False  # first sight
    index_generation.mark_index_changed(db)
    assert index_generation.changed_elsewhere(db) is False  # our own write


def test_another_process_write_is_noticed_once(tmp_path, fresh_generations):
    db = tmp_path / "chroma_db"
    index_generation.changed_elsewhere(db)
    subprocess.run(
        [sys.executable, "-c",
         "import sys; from zotero_mcp.index_generation import mark_index_changed;"
         " mark_index_changed(sys.argv[1])", str(db)],
        check=True, timeout=60,
    )
    assert index_generation.changed_elsewhere(db) is True
    assert index_generation.changed_elsewhere(db) is False


def test_vectors_written_by_another_process_become_searchable(tmp_path, fresh_generations):
    """Without the reload, the server's cached index never returns them."""
    chromadb = pytest.importorskip("chromadb")
    db = str(tmp_path / "chroma_db")

    def query(client):
        col = client.get_collection("zotero_test")
        return col.query(query_embeddings=[[0.0, 1.0, 0.0]], n_results=5)["ids"][0]

    index_generation.refresh_if_changed_elsewhere(db)
    server = chromadb.PersistentClient(path=db)
    col = server.get_or_create_collection("zotero_test", embedding_function=None)
    col.add(ids=["a"], embeddings=[[1.0, 0.0, 0.0]], documents=["a"])
    assert query(server) == ["a"]

    writer = (
        "import sys, chromadb\n"
        "from zotero_mcp.index_generation import mark_index_changed\n"
        "col = chromadb.PersistentClient(path=sys.argv[1]).get_collection('zotero_test')\n"
        "col.add(ids=['b'], embeddings=[[0.0, 1.0, 0.0]], documents=['b'])\n"
        "mark_index_changed(sys.argv[1])\n"
    )
    subprocess.run([sys.executable, "-c", writer, db], check=True, timeout=120)

    assert "b" not in query(chromadb.PersistentClient(path=db))  # the stale cache
    assert index_generation.refresh_if_changed_elsewhere(db) is True
    assert query(chromadb.PersistentClient(path=db))[0] == "b"
    # The old handle a running search may hold still works.
    col.count()


def test_update_marks_the_generation_beside_the_index(tmp_path, fresh_generations):
    from zotero_mcp.semantic_search import ZoteroSemanticSearch

    db = tmp_path / "chroma_db"
    fake = SimpleNamespace(chroma_client=SimpleNamespace(persist_directory=str(db)))
    ZoteroSemanticSearch._mark_index_changed(fake)
    pid, _ns = index_generation.generation_path(db).read_text().split()
    assert pid == str(os.getpid())

    # A mocked client without a real path is ignored, not written as "<Mock>".
    ZoteroSemanticSearch._mark_index_changed(
        SimpleNamespace(chroma_client=SimpleNamespace(persist_directory=object()))
    )


# --- long OCR is announced ------------------------------------------------------


def test_long_ocr_is_announced(monkeypatch, capsys, tmp_path):
    from zotero_mcp import ocr

    class _Page:
        def get_textpage_ocr(self, **kw):
            return None

        def get_text(self, textpage=None):
            return "words"

    class _Doc:
        page_count = 12

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __getitem__(self, i):
            return _Page()

    monkeypatch.setitem(sys.modules, "pymupdf", SimpleNamespace(open=lambda p: _Doc()))
    settings = ocr.OcrSettings(tessdata=str(tmp_path))
    out = ocr.ocr_pdf_pages(tmp_path / "Smith 1990 - Big book.pdf", list(range(12)), settings)
    assert len(out) == 12
    err = capsys.readouterr().err
    assert "OCR: Smith 1990 - Big book.pdf, 12 pages" in err
    assert "OCR done: Smith 1990 - Big book.pdf, text on 12 of 12 pages" in err

    ocr.ocr_pdf_pages(tmp_path / "short.pdf", [0, 1], settings)
    assert "short.pdf" not in capsys.readouterr().err


def test_config_json_is_left_alone(home):
    """Reading whether an update is due must never rewrite the config."""
    _write_config(home, _DUE)
    before = (home / ".config" / "zotero-mcp" / "config.json").read_text()
    assert background_update.update_is_due() is True
    assert json.loads((home / ".config" / "zotero-mcp" / "config.json").read_text()) == json.loads(before)


# --- extraction progress --------------------------------------------------------


def test_extraction_watch_reports_each_file_and_long_ones(capsys):
    from zotero_mcp import local_db

    with local_db._ExtractionWatch(2, interval=0.2) as watch:
        watch.begin("a", "Long scan.pdf [KEY1]")
        time.sleep(0.5)
        watch.end("a", 1234, "pdf")
        watch.begin("b", "empty.pdf [KEY2]")
        watch.end("b", 0)
    err = capsys.readouterr().err
    assert "still extracting Long scan.pdf [KEY1]" in err
    assert "[1/2] Long scan.pdf [KEY1]: 1,234 characters (pdf)" in err
    assert "[2/2] empty.pdf [KEY2]: no text" in err


# --- OCR text layers --------------------------------------------------------------


def test_text_layer_is_written_into_storage_files_only(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from zotero_mcp import ocr

    folder = tmp_path / "storage" / "ABCD1234"
    folder.mkdir(parents=True)
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100), 0)
    pix.clear_with(220)
    page.insert_image(pymupdf.Rect(72, 72, 272, 172), pixmap=pix)
    pdf = folder / "scan.pdf"
    doc.save(str(pdf))
    doc.close()

    assert ocr.is_zotero_storage_file(pdf)
    assert not ocr.is_zotero_storage_file(tmp_path / "linked.pdf")
    assert ocr.text_layer_status(pdf)["needs_ocr"] is True
    words = {0: [(72, 80, 140, 95, "Autonomy", 0, 0, 0), (145, 80, 200, 95, "support", 0, 0, 1)]}
    assert ocr.apply_text_layer(pdf, words) == "written"
    with pymupdf.open(str(pdf)) as check:
        assert check[0].search_for("Autonomy support")
    assert ocr.apply_text_layer(pdf, words) == "unchanged"  # a page with text is left alone
    assert sorted(p.name for p in folder.iterdir()) == ["scan.pdf"]  # no temp file left
    assert ocr.text_layer_status(pdf)["needs_ocr"] is False


def test_ocr_pdfs_reports_every_file_and_remembers_them(tmp_path, monkeypatch):
    pymupdf = pytest.importorskip("pymupdf")
    from pathlib import Path

    from zotero_mcp import ocr

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pdfs = []
    for n, text in enumerate(["Some text on the page " * 20, ""]):
        folder = tmp_path / "storage" / f"KEY0000{n}"
        folder.mkdir(parents=True)
        doc = pymupdf.open()
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text[:80])
            page.insert_text((72, 90), text[80:160])
        doc.save(str(folder / "f.pdf"))
        doc.close()
        pdfs.append((f"KEY0000{n}", f"PAR0000{n}", folder / "f.pdf"))
    monkeypatch.setattr(ocr, "library_pdfs", lambda reader: pdfs)
    settings = ocr.OcrSettings(tessdata=str(tmp_path))
    lines = []
    counts = ocr.add_text_layers(settings, dry_run=True, log=lines.append, reader=object())
    assert counts == {"has text": 1, "needs OCR": 1}
    assert any("PAR00000]: has text, 1 pages" in line for line in lines)
    assert any("PAR00001]: needs OCR" in line for line in lines)

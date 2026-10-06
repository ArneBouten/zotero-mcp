"""Tests for OCR of scanned PDFs on the indexing path (zotero_mcp.ocr)."""

from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from zotero_mcp import ocr  # noqa: E402
from zotero_mcp.extract import PAGE_SEPARATOR, extract_file, with_ocr  # noqa: E402

SENTENCE = "Children engage in risky play when they climb heights and explore alone."


def _real_tessdata():
    """A folder with English language data on this machine, if any."""
    for d in ocr._candidate_dirs(None):
        if (d / "eng.traineddata").is_file():
            return d
    return None


@pytest.fixture
def settings():
    folder = _real_tessdata()
    if folder is None:
        pytest.skip("no Tesseract language data on this machine")
    return ocr.OcrSettings(tessdata=str(folder), languages="eng", max_pages=10, dpi=300)


def _page_image_pdf(path: Path, texts: list[str | None]) -> Path:
    """A PDF whose pages are images of text (a scan); None = a real text page."""
    out = pymupdf.open()
    for text in texts:
        src = pymupdf.open()
        page = src.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 520, 770), text or SENTENCE, fontsize=14)
        if text is None:
            out.insert_pdf(src)  # born-digital page with a text layer
        else:
            pix = page.get_pixmap(dpi=200)
            new = out.new_page(width=page.rect.width, height=page.rect.height)
            new.insert_image(new.rect, pixmap=pix)
    out.save(str(path))
    return path


def test_scanned_pdf_is_read_by_ocr_with_its_page_numbers(tmp_path, settings):
    pdf = _page_image_pdf(tmp_path / "scan.pdf", [f"Page one. {SENTENCE}", f"Page two. {SENTENCE}"])
    plain = extract_file(pdf, max_pages=50)
    assert not plain  # nothing without OCR

    doc = with_ocr(plain, pdf, max_pages=50, settings=settings)
    assert doc and "risky play" in doc.text
    assert doc.page_numbers == (0, 1)
    assert "Page one" in doc.pages[0] and "Page two" in doc.pages[1]
    assert doc.text.count(PAGE_SEPARATOR) == 1


def test_born_digital_pdf_is_left_alone(tmp_path, settings, monkeypatch):
    pdf = _page_image_pdf(tmp_path / "digital.pdf", [None, None, None])
    doc = extract_file(pdf, max_pages=50)
    assert doc

    calls = []
    monkeypatch.setattr(ocr, "ocr_pdf_pages", lambda *a, **k: calls.append(a) or {})
    assert with_ocr(doc, pdf, max_pages=50, settings=settings) is doc
    assert calls == []


def test_ocr_respects_its_page_cap(tmp_path, settings):
    capped = ocr.OcrSettings(tessdata=settings.tessdata, max_pages=1)
    pdf = _page_image_pdf(tmp_path / "scan.pdf", [SENTENCE, SENTENCE, SENTENCE])
    doc = with_ocr(None, pdf, max_pages=50, settings=capped)
    assert doc and "risky play" in doc.pages[0]
    assert doc.pages[1] == "" and doc.truncated


def test_no_settings_means_no_ocr(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert with_ocr(None, pdf, max_pages=50, settings=None) is None


def test_resolve_settings(tmp_path, monkeypatch):
    folder = tmp_path / "tessdata"
    folder.mkdir()
    (folder / "eng.traineddata").write_bytes(b"x")
    monkeypatch.setattr(ocr, "find_tessdata", ocr._find_tessdata_real)
    monkeypatch.setattr(ocr, "_candidate_dirs", lambda configured: [Path(configured)])

    cfg = {"tessdata": str(folder)}
    assert ocr.resolve_settings({**cfg, "enabled": False}) is None
    assert ocr.resolve_settings({**cfg, "languages": "eng,nld"}) is None  # nld missing
    (folder / "nld.traineddata").write_bytes(b"x")
    got = ocr.resolve_settings({**cfg, "languages": "eng,nld", "max_pages": 50})
    assert (got.tessdata, got.languages, got.max_pages) == (str(folder), "eng+nld", 50)
    assert ocr.resolve_settings(cfg).languages == "eng"  # the default


@pytest.mark.parametrize("workers", [1, 2])
def test_indexing_reader_ocrs_scans_in_both_extraction_paths(tmp_path, settings, workers):
    """Sequential and process-pool extraction both apply OCR when set."""
    from test_multicore_extraction import FakeReader

    scan = _page_image_pdf(tmp_path / "scan.pdf", [SENTENCE])
    digital = _page_image_pdf(tmp_path / "digital.pdf", [None])
    reader = FakeReader([scan, digital], workers=workers)
    reader.ocr_settings = settings
    got = dict(reader.extract_fulltext_for_items([(1, "K1"), (2, "K2")]))
    assert "risky play" in got[1][0]
    assert "risky play" in got[2][0]

    reader.ocr_settings = None
    got = dict(reader.extract_fulltext_for_items([(1, "K1")]))
    assert not got[1]


def test_status_line(tmp_path, monkeypatch):
    from zotero_mcp.tools import search as search_tools

    cfg = tmp_path / "config.json"
    cfg.write_text('{"semantic_search": {"extraction": {"ocr": {"languages": "eng"}}}}')
    assert "ocr-setup" in search_tools._ocr_status(cfg)  # suite disables language data

    monkeypatch.setattr(ocr, "resolve_settings", lambda c: ocr.OcrSettings(tessdata="x"))
    assert search_tools._ocr_status(cfg).startswith("on (eng")

    cfg.write_text('{"semantic_search": {"extraction": {"ocr": {"enabled": false}}}}')
    assert search_tools._ocr_status(cfg).startswith("off (disabled")

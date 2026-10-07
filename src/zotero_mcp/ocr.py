"""OCR for attachments without a text layer, on the indexing path only.

Scanned PDFs (page images, no text layer) give the extractor nothing, so they
were indexed metadata-only. When Tesseract language data is available, the
indexer now recognises such pages with PyMuPDF, whose MuPDF build embeds the
Tesseract engine: only the ``*.traineddata`` files are needed, not a separate
Tesseract install. ``zotero-mcp ocr-setup`` downloads them into
``~/.config/zotero-mcp/tessdata``; an existing Tesseract install's
``tessdata`` folder is found as well.

OCR is deliberately not used by the interactive tools (``zotero_get_item_
fulltext`` and friends): recognising a scanned book takes minutes, which a
tool call cannot wait for. Indexing runs in the background, caches what it
recognised, and so pays once per file.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGES = "eng"
#: Pages recognised per document. A scanned book at 300 dpi takes roughly a
#: second or two per page, so this bounds one document to a few minutes.
DEFAULT_MAX_PAGES = 300
DEFAULT_DPI = 300
#: A document is OCR'd when fewer than this share of its pages has text.
#: Born-digital PDFs with a few figure-only pages stay as they are.
TEXT_PAGE_SHARE = 0.2

#: LSTM "best" models: slower than "fast" but noticeably more accurate on old
#: book scans, and indexing is a background job.
TESSDATA_URL = "https://github.com/tesseract-ocr/tessdata_best/raw/main/{lang}.traineddata"


@dataclass(frozen=True)
class OcrSettings:
    """Resolved OCR configuration. Frozen and plain, so it pickles to workers."""

    tessdata: str
    languages: str = DEFAULT_LANGUAGES
    max_pages: int = DEFAULT_MAX_PAGES
    dpi: int = DEFAULT_DPI


def user_tessdata_dir() -> Path:
    return Path.home() / ".config" / "zotero-mcp" / "tessdata"


def _candidate_dirs(configured: str | None) -> list[Path]:
    dirs: list[Path] = []
    if configured:
        dirs.append(Path(configured).expanduser())
    env = os.getenv("TESSDATA_PREFIX")
    if env:
        dirs += [Path(env), Path(env) / "tessdata"]
    dirs.append(user_tessdata_dir())
    if sys.platform == "win32":
        for base in (os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)"), os.getenv("LOCALAPPDATA")):
            if base:
                dirs += [Path(base) / "Tesseract-OCR" / "tessdata", Path(base) / "Programs" / "Tesseract-OCR" / "tessdata"]
    else:
        dirs += [
            Path("/usr/share/tesseract-ocr/5/tessdata"),
            Path("/usr/share/tesseract-ocr/4.00/tessdata"),
            Path("/usr/share/tessdata"),
            Path("/usr/local/share/tessdata"),
            Path("/opt/homebrew/share/tessdata"),
        ]
    return dirs


def _languages(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = str(value or DEFAULT_LANGUAGES).replace(",", "+").split("+")
    return [p.strip() for p in parts if p.strip()]


def find_tessdata(languages: list[str], configured: str | None = None) -> Path | None:
    """The first folder holding a ``.traineddata`` file for every language."""
    for d in _candidate_dirs(configured):
        if all((d / f"{lang}.traineddata").is_file() for lang in languages):
            return d
    return None


#: The unpatched lookup, for tests that need it while the suite disables OCR.
_find_tessdata_real = find_tessdata


def resolve_settings(cfg: dict | None) -> OcrSettings | None:
    """OCR settings from ``semantic_search.extraction.ocr``, or None when off.

    On by default whenever language data can be found; ``"enabled": false``
    turns it off. Keys: ``languages`` ("eng", or "eng+nld" for several),
    ``max_pages``, ``dpi``, ``tessdata`` (a folder).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if cfg.get("enabled") is False:
        return None
    langs = _languages(cfg.get("languages"))
    folder = find_tessdata(langs, cfg.get("tessdata"))
    if folder is None:
        return None
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        return None
    return OcrSettings(
        tessdata=str(folder),
        languages="+".join(langs),
        max_pages=max(1, int(cfg.get("max_pages") or DEFAULT_MAX_PAGES)),
        dpi=max(72, int(cfg.get("dpi") or DEFAULT_DPI)),
    )


def ocr_pdf_pages(path: str | Path, pages: list[int], settings: OcrSettings) -> dict[int, str]:
    """Recognise the given 0-indexed pages; a page that fails is left out."""
    import pymupdf

    out: dict[int, str] = {}
    announce = len(pages) >= ANNOUNCE_PAGES
    started = time.monotonic()
    if announce:
        _progress(f"OCR: {Path(path).name}, {len(pages)} pages (can take a while)")
    with pymupdf.open(str(path)) as doc:
        for number in pages:
            if not 0 <= number < doc.page_count:
                continue
            page = doc[number]
            try:
                textpage = page.get_textpage_ocr(
                    language=settings.languages,
                    dpi=settings.dpi,
                    full=True,
                    tessdata=settings.tessdata,
                )
                text = page.get_text(textpage=textpage)
            except Exception as e:
                logger.debug("OCR failed on page %s of %s: %s", number + 1, path, e)
                continue
            if text and text.strip():
                out[number] = text
    if announce:
        _progress(
            f"OCR done: {Path(path).name}, text on {len(out)} of {len(pages)} pages "
            f"in {time.monotonic() - started:.0f}s"
        )
    return out


#: OCR of at least this many pages is announced on stderr, so a long scan
#: does not look like a hung update.
ANNOUNCE_PAGES = 10


def _progress(message: str) -> None:
    """One line on stderr, clearing an in-place progress line first."""
    try:
        sys.stderr.write(f"\r  {message}{' ' * 20}\n")
        sys.stderr.flush()
    except Exception:
        pass


def pdf_page_total(path: str | Path) -> int:
    import pymupdf

    with pymupdf.open(str(path)) as doc:
        return doc.page_count


def download_languages(languages: list[str], dest: Path | None = None, progress=print) -> Path:
    """Fetch ``.traineddata`` files for ``languages`` into ``dest``."""
    dest = dest or user_tessdata_dir()
    dest.mkdir(parents=True, exist_ok=True)
    for lang in languages:
        target = dest / f"{lang}.traineddata"
        if target.is_file() and target.stat().st_size > 1_000_000:
            progress(f"  {lang}: already present")
            continue
        url = TESSDATA_URL.format(lang=lang)
        progress(f"  {lang}: downloading {url}")
        tmp = target.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
        if tmp.stat().st_size < 1_000_000:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"{url} did not return language data")
        tmp.replace(target)
    return dest

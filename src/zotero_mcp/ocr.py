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
    #: Write the recognised text into the PDF as an invisible layer, so the
    #: file itself becomes searchable and the text syncs with it.
    write_text_layer: bool = False


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
    ``max_pages``, ``dpi``, ``tessdata`` (a folder), ``write_text_layer``
    (true: put the recognised text into the PDF file itself).
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
        write_text_layer=bool(cfg.get("write_text_layer", False)),
    )


def ocr_pdf_pages(path: str | Path, pages: list[int], settings: OcrSettings) -> dict[int, str]:
    """Recognise the given 0-indexed pages; a page that fails is left out."""
    import pymupdf

    out: dict[int, str] = {}
    words: dict[int, list] = {}
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
                if settings.write_text_layer:
                    words[number] = page.get_text("words", textpage=textpage)
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
    if settings.write_text_layer and words and is_zotero_storage_file(path):
        status = apply_text_layer(path, words)
        if announce or status != "written":
            _progress(f"Text layer {status}: {Path(path).name}")
    return out


def is_zotero_storage_file(path: str | Path) -> bool:
    """Only files Zotero manages (``storage/<KEY>/``) are rewritten, never a
    linked file somewhere else on the disk."""
    parts = Path(path).resolve().parts
    return len(parts) >= 3 and parts[-3].lower() == "storage" and len(parts[-2]) == 8


def apply_text_layer(path: str | Path, words_by_page: dict[int, list]) -> str:
    """Put OCR'd words into the PDF as invisible text, behind each page image.

    The page images are left exactly as they are; only text is added, in
    render mode 3 (invisible), at each word's position, the way scanners
    make "searchable PDFs". Pages that already have text are skipped. The
    new file is written beside the original, checked, then swapped in.
    Returns "written", "unchanged" or the reason it could not be done.
    """
    import pymupdf

    src = Path(path)
    tmp = src.with_name(src.name + ".textlayer.tmp")
    added = 0
    try:
        with pymupdf.open(str(src)) as doc:
            if doc.needs_pass or doc.is_encrypted:
                return "skipped (encrypted PDF)"
            pages = doc.page_count
            for number, words in words_by_page.items():
                if not 0 <= number < pages:
                    continue
                page = doc[number]
                if page.get_text().strip():
                    continue
                writer = pymupdf.TextWriter(page.rect)
                for w in words:
                    x0, y0, x1, y1, word = w[0], w[1], w[2], w[3], w[4]
                    height = y1 - y0
                    if height <= 0 or not str(word).strip():
                        continue
                    try:
                        # Size each word to its box, so selecting text in a
                        # viewer highlights roughly the right stretch.
                        unit = pymupdf.get_text_length(str(word), fontname="helv", fontsize=1) or 1.0
                        size = max(1.0, min(0.85 * height, (x1 - x0) / unit))
                        writer.append((x0, y1 - 0.2 * height), str(word), fontsize=size)
                        added += 1
                    except Exception:
                        continue
                writer.write_text(page, render_mode=3)
            if not added:
                return "unchanged"
            doc.save(str(tmp), garbage=0, deflate=True)
        with pymupdf.open(str(tmp)) as check:
            if check.page_count != pages or not any(
                check[n].get_text().strip() for n in words_by_page if 0 <= n < pages
            ):
                raise ValueError("the rewritten file did not check out")
        os.replace(tmp, src)
        return "written"
    except PermissionError:
        return "skipped (file in use; is it open in Zotero?)"
    except Exception as e:
        return f"skipped ({type(e).__name__}: {e})"
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def text_layer_status(path: str | Path, max_pages: int = DEFAULT_MAX_PAGES) -> dict:
    """Pages, and which of the first ``max_pages`` have no text."""
    import pymupdf

    with pymupdf.open(str(path)) as doc:
        total = doc.page_count
        limit = min(total, max_pages)
        empty = [n for n in range(limit) if not doc[n].get_text().strip()]
    return {"pages": total, "checked": limit, "empty": empty,
            "needs_ocr": limit > 0 and (limit - len(empty)) < TEXT_PAGE_SHARE * limit}


def add_text_layer_to_file(path: str | Path, settings: OcrSettings, check_only: bool = False) -> dict:
    """OCR a scanned PDF's empty pages and write the text into the file."""
    info = text_layer_status(path, settings.max_pages)
    if not info["needs_ocr"]:
        return {**info, "status": "has text"}
    if check_only:
        return {**info, "status": "needs OCR"}
    started = time.monotonic()
    layered = OcrSettings(settings.tessdata, settings.languages, settings.max_pages, settings.dpi, True)
    recognised = ocr_pdf_pages(path, info["empty"], layered)
    if not recognised:
        return {**info, "status": "no text recognised"}
    after = text_layer_status(path, settings.max_pages)
    status = "written" if len(after["empty"]) < len(info["empty"]) else "not written"
    return {**info, "status": status, "ocr_pages": len(recognised), "seconds": round(time.monotonic() - started)}


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


def _state_path() -> Path:
    return Path.home() / ".config" / "zotero-mcp" / "textlayer-state.json"


def library_pdfs(reader) -> list[tuple[str, str, Path]]:
    """``(attachment key, parent key, file)`` for every PDF in Zotero storage."""
    rows = reader._get_connection().execute(
        """
        SELECT att.key AS attachmentKey, parent.key AS parentKey, ia.path AS path
        FROM itemAttachments ia
        JOIN items att ON att.itemID = ia.itemID
        LEFT JOIN items parent ON parent.itemID = ia.parentItemID
        LEFT JOIN deletedItems d ON d.itemID = ia.itemID
        WHERE ia.contentType = 'application/pdf' AND ia.path LIKE 'storage:%' AND d.itemID IS NULL
        ORDER BY ia.itemID
        """
    ).fetchall()
    out = []
    for row in rows:
        path = reader._resolve_attachment_path(row["attachmentKey"], row["path"])
        if path and path.exists():
            out.append((row["attachmentKey"], row["parentKey"] or "", path))
    return out


def add_text_layers(
    settings: OcrSettings,
    *,
    keys: list[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    workers: int = 2,
    log=print,
    reader=None,
) -> dict[str, int]:
    """OCR every scanned PDF in Zotero storage that has no text, into the file.

    Each file is handled in its own process (PyMuPDF can crash on a damaged
    file), and files already checked are remembered by size and date, so a
    second run only looks at new or changed files.
    """
    import json
    import subprocess
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import asdict

    if reader is None:
        from zotero_mcp.local_db import get_local_zotero_reader

        reader = get_local_zotero_reader()
        if reader is None:
            raise RuntimeError("cannot read the local Zotero database")
    pdfs = library_pdfs(reader)
    if keys:
        wanted = set(keys)
        pdfs = [p for p in pdfs if p[0] in wanted or p[1] in wanted]
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        state = {}

    def stamp(path: Path) -> list:
        st = path.stat()
        return [st.st_size, int(st.st_mtime)]

    todo = [p for p in pdfs if keys or state.get(p[0], {}).get("stamp") != stamp(p[2])]
    if limit:
        todo = todo[:limit]
    log(f"{len(pdfs)} PDF(s) in Zotero storage; {len(todo)} to check"
        + (" (dry run: nothing is written)" if dry_run else ""))
    counts: dict[str, int] = {}
    raw = json.dumps(asdict(settings))
    timeout = max(900, 10 * settings.max_pages)

    def one(entry):
        key, parent, path = entry
        try:
            # stdout carries the result; stderr is left on the console, so
            # "OCR: <file>, N pages" shows while a long scan is worked on.
            proc = subprocess.run(
                [sys.executable, "-I", "-m", "zotero_mcp.ocr", "check" if dry_run else "layer", str(path), raw],
                stdout=subprocess.PIPE, stderr=None, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
            result = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {
                "status": f"error (exit {proc.returncode})"}
        except subprocess.TimeoutExpired:
            result = {"status": f"error (took over {timeout // 60} min)"}
        except Exception as e:
            result = {"status": f"error ({type(e).__name__})"}
        return entry, result

    def save_state():
        if dry_run:
            return
        try:
            _state_path().parent.mkdir(parents=True, exist_ok=True)
            _state_path().write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass

    from concurrent.futures import as_completed

    done = 0
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(one, entry) for entry in todo]
        for future in as_completed(futures):
            (key, parent, path), result = future.result()
            done += 1
            status = result.get("status", "error")
            counts[status] = counts.get(status, 0) + 1
            pages = result.get("pages")
            extra = f", {pages} pages" if pages else ""
            if result.get("ocr_pages"):
                extra += f", {result['ocr_pages']} OCR'd in {result.get('seconds', 0)}s"
            elapsed = time.monotonic() - started
            log(f"[{done}/{len(todo)}, {elapsed / 60:.0f} min] {path.name} [{parent or key}]: {status}{extra}")
            if not dry_run and (status in ("has text", "written", "no text recognised")):
                try:
                    state[key] = {"stamp": stamp(path), "status": status}
                except OSError:
                    pass
            if done % 25 == 0:
                save_state()  # a stopped run keeps what it already checked
    save_state()
    return counts


def _main(argv: list[str]) -> int:  # pragma: no cover - run as a child process
    """``python -m zotero_mcp.ocr layer|check <pdf> <settings-json>``."""
    import json

    mode, path, raw = argv[0], argv[1], json.loads(argv[2])
    settings = OcrSettings(**raw)
    try:
        result = add_text_layer_to_file(path, settings, check_only=(mode == "check"))
    except Exception as e:
        result = {"status": f"error ({type(e).__name__}: {e})"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv[1:]))

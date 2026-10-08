"""The server's startup update of the semantic index, run in its own process.

Extracting text from new attachments, OCR of scans most of all, keeps the
interpreter busy in long C calls that do not let other threads run. Inside the
server that starved the event loop: a catch-up of a few dozen papers made the
server miss Claude Desktop's request timeout, the client killed it, and the
next start began the same catch-up again. Run as a child process, the update
takes its own interpreter and the server keeps answering.

``python -m zotero_mcp.background_update`` is the child's entry point. This
module imports only the standard library and ``config_light`` at the top, so
the server pays nothing for it when no update is due.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

#: Set to 1 to run the startup update inside the server again, as before
#: 0.13.2+arne.9.
IN_PROCESS_ENV = "ZOTERO_MCP_UPDATE_IN_PROCESS"


def _config_path() -> Path:
    return Path.home() / ".config" / "zotero-mcp" / "config.json"


def update_is_due() -> bool:
    """Whether the config asks for an update now. Reads the config file only."""
    from zotero_mcp.config_light import should_update

    config_path = _config_path()
    if not config_path.exists():
        return False
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        update_cfg = cfg.get("semantic_search", {}).get("update_config", {})
    except Exception:
        # An unreadable config cannot say an update is due, and guessing "yes"
        # here is what would drag the heavy import back in on every startup.
        return False
    return bool(should_update(update_cfg))


def sync_semantic_update() -> bool:
    """Check for and run the semantic-search auto-update. True if it ran one.

    Every early return below happens *before* ``zotero_mcp.semantic_search`` is
    imported. That module pulls in ChromaDB and numpy, which costs roughly a
    second even when warm, and on Windows the import wedged the process for
    the length of the first tool call (#485). Only a run that is actually
    about to index anything pays for ChromaDB.
    """
    if not update_is_due():
        return False
    _maintain_new_items()

    from zotero_mcp.semantic_search import create_semantic_search
    from zotero_mcp.utils import is_local_mode

    search = create_semantic_search(str(_config_path()))
    if not search.should_update_database():
        return False
    # "Due" by schedule is not the same as "something to do": with
    # update_frequency "startup" every start is due, and a full scan of a
    # large library costs minutes of disk and CPU for nothing when the
    # library has not changed since the last complete update.
    if search.index_is_current() is True:
        sys.stderr.write("Semantic search index is current; no update needed.\n")
        return False

    sys.stderr.write("Auto-updating semantic search database...\n")
    stats = search.update_database(extract_fulltext=is_local_mode()) or {}
    if stats.get("skipped_reason"):
        return False
    sys.stderr.write(
        f"Database update completed: {stats.get('processed_items', 0)} items processed\n"
    )
    return True


def _maintain_new_items() -> None:
    """Before indexing: check the metadata of items added since the last start and fetch
    their PDFs, so the index gets them complete (``"maintenance": {"new_items": true}``)."""
    try:
        from zotero_mcp import maintenance

        if not maintenance.config_new_items(_config_path()):
            return
        sys.stderr.write("Maintaining new items (metadata, then PDFs)...\n")
        maintenance.run(new=True, log=lambda m: sys.stderr.write(m + "\n"))
    except Exception as e:  # never in the way of the index update
        sys.stderr.write(f"Warning: maintaining new items failed: {e}\n")


def _child_stdout():
    """Where the child's stdout goes: the server's stderr, never its stdout.

    The server's stdout carries the MCP protocol; one stray line from the
    child there would corrupt the stream.
    """
    try:
        return sys.__stderr__.fileno()
    except Exception:
        return subprocess.DEVNULL


def spawn() -> subprocess.Popen | None:
    """Start the update in a child process if one is due; None if not.

    The child inherits the environment (API keys, ZOTERO_LOCAL) and the
    server's stderr, so its progress lands in Claude Desktop's server log. It
    runs at below-normal priority, and is left to finish if the server exits:
    an interrupted update only resumes on the next start, while a finished one
    is what makes later starts quick.
    """
    if not update_is_due():
        return None
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": _child_stdout(),
        "stderr": None,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        [sys.executable, "-m", "zotero_mcp.background_update"], **kwargs
    )


def main() -> int:
    """Child entry point. Exit 0 when nothing failed, 1 on an error."""
    if sys.platform != "win32":
        try:
            os.nice(5)
        except Exception:
            pass
    # Extract in this process, one attachment at a time, as the server did:
    # a process pool inside a child the client may kill leaves orphans.
    from zotero_mcp import _runtime

    _runtime.mark_server_process()
    try:
        sync_semantic_update()
    except Exception as e:
        sys.stderr.write(f"Warning: semantic search auto-update failed: {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

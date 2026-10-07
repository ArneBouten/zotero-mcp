"""FastMCP application instance and server lifecycle."""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from zotero_mcp._context import sync_context
from zotero_mcp._version import __version__

# Configure logging from environment variable
# Set ZOTERO_MCP_LOG_LEVEL=DEBUG in Claude Desktop config to enable debug logs
_log_level = os.environ.get("ZOTERO_MCP_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.WARNING),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)


def _sync_semantic_update() -> bool:
    """Run the semantic-search auto-update in this process, if one is due.

    Kept for ``ZOTERO_MCP_UPDATE_IN_PROCESS=1`` and as the fallback when the
    child process cannot be started; see ``zotero_mcp.background_update``.
    """
    from zotero_mcp.background_update import sync_semantic_update

    return sync_semantic_update()


def _start_background_update():
    """Start the startup update; returns the child process, or None.

    None means nothing is running any more: no update was due, or it ran in
    this thread because a child process could not be used.
    """
    from zotero_mcp import background_update

    if os.environ.get(background_update.IN_PROCESS_ENV, "").strip().lower() in {"1", "true", "yes"}:
        _sync_semantic_update()
        return None
    try:
        return background_update.spawn()
    except Exception as e:
        sys.stderr.write(
            f"Warning: could not start the semantic search update process ({e}); "
            "running it inside the server instead.\n"
        )
        _sync_semantic_update()
        return None


@asynccontextmanager
async def server_lifespan(server: FastMCP):
    """Manage server startup and shutdown lifecycle.

    The startup update of the semantic index runs in a child process
    (``zotero_mcp.background_update``). In a worker thread it still
    shared the interpreter, and text extraction and OCR of new
    attachments kept the event loop from answering requests until the
    client timed out. The check whether an update is due reads only the
    config file, so a start with nothing to do spawns nothing.

    On shutdown the child is left to finish its update. ChromaDB is
    crash-safe, so an update that is interrupted anyway resumes on the
    next startup.
    """
    sys.stderr.write("Starting Zotero MCP server...\n")

    async def _background_update():
        try:
            proc = await asyncio.to_thread(_start_background_update)
            if proc is not None:
                code = await asyncio.to_thread(proc.wait)
                if code:
                    sys.stderr.write(
                        f"Warning: semantic search auto-update exited with code {code}\n"
                    )
        except Exception as e:
            sys.stderr.write(f"Warning: Could not check semantic search auto-update: {e}\n")

    async def _refresh_schema():
        # TTL-gated conditional GET; degrades to the vendored floor on failure.
        try:
            from zotero_mcp import schema
            if await asyncio.to_thread(schema.refresh) == "offline":
                sys.stderr.write(
                    "Warning: could not refresh the Zotero schema; using the "
                    "cached or vendored copy.\n"
                )
        except Exception as e:
            sys.stderr.write(f"Warning: Zotero schema refresh task failed: {e}\n")

    asyncio.create_task(_background_update())
    asyncio.create_task(_refresh_schema())

    yield {}

    sys.stderr.write("Shutting down Zotero MCP server...\n")


class _ZoteroMCP(FastMCP):
    """FastMCP whose tools get a context they can log to synchronously."""

    def tool(self, name_or_fn=None, **kwargs):
        if callable(name_or_fn):
            return super().tool(sync_context(name_or_fn), **kwargs)
        register = super().tool(name_or_fn, **kwargs)
        return lambda fn: register(sync_context(fn))


# Create an MCP server (fastmcp 2.14+ no longer accepts `dependencies`)
mcp = _ZoteroMCP("Zotero", version=__version__, lifespan=server_lifespan)

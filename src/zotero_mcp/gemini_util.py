"""Small Gemini helpers: one prompt in, JSON text out.

Uses the same Gemini key as the search index (``semantic_search.embedding_config``
in config.json, else ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gemini-flash-latest"


def load_semantic_config(config_path: str | None = None) -> dict:
    """The ``semantic_search`` section of config.json ({} when unreadable)."""
    try:
        if config_path:
            path = Path(config_path)
        else:
            from zotero_mcp.cli import _semantic_config_path

            path = _semantic_config_path(None)
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("semantic_search") or {}
    except Exception:
        return {}


def json_asker(model: str, schema: dict, embedding_config: dict | None = None) -> Callable[[str], str]:
    """A function that sends one prompt and returns the JSON answer as text."""
    from google.genai import types

    from zotero_mcp.gemini_batch import create_gemini_client

    client = create_gemini_client(embedding_config or {})

    def ask(prompt: str) -> str:
        resp = client.models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json",
                                               response_schema=schema),
        )
        return resp.text or ""

    return ask


def ask_json(ask: Callable[[str], str], prompt: str) -> Any:
    """``ask`` and parse; None when the answer is not JSON."""
    try:
        return json.loads(ask(prompt))
    except Exception:
        return None

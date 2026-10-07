"""Small Gemini helpers: one prompt in, JSON text out.

Uses the same Gemini key as the search index (``semantic_search.embedding_config``
in config.json, else ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

# A fixed model, not the "gemini-flash-latest" alias: Google moves the alias to
# each new Flash model, which would mix answers of different models in the caches.
DEFAULT_MODEL = "gemini-3.8-flash"


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


def json_asker(model: str, schema: dict, embedding_config: dict | None = None,
               thinking: str | None = "low") -> Callable[[str], str]:
    """A function that sends one prompt and returns the JSON answer as text.

    No temperature: Google deprecated it in July 2026 and newer models reject it;
    the JSON schema keeps answers consistent. Thinking is kept ``low``: these are
    reading tasks, and thinking tokens are billed as output. A model that does not
    take a thinking level (Gemini 2.5) is asked again without it.
    """
    from google.genai import types

    from zotero_mcp.gemini_batch import create_gemini_client

    client = create_gemini_client(embedding_config or {})
    state = {"thinking": thinking}

    def config() -> types.GenerateContentConfig:
        extra = {}
        if state["thinking"]:
            extra["thinking_config"] = types.ThinkingConfig(thinking_level=state["thinking"].upper())
        return types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=schema,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True), **extra)

    def ask(prompt: str) -> str:
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=config())
        except Exception as e:
            if not (state["thinking"] and "thinking" in str(e).lower()):
                raise
            state["thinking"] = None
            resp = client.models.generate_content(model=model, contents=prompt, config=config())
        return resp.text or ""

    return ask


def ask_json(ask: Callable[[str], str], prompt: str) -> Any:
    """``ask`` and parse; None when the answer is not JSON."""
    try:
        return json.loads(ask(prompt))
    except Exception:
        return None

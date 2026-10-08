"""Small Gemini helpers: one prompt in, JSON text out.

Uses the same Gemini key as the search index (``semantic_search.embedding_config``
in config.json, else ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``).
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

# A fixed model, not the "gemini-flash-latest" alias: Google moves the alias to
# each new Flash model, which would mix answers of different models in the caches.
DEFAULT_MODEL = "gemini-3.8-flash"

#: US dollars per million tokens (input, output; thinking counts as output), standard tier,
#: prompts up to 200k tokens. Google's list price for 2026; it doubles from January 2027.
PRICES = {"gemini-3.8-flash": (0.75, 3.75), "gemini-3.7-flash": (0.75, 3.75), "gemini-3.6-flash": (0.75, 3.75),
          "gemini-3.5-flash": (1.50, 9.00), "gemini-3.5-flash-lite": (0.30, 2.50)}
DEFAULT_THINKING = "low"

#: Tokens and calls of this process, per model: what a run cost.
USAGE: dict[str, Counter] = {}
_USAGE_LOCK = threading.Lock()


def _count(model: str, resp=None, failed: bool = False) -> None:
    with _USAGE_LOCK:
        c = USAGE.setdefault(model, Counter())
        if failed:
            c["failed"] += 1
            return
        c["calls"] += 1
        meta = getattr(resp, "usage_metadata", None)
        if meta is not None:
            c["input"] += int(getattr(meta, "prompt_token_count", 0) or 0)
            c["output"] += int(getattr(meta, "candidates_token_count", 0) or 0)
            c["thinking"] += int(getattr(meta, "thoughts_token_count", 0) or 0)


def _tokens(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1_000_000 else f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def usage_summary() -> str:
    """"gemini-3.8-flash: 412 call(s), 1.60M input tokens, 310.0k output (120.0k of it thinking), about $2.36"."""
    parts = []
    with _USAGE_LOCK:
        for model, c in USAGE.items():
            if not c["calls"] and not c["failed"]:
                continue
            text = (f"{model}: {c['calls']} call(s), {_tokens(c['input'])} input tokens, "
                    f"{_tokens(c['output'] + c['thinking'])} output ({_tokens(c['thinking'])} of it thinking)")
            base = model.split(" ")[0]
            if base in PRICES:
                pin, pout = PRICES[base]
                cost = c["input"] / 1e6 * pin + (c["output"] + c["thinking"]) / 1e6 * pout
                text += f", about ${cost:.2f}"
            if c["failed"]:
                text += f"; {c['failed']} failed"
            parts.append(text)
    return "; ".join(parts)


def _retry_delay(error: Exception) -> float | None:
    """Seconds to wait before asking again, for a rate limit (HTTP 429); None otherwise."""
    text = str(error)
    if "429" not in text and "RESOURCE_EXHAUSTED" not in text:
        return None
    m = re.search(r"retry(?:Delay)?['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", text, re.I) \
        or re.search(r"retry in (\d+(?:\.\d+)?)\s*s", text, re.I)
    return float(m.group(1)) if m else 20.0


def model_settings(cfg: dict) -> tuple[str, str]:
    """(model, thinking level) from ``semantic_search.structure`` in config.json."""
    st = cfg.get("structure") or {}
    return st.get("gemini_model") or DEFAULT_MODEL, st.get("gemini_thinking") or DEFAULT_THINKING


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
               thinking: str | None = DEFAULT_THINKING, sleep: Callable[[float], None] = time.sleep,
               usage_key: str | None = None) -> Callable[[str], str]:
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
        for attempt in range(3):
            try:
                resp = client.models.generate_content(model=model, contents=prompt, config=config())
                _count(usage_key or model, resp)
                return resp.text or ""
            except Exception as e:
                if state["thinking"] and "thinking" in str(e).lower():
                    state["thinking"] = None        # a model without thinking levels: ask again without
                    continue
                delay = _retry_delay(e)
                # A per-minute limit passes after a short wait; a daily limit or a spending
                # cap does not, and is reported after two tries.
                if delay is not None and attempt < 2 and delay <= 60:
                    sleep(delay + 1)
                    continue
                _count(usage_key or model, failed=True)
                raise
        _count(usage_key or model, failed=True)
        raise RuntimeError("Gemini did not answer")

    return ask


def ask_json(ask: Callable[[str], str], prompt: str) -> Any:
    """``ask`` and parse; None when the answer is not JSON."""
    try:
        return json.loads(ask(prompt))
    except Exception:
        return None

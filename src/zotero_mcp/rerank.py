"""Re-rankers: a local cross-encoder or a hosted rerank endpoint.

Kept apart from ``semantic_search`` (which imports ChromaDB and the embedding
SDKs) so that ranking passages of one paper does not pay for those imports.
``semantic_search`` re-exports both classes and keeps the process-wide cache.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger("zotero_mcp.semantic_search")

class CrossEncoderReranker:
    """Optional cross-encoder re-ranker for semantic search results."""

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        from sentence_transformers import CrossEncoder

        self.model = CrossEncoder(model_name)
        self.label = f"local/{model_name}"

    def rerank(self, query: str, documents: list[str], top_k: int) -> list[int]:
        """Re-rank documents by relevance to query.

        Returns indices of top_k documents in descending relevance order.
        """
        return [idx for idx, _ in self.rerank_with_scores(query, documents, top_k)]

    def rerank_with_scores(self, query: str, documents: list[str], top_k: int) -> list[tuple[int, float]]:
        """Re-rank documents, returning ``(index, score)`` pairs.

        Scores are the raw cross-encoder relevance logits, surfaced so search
        results can report *why* an item ranked where it did, not just an
        opaque order.
        """
        pairs = [[query, doc] for doc in documents]
        scores = self.model.predict(pairs)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [(i, float(scores[i])) for i in ranked[:top_k]]

    def rerank_detailed(
        self, query: str, documents: list[str], top_k: int
    ) -> tuple[list[tuple[int, float]], str | None]:
        """``(pairs, error)``, matching :class:`APIReranker`; a local model has no soft failure."""
        return self.rerank_with_scores(query, documents, top_k), None


# Hosted rerank endpoints. All of them take a query, a list of documents and a
# cut-off, and answer with ``{index, relevance_score}`` records, so one client
# covers them and switching vendors is a config edit. What differs:
#
# * the spelling of the cut-off (``top_k`` vs ``top_n``);
# * the key the records are listed under -- Voyage answers
#   ``{"object": "list", "data": [...]}`` while Cohere, OpenRouter and
#   Contextual answer ``{"results": [...]}``. Both are read, so a provider that
#   changes its envelope does not silently turn re-ranking off;
# * how an instruction is passed. Contextual takes an ``instruction`` field.
#   Voyage's instruction-following models (rerank-2.5 and later) read the
#   instruction from the query itself and have no such field, so for them,
#   and for any provider without a documented field, it is prepended.
_RERANK_PROVIDERS: dict[str, dict[str, str]] = {
    "voyage": {
        "url": "https://api.voyageai.com/v1/rerank",
        "key_env": "VOYAGE_API_KEY",
        "top_field": "top_k",
        "instruction": "query",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/rerank",
        "key_env": "OPENROUTER_API_KEY",
        "top_field": "top_n",
        "instruction": "query",
    },
    "cohere": {
        "url": "https://api.cohere.com/v2/rerank",
        "key_env": "COHERE_API_KEY",
        "top_field": "top_n",
        "instruction": "query",
    },
    "contextual": {
        "url": "https://api.contextual.ai/v1/rerank",
        "key_env": "CONTEXTUAL_API_KEY",
        "top_field": "top_n",
        "instruction": "field",
    },
}

# Statuses worth one more attempt: throttling and transient server errors.
_RERANK_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Ceiling on a Retry-After wait. A search is interactive; past this it is
# better to answer in retrieval order than to keep the caller waiting.
_RERANK_MAX_RETRY_WAIT = 5.0


class APIReranker:
    """Re-rank through a hosted cross-encoder instead of a local model.

    Interface-compatible with :class:`CrossEncoderReranker`, so the search path
    does not care which one it holds. Exists because the strong local
    cross-encoders need a GPU to be interactive — on CPU they add seconds to
    every query, while these endpoints answer in 200-600ms.

    A failed call degrades to the retriever's own order rather than raising:
    losing the re-ranking is a worse search, but losing the search entirely
    because a network call timed out is a broken tool. The failure is not
    silent, though: :meth:`rerank_detailed` returns the reason, and the search
    result carries it, so a misconfigured model or key shows up in the very
    next search instead of only in a log nobody reads.
    """

    def __init__(
        self,
        model_name: str,
        provider: str,
        base_url: str | None = None,
        api_key_env: str | None = None,
        instruction: str | None = None,
        timeout: float = 30.0,
    ):
        preset = _RERANK_PROVIDERS.get(provider, {})
        if not preset and not (base_url and api_key_env):
            raise ValueError(
                f"Unknown rerank provider '{provider}'. Use one of "
                f"{', '.join(sorted(_RERANK_PROVIDERS))}, or set both "
                "reranker.base_url and reranker.api_key_env."
            )
        self.provider = provider
        self.model_name = model_name
        self.url = base_url or preset["url"]
        self.top_field = preset.get("top_field", "top_n")
        self.instruction_mode = preset.get("instruction", "query")
        self.key_env = api_key_env or preset.get("key_env", "")
        self.instruction = instruction
        self.timeout = timeout

        self.api_key = os.getenv(self.key_env) if self.key_env else None
        if not self.api_key:
            raise ValueError(
                f"Rerank provider '{provider}' needs {self.key_env} in the "
                "environment; it is unset."
            )

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model_name}"

    def rerank(self, query: str, documents: list[str], top_k: int) -> list[int]:
        """Indices of the top_k documents, most relevant first."""
        return [idx for idx, _ in self.rerank_with_scores(query, documents, top_k)]

    def rerank_with_scores(
        self, query: str, documents: list[str], top_k: int
    ) -> list[tuple[int, float]]:
        """``(index, relevance_score)`` pairs from the hosted reranker."""
        return self.rerank_detailed(query, documents, top_k)[0]

    def _payload(self, query: str, documents: list[str], cutoff: int) -> dict[str, Any]:
        if self.instruction and self.instruction_mode == "query":
            query = f"{self.instruction.strip()}\n\nQuery: {query}"
        payload: dict[str, Any] = {
            "model": self.model_name,
            "query": query,
            "documents": documents,
            self.top_field: cutoff,
        }
        if self.instruction and self.instruction_mode == "field":
            payload["instruction"] = self.instruction
        return payload

    @staticmethod
    def _parse(body: Any, n_documents: int) -> list[tuple[int, float]]:
        """Read ``{index, relevance_score}`` records from either envelope."""
        if not isinstance(body, dict):
            return []
        records = body.get("results")
        if not isinstance(records, list):
            records = body.get("data")
        if not isinstance(records, list):
            return []
        ranked: list[tuple[int, float]] = []
        seen: set[int] = set()
        for r in records:
            if not isinstance(r, dict) or "index" not in r:
                continue
            try:
                idx = int(r["index"])
                score = float(r.get("relevance_score", r.get("score", 0.0)))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < n_documents and idx not in seen:
                seen.add(idx)
                ranked.append((idx, score))
        # Providers return records sorted, but nothing in their contracts says
        # so for every vendor; sort rather than trust.
        ranked.sort(key=lambda pair: pair[1], reverse=True)
        return ranked

    def rerank_detailed(
        self, query: str, documents: list[str], top_k: int
    ) -> tuple[list[tuple[int, float]], str | None]:
        """``(pairs, error)``: the ranking, and why it fell back if it did.

        On failure the pairs are the first ``top_k`` documents in their
        original (retrieval) order with score 0.0, and ``error`` says why.
        """
        import requests

        if not documents:
            return [], None
        cutoff = max(1, min(top_k, len(documents)))
        fallback = [(i, 0.0) for i in range(cutoff)]
        payload = self._payload(query, documents, cutoff)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        error: str | None = None
        for attempt in range(2):
            try:
                response = requests.post(
                    self.url, json=payload, headers=headers, timeout=self.timeout
                )
            except Exception as e:  # network error, timeout
                error = f"request failed: {e}"
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                break

            if response.status_code in _RERANK_RETRY_STATUSES and attempt == 0:
                wait = 1.0
                try:
                    wait = float(response.headers.get("Retry-After", wait))
                except (TypeError, ValueError):
                    pass
                time.sleep(max(0.0, min(wait, _RERANK_MAX_RETRY_WAIT)))
                continue
            if response.status_code >= 400:
                detail = (response.text or "").strip().replace("\n", " ")[:200]
                error = f"HTTP {response.status_code}: {detail}" if detail else f"HTTP {response.status_code}"
                break
            try:
                ranked = self._parse(response.json(), len(documents))
            except ValueError as e:
                error = f"unreadable response: {e}"
                break
            if ranked:
                return ranked[:top_k], None
            error = "response listed no ranked documents"
            break

        logger.warning(f"Rerank via {self.label} failed ({error}); keeping retrieval order.")
        return fallback, error

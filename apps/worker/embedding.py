"""Embed stage logic: texts -> vectors, plus the float32 byte encoding shared by MySQL and Redis."""

import hashlib
import math
import random
import struct
from typing import Protocol

from openai import OpenAI

from .errors import PermanentError


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    def __init__(self, api_key: str, model: str, dim: int, timeout_s: float = 60.0, max_retries: int = 2):
        # The SDK retries 429/5xx itself (honouring Retry-After) up to max_retries; P3 adds stage retries.
        # The API's query embedding passes a shorter timeout (a request thread waits on it).
        self._client = OpenAI(api_key=api_key, timeout=timeout_s, max_retries=max_retries)
        self.model = model
        self._dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.embed_with_usage(texts)[0]

    def embed_with_usage(self, texts: list[str]) -> tuple[list[list[float]], int]:
        """Vectors plus the prompt-token count (for the per-request token log)."""
        kwargs = {"dimensions": self._dim} if self.model.startswith("text-embedding-3") else {}
        resp = self._client.embeddings.create(model=self.model, input=texts, **kwargs)
        vectors = [item.embedding for item in sorted(resp.data, key=lambda d: d.index)]
        return vectors, (resp.usage.prompt_tokens if resp.usage else 0)


class FaultyEmbedder:
    """Chaos/DLQ demo only (FAULT_EMBED_429_PREFIX): HTTP 429 for version 1 of matching filenames.

    Selected in context.py, never by task code, and refused with APP_ENV=production (Settings).
    Raises a genuine openai.RateLimitError (with Retry-After), so the production classification and
    retry path runs unchanged. A replay (version >= 2) passes through, so it can succeed.
    """

    def __init__(self, inner: "Embedder", prefix: str, retry_after_s: int = 1):
        self.inner = inner
        self.prefix = prefix
        self.retry_after_s = retry_after_s
        self.model = getattr(inner, "model", "?")

    def for_document(self, filename: str, version: int) -> "Embedder":
        if version == 1 and filename.startswith(self.prefix):
            return _RateLimited(self.model, self.retry_after_s)
        return self.inner

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.inner.embed(texts)


class _RateLimited:
    def __init__(self, model: str, retry_after_s: int):
        self.model = model
        self.retry_after_s = retry_after_s

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx2
        import openai

        request = httpx2.Request("POST", "https://api.openai.com/v1/embeddings")
        response = httpx2.Response(429, headers={"retry-after": str(self.retry_after_s)}, request=request)
        raise openai.RateLimitError("Rate limit reached (injected fault)", response=response, body=None)


class UnconfiguredEmbedder:
    """Used when no API key is set: fails the stage with a clear code instead of a crash."""

    model = "unconfigured"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise PermanentError("EMBEDDING_NOT_CONFIGURED", "OPENAI_API_KEY is not set")

    def embed_with_usage(self, texts: list[str]) -> tuple[list[list[float]], int]:
        return self.embed(texts), 0


class FakeEmbedder:
    """Deterministic unit vectors from SHA-256 of the text. For tests: no network, no cost."""

    def __init__(self, dim: int = 1536, model: str = "fake-embedder"):
        self.model = model
        self.dim = dim
        self.calls: list[int] = []  # batch sizes, so tests can count API calls

    def embed_with_usage(self, texts: list[str]) -> tuple[list[list[float]], int]:
        return self.embed(texts), sum(len(t.split()) for t in texts)

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        out = []
        for text in texts:
            rng = random.Random(hashlib.sha256(text.encode("utf-8")).digest())
            vec = [rng.gauss(0.0, 1.0) for _ in range(self.dim)]
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


def to_float32_bytes(vector: list[float]) -> bytes:
    """Little-endian float32, the layout Redis VECTOR FLOAT32 expects."""
    return struct.pack(f"<{len(vector)}f", *vector)


def from_float32_bytes(data: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(data) // 4}f", data))

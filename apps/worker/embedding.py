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
    def __init__(self, api_key: str, model: str, dim: int):
        # The SDK retries 429/5xx itself (honouring Retry-After) up to max_retries; P3 adds stage retries.
        self._client = OpenAI(api_key=api_key, timeout=60.0, max_retries=2)
        self.model = model
        self._dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        kwargs = {"dimensions": self._dim} if self.model.startswith("text-embedding-3") else {}
        resp = self._client.embeddings.create(model=self.model, input=texts, **kwargs)
        return [item.embedding for item in sorted(resp.data, key=lambda d: d.index)]


class UnconfiguredEmbedder:
    """Used when no API key is set: fails the stage with a clear code instead of a crash."""

    model = "unconfigured"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise PermanentError("EMBEDDING_NOT_CONFIGURED", "OPENAI_API_KEY is not set")


class FakeEmbedder:
    """Deterministic unit vectors from SHA-256 of the text. For tests: no network, no cost."""

    def __init__(self, dim: int = 1536, model: str = "fake-embedder"):
        self.model = model
        self.dim = dim
        self.calls: list[int] = []  # batch sizes, so tests can count API calls

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

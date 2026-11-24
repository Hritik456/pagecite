import math

import pytest

from apps.worker.embedding import FakeEmbedder, UnconfiguredEmbedder, from_float32_bytes, to_float32_bytes
from apps.worker.errors import PermanentError


def test_float32_roundtrip_is_little_endian_and_sized():
    vec = [0.5, -1.25, 3.0]
    data = to_float32_bytes(vec)
    assert len(data) == 12
    assert data[:4] == b"\x00\x00\x00\x3f"  # 0.5 little-endian
    assert from_float32_bytes(data) == vec


def test_fake_embedder_is_deterministic_unit_length():
    emb = FakeEmbedder(dim=64)
    a1, b = emb.embed(["alpha", "beta"])
    (a2,) = emb.embed(["alpha"])
    assert a1 == a2
    assert a1 != b
    assert math.isclose(sum(v * v for v in a1), 1.0, rel_tol=1e-9)
    assert emb.calls == [2, 1]


def test_unconfigured_embedder_fails_permanently():
    with pytest.raises(PermanentError) as exc:
        UnconfiguredEmbedder().embed(["x"])
    assert exc.value.code == "EMBEDDING_NOT_CONFIGURED"

"""Object keys: tenant/{tenantId}/batch/{batchId}/{documentId}/{safeFilename}. Pure functions."""

import os
import re
import unicodedata

MAX_FILENAME_LEN = 128
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _basename(name: str) -> str:
    """Last path segment for both POSIX and Windows separators."""
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def _clean(part: str) -> str:
    ascii_part = unicodedata.normalize("NFKD", part).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"_{2,}", "_", _UNSAFE.sub("_", ascii_part))


def safe_filename(name: str, max_len: int = MAX_FILENAME_LEN) -> str:
    """Basename only, ASCII [A-Za-z0-9._-], no leading dot, at most max_len chars, extension kept."""
    base = _basename(name)
    stem, ext = os.path.splitext(base)
    ext = _clean(ext.lstrip(".")).strip("._-").lower()[:10]
    stem = _clean(stem).strip("._-") or "file"
    if ext:
        return f"{stem[: max_len - len(ext) - 1]}.{ext}"
    return stem[:max_len]


def tenant_prefix(tenant_id: int) -> str:
    return f"tenant/{tenant_id}/"


def object_key(tenant_id: int, batch_id: int, document_id: int, filename: str) -> str:
    return f"{tenant_prefix(tenant_id)}batch/{batch_id}/{document_id}/{safe_filename(filename)}"


def file_extension(name: str) -> str:
    return os.path.splitext(_basename(name))[1].lstrip(".").lower()

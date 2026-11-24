"""In-memory stand-in for S3Storage that enforces the same signed-header rules as S3/MinIO."""

import base64
import hashlib
from dataclasses import dataclass

from apps.api.storage import ObjectInfo

FAKE_HOST = "https://fake-s3.test/"


@dataclass
class _Presigned:
    content_type: str
    checksum_sha256_b64: str


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.presigned: dict[str, _Presigned] = {}
        self.deleted: list[str] = []

    # --- Storage protocol ---
    def presign_put(self, key: str, content_type: str, checksum_sha256_b64: str) -> str:
        self.presigned[key] = _Presigned(content_type, checksum_sha256_b64)
        return FAKE_HOST + key

    def head(self, key: str) -> ObjectInfo | None:
        data = self.objects.get(key)
        if data is None:
            return None
        return ObjectInfo(size=len(data), checksum_sha256_b64=b64_sha256(data))

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.deleted.append(key)

    # --- test helpers ---
    def put_via_url(self, url: str, data: bytes, headers: dict[str, str]) -> int:
        """Mimic a browser PUT to the presigned URL. Returns the HTTP status S3 would give."""
        key = url.removeprefix(FAKE_HOST)
        signed = self.presigned.get(key)
        if signed is None:
            return 403
        if headers.get("Content-Type") != signed.content_type:
            return 403  # SignatureDoesNotMatch
        if headers.get("x-amz-checksum-sha256") != signed.checksum_sha256_b64:
            return 403
        if b64_sha256(data) != signed.checksum_sha256_b64:
            return 400  # XAmzContentChecksumMismatch
        self.objects[key] = data
        return 200

    def put_raw(self, key: str, data: bytes) -> None:
        """Write an object bypassing the checksum (to simulate a storage that did not verify it)."""
        self.objects[key] = data


def b64_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")

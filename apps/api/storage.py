"""Object storage behind a small protocol, so unit tests can swap in a fake."""

from dataclasses import dataclass
from typing import Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .config import Settings
from .errors import AppError


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    checksum_sha256_b64: str | None


class Storage(Protocol):
    def presign_put(self, key: str, content_type: str, checksum_sha256_b64: str) -> str: ...
    def head(self, key: str) -> ObjectInfo | None: ...
    def delete(self, key: str) -> None: ...


def required_put_headers(content_type: str, checksum_sha256_b64: str) -> dict[str, str]:
    """Headers signed into the presigned PUT. The client must send exactly these."""
    return {"Content-Type": content_type, "x-amz-checksum-sha256": checksum_sha256_b64}


def _unavailable(exc: Exception) -> AppError:
    err = AppError("STORAGE_UNAVAILABLE", "object storage request failed", 503)
    err.__cause__ = exc
    return err


class S3Storage:
    def __init__(self, settings: Settings, bucket: str | None = None):
        cfg = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            # Only send checksums we ask for; botocore's default CRC32 would be signed into the URL.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 3, "mode": "standard"},
        )
        common = {
            "region_name": settings.aws_region,
            "aws_access_key_id": settings.aws_access_key_id,
            "aws_secret_access_key": settings.aws_secret_access_key,
            "config": cfg,
        }
        self.bucket = bucket or settings.s3_bucket
        self._expires = settings.presign_expires_seconds
        self._client = boto3.client("s3", endpoint_url=settings.s3_endpoint_url, **common)
        public = settings.s3_public_endpoint_url or settings.s3_endpoint_url
        self._presigner = boto3.client("s3", endpoint_url=public, **common)

    def presign_put(self, key: str, content_type: str, checksum_sha256_b64: str) -> str:
        # ContentType and ChecksumSHA256 become signed headers: S3 rejects other bytes or another type.
        return self._presigner.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": content_type,
                "ChecksumSHA256": checksum_sha256_b64,
            },
            ExpiresIn=self._expires,
            HttpMethod="PUT",
        )

    def head(self, key: str) -> ObjectInfo | None:
        try:
            resp = self._client.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise _unavailable(exc) from exc
        except BotoCoreError as exc:
            raise _unavailable(exc) from exc
        return ObjectInfo(size=resp["ContentLength"], checksum_sha256_b64=resp.get("ChecksumSHA256"))

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except (ClientError, BotoCoreError) as exc:
            raise _unavailable(exc) from exc

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
    def get(self, key: str) -> bytes: ...
    def presign_get(self, key: str, filename: str, content_type: str) -> str: ...
    def delete(self, key: str) -> None: ...


def required_put_headers(content_type: str, checksum_sha256_b64: str) -> dict[str, str]:
    """Headers signed into the presigned PUT. The client must send exactly these."""
    return {"Content-Type": content_type, "x-amz-checksum-sha256": checksum_sha256_b64}


def _unavailable(exc: Exception) -> AppError:
    err = AppError("STORAGE_UNAVAILABLE", "object storage request failed", 503)
    err.__cause__ = exc
    return err


def regional_endpoint(region: str) -> str:
    """AWS S3's regional endpoint. Presigning against the global one (s3.amazonaws.com) makes S3 answer
    the browser's PUT/GET outside us-east-1 with a 307 redirect, which a presigned request can't follow."""
    return f"https://s3.{region}.amazonaws.com"


class S3Storage:
    """MinIO in dev (S3_ENDPOINT_URL + path style); AWS in prod: no endpoint setting, virtual-hosted style,
    the regional endpoint of AWS_REGION, SigV4, and credentials from the instance role (no keys set)."""

    def __init__(self, settings: Settings, bucket: str | None = None):
        cfg = Config(
            signature_version="s3v4",
            s3={"addressing_style": settings.s3_addressing_style},
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
        internal = settings.s3_endpoint_url or regional_endpoint(settings.aws_region)
        self._client = boto3.client("s3", endpoint_url=internal, **common)
        public = settings.s3_public_endpoint_url or internal
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

    def presign_get(self, key: str, filename: str, content_type: str) -> str:
        """A short-lived GET URL for the browser (citation chips open the cited page)."""
        safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in filename)[:120] or "document"
        return self._presigner.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentType": content_type,
                "ResponseContentDisposition": f'inline; filename="{safe_name}"',
            },
            ExpiresIn=self._expires,
            HttpMethod="GET",
        )

    def get(self, key: str) -> bytes:
        """Whole object (used by the parse worker; file sizes are capped by tenant quotas)."""
        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as exc:
            # A missing object will never appear on retry (permanent); anything else may be transient.
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                raise AppError("OBJECT_NOT_FOUND", "the stored object does not exist", 404) from exc
            raise _unavailable(exc) from exc
        except BotoCoreError as exc:
            raise _unavailable(exc) from exc

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except (ClientError, BotoCoreError) as exc:
            raise _unavailable(exc) from exc

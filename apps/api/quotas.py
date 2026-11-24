"""Batch request schema and tenant quota checks. Pure: no DB, no Flask."""

import base64
from dataclasses import dataclass

from pydantic import BaseModel, Field

from .errors import AppError
from .keys import file_extension

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ALLOWED_TYPES: dict[str, set[str]] = {PDF: {"pdf"}, DOCX: {"docx"}}
MB = 1024 * 1024


class FileSpec(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    mime: str = Field(min_length=1, max_length=127)


class BatchRequest(BaseModel):
    files: list[FileSpec] = Field(min_length=1)


@dataclass(frozen=True)
class TenantLimits:
    max_file_mb: int
    max_batch_files: int


def check_batch(files: list[FileSpec], limits: TenantLimits) -> None:
    """Raise AppError on the first violation. Runs before any row or presigned URL is created."""
    if len(files) > limits.max_batch_files:
        raise AppError(
            "QUOTA_EXCEEDED",
            f"batch has {len(files)} files; the limit is {limits.max_batch_files}",
            413,
        )
    max_bytes = limits.max_file_mb * MB
    for i, f in enumerate(files):
        if f.size > max_bytes:
            raise AppError(
                "QUOTA_EXCEEDED",
                f"files[{i}] ({f.name}) is {f.size} bytes; the limit is {limits.max_file_mb} MB",
                413,
            )
        allowed_ext = ALLOWED_TYPES.get(f.mime)
        if allowed_ext is None:
            raise AppError("UNSUPPORTED_TYPE", f"files[{i}] ({f.name}): only PDF and DOCX are accepted", 415)
        if file_extension(f.name) not in allowed_ext:
            raise AppError(
                "UNSUPPORTED_TYPE", f"files[{i}] ({f.name}): extension does not match type {f.mime}", 415
            )


def sha256_hex_to_b64(hex_digest: str) -> str:
    """S3's x-amz-checksum-sha256 is the base64 of the raw digest."""
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")

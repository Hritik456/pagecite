import base64
import hashlib

import pytest
from pydantic import ValidationError

from apps.api.errors import AppError
from apps.api.quotas import (
    DOCX,
    MB,
    PDF,
    BatchRequest,
    FileSpec,
    TenantLimits,
    check_batch,
    sha256_hex_to_b64,
)

LIMITS = TenantLimits(max_file_mb=5, max_batch_files=3)
HASH = "a" * 64


def spec(name="a.pdf", size=1000, mime=PDF, sha256=HASH) -> FileSpec:
    return FileSpec(name=name, size=size, sha256=sha256, mime=mime)


def rejected(files) -> AppError:
    with pytest.raises(AppError) as exc_info:
        check_batch(files, LIMITS)
    return exc_info.value


def test_within_limits_passes():
    check_batch([spec("a.pdf"), spec("b.docx", mime=DOCX), spec("C.PDF")], LIMITS)


def test_file_at_exact_limit_passes():
    check_batch([spec(size=5 * MB)], LIMITS)


def test_file_one_byte_over_limit_is_rejected():
    err = rejected([spec(size=5 * MB + 1)])
    assert (err.code, err.status) == ("QUOTA_EXCEEDED", 413)


def test_too_many_files_is_rejected():
    err = rejected([spec(f"{i}.pdf") for i in range(4)])
    assert (err.code, err.status) == ("QUOTA_EXCEEDED", 413)


@pytest.mark.parametrize("mime", ["text/plain", "image/png", "application/msword"])
def test_unsupported_mime_is_rejected(mime):
    err = rejected([spec("a.pdf", mime=mime)])
    assert (err.code, err.status) == ("UNSUPPORTED_TYPE", 415)


@pytest.mark.parametrize(("name", "mime"), [("a.docx", PDF), ("a.pdf", DOCX), ("a.exe", PDF), ("noext", PDF)])
def test_extension_must_match_mime(name, mime):
    err = rejected([spec(name, mime=mime)])
    assert (err.code, err.status) == ("UNSUPPORTED_TYPE", 415)


def test_schema_rejects_empty_batch():
    with pytest.raises(ValidationError):
        BatchRequest.model_validate({"files": []})


@pytest.mark.parametrize("bad", ["abc", "g" * 64, "a" * 63, "a" * 65])
def test_schema_rejects_bad_sha256(bad):
    with pytest.raises(ValidationError):
        spec(sha256=bad)


@pytest.mark.parametrize("size", [0, -1])
def test_schema_rejects_non_positive_size(size):
    with pytest.raises(ValidationError):
        spec(size=size)


def test_sha256_hex_to_b64_matches_s3_format():
    data = b"hello"
    digest = hashlib.sha256(data)
    assert sha256_hex_to_b64(digest.hexdigest()) == base64.b64encode(digest.digest()).decode()

"""Prod S3 (P5B): presigned URLs go to the ap-south-1 regional, virtual-hosted endpoint with SigV4, and
the bucket CORS (deploy/s3-cors.json) allows exactly the headers the presigned PUT needs."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from apps.api.config import Settings
from apps.api.storage import S3Storage, required_put_headers

ROOT = Path(__file__).resolve().parents[2]
BUCKET = "pagecite-prod-k3x9q2"
HOST = f"{BUCKET}.s3.ap-south-1.amazonaws.com"


@pytest.fixture
def prod_storage() -> S3Storage:
    # Dummy credentials: presigning is local (no network). Prod itself sets no keys (instance role).
    settings = Settings(
        database_url="x", s3_bucket=BUCKET, aws_region="ap-south-1", s3_addressing_style="virtual",
        s3_endpoint_url=None, s3_public_endpoint_url=None,
        aws_access_key_id="AKIAEXAMPLEEXAMPLE00", aws_secret_access_key="dummy-secret",
    )
    return S3Storage(settings)


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_presigned_put_is_regional_virtual_hosted_sigv4(prod_storage):
    url = prod_storage.presign_put("tenant/7/batch/3/12/circular.pdf", "application/pdf", "q" * 43 + "=")
    parsed = urlparse(url)
    assert parsed.scheme == "https" and parsed.hostname == HOST
    assert parsed.path == "/tenant/7/batch/3/12/circular.pdf"  # virtual-hosted: no bucket in the path
    q = _query(url)
    assert q["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
    assert "/ap-south-1/s3/aws4_request" in q["X-Amz-Credential"]
    assert q["X-Amz-SignedHeaders"] == "content-type;host;x-amz-checksum-sha256"


def test_presigned_get_is_regional_too(prod_storage):
    url = prod_storage.presign_get("tenant/7/batch/3/12/circular.pdf", "circular.pdf", "application/pdf")
    assert urlparse(url).hostname == HOST
    assert "/ap-south-1/s3/aws4_request" in _query(url)["X-Amz-Credential"]


def test_cors_allows_exactly_the_presigned_put_headers():
    (rule,) = json.loads((ROOT / "deploy" / "s3-cors.json").read_text(encoding="utf-8"))
    sent_by_browser = {h.lower() for h in required_put_headers("application/pdf", "x")}
    assert set(rule["AllowedHeaders"]) == sent_by_browser == {"content-type", "x-amz-checksum-sha256"}
    assert rule["AllowedMethods"] == ["PUT"]  # GETs are navigations (PDF opens in a tab), not CORS requests
    assert rule["AllowedOrigins"] == ["https://pagecite.duckdns.org"]


def test_signed_headers_match_what_the_browser_is_told_to_send(prod_storage):
    url = prod_storage.presign_put("tenant/1/batch/1/1/a.pdf", "application/pdf", "q" * 43 + "=")
    signed = set(_query(url)["X-Amz-SignedHeaders"].split(";")) - {"host"}
    assert signed == {h.lower() for h in required_put_headers("application/pdf", "x")}

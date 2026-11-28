"""X-Tenant-Key auth: the key is hashed (SHA-256) and looked up.

- tenants.api_key_hash: the tenant's full key (`pc_...`), every route.
- api_keys (V5), scope 'ask' (`pca_...`): the public demo's browser key. Deny by default: GET/HEAD
  and POST /ask only; any other route (upload, complete, replay, cancel, and whatever is added
  later) is 403 FORBIDDEN_FOR_KEY. A revoked ask key is unknown (401).
"""

import hashlib
import secrets

from flask import g, request
from sqlalchemy import select

from .errors import AppError
from .models import ApiKey, Tenant
from .quotas import TenantLimits
from .state import TenantContext, app_state

HEADER = "X-Tenant-Key"
PUBLIC_ENDPOINTS = {"health"}
ASK_SCOPE_ENDPOINTS = {"ask.ask"}  # the only non-read endpoint an ask key may call
READ_METHODS = {"GET", "HEAD"}


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    return "pc_" + secrets.token_urlsafe(32)


def new_ask_key() -> str:
    """An ask-only key. Its own prefix, so a CI check can tell it from a full key in a bundle."""
    return "pca_" + secrets.token_urlsafe(32)


def scope_allows(scope: str, method: str, endpoint: str | None) -> bool:
    if scope == "full":
        return True
    if scope == "ask":
        return method in READ_METHODS or endpoint in ASK_SCOPE_ENDPOINTS
    return False


def authenticate() -> None:
    # Unknown routes (endpoint None) fall through to the 404 handler.
    if request.endpoint is None or request.endpoint in PUBLIC_ENDPOINTS:
        return
    key = request.headers.get(HEADER, "")
    if not key:
        raise AppError("UNAUTHORIZED", f"missing {HEADER} header", 401)
    # A short session of its own, so the request session stays free for explicit transactions.
    digest = hash_key(key)
    scope = "full"
    with app_state().sessions() as session:
        tenant = session.scalar(select(Tenant).where(Tenant.api_key_hash == digest))
        if tenant is None:
            row = session.execute(
                select(Tenant, ApiKey.scope)
                .join(ApiKey, ApiKey.tenant_id == Tenant.id)
                .where(ApiKey.key_hash == digest, ApiKey.revoked_at.is_(None))
            ).first()
            if row is not None:
                tenant, scope = row
    if tenant is None:
        raise AppError("UNAUTHORIZED", f"invalid {HEADER}", 401)
    if not scope_allows(scope, request.method, request.endpoint):
        raise AppError("FORBIDDEN_FOR_KEY", "this key can only ask questions", 403)
    g.tenant = TenantContext(
        id=tenant.id,
        limits=TenantLimits(max_file_mb=tenant.max_file_mb, max_batch_files=tenant.max_batch_files),
        scope=scope,
    )

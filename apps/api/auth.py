"""X-Tenant-Key auth: the key is hashed (SHA-256) and looked up in tenants.api_key_hash."""

import hashlib
import secrets

from flask import g, request
from sqlalchemy import select

from .errors import AppError
from .models import Tenant
from .quotas import TenantLimits
from .state import TenantContext, app_state

HEADER = "X-Tenant-Key"
PUBLIC_ENDPOINTS = {"health"}


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    return "pc_" + secrets.token_urlsafe(32)


def authenticate() -> None:
    # Unknown routes (endpoint None) fall through to the 404 handler.
    if request.endpoint is None or request.endpoint in PUBLIC_ENDPOINTS:
        return
    key = request.headers.get(HEADER, "")
    if not key:
        raise AppError("UNAUTHORIZED", f"missing {HEADER} header", 401)
    # A short session of its own, so the request session stays free for explicit transactions.
    with app_state().sessions() as session:
        tenant = session.scalar(select(Tenant).where(Tenant.api_key_hash == hash_key(key)))
    if tenant is None:
        raise AppError("UNAUTHORIZED", f"invalid {HEADER}", 401)
    g.tenant = TenantContext(
        id=tenant.id,
        limits=TenantLimits(max_file_mb=tenant.max_file_mb, max_batch_files=tenant.max_batch_files),
    )

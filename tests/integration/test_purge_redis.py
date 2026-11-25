"""purge-tenant on the real Redis: tenant 1's pattern must not touch tenant 12's keys."""

import pytest

from apps.api.search_index import delete_tenant_keys

pytestmark = pytest.mark.integration


def test_tenant_key_pattern_does_not_cross_tenants(redis_index):
    r, cfg = redis_index
    for key in (f"{cfg.prefix}1:10", f"{cfg.prefix}1:11", f"{cfg.prefix}12:10", f"{cfg.prefix}112:5"):
        r.hset(key, mapping={"tenant": "x"})
    assert delete_tenant_keys(r, cfg, 1) == 2
    assert sorted(k.decode() if isinstance(k, bytes) else k for k in r.scan_iter(match=f"{cfg.prefix}*")) == [
        f"{cfg.prefix}112:5", f"{cfg.prefix}12:10",
    ]

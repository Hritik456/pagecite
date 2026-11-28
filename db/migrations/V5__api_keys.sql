-- PageCite V5: scoped API keys (P5B public demo).
--   tenants.api_key_hash stays the tenant's full key (unchanged for every existing tenant).
--   api_keys holds extra keys with a narrower scope. 'ask': may read (GET/HEAD) and POST /ask, nothing
--   else (deny by default, apps/api/auth.py). The public demo's browser key is one of these.
--   Additive only: code from before V5 keeps working against this schema (rollback stays possible).

CREATE TABLE api_keys (
    id         BIGINT      NOT NULL AUTO_INCREMENT,
    tenant_id  BIGINT      NOT NULL,
    key_hash   CHAR(64)    NOT NULL,
    scope      ENUM('ask') NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    revoked_at DATETIME(6) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_api_keys_key_hash (key_hash),
    KEY ix_api_keys_tenant (tenant_id),
    CONSTRAINT fk_api_keys_tenant FOREIGN KEY (tenant_id) REFERENCES tenants (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

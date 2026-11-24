-- PageCite V1: every table from docs/specs.md §5, so later phases need no schema churn.
-- Deviation from the spec table: tenants.api_key_hash (SHA-256 hex of the X-Tenant-Key).
-- Timestamps are UTC, DATETIME(6).

CREATE TABLE tenants (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    name            VARCHAR(120) NOT NULL,
    api_key_hash    CHAR(64)     NOT NULL,
    max_file_mb     INT          NOT NULL,
    max_batch_files INT          NOT NULL,
    created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_tenants_name (name),
    UNIQUE KEY uq_tenants_api_key_hash (api_key_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE batches (
    id         BIGINT      NOT NULL AUTO_INCREMENT,
    tenant_id  BIGINT      NOT NULL,
    status     ENUM('UPLOADING', 'PROCESSING', 'DONE', 'PARTIAL', 'CANCELLED') NOT NULL DEFAULT 'UPLOADING',
    total      INT         NOT NULL,
    succeeded  INT         NOT NULL DEFAULT 0,
    failed     INT         NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_batches_tenant (tenant_id),
    CONSTRAINT fk_batches_tenant FOREIGN KEY (tenant_id) REFERENCES tenants (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE documents (
    id                 BIGINT       NOT NULL AUTO_INCREMENT,
    batch_id           BIGINT       NOT NULL,
    tenant_id          BIGINT       NOT NULL,
    object_key         VARCHAR(512) NOT NULL,
    filename           VARCHAR(255) NOT NULL,
    sha256             CHAR(64)     NOT NULL,
    size_bytes         BIGINT       NOT NULL,
    mime               VARCHAR(127) NOT NULL,
    -- Full document state machine (docs/specs.md §5).
    status             ENUM('CREATED', 'UPLOADED', 'PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXED',
                            'RETRY_WAIT', 'FAILED', 'CANCEL_REQUESTED', 'CANCELLED') NOT NULL DEFAULT 'CREATED',
    processing_version INT          NOT NULL DEFAULT 1,
    created_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_documents_object_key (object_key),
    KEY ix_documents_batch (batch_id),
    KEY ix_documents_tenant_status (tenant_id, status),
    CONSTRAINT fk_documents_batch FOREIGN KEY (batch_id) REFERENCES batches (id),
    CONSTRAINT fk_documents_tenant FOREIGN KEY (tenant_id) REFERENCES tenants (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE document_stages (
    id          BIGINT      NOT NULL AUTO_INCREMENT,
    document_id BIGINT      NOT NULL,
    stage       ENUM('parse', 'chunk', 'embed', 'index') NOT NULL,
    version     INT         NOT NULL,
    status      ENUM('PENDING', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED', 'FAILED', 'CANCELLED') NOT NULL DEFAULT 'PENDING',
    attempts    INT         NOT NULL DEFAULT 0,
    last_error  TEXT        NULL,
    started_at  DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_document_stages (document_id, stage, version),
    CONSTRAINT fk_document_stages_document FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE chunks (
    id             BIGINT     NOT NULL AUTO_INCREMENT,
    document_id    BIGINT     NOT NULL,
    tenant_id      BIGINT     NOT NULL,
    version        INT        NOT NULL,
    ord            INT        NOT NULL,
    page_from      INT        NOT NULL,
    page_to        INT        NOT NULL,
    text           MEDIUMTEXT NOT NULL,
    token_count    INT        NOT NULL,
    embedding_done BOOL       NOT NULL DEFAULT FALSE,
    PRIMARY KEY (id),
    UNIQUE KEY uq_chunks (document_id, version, ord),
    KEY ix_chunks_tenant (tenant_id),
    CONSTRAINT fk_chunks_document FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE,
    CONSTRAINT fk_chunks_tenant FOREIGN KEY (tenant_id) REFERENCES tenants (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE processed_jobs (
    job_key      VARCHAR(191) NOT NULL,  -- doc:{id}:{stage}:v{n}
    processed_at DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (job_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE dead_letters (
    id          BIGINT      NOT NULL AUTO_INCREMENT,
    document_id BIGINT      NOT NULL,
    stage       ENUM('parse', 'chunk', 'embed', 'index') NOT NULL,
    version     INT         NOT NULL,
    error       TEXT        NOT NULL,
    payload     JSON        NULL,
    created_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    replayed_at DATETIME(6) NULL,
    PRIMARY KEY (id),
    KEY ix_dead_letters_document (document_id),
    CONSTRAINT fk_dead_letters_document FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE outbox_events (
    id           BIGINT      NOT NULL AUTO_INCREMENT,
    type         VARCHAR(64) NOT NULL,
    aggregate_id BIGINT      NOT NULL,
    payload      JSON        NOT NULL,
    created_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    published_at DATETIME(6) NULL,
    PRIMARY KEY (id),
    -- The P2 relay scans unpublished rows in id order.
    KEY ix_outbox_unpublished (published_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE eval_runs (
    id           BIGINT        NOT NULL AUTO_INCREMENT,
    config       JSON          NOT NULL,
    n_questions  INT           NOT NULL,
    hit_at_5     DECIMAL(6, 4) NULL,
    mrr          DECIMAL(6, 4) NULL,
    faithfulness DECIMAL(6, 4) NULL,
    created_at   DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- PageCite V2: storage the P2 pipeline needs that docs/specs.md §5 does not list (see docs/adr/0002).
--   * document_pages: parse output (text per page), consumed by the chunk stage.
--   * chunks.embedding: the vector lives in MySQL too, so `reindex` rebuilds the Redis index
--     byte-for-byte from MySQL without calling the embedding API again.

CREATE TABLE document_pages (
    id          BIGINT     NOT NULL AUTO_INCREMENT,
    document_id BIGINT     NOT NULL,
    version     INT        NOT NULL,
    page_no     INT        NOT NULL,
    text        MEDIUMTEXT NOT NULL,
    ocr         BOOL       NOT NULL DEFAULT FALSE,
    PRIMARY KEY (id),
    UNIQUE KEY uq_document_pages (document_id, version, page_no),
    CONSTRAINT fk_document_pages_document FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

ALTER TABLE chunks
    ADD COLUMN embedding       BLOB        NULL AFTER token_count,  -- little-endian float32 x embed_dim
    ADD COLUMN embedding_model VARCHAR(64) NULL AFTER embedding;

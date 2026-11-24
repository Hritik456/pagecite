-- PageCite V3: replay + DLQ (P3).
--   * documents.active_version: the one version retrieval serves. Replay bumps processing_version
--     but keeps active_version until the new version is INDEXED; the switch happens in the same
--     transaction that marks it INDEXED (docs/adr/0004).
--   * dead_letters.kind / error_code: why the document landed in the DLQ (every final failure does).

ALTER TABLE documents
    ADD COLUMN active_version INT NULL AFTER processing_version;

UPDATE documents SET active_version = processing_version WHERE status = 'INDEXED';

ALTER TABLE dead_letters
    ADD COLUMN kind       VARCHAR(16) NOT NULL DEFAULT 'exhausted' AFTER version,  -- exhausted | permanent | attempt_limit
    ADD COLUMN error_code VARCHAR(40) NULL AFTER kind,
    ADD KEY ix_dead_letters_open (replayed_at, id);

-- +goose Up
CREATE TABLE facts (
    id              BIGSERIAL       PRIMARY KEY,
    namespace_id    BIGINT          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    content         TEXT            NOT NULL,
    embedding       vector          NULL,
    embedding_model TEXT            NULL,
    confidence      REAL            NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    valid_from      TIMESTAMPTZ     NULL,
    valid_until     TIMESTAMPTZ     NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ     NULL
);

CREATE INDEX ON facts (namespace_id, confidence);
CREATE INDEX ON facts (namespace_id, valid_from, valid_until);
CREATE INDEX ON facts (namespace_id, deleted_at);

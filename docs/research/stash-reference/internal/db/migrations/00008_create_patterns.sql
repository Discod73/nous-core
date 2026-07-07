-- +goose Up
CREATE TABLE patterns (
    id              BIGSERIAL       PRIMARY KEY,
    namespace_id    BIGINT          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    content         TEXT            NOT NULL,
    confidence      REAL            NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    source_fact_ids BIGINT[]        NOT NULL DEFAULT '{}',
    source_rel_ids  BIGINT[]        NOT NULL DEFAULT '{}',
    coherence_score REAL            NOT NULL DEFAULT 0.0,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ     NULL
);

CREATE INDEX ON patterns (namespace_id);

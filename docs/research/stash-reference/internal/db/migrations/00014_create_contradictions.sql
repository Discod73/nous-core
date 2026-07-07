-- +goose Up
CREATE TABLE contradictions (
    id              BIGSERIAL       PRIMARY KEY,
    namespace_id    BIGINT          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    old_fact_id     BIGINT          NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    new_fact_id     BIGINT          NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    entity          TEXT            NOT NULL,
    property        TEXT            NOT NULL,
    old_value       TEXT            NOT NULL,
    new_value       TEXT            NOT NULL,
    confidence      REAL            NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    method          TEXT            NOT NULL DEFAULT 'structured',
    resolved        BOOLEAN         NOT NULL DEFAULT FALSE,
    resolution      TEXT            NULL,
    resolved_at     TIMESTAMPTZ     NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now()
);

CREATE INDEX ON contradictions (namespace_id, resolved);
CREATE INDEX ON contradictions (old_fact_id);
CREATE INDEX ON contradictions (new_fact_id);

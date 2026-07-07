-- +goose Up
CREATE TABLE relationships (
    id              BIGSERIAL       PRIMARY KEY,
    namespace_id    BIGINT          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    from_entity     TEXT            NOT NULL,
    relation_type   TEXT            NOT NULL,
    to_entity       TEXT            NOT NULL,
    confidence      REAL            NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    source_fact_id  BIGINT          NULL REFERENCES facts(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ     NULL
);

CREATE INDEX ON relationships (namespace_id, from_entity);
CREATE INDEX ON relationships (namespace_id, to_entity);
CREATE INDEX ON relationships (namespace_id, relation_type);

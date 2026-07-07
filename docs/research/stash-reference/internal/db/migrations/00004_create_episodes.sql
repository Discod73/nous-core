-- +goose Up
CREATE TABLE episodes (
    id              BIGSERIAL       PRIMARY KEY,
    namespace_id    BIGINT          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    content         TEXT            NOT NULL,
    embedding       vector          NULL,
    embedding_model TEXT            NULL,
    occurred_at     TIMESTAMPTZ     NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ     NULL
);

CREATE INDEX ON episodes (namespace_id, occurred_at);
CREATE INDEX ON episodes (namespace_id, deleted_at);

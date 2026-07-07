-- +goose Up
CREATE TABLE goals (
    id             BIGSERIAL   PRIMARY KEY,
    namespace_id   BIGINT      NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    parent_id      BIGINT      NULL REFERENCES goals(id) ON DELETE CASCADE,
    content        TEXT        NOT NULL,
    status         TEXT        NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'completed', 'abandoned')),
    priority       INT         NOT NULL DEFAULT 0,
    notes          TEXT        NOT NULL DEFAULT '',
    completed_at   TIMESTAMPTZ NULL,
    abandoned_at   TIMESTAMPTZ NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at     TIMESTAMPTZ NULL
);

CREATE INDEX ON goals (namespace_id, status);
CREATE INDEX ON goals (namespace_id, parent_id) WHERE deleted_at IS NULL;
CREATE INDEX ON goals (parent_id) WHERE deleted_at IS NULL;

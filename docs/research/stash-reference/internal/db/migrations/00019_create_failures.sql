-- +goose Up
CREATE TABLE failures (
    id             BIGSERIAL   PRIMARY KEY,
    namespace_id   BIGINT      NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    goal_id        BIGINT      NULL REFERENCES goals(id) ON DELETE SET NULL,
    content        TEXT        NOT NULL,
    reason         TEXT        NOT NULL DEFAULT '',
    lesson         TEXT        NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at     TIMESTAMPTZ NULL
);

CREATE INDEX ON failures (namespace_id, deleted_at) WHERE deleted_at IS NULL;
CREATE INDEX ON failures (goal_id) WHERE goal_id IS NOT NULL;

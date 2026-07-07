-- +goose Up
CREATE TABLE contexts (
    namespace_id    BIGINT          NOT NULL PRIMARY KEY REFERENCES namespaces(id) ON DELETE CASCADE,
    focus           TEXT            NOT NULL DEFAULT '',
    expires_at      TIMESTAMPTZ     NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now()
);

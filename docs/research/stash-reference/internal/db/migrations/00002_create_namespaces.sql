-- +goose Up
CREATE TABLE namespaces (
    id          BIGSERIAL       PRIMARY KEY,
    slug        TEXT            NOT NULL UNIQUE,
    name        TEXT            NOT NULL DEFAULT '',
    description TEXT            NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT now()
);

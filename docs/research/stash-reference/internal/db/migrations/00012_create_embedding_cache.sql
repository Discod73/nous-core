-- +goose Up
CREATE TABLE embedding_cache (
    text_hash       TEXT            NOT NULL,
    model           TEXT            NOT NULL,
    text            TEXT            NOT NULL,
    embedding       vector          NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    PRIMARY KEY (text_hash, model)
);

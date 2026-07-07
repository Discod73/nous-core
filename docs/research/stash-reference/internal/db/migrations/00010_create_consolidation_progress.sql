-- +goose Up
CREATE TABLE consolidation_progress (
    namespace_id                BIGINT          NOT NULL PRIMARY KEY REFERENCES namespaces(id) ON DELETE CASCADE,
    last_episode_id             BIGINT          NOT NULL DEFAULT 0,
    last_fact_id                BIGINT          NOT NULL DEFAULT 0,
    last_relationship_id        BIGINT          NOT NULL DEFAULT 0,
    last_pattern_fact_id        BIGINT          NOT NULL DEFAULT 0,
    last_pattern_rel_id         BIGINT          NOT NULL DEFAULT 0,
    last_run                    TIMESTAMPTZ     NULL,
    updated_at                  TIMESTAMPTZ     NOT NULL DEFAULT now()
);

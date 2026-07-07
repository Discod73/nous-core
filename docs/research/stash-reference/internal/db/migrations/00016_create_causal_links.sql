-- +goose Up
CREATE TABLE causal_links (
    id              BIGSERIAL       PRIMARY KEY,
    namespace_id    BIGINT          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    cause_fact_id   BIGINT          NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    effect_fact_id  BIGINT          NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    confidence      REAL            NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    method          TEXT            NOT NULL DEFAULT 'extracted',
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ     NULL
);

CREATE UNIQUE INDEX ON causal_links (cause_fact_id, effect_fact_id) WHERE deleted_at IS NULL;
CREATE INDEX ON causal_links (namespace_id, cause_fact_id) WHERE deleted_at IS NULL;
CREATE INDEX ON causal_links (namespace_id, effect_fact_id) WHERE deleted_at IS NULL;

-- +goose Up
CREATE TABLE hypotheses (
    id                BIGSERIAL   PRIMARY KEY,
    namespace_id      BIGINT      NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    content           TEXT        NOT NULL,
    confidence        REAL        NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    status            TEXT        NOT NULL DEFAULT 'proposed'
                        CHECK (status IN ('proposed', 'testing', 'confirmed', 'rejected')),
    verification_plan TEXT        NOT NULL DEFAULT '',
    method            TEXT        NOT NULL DEFAULT 'asserted',
    confirmed_fact_id BIGINT      NULL REFERENCES facts(id),
    rejection_reason  TEXT        NULL,
    source_fact_ids   BIGINT[]    NOT NULL DEFAULT '{}',
    tested_at         TIMESTAMPTZ NULL,
    confirmed_at      TIMESTAMPTZ NULL,
    rejected_at       TIMESTAMPTZ NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at        TIMESTAMPTZ NULL
);

CREATE INDEX ON hypotheses (namespace_id, status);
CREATE INDEX ON hypotheses (namespace_id, deleted_at) WHERE deleted_at IS NULL;
CREATE INDEX ON hypotheses (confirmed_fact_id) WHERE confirmed_fact_id IS NOT NULL;

-- +goose Up
CREATE TABLE fact_sources (
    fact_id     BIGINT          NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    episode_id  BIGINT          NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    PRIMARY KEY (fact_id, episode_id)
);

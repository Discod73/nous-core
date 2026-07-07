-- +goose Up
ALTER TABLE consolidation_progress ADD COLUMN last_decay_run TIMESTAMPTZ;

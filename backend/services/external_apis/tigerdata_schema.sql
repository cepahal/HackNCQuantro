-- Apply once to the Tiger Data/Postgres database used by TIGERDATA_DSN.
-- Runtime storage performs no DDL. Grant the backend role schema USAGE and
-- INSERT/SELECT on this table; use a separate migration role to apply this file.
CREATE SCHEMA IF NOT EXISTS quantro_retrieval;

CREATE TABLE IF NOT EXISTS quantro_retrieval.posts (
    post_id TEXT PRIMARY KEY CHECK (char_length(post_id) BETWEEN 1 AND 255),
    media_id TEXT NOT NULL UNIQUE CHECK (char_length(media_id) BETWEEN 1 AND 128),
    mime_type TEXT NOT NULL CHECK (mime_type IN ('image/jpeg', 'video/mp4')),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes BIGINT NOT NULL CHECK (size_bytes BETWEEN 1 AND 100000000),
    media_bytes BYTEA NOT NULL CHECK (octet_length(media_bytes) = size_bytes),
    metadata JSONB NOT NULL CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

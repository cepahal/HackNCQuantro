-- Run once in the database/schema configured for the backend's Snowflake role.
-- Runtime code performs no DDL. If you override the default stage/table names,
-- create equivalent objects and set SNOWFLAKE_MEDIA_STAGE / SNOWFLAKE_POSTS_TABLE.
CREATE STAGE IF NOT EXISTS QUANTRO_MEDIA;

CREATE TABLE IF NOT EXISTS QUANTRO_POSTS (
    POST_ID VARCHAR(255) NOT NULL,
    MEDIA_ID VARCHAR(128) NOT NULL,
    STAGE_PATH VARCHAR NOT NULL,
    SHA256 VARCHAR(64) NOT NULL,
    SIZE_BYTES NUMBER(38, 0) NOT NULL,
    MIME_TYPE VARCHAR NOT NULL,
    METADATA VARIANT NOT NULL,
    CREATED_AT TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

-- Grant the backend role USAGE on the database/schema and warehouse,
-- READ and WRITE on the internal stage, and INSERT/SELECT on QUANTRO_POSTS.
-- A failed metadata transaction can leave an unreferenced uploaded file.
-- Keep it for investigation; do not remove another call's committed media.

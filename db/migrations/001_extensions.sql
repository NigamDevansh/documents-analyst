-- 001 · Extensions and the migration ledger
--
-- Raw SQL rather than Alembic on purpose: the schema IS the story of this
-- system, and numbered files a reviewer can read top to bottom beat generated
-- revision chains. pgvector's HNSW DDL is also awkward through Alembic.

CREATE EXTENSION IF NOT EXISTS vector;      -- embeddings, 384-dim
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- trigram search on document text

-- Applied-version ledger. `migrate.py` reads this to decide what to run, so it
-- must be the first thing that exists.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text        PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    checksum    text        NOT NULL
);

COMMENT ON TABLE schema_migrations IS
    'One row per applied migration file. checksum is sha256 of the file bytes, '
    'so an edited-after-apply migration is detected rather than silently skipped.';

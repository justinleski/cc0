-- Runs once, when the data volume is first created.
-- Alembic will own tables and roles. This only turns on the extension the image ships.
CREATE EXTENSION IF NOT EXISTS vector;

-- Hand-augmented migration (Tech Plan §3, D5): the pgvector extension MUST
-- exist before any later migration adds a vector(1536) column (IN-2).
-- Requires pgvector to be installed on the server (compose uses
-- pgvector/pgvector:pg16).
CREATE EXTENSION IF NOT EXISTS vector;

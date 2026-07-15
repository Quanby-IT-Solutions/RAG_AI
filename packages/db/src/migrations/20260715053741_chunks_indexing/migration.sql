CREATE TABLE "chunks" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	"document_id" uuid NOT NULL,
	"content" text NOT NULL,
	"embedding" vector(1536) NOT NULL,
	"tsv" tsvector GENERATED ALWAYS AS (to_tsvector('english', "content")) STORED,
	"metadata" jsonb,
	"content_hash" text NOT NULL UNIQUE,
	"chunk_index" integer NOT NULL,
	"embedding_model" text NOT NULL
);
--> statement-breakpoint
ALTER TABLE "chunks" ADD CONSTRAINT "chunks_document_id_documents_id_fkey" FOREIGN KEY ("document_id") REFERENCES "documents"("id") ON DELETE CASCADE;
--> statement-breakpoint
-- Hand-augmented (Tech Plan §3 — drizzle-kit cannot emit these index types).
-- Ordered AFTER 20260714100652_enable_pgvector_extension, so the `vector`
-- type and HNSW access method already exist (pgvector/pgvector:pg16).
-- Standard vector(1536) HNSW index for cosine retrieval (RT-1); m /
-- ef_construction per Tech Plan §VII defaults.
CREATE INDEX "chunks_embedding_hnsw_idx" ON "chunks" USING hnsw ("embedding" vector_cosine_ops) WITH (m = 16, ef_construction = 64);
--> statement-breakpoint
-- GIN index over the generated tsvector for keyword retrieval (RT-2).
CREATE INDEX "chunks_tsv_gin_idx" ON "chunks" USING gin ("tsv");
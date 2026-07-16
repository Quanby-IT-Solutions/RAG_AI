CREATE TABLE "context_cache" (
	"chunk_hash" text,
	"model" text,
	"prompt_version" text,
	"context" text NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "context_cache_pkey" PRIMARY KEY("chunk_hash","model","prompt_version")
);
--> statement-breakpoint
ALTER TABLE "chunks" ADD COLUMN "context_text" text;--> statement-breakpoint
ALTER TABLE "chunks" DROP COLUMN "tsv";--> statement-breakpoint
ALTER TABLE "chunks" ADD COLUMN "tsv" tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce("context_text" || ' ', '') || "content")) STORED;--> statement-breakpoint
-- Hand-augmented (R-DDL precedent): dropping the generated tsv column above
-- also dropped its GIN index — recreate it for the keyword arm (RT-2).
CREATE INDEX "chunks_tsv_gin_idx" ON "chunks" USING gin ("tsv");
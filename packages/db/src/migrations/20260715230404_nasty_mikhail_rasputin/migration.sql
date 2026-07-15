CREATE TABLE "reembed_migrations" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	"target_model" text NOT NULL,
	"status" text DEFAULT 'shadowing' NOT NULL,
	"metrics" jsonb,
	"error" text,
	"triggered_by" text NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
ALTER TABLE "chunks" ADD COLUMN "embedding_shadow" vector(1536);--> statement-breakpoint
ALTER TABLE "chunks" ADD COLUMN "shadow_embedding_model" text;--> statement-breakpoint
CREATE INDEX "reembed_migrations_created_at_idx" ON "reembed_migrations" ("created_at");--> statement-breakpoint
-- IN-8: shadow HNSW index so the EV-2 verification and post-cutover serving
-- use an indexed scan (hand-augmented: drizzle cannot emit HNSW DDL).
CREATE INDEX "chunks_embedding_shadow_hnsw_idx" ON "chunks"
  USING hnsw ("embedding_shadow" vector_cosine_ops);

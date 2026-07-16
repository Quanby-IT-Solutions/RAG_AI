ALTER TABLE "reembed_migrations" ADD COLUMN "context_enrichment" boolean DEFAULT false NOT NULL;--> statement-breakpoint
ALTER TABLE "reembed_migrations" ADD COLUMN "context_prompt_version" text;
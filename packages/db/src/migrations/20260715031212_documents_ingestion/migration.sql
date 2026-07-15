-- Hand-augmented: gen_random_uuid() ships in core Postgres 13+, but install
-- pgcrypto defensively so the default works on any supported image.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
--> statement-breakpoint
CREATE TABLE "documents" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	"source_uri" text NOT NULL,
	"doc_type" text NOT NULL,
	"content_hash" text NOT NULL UNIQUE,
	"status" text DEFAULT 'registered' NOT NULL,
	"metadata" jsonb,
	"error" text,
	"ingested_at" timestamp with time zone DEFAULT now() NOT NULL
);

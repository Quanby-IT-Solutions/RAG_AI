CREATE TABLE "graph_extraction_cache" (
	"chunk_hash" text,
	"model" text,
	"prompt_version" text,
	"extraction" jsonb NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "graph_extraction_cache_pkey" PRIMARY KEY("chunk_hash","model","prompt_version")
);

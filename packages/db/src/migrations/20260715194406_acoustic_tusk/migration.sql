CREATE TABLE "embedding_cache" (
	"content_hash" text,
	"model" text,
	"embedding" vector(1536) NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "embedding_cache_pkey" PRIMARY KEY("content_hash","model")
);

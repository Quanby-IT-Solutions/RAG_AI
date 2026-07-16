CREATE TABLE "caption_cache" (
	"image_hash" text,
	"model" text,
	"prompt_version" text,
	"caption" text NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "caption_cache_pkey" PRIMARY KEY("image_hash","model","prompt_version")
);
--> statement-breakpoint
CREATE TABLE "page_images" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	"document_id" uuid NOT NULL,
	"page" integer NOT NULL,
	"content_hash" text NOT NULL,
	"mime_type" text DEFAULT 'image/png' NOT NULL,
	"image" bytea NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "page_images_document_page_uq" UNIQUE("document_id","page")
);
--> statement-breakpoint
ALTER TABLE "page_images" ADD CONSTRAINT "page_images_document_id_documents_id_fkey" FOREIGN KEY ("document_id") REFERENCES "documents"("id") ON DELETE CASCADE;
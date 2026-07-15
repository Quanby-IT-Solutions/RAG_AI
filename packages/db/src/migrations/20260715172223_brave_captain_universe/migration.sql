CREATE TABLE "golden_items" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	"question" text NOT NULL,
	"ideal_answer" text NOT NULL,
	"expected_chunk_hashes" jsonb NOT NULL,
	"tags" jsonb DEFAULT '[]' NOT NULL,
	"status" text DEFAULT 'active' NOT NULL,
	"created_by" text NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE INDEX "golden_items_status_idx" ON "golden_items" ("status");--> statement-breakpoint
ALTER TABLE "golden_items" ADD CONSTRAINT "golden_items_created_by_users_id_fkey" FOREIGN KEY ("created_by") REFERENCES "users"("id") ON DELETE RESTRICT;
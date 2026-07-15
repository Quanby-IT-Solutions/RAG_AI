CREATE TABLE "api_keys" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	"name" text NOT NULL,
	"key_hash" text NOT NULL UNIQUE,
	"key_prefix" text NOT NULL,
	"owner_user_id" text NOT NULL,
	"scopes" jsonb NOT NULL,
	"last_used_at" timestamp with time zone,
	"revoked_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE INDEX "api_keys_owner_idx" ON "api_keys" ("owner_user_id");--> statement-breakpoint
ALTER TABLE "api_keys" ADD CONSTRAINT "api_keys_owner_user_id_users_id_fkey" FOREIGN KEY ("owner_user_id") REFERENCES "users"("id") ON DELETE CASCADE;
CREATE TABLE "audit_log" (
	"id" bigserial PRIMARY KEY,
	"actor_id" text,
	"actor_type" text NOT NULL,
	"action" text NOT NULL,
	"resource_type" text NOT NULL,
	"resource_id" text,
	"before" jsonb,
	"after" jsonb,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);

--> statement-breakpoint
-- Hand-augmented (Tech Plan §6): audit_log is append-only. Enforce at the DB
-- so EVERY writer (NestJS now, Python later) is covered: any UPDATE or DELETE
-- raises. INSERTs are unaffected.
CREATE OR REPLACE FUNCTION audit_log_block_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
	RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP;
END;
$$;
--> statement-breakpoint
CREATE TRIGGER audit_log_no_update_delete
BEFORE UPDATE OR DELETE ON "audit_log"
FOR EACH ROW EXECUTE FUNCTION audit_log_block_mutation();

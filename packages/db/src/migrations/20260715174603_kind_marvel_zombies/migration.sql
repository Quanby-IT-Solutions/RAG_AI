CREATE TABLE "eval_runs" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	"kind" text NOT NULL,
	"status" text NOT NULL,
	"git_sha" text NOT NULL,
	"config" jsonb NOT NULL,
	"metrics" jsonb,
	"per_item" jsonb,
	"error" text,
	"triggered_by" text NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"finished_at" timestamp with time zone
);
--> statement-breakpoint
CREATE INDEX "eval_runs_created_at_idx" ON "eval_runs" ("created_at");
CREATE TABLE "eval_baselines" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	"run_id" uuid NOT NULL,
	"promoted_by" text NOT NULL,
	"note" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE INDEX "eval_baselines_created_at_idx" ON "eval_baselines" ("created_at");--> statement-breakpoint
ALTER TABLE "eval_baselines" ADD CONSTRAINT "eval_baselines_run_id_eval_runs_id_fkey" FOREIGN KEY ("run_id") REFERENCES "eval_runs"("id") ON DELETE RESTRICT;
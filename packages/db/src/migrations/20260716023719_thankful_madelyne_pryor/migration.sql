CREATE TABLE "report_files" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	"schedule_id" uuid,
	"report_key" text NOT NULL,
	"format" text NOT NULL,
	"filename" text NOT NULL,
	"content_type" text NOT NULL,
	"content_base64" text NOT NULL,
	"generated_by" text NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "report_schedules" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	"report_key" text NOT NULL,
	"cron" text NOT NULL,
	"filters" jsonb DEFAULT '{}' NOT NULL,
	"formats" jsonb NOT NULL,
	"recipients" jsonb DEFAULT '[]' NOT NULL,
	"enabled" boolean DEFAULT true NOT NULL,
	"created_by" text NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	"last_run_at" timestamp with time zone
);
--> statement-breakpoint
CREATE INDEX "report_files_created_at_idx" ON "report_files" ("created_at");--> statement-breakpoint
CREATE INDEX "report_schedules_enabled_idx" ON "report_schedules" ("enabled");--> statement-breakpoint
ALTER TABLE "report_files" ADD CONSTRAINT "report_files_schedule_id_report_schedules_id_fkey" FOREIGN KEY ("schedule_id") REFERENCES "report_schedules"("id") ON DELETE SET NULL;
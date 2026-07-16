Perform a full implementation pass and make ONE ticket PASS. Read the ticket from D:\PROJECTS\BidOps\docs\TICKETS.md.
TICKET TO IMPLEMENT: `SCORE-2` — Win-rate analytics

Your job is not just to audit the ticket, but to make the codebase comply: implement the missing work, fix defects, validate, and document what was done. Work only on this one ticket.
WORKFLOW

Read the ticket from docs\TICKETS.md — find the section for the ticket ID above. Extract: the user story, its enforced permission key, all acceptance criteria (Given/When/Then), in-scope vs out-of-scope, technical notes, the named risky-path Flow if any, and its Dependencies.
Check dependencies first. The ticket's "Dependencies" line lists tickets that must already exist. If any dependency isn't implemented, stop and tell me — don't build on missing foundation.
Summary: restate required behavior, affected routes/components/modules, schema/tables, tests.
Current state: what exists, what's missing, what's incorrect, whether it truly matches the acceptance criteria.
Implement: root-cause changes (not patches), following existing conventions/architecture. Reuse the shared spine — never re-implement it: writeAudit() (F1), the permission middleware (F2), the Drive helper drive.ts — ensureFolder/ensureFolderPath/moveFolder/sha256 (F3), and notify() (ALERT-1). Respect the DOMAIN-MODEL ownership rules noted in the ticket.
Validate immediately: relevant unit/integration tests; narrow typecheck/lint/test for the touched area; browser validation for UI (exercise the feature; console + network tab free of errors, warnings, failed requests); server logs clean for BE; accessibility — keyboard nav, focus, labelled inputs, visible error messages, no horizontal overflow on mobile, screen-reader-safe semantics, destructive-action confirmations.
Prove EVERY acceptance criterion explicitly, including the permission branch where the ticket has one: a user WITH the permission succeeds; a user WITHOUT it gets 403 and the denial is audited — verify both. Confirm any required audit rows actually land in PostgreSQL.
Verify the risky-path Flow ordering and failure branch if the ticket names one (e.g. Flow 1 decision→Drive sync: DB write → ensureFolderPath → moveFolder → writeAudit, never roll back DB on Drive failure; Flow 2 upload→dedup→read-once; Flow 3 RBAC). Verify idempotency wherever the ticket calls for it.

RULES

"Investigated" is NOT "done." Complete = implementation + validation + verification all finished, with a clean console/logs and every criterion proven.
Treat ANY console error/warning, failed request, unhandled rejection, or server-log error from the touched area as a defect: root-cause it, fix it, re-check until clean.
If validation fails, keep fixing THIS ticket until it passes or you're genuinely blocked.
Don't stop at identifying issues — fix them unless truly blocked. If blocked, document what's blocked, why, what's missing, and the exact next action to unblock.
For foundation tickets (F1–F3), show me your plan (files to add/change) before you commit, since these establish patterns everything else reuses. Do not commit until I review.

OUTPUT

Status: PASS / PARTIAL / BLOCKED / FAIL.
Evidence: files changed; tests/commands run; browser + console + a11y state; permission allow + deny(403+audit) both verified; audit rows confirmed in PostgreSQL where required; exactly why it passes or doesn't.
Any remaining gaps or blockers with the next action.
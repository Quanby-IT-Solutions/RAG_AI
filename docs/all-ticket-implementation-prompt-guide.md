Implement the tickets from the file at: D:\PROJECTS\FMMS\docs\TICKETS.md
Read the whole file first, then determine the correct implementation order yourself from each ticket's Dependencies line and the Build order section (if present) — build the complete dependency sequence and follow it. Implement every ticket, one at a time, working locally only. Do not push to GitHub, do not create branches, do not open pull requests. All work stays local.
FIRST — plan the sequence:

Read all tickets and their Dependencies.
Produce the ordered build sequence (a ticket may only start once every ticket in its Dependencies line is already implemented). If the file has a "Build order" section, treat it as authoritative — reconcile your computed order against it and flag any discrepancy.
Show me the sequence you'll follow before you start building.

Per-ticket loop, in that order:

Read the ticket. Extract everything it specifies: user story, any enforced permission/role, all acceptance criteria (Given/When/Then), in-scope vs out-of-scope, technical notes, any named critical flow, and Dependencies. Confirm all dependencies are already implemented before building — if not, STOP and tell me.
Implement with root-cause changes (not patches), following the existing conventions and architecture of the codebase. Reuse shared/common code rather than re-implementing it — if the ticket or the codebase already has shared helpers, middleware, services, or utilities that this ticket needs, use them; don't duplicate. Respect any module/schema ownership rules the tickets define (i.e. don't have one ticket rewrite another's data or columns).
Validate: relevant unit/integration tests; narrow typecheck/lint/test for the touched area; browser validation for UI (exercise it; browser console + network tab clean of errors, warnings, failed requests); server logs clean for backend; accessibility (keyboard nav, focus, labelled inputs, visible error messages, no horizontal overflow on mobile, screen-reader-safe semantics, destructive-action confirmations).
Prove EVERY acceptance criterion explicitly. If the ticket enforces a permission/role, verify both branches: a user WITH it succeeds; a user WITHOUT it is denied as the ticket specifies (and the denial is recorded if required). Verify any critical-flow ordering/failure branch and any idempotency the ticket calls for.
When genuinely PASS (implementation + validation + every criterion proven + clean console/logs), commit locally only:

   git add -A && git commit -m "<ticket-id>: <short summary>"
Do NOT push. Leave the commit local.

Log the result (PASS/PARTIAL/BLOCKED/FAIL + files changed + what was validated), then move to the next ticket in your sequence and repeat from step 1.

RULES:

"Investigated" is NOT "done." A ticket is done only when implemented, validated, every criterion proven, and committed locally.
Treat any console/log error, warning, failed request, or unhandled rejection from the touched area as a defect — root-cause and fix until clean.
If a ticket fails validation, keep fixing THAT ticket until it passes or you're genuinely blocked. Don't move on with a broken ticket.
If blocked, STOP, document what's blocked / why / what's missing / the exact next action, and wait for me.
For any foundational tickets (the ones other tickets depend on), show me your plan and pause for review before committing — they establish patterns everything reuses. After the foundation is confirmed, you may run the loop continuously.
Never push, never open a PR, never create a branch. Local commits only.

OUTPUT: first the computed build sequence; then a running log per ticket (status, changes, validation state, local-commit confirmation); at the end, totals (completed / partial / blocked / failed) + confirmation that shared code was reused, not duplicated.
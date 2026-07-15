# Per-Ticket Implementation Prompt Guide

Use this prompt to run a full implementation pass for one ticket and drive it to PASS.

## Prompt Template

Perform a full implementation pass and make ONE ticket PASS. Read the ticket from [docs/TICKETS.md](docs/TICKETS.md).

TICKET TO IMPLEMENT: `<TICKET_ID>` — `<TICKET_TITLE>`

Your job is not just to audit the ticket, but to make the codebase comply: implement the missing work, fix defects, validate, and document what was done. Work only on this one ticket.

## Workflow

1. Read the ticket from [docs/TICKETS.md](docs/TICKETS.md) and find the section for the ticket ID.
2. Extract and use:
   - User story
   - Enforced permission key
   - All acceptance criteria (Given/When/Then)
   - In-scope and out-of-scope
   - Technical notes
   - Named risky-path Flow, if any
   - Dependencies
3. Check dependencies first.
   - If any dependency ticket is not implemented, stop and report it.
   - Do not build on missing foundation.
4. Provide a summary of:
   - Required behavior
   - Affected routes/components/modules
   - Schema/tables touched
   - Tests required
5. Assess current state:
   - What exists
   - What is missing
   - What is incorrect
   - Whether behavior truly matches each acceptance criterion
6. Implement root-cause fixes (not superficial patches), following existing architecture and conventions.
7. Reuse shared foundations only. Do not re-implement them:
   - writeAudit() (F1)
   - Permission middleware (F2)
   - Drive helper drive.ts: ensureFolder / ensureFolderPath / moveFolder / sha256 (F3)
   - notify() (ALERT-1)
8. Respect DOMAIN-MODEL ownership rules documented in the ticket.
9. Validate immediately after changes:
   - Relevant unit/integration tests
   - Narrow lint/typecheck/test on touched areas
   - Browser validation for UI behavior
   - Console and network tab with zero errors/warnings/failed requests
   - Backend logs clean for touched paths
   - Accessibility checks:
     - Keyboard navigation
     - Focus behavior
     - Labeled inputs
     - Visible error messages
     - No horizontal overflow on mobile
     - Screen-reader-safe semantics
     - Destructive-action confirmations
10. Prove every acceptance criterion explicitly, including permission branch when present:
   - User WITH permission succeeds
   - User WITHOUT permission receives 403
   - Denial is audited
11. Confirm required audit rows are written to PostgreSQL.
12. Verify risky-path Flow order and failure branch if the ticket defines one.
   - Example verification patterns:
     - Flow 1 decision -> Drive sync: DB write -> ensureFolderPath -> moveFolder -> writeAudit, and no DB rollback on Drive failure
     - Flow 2 upload -> dedup -> read-once
     - Flow 3 RBAC
13. Verify idempotency where required by the ticket.

## Rules

1. Investigated is not done.
2. Complete means implementation + validation + verification are all finished.
3. Treat any console warning/error, failed request, unhandled rejection, or server-log error in touched areas as a defect.
4. Root-cause and fix every defect found, then re-check until clean.
5. If validation fails, continue fixing this ticket until PASS or genuine blocker.
6. Do not stop at issue identification; implement fixes unless truly blocked.
7. If blocked, document:
   - What is blocked
   - Why blocked
   - What is missing
   - Exact next action to unblock
8. For foundation tickets F1-F3:
   - Provide file-level implementation plan before commit
   - Do not commit until reviewer approval

## Output Format

- Status: PASS | PARTIAL | BLOCKED | FAIL
- Evidence:
  - Files changed
  - Tests and commands run
  - Browser, console, and accessibility state
  - Permission allow path verified
  - Permission deny path verified (403 + audited)
  - Audit rows confirmed in PostgreSQL where required
  - Explicit reason ticket passes or does not pass
- Remaining gaps or blockers:
  - List each gap
  - Provide exact next action for each

## Usage Example

Perform a full implementation pass and make ONE ticket PASS. Read the ticket from [docs/TICKETS.md](docs/TICKETS.md).

TICKET TO IMPLEMENT: `DOC-2` — Handle amended / supplemental documents

Your job is not just to audit the ticket, but to make the codebase comply: implement the missing work, fix defects, validate, and document what was done. Work only on this one ticket.

Follow all sections in this guide exactly.
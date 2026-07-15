# All-Tickets Implementation Prompt Guide

Use this prompt to implement every ticket from a ticket file in correct dependency order, with strict validation and local-only commits.

## Prompt Template

Implement the tickets from the file at: `<ABSOLUTE_PATH_TO_TICKETS_MD>`

Read the whole file first, then determine the correct implementation order yourself from each ticket's Dependencies line and the Build order section (if present) — build the complete dependency sequence and follow it.

Implement every ticket, one at a time, working locally only.

Do not push to GitHub, do not create branches, do not open pull requests.

All work stays local.

## First Phase: Plan the Sequence

1. Read all tickets and their Dependencies.
2. Produce the ordered build sequence.
   - A ticket may only start once every ticket in its Dependencies line is already implemented.
3. If the file has a Build order section, treat it as authoritative.
   - Reconcile your computed order against Build order.
   - Flag any discrepancy explicitly.
4. Show the sequence before starting implementation.

## Per-Ticket Loop (Follow Sequence Exactly)

1. Read the current ticket and extract:
   - User story
   - Enforced permission or role (if any)
   - All acceptance criteria (Given/When/Then)
   - In-scope and out-of-scope
   - Technical notes
   - Named critical flow (if any)
   - Dependencies
2. Confirm all dependencies are already implemented.
   - If not, STOP and report missing dependency.
3. Implement root-cause fixes (not superficial patches), following codebase conventions and architecture.
4. Reuse shared/common code.
   - Do not duplicate existing helpers/middleware/services/utilities.
5. Respect module/schema ownership rules.
   - Do not have one ticket rewrite another ticket's owned data or columns.
6. Validate thoroughly:
   - Relevant unit and integration tests
   - Narrow lint/typecheck/test for touched area
   - Browser validation for UI paths
   - Browser console and network tab clean: no errors, warnings, failed requests
   - Backend logs clean for touched paths
   - Accessibility checks:
     - Keyboard navigation
     - Focus behavior
     - Labeled inputs
     - Visible error messages
     - No horizontal overflow on mobile
     - Screen-reader-safe semantics
     - Destructive-action confirmations
7. Prove every acceptance criterion explicitly.
8. If permission/role exists, verify both branches:
   - WITH permission/role succeeds
   - WITHOUT permission/role is denied exactly as specified
   - Denial is recorded where required
9. Verify named critical-flow ordering/failure branches and idempotency where required.
10. Only when truly PASS (implementation + validation + criterion proof + clean console/logs), commit locally:

```bash
git add -A && git commit -m "<ticket-id>: <short summary>"
```

11. Do not push. Keep commit local.
12. Log result and move to next ticket.

## Rules

1. Investigated is not done.
2. Done means implemented, validated, every criterion proven, and committed locally.
3. Treat any touched-area console/log error, warning, failed request, or unhandled rejection as a defect.
   - Root-cause and fix until clean.
4. If validation fails, continue fixing the same ticket until PASS or genuine blocker.
   - Do not move on with a broken ticket.
5. If blocked, STOP and report:
   - What is blocked
   - Why blocked
   - What is missing
   - Exact next action to unblock
6. For foundational tickets (tickets depended on by others):
   - Show implementation plan
   - Pause for review before committing
   - After confirmation, continue continuous loop
7. Never push, never open PR, never create branch.
   - Local commits only.

## Required Output Format

1. First output: computed build sequence.
2. Then maintain a running log per ticket:
   - Status: PASS | PARTIAL | BLOCKED | FAIL
   - Files changed
   - Validation executed
   - Local commit confirmation
3. Final output:
   - Totals: completed / partial / blocked / failed
   - Confirmation shared code was reused and not duplicated

## Usage Example

Implement the tickets from the file at: `D:\PROJECTS\FMMS\docs\TICKETS.md`

Read the whole file first, compute dependency-correct build order, reconcile with Build order (if present), show sequence, then execute ticket-by-ticket until all are complete or blocked, with local-only commits.
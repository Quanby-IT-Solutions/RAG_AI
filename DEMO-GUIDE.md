# Prototype Demo Guide: RAG AI Workspace (With Alex Johnson Perspective)

## Pre-Demo Checklist

- Open `http://localhost:3001/login` in two separate browser profiles or windows.
- Log in as Admin User: `admin@turbo-template.local` / `password123`.
- Log in as Alex Johnson: `alex@turbo-template.local` / `password123`.
- Reset the demo data before presenting if you need a clean start: `pnpm --filter @repo/db seed`.
- Pre-load Admin User on `/dashboard`, Alex Johnson on `/dashboard`, and Admin User on `/reports` in a separate tab.
- Keep the Admin User `/user-management` page ready, with the Users tab visible.

## Purpose

This demo tells the story of Admin User giving Alex Johnson the access he needs to do real work. It proves that the workspace connects people, permissions, and audit-ready reporting without paper handoffs or back-channel requests.

## Demo Story Flow

1. Daily starting point for Admin User

> As a System Administrator, this is what I see when I start my day: who I am, what I can reach, and the work already moving through the workspace.

*Open Admin User at `/dashboard`. Point at `Welcome back, Admin`, the `System Administrator` badge, the Quick actions list, and the seeded todo names: `Review onboarding checklist`, `Verify backend health endpoint`, `Connect mobile app to local API`, and `Draft release notes for staging`.*

Narration: One workspace gives the administrator the current state instead of asking someone to chase it down.

2. Find the person who needs help

> As Admin User, I open User Management and find Alex Johnson because his work is blocked until he has the right access.

*Open `/user-management`, click the `Users` tab, and point at the seeded row for `Alex Johnson` with `alex@turbo-template.local`. Point out that Alex starts with the `End User` role.*

Narration: No spreadsheet of accounts, no guessing who has access; the user record is right here.

3. Give Alex the role he needs

> As Admin User, I add `Knowledge Manager` to Alex Johnson so he can work with the shared knowledge library.

*In Alex Johnson's row on `/user-management`, open `Add role`, choose `Knowledge Manager`, and point at the new `Knowledge Manager` badge beside `End User`.*

Narration: A permission change is a normal business action, not a support ticket waiting in a queue.

4. Switch to Alex Johnson's view

> As Alex Johnson, I refresh my dashboard and the workspace changes around my new responsibility.

*Switch to Alex Johnson at `/dashboard` and refresh. Point at the role badges: `End User` and `Knowledge Manager`. Point at the Quick actions list now showing `Corpus` alongside `Submit Ticket`, `Todos / Posts`, and `Chat`.*

Narration: The same person, Alex Johnson, now sees the work he is allowed to do without anyone sending a separate link.

5. Show the shared work list Alex already uses

> As Alex Johnson, I can still see the daily work list, including my seeded item `Connect mobile app to local API`.

*Open Alex Johnson at `/todos`. Point at `Connect mobile app to local API`, `Review onboarding checklist`, `Verify backend health endpoint`, and `Draft release notes for staging`.*

Narration: Access changes do not move people into a separate tool; they expand what they can do in the same workspace.

6. Quantify the benefit for the organization

> As Admin User, I can prove exactly what changed and who changed it.

*Open Admin User at `/reports`. Keep `System Audit Trail` selected, type `users.roles.assign` into `Action (prefix)`, click `Apply filters`, and point at the row with actor `seed-user-admin`, action `users.roles.assign`, resource `user`, and resource id `seed-user-alex`.*

Narration: The organization gets a record of the access change without a separate audit spreadsheet.

### The "Aha!" Moment

7. The role change becomes Alex's new workspace

> As Alex Johnson, I refresh once and my dashboard immediately reflects the role Admin User just assigned.

*Put Admin User's `/user-management` tab and Alex Johnson's `/dashboard` tab side by side. Point from Alex Johnson's `Knowledge Manager` badge to the new `Corpus` quick action.*

Narration: The magic is the cross-role handoff: one person's admin action becomes another person's usable workspace.

### Wrap-Up

Admin User controls access, Alex Johnson gets the right tools, and the organization keeps a visible audit trail of the change.

## Tips for Presenting

- Use the seeded names exactly: Admin User, Alex Johnson, `End User`, and `Knowledge Manager`.
- Narrate the before and after: Alex starts without `Corpus`, then gains it after Admin User assigns the role.
- Keep it under 5 minutes by skipping code, API details, and implementation terms.
- Pre-load the Admin User and Alex Johnson sessions before going on stage; use separate browser profiles so both users can stay signed in.
- If live login or network behavior misbehaves, fall back to the Admin User `/reports` tab filtered to `users.roles.assign` and explain that the audit row is the durable proof of the handoff.

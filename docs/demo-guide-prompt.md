Create a Prototype Demo Guide (Markdown, DEMO-GUIDE.md) for the system in this repository, telling one continuous story of how real users experience it. Local only — no push/branch/PR.
FIRST — learn the real system (don't invent screens):

Explore this repo's app (start the dev server if needed) using the seeded demo data and the seeded role logins. Inventory the roles and the pages each role actually uses.
Identify the system's core value loop — the flow where one role's action becomes another role's benefit (e.g. one role creates/approves a record → another role acts on it → a third sees the result in analytics/reports). The demo story must ride this loop.
Note the 1–2 "wow" screens (live view, real-time update, instant report) — these anchor the "Aha!" moment.

THEN — write the guide in exactly this structure:

# Prototype Demo Guide: <System Name> (With <Role B> Perspective)
## Purpose — 2–3 sentences: whose story we tell, what benefit we prove.
## Demo Story Flow — 6–9 numbered scenes. EVERY scene has three layers:

A > blockquote of the presenter's spoken script, first-person as the current role ("As a <role>, this is what I see when…").
An italic stage direction — the exact page to open, what to click, what to point at, using the REAL seeded data by name (real record names, real seeded users — never invented placeholders when the seed has real names).
A Narration: line — the everyday-life value ("No more paper records in a drawer somewhere.").


Story arc requirements: open in the primary role's daily view (their dashboard/profile moment) → show a create/act flow → switch perspective to a second role and show the SAME record from their side → show the update reflecting back instantly across roles → a scene quantifying the benefit for the organization (reports/analytics/audit) → ### The "Aha!" Moment — one short scene naming where the magic clicks (the cross-role sync) → ### Wrap-Up — one sentence of shared benefit.
## Tips for Presenting — 4–6 bullets: use the seeded realistic data; narrate value not clicks; keep it under 5 minutes; highlight before/after; what to pre-load before going on stage (logged-in sessions per role, browser tabs, mobile device if the system has a mobile role); one fallback if live data/network misbehaves.

RULES: every scene must reference a screen that actually exists in this repo and data that is actually seeded — verify by opening each one; if a scene's page is empty or broken, flag it at the top as "PREP NEEDED" instead of writing fiction; first-person present tense throughout; plain language, zero jargon (the audience is the client, not developers); the whole walkthrough must be performable in under 5 minutes.
OUTPUT: DEMO-GUIDE.md at the repo root, plus a "Pre-Demo Checklist" section at the top of the file listing exactly what to open and which accounts to log in before presenting.
# Deployment-scale epic (DP-1..DP-4) — blocker record

Status date: 2026-07-16. Recorded during the all-tickets implementation run
after RP-6; every prior ticket (F1..RP-6) is implemented and committed.

## Why this epic is blocked

All four tickets require a **production container host** (PRD Annex A#9 left
the host undecided; the tickets target a Render/Fly-compatible contract).
No host account, domain, TLS certificate authority, or host secret store is
available in this environment, so the acceptance criteria below cannot be
executed or proven — and faking them would violate the tickets' own
definition of done.

## Per-ticket status

### DP-1 — Multi-replica production deployment (BLOCKED)

Blocked on: host account (Render/Fly or equivalent), load balancer, TLS
termination + scan, host secret store, at-rest-encryption review of managed
Postgres/Redis.

Already in place (host-agnostic container contract the ticket asks for):

- Stateless API replicas by construction: sessions in Postgres (Better
  Auth), cache/locks/rate-limits/single-flight in Redis, no local state.
- `docker-compose.yml` builds production images for web / backend / rag /
  rag-worker; the arq worker already runs as a separate process from the API
  (DP-1 technical note).
- `/health` liveness/readiness (F1, OB-3) is LB-ready; OB-4 implements
  SIGTERM graceful drain (readiness flips, in-flight requests finish) — the
  replica-kill test needs only a real LB in front.
- Secrets: every service reads configuration from the environment
  (pydantic-settings / zod env validation, fail-fast); SecretStr masking
  (OB-5). Bulk import into a host secret store is a host-side task.
- EV-4 eval gate exists as a CI workflow (.github/workflows/eval-gate.yml)
  ready to wire as the deploy precondition (Flow F).

### DP-2 — Autoscaling on CPU/latency (BLOCKED)

Blocked on: DP-1 (host autoscaler is a host feature). Signal separation is
already implemented: OB-4 exports `rag_db_connections` /
`rag_db_longest_query_seconds` so DB pressure alerts route to DB scaling,
not replica scaling; `rag_inflight_requests` + latency histograms (OB-2)
are the scale-out inputs.

### DP-3 — Load test 50 users / 20 QPS (BLOCKED)

Blocked on: DP-2 (autoscaling must be verified during the run) and a
production-shaped environment — a laptop-local run cannot honestly produce
the pass/fail numbers that gate "production designation". The runtime
levers the test exercises are all implemented and unit/e2e-proven:
CP-1 cache (hit-rate metric), CP-3 single-flight, CP-4 provider budget,
OB-4 load shedding, GN-3 streaming TTFT metric (`rag_chat_ttft_seconds`).
The RP-4 report already encodes the acceptance thresholds (TTFT p95 <= 2s,
p95 <= 15s, errors < 1%, hit-rate >= 30%) as NFR overlays.

### DP-4 — API docs, runbook, knowledge transfer (BLOCKED by dependency)

Depends on DP-3 ("final numbers referenced in docs") and a human-delivered
KT session with recorded attendance. Interim coverage that already exists:

- OpenAPI served live from the oRPC contracts (single source of truth) at
  `/api/v1/docs`; contracts are typed and versioned in packages/contracts.
- docs/RUNNING.md (local run), ops/ (Prometheus alerts, Grafana dashboards
  as code), per-feature behavior documented in docs/TICKETS.md and the
  audit/permission catalog in packages/contracts rbac.catalog.ts.

## Unblock checklist

1. Choose host (Annex A#9), create account + project, import secrets.
2. Deploy >= 2 API replicas + 1 worker from the existing images; point the
   host LB at `/health`; enable TLS 1.2+ and scan.
3. Configure autoscaler min/max on CPU/latency; verify scale-in drain.
4. Write and run the k6/locust scenario at 50 users / 20 QPS; store the
   versioned report; finalize OB-3 alert thresholds from the results.
5. Assemble DP-4 docs with the real numbers; deliver KT; record attendance.

# RAG_PROFILE evaluation matrix (CE-9)

Dev-environment reference numbers (2026-07-16, local single-instance stack —
label them as such; DP-3 production numbers do not exist yet). Produced by
`uv run python -m app.profile_matrix` (apps/rag): one stored EV-2/EV-3
generation eval per profile against the live corpus (196 chunks, 15 active
golden items incl. 3 multi-hop + 1 visual). Every leg's run config carries
its profile identity, so each profile's EV-4 gate baseline can be promoted
from its own leg (`eval.gate.approve`).

| Profile | Hit rate | MRR | Faithfulness | Relevancy | Retrieval p50 | Retrieval p95 | Eval tokens (in/out) | Run |
|---|---|---|---|---|---|---|---|---|
| baseline | 1.000 | 0.911 | 0.911 | 1.000 | 2698 ms | 3484 ms | 25630/1253 | `5a6c092e` |
| contextual | 1.000 | 0.911 | 0.911 | 1.000 | 2832 ms | 2933 ms | 25636/1212 | `af293488` |
| agentic | 1.000 | 0.911 | 0.933 | 1.000 | 2658 ms | 2961 ms | 25662/1248 | `7f4f91f7` |
| full | 1.000 | 0.911 | 0.933 | 1.000 | 2766 ms | 2953 ms | 25824/1206 | `345f066c` |

## Reading this honestly

- **The corpus is at ceiling.** 15 well-aligned questions over ~200 chunks
  retrieve perfectly under every profile, so this matrix demonstrates **no
  regression between profiles**, not the headline lifts (contextual's
  ~50%-class failed-retrieval reduction, graph's multi-hop wins) — those
  appear on corpora large/ambiguous enough for flat retrieval to fail.
  Faithfulness jitter (±0.02) is LLM-judge noise, not signal.
- **What the harness measures vs what the chat pipeline adds.** The harness
  exercises retrieval + the shared grounded generation path. The agentic
  loop (grade → rewrite → groundedness gate) and the vision answer path run
  in the CHAT pipeline only; their live-measured effects — agentic
  +3.4–7.5 s per uncached question, vision reading page labels invisible to
  text extraction — are in docs/CONTEXTUAL-RETRIEVAL-RESULTS.md.
- **Retrieval latency** here includes per-call DB connection setup and the
  query-embedding API round trip (~2.5 s of the p50); it is a
  profile-comparison number, not a serving-latency claim (the RP-4 report
  tracks those from Prometheus).

## Per-profile cost/infra summary (template consumer view)

| Profile | Extra infra | Index cost | Query cost | Notes |
|---|---|---|---|---|
| baseline | none | — | — | the pre-Phase-7 template exactly |
| contextual | none | +1 cheap LLM call/chunk at ingest (cached; $0.012 per 175 chunks measured) | none | enabling on an existing corpus needs the CE-2 shadow re-index (RUNNING.md runbook) |
| agentic | none | as contextual | +2 cheap LLM calls happy-path, up to +5 with rewrites/regeneration (+3.4–7.5 s measured) | falls through to linear on any loop failure |
| full | Neo4j (`docker compose --profile graph up`) | + entity extraction/chunk + vision caption/page (both cached; ~5× on vision-worthy pages) | + graph arm (Cypher, ms-scale) + vision call when charts are in context | graph/vision degrade to hybrid/captions when unavailable |

# TICKETS — Phase 7 Cutting-Edge Stack (RAG_PROFILE template variants)

Companion to TICKETS.md (same ticket schema, same definition-of-done standard). Prerequisite: all F1..RP-6 tickets complete (they are). DP-1..DP-4 remain a parallel, independent track.

**Design decision (locked):** one codebase, no per-feature branches. Every Phase 7 capability ships behind a feature flag that defaults to **off**; `RAG_PROFILE` presets expand to flag sets so a template consumer changes exactly one line of `.env` to pick a variant. Flag off = byte-identical behavior to today, proven by the existing suites passing untouched.

## Profile → flag expansion (the template contract)

| `RAG_PROFILE` | `CONTEXTUAL_CHUNKS_ENABLED` | `RAG_MODE` | `GRAPH_RAG_ENABLED` | `MULTIMODAL_ENABLED` |
|---|---|---|---|---|
| `baseline` (default) | false | `linear` | false | false |
| `contextual` | true | `linear` | false | false |
| `agentic` | true | `agentic` | false | false |
| `full` | true | `agentic` | true | true |

Rules:
- Individual flags may still be set explicitly; an explicit flag **overrides** the profile expansion (power-user escape hatch). Profile only fills unset flags.
- Every flag participates in the CP-1 cache config fingerprint — flipping any flag/profile invalidates the response cache automatically, never serves stale-config answers.
- Every flag appears in `GET /internal/config` (OB-5) and the `/ops-config` console, masked rules unchanged.
- `agentic` and `full` imply contextual because the roadmap thesis is compositional (hybrid + contextual + agentic + multimodal); a consumer wanting agentic-without-contextual sets `RAG_MODE=agentic CONTEXTUAL_CHUNKS_ENABLED=false` explicitly.

---

## Risky-path flows (referenced by ticket technical notes)

### Flow G — Contextual enrichment re-index (cache + shadow + cutover)
```
CONTEXTUAL_CHUNKS_ENABLED flips on (or context prompt version bumps)
  → chunking output unchanged; for each chunk, build enrichment input {doc summary window, chunk text}
  → context_cache lookup (doc_content_hash, chunk_content_hash, context_model, prompt_version)
        HIT  → reuse generated context, zero LLM cost
        MISS → batched cheap-model calls; write cache rows
  → index_text = context + "\n\n" + chunk_text   [embedding AND tsvector both index index_text]
  → display/citation text remains the ORIGINAL chunk_text (context never shown as corpus content)
  → re-index runs through the IN-8 shadow path: shadow embeddings built with enriched text,
    golden-set metrics on shadow ≥ baseline, atomic cutover, response-cache generation bump (CP-2)
FAILURE: context generation fails for a chunk → fall back to raw chunk_text for that chunk,
  count it (metric), never block the pipeline; job re-run regenerates only cache misses.
```
Risky because: three invariants must hold at once — enriched text feeds *both* retrieval indexes, raw text stays the citation surface, and the IN-8 pinned-model invariant (index config == query config) now includes the enrichment config.

### Flow H — Agentic self-correcting loop (bounded, streaming-compatible)
```
RAG_MODE=agentic, POST /chat passes SC middleware + cache (Flow B stages unchanged, order preserved)
  → LangGraph loop replaces the linear retrieve→generate segment ONLY:
     retrieve → grade relevance (cheap model, structured verdict)
        sufficient → generate
        insufficient → rewrite query → retrieve again   [max 2 rewrites, hard-capped]
     generate → groundedness check
        grounded → final flush (validated, cited)
        ungrounded → ONE regenerate with stricter grounding prompt → still ungrounded → B2 "I don't know"
  → every loop step emits an SSE status event (retrieving / grading / rewriting / regenerating)
    BEFORE token streaming starts, so TTFT-perceived is a status event, not silence
  → trace records each hop (query versions, verdicts, hop count); metrics: hop histogram, rewrite rate
FAILURE: any grading/rewrite call errors or times out → fall through to the linear path result
  for the current retrieval set (degrade to baseline behavior, never to an error).
```
Risky because: an unbounded loop is a cost/latency bomb; the cap, the fall-through-to-linear rule, and cache/security ordering (loop sits strictly inside Flow B's cache-miss branch) are the contract.

### Flow I — Graph dual-store consistency
```
Ingestion job with GRAPH_RAG_ENABLED
  → after chunks upsert (Flow A unchanged), entity/relationship extraction per chunk
    (cheap model, structured output) → upsert into Neo4j keyed on (document_id, chunk content_hash)
  → document delete / re-ingest → graph nodes/edges for stale chunk hashes removed in the same job
  → /chat retrieval: graph query runs PARALLEL to vector+FTS; results enter the SAME RRF fusion
    as a third ranked list; graph unavailable → hybrid continues, flag a degraded-trace marker
FAILURE: Neo4j down at ingest → job records graph-extraction failed for the document,
  document itself still succeeds (Postgres is source of truth); rerun repairs graph only.
```
Risky because: two stores must stay consistent under re-ingestion/deletion, and the retrieval path must treat the graph as an optional accelerator, never a single point of failure.

---

## Epic map

| Epic key | Label | Roadmap source | Note |
|---|---|---|---|
| `template-profiles` | Profile & flag spine | Phase 7 preamble | `RAG_PROFILE` presets; every later ticket hangs a flag on this spine. |
| `contextual-retrieval` | Contextual retrieval | §7.1 | Best ROI; index-time enrichment; pairs with existing hybrid + IN-8. |
| `agentic-rag` | Agentic / self-correcting RAG | §7.2 | LangGraph loop behind `RAG_MODE`; runtime-only, no re-index. |
| `graph-rag` | Graph RAG | §7.3 | Neo4j as optional compose profile; heaviest, ships last. |
| `multimodal-rag` | Multimodal RAG | §7.4 | Vision loader route + vision answer path; ~5× cost, documented. |

## Build order

**Wave 1 — spine + best ROI:**
1. **CE-0** — `RAG_PROFILE` presets + feature-flag spine (config, fingerprint, ops visibility)
2. **CE-1** — Contextual chunk enrichment at index time + context cache
3. **CE-2** — Contextual rollout: shadow re-index + eval lift verification

**Wave 2 — agentic:**
4. **CE-3** — LangGraph loop: relevance grading + bounded query-rewrite retry
5. **CE-4** — Groundedness gate + regenerate + SSE loop-status events

**Wave 3 — heavy variants (CE-5/6 and CE-7/8 are independent tracks, parallelizable):**
6. **CE-5** — Neo4j compose profile + entity/relationship extraction at ingest
7. **CE-6** — Graph retrieval fused into hybrid RRF + eval
8. **CE-7** — Multimodal ingestion: page-image path for vision-worthy PDFs
9. **CE-8** — Vision answer path + page-image citations

**Wave 4 — template assembly:**
10. **CE-9** — Profile eval matrix + template documentation + cost/latency tradeoff table

---

## Tickets

### Epic: `template-profiles`

---

**Title:** [CE-0] `RAG_PROFILE` presets + feature-flag spine
**Epic:** `template-profiles`
**Problem / context:** Phase 7 ships four optional capabilities as template variants of ONE codebase. Consumers must select a variant by changing one `.env` line; maintainers must never fork. Nothing exists yet to hang the four flags on.
**User story:** As a template consumer, I want to set `RAG_PROFILE=contextual` (or `agentic`, `full`) and get that variant's behavior so that I never touch more than one configuration line to pick my architecture.
**Acceptance criteria:**
- *Given* no profile/flag env vars, *when* the rag service boots, *then* it runs `baseline` (all four flags off / `RAG_MODE=linear`) and the entire existing test suite passes with zero modifications.
- *Given* `RAG_PROFILE=<preset>`, *when* the service boots, *then* the four flags resolve per the expansion table, and an explicitly set individual flag overrides the preset value.
- *Given* an invalid `RAG_PROFILE` or `RAG_MODE` value, *when* the service boots, *then* it fails fast with a clear error (pydantic-settings validation, F1 rule).
- *Given* any flag or profile change, *when* the next /chat runs, *then* the CP-1 cache key config fingerprint differs (integration test: flip flag → prior cached entry misses).
- *Given* `GET /internal/config` and the `/ops-config` console, *when* viewed, *then* resolved profile + all four flags are visible (OB-5 pattern, no secrets involved).
**In scope:** Settings model (profile enum, four flags, resolution/override logic), config-fingerprint inclusion, `/internal/config` + backend `/ops-config` surfacing, `.env.example` documentation of the table, boot log line stating resolved profile.
**Out of scope:** Any actual feature behavior (CE-1..CE-8); per-corpus or per-request overrides (later, only if template feedback demands it).
**Technical notes:** Flags live in the rag service's pydantic-settings alongside `CACHE_ENABLED`/`PROVIDER_BUCKET_ENABLED` precedent. Resolution order: explicit env flag > profile expansion > default. `RAG_MODE` is an enum (`linear|agentic`), not a boolean, to leave room for future modes. Conftest pins `RAG_PROFILE=baseline` the same way it pins `CACHE_ENABLED=false`.
**Definition of done:** as standard.
**Dependencies:** none (spine ticket).

---

### Epic: `contextual-retrieval`

---

**Title:** [CE-1] Contextual chunk enrichment at index time + context cache
**Epic:** `contextual-retrieval`
**Problem / context:** §7.1: prepending a short LLM-generated description of how each chunk fits its parent document substantially reduces failed retrievals, especially with hybrid search. Enrichment must be cheap (cached), fail-soft, and feed BOTH retrieval indexes while leaving citation text untouched. **Flow G.**
**User story:** As a user with `chat.query.submit` permission, I want chunks indexed with their document context so that questions phrased differently from the chunk's literal wording still retrieve the right passage.
**Acceptance criteria:**
- *Given* `CONTEXTUAL_CHUNKS_ENABLED=true` and an ingestion job, *when* a chunk is indexed, *then* its embedding AND tsvector are computed over `context + chunk_text`, while the stored display/citation text remains the original `chunk_text`.
- *Given* an identical (doc hash, chunk hash, context model, prompt version) tuple, *when* re-ingested, *then* the context comes from cache with zero LLM calls (Flow A economics extended to enrichment).
- *Given* a context-generation failure for one chunk, *when* the job runs, *then* that chunk indexes with raw text, a fallback counter increments, and the job still succeeds (fail-soft, Flow G).
- *Given* the flag off, *when* ingesting, *then* the pipeline is byte-identical to today (no enrichment calls, no schema-value changes, existing tests untouched).
- *Given* enrichment calls, *when* they run, *then* they go through the CP-4 provider bucket and are batched, with cost recorded per job (OB-1 cost accounting).
**In scope:** Context-generation step in the indexing pipeline (cheap routed model, batched), `context_cache` storage (content-addressed, mirroring `embedding_cache` design), chunk storage of context (metadata or column — implementer's call, must survive re-index), prompt with version constant, fallback + metrics, unit tests with mocked LLM.
**Out of scope:** Re-indexing the existing corpus and proving eval lift (CE-2); any query-time behavior change.
**Technical notes:** **Flow G.** Context prompt: document summary window + chunk → ≤ ~80-token situating sentence(s). Prompt text carries an explicit `PROMPT_VERSION`; it is part of the cache key and the index-config fingerprint so a prompt edit forces re-enrichment rather than silently mixing generations. Enrichment happens inside the existing one-transaction `index_chunks` boundaries (IN-2) — orphan-free rule unchanged.
**Definition of done:** as standard.
**Dependencies:** CE-0.

---

**Title:** [CE-2] Contextual rollout: shadow re-index + eval lift verification
**Epic:** `contextual-retrieval`
**Problem / context:** Turning enrichment on changes what every embedding means — the same class of change as an embedding-model swap, so it must ride the IN-8 zero-downtime shadow path, and the template's headline claim ("contextual reduces failed retrievals") must be proven with the EV harness, not asserted. **Flow G / Flow D.**
**User story:** As a user with `corpus.reembed.run` permission, I want enabling contextual retrieval to re-index through the shadow path with a measured quality gate so that the live corpus never serves a mid-migration mix and the lift is documented fact.
**Acceptance criteria:**
- *Given* the flag flips on with an existing corpus, *when* the operator triggers the re-embed job, *then* enriched embeddings build in the shadow column (IN-8 machinery), dual-write covers concurrent ingestion, and cutover is atomic.
- *Given* the shadow is built, *when* golden-set retrieval metrics run on it, *then* cutover proceeds only if metrics ≥ baseline (Flow D verify gate); otherwise abort leaves the serving path untouched.
- *Given* cutover completes, *when* it finishes, *then* the response-cache generation bumps (CP-2) and the active index-config fingerprint records enrichment on + prompt version.
- *Given* the EV harness, *when* baseline vs contextual runs complete, *then* both eval_runs are stored and hit rate / MRR / P@k / R@k deltas land in a committed `docs/` comparison note — the template's evidence artifact.
**In scope:** Wiring enrichment config into the IN-8 re-embed state machine's config identity, operator runbook section, EV comparison runs + committed results note, e2e: enable → re-embed → ask → answer cites correctly.
**Out of scope:** Auto-triggering re-index on flag flip (operator-initiated, deliberate); tuning the context prompt for score (separate iteration once numbers exist).
**Technical notes:** IN-8's "pinned model invariant" generalizes to "pinned index-config invariant": (embedding model, enrichment on/off, prompt version) must match between index and query paths at every instant — enrichment is index-side only, so the query path needs no text change, but the fingerprint check must still gate mixed states.
**Definition of done:** as standard.
**Dependencies:** CE-1, IN-8, EV-2.

---

### Epic: `agentic-rag`

---

**Title:** [CE-3] LangGraph loop: relevance grading + bounded query-rewrite retry
**Epic:** `agentic-rag`
**Problem / context:** §7.2: single-shot retrieval sometimes returns garbage; the linear chain answers from it anyway. Replace the retrieve→generate segment with a LangGraph decision loop that grades retrieval and rewrites the query instead of answering from noise — strictly bounded, degrading to linear behavior on any loop failure. **Flow H.**
**User story:** As a user with `chat.query.submit` permission, I want poorly-matched retrievals to trigger an automatic query rewrite and retry so that I get a grounded answer instead of an answer built from irrelevant chunks.
**Acceptance criteria:**
- *Given* `RAG_MODE=agentic` and a query whose retrieval grades insufficient, *when* the loop runs, *then* the query is rewritten (cheap model), retrieval re-runs, and the hop count is capped at 2 rewrites — asserted by a test that forces perpetual insufficiency and counts LLM/retrieval invocations.
- *Given* a retrieval that grades sufficient on the first pass, *when* the loop runs, *then* exactly one retrieval and zero rewrites occur (no added cost on the happy path beyond one grading call).
- *Given* a grading or rewrite call failure/timeout, *when* it occurs, *then* the pipeline falls through to the linear path over the current retrieval set — the user gets a baseline-quality answer, never an error (Flow H failure rule).
- *Given* `RAG_MODE=linear`, *when* /chat runs, *then* behavior and traces are byte-identical to today (existing suite untouched).
- *Given* any agentic run, *when* traced, *then* the trace records each hop's query text, verdict, and timing, and Prometheus gains hop-count + rewrite-rate metrics (OB-2 registry).
**In scope:** LangGraph graph replacing the retrieve→generate segment behind the GenerationService seam (RT-1 DI seam), structured-output grader + rewriter on the cheap routed model, hop cap constant, fall-through-to-linear, tracing + metrics, unit tests with scripted verdicts.
**Out of scope:** Groundedness check + regenerate (CE-4); multi-source routing between tools (not in this template's scope); streaming status events (CE-4).
**Technical notes:** **Flow H.** The loop lives strictly inside Flow B's cache-miss branch — SC middleware, cache lookup, budget, provider bucket, validation ordering all unchanged. Rewrite prompts receive the original query + graded-out chunk titles so rewrites diversify rather than paraphrase. `langgraph` is already a locked core dep (Phase 1.1).
**Definition of done:** as standard.
**Dependencies:** CE-0.

---

**Title:** [CE-4] Groundedness gate + regenerate + SSE loop-status events
**Epic:** `agentic-rag`
**Problem / context:** §7.2 second half: even with good retrieval the model can answer ungrounded; the loop must check groundedness and regenerate once before admitting "I don't know". Loop hops add seconds — the stream must show progress so perceived TTFT survives the NFR conversation. **Flow H.**
**User story:** As a user with `chat.query.submit` permission, I want ungrounded draft answers regenerated (and honest "I don't know" when regeneration fails) with live progress shown so that extra quality never looks like a hung request.
**Acceptance criteria:**
- *Given* a generated draft that fails the groundedness check, *when* the loop continues, *then* exactly one regeneration runs with a stricter grounding prompt; a second failure returns the B2 "I don't know" response (never the ungrounded draft).
- *Given* an agentic run, *when* streaming, *then* SSE status events (`retrieving`, `grading`, `rewriting`, `regenerating`) precede token streaming, and the web chat renders them as progress states (CH-2 degradation-state pattern extended).
- *Given* the groundedness checker itself fails, *when* it occurs, *then* the draft ships as-is with a trace marker (fail-open on the checker, consistent with Flow H's degrade-to-baseline rule) — the checker is an enhancer, not a gate that can take the service down.
- *Given* `RAG_MODE=linear`, *when* streaming, *then* the SSE contract is unchanged (GN-3 clients unaffected; new event types only appear in agentic mode).
- *Given* the EV harness, *when* linear vs agentic runs complete on the golden set, *then* faithfulness/relevancy deltas and latency deltas are stored and committed to the CE-9 comparison note inputs.
**In scope:** Groundedness node (structured verdict over draft + used chunks), one-shot regenerate node, new SSE event types + web chat progress rendering, latency histogram split by mode, EV comparison run, e2e: agentic question streams status then answer.
**Out of scope:** Multi-round regeneration; answer-quality tuning beyond the stricter-prompt constant.
**Technical notes:** **Flow H.** Status events reuse the GN-3 SSE envelope with a new `status` event type — additive, contract-versioned in packages/contracts so backend/web types stay in lockstep. CP-3 single-flight interaction: waiters still receive the completed cached answer; only the leader streams loop statuses.
**Definition of done:** as standard.
**Dependencies:** CE-3, GN-3, CH-2.

---

### Epic: `graph-rag`

---

**Title:** [CE-5] Neo4j compose profile + entity/relationship extraction at ingest
**Epic:** `graph-rag`
**Problem / context:** §7.3: multi-hop questions ("what connects X to Y") defeat flat chunk retrieval. First half of Graph RAG: stand up Neo4j as OPTIONAL infrastructure and populate a knowledge graph at ingest time, keeping Postgres the source of truth and the graph rebuildable from it. **Flow I.**
**User story:** As a user with `corpus.ingest.run` permission, I want ingestion to extract entities and relationships into a knowledge graph so that relationship-shaped queries have a substrate flat chunks can't provide.
**Acceptance criteria:**
- *Given* the `graph` compose profile is not started, *when* the stack runs with `GRAPH_RAG_ENABLED=false`, *then* nothing references Neo4j (no connection attempts, no health-check dependency) — baseline consumers never pay for it.
- *Given* `GRAPH_RAG_ENABLED=true` and an ingestion job, *when* chunks index, *then* per-chunk entity/relationship extraction (cheap model, structured output, CP-4 bucketed, content-hash cached like CE-1) upserts nodes/edges keyed on (document_id, chunk content_hash).
- *Given* a document is re-ingested with changed content or deleted, *when* the job completes, *then* graph elements for stale chunk hashes are gone (Flow I consistency test: ingest → modify → re-ingest → graph reflects only current chunks).
- *Given* Neo4j is down during ingest, *when* the job runs, *then* the document still ingests successfully with a recorded graph-extraction failure, and a rerun repairs the graph only (Flow I failure rule).
- *Given* `GRAPH_RAG_ENABLED=true`, *when* `/health` runs, *then* Neo4j reachability appears as a component check (OB-3 pattern) — but only when the flag is on.
**In scope:** `graph` docker-compose profile (Neo4j + volume, `ops/` profile precedent), Neo4j client in the rag service (env-validated, flag-gated), extraction prompt + structured schema + extraction cache, ingest-pipeline hook after chunk upsert, deletion/re-ingest consistency, metrics (extractions, failures, graph size), unit tests with mocked extractor + fake graph client.
**Out of scope:** Query-time graph retrieval (CE-6); graph visualization UI; ontology/schema curation beyond (Entity)-[RELATES {type}]->(Entity) with provenance properties.
**Technical notes:** **Flow I.** Graph is derived data — rebuildable from Postgres via job rerun; never migrate it, re-derive it. Provenance on every edge: document_id, chunk hash, extraction prompt version. Extraction cache keyed like CE-1's (chunk hash, model, prompt version) — re-ingest of unchanged content costs zero LLM calls.
**Definition of done:** as standard.
**Dependencies:** CE-0. (Independent of CE-1..CE-4; parallelizable with the multimodal track.)

---

**Title:** [CE-6] Graph retrieval fused into hybrid RRF + eval
**Epic:** `graph-rag`
**Problem / context:** §7.3 second half: retrieve over the graph and merge with hybrid results so multi-hop questions surface chunks that vector+FTS ranking alone would miss — with the graph as an accelerator, never a point of failure. **Flow I.**
**User story:** As a user with `chat.query.submit` permission, I want relationship questions answered using graph traversal so that multi-hop connections across documents produce grounded, cited answers.
**Acceptance criteria:**
- *Given* `GRAPH_RAG_ENABLED=true` and a /chat query, *when* retrieval runs, *then* a graph query (entity linking → bounded traversal → provenance chunks) executes in parallel with vector+FTS and its ranked chunk list enters the SAME RRF fusion (RT-2) as a third list with a configurable weight.
- *Given* Neo4j is unreachable at query time, *when* /chat runs, *then* hybrid retrieval proceeds without it, the trace carries a graph-degraded marker, and the user sees a normal answer (Flow I failure rule).
- *Given* the flag off, *when* /chat runs, *then* the retrieval path is byte-identical to today.
- *Given* a golden-set extension of multi-hop questions (added to EV-1 set), *when* EV runs flag-on vs flag-off, *then* multi-hop hit rate improves while single-hop metrics stay ≥ baseline − tolerance, results committed.
- *Given* graph-sourced chunks in the answer, *when* citations render, *then* they cite the provenance chunks like any other source (no special-cased citation shape).
**In scope:** Entity linking on the query (cheap model or graph-side match), bounded traversal (depth cap constant), provenance-chunk resolution back to Postgres chunks, third RRF list + weight env knob, degradation marker + metric, multi-hop golden-set additions, EV comparison, e2e with the graph profile up.
**Out of scope:** Cypher generation from natural language (traversal is templated, not LLM-generated — cost and injection surface); graph-only answering without chunk grounding.
**Technical notes:** **Flow I.** Fusion contract: graph list is ranked by traversal relevance but the ANSWER text always comes from Postgres chunk content — the graph finds chunks, it never becomes the corpus. Retrieval trace logs the three lists separately (retrieved-vs-used split from RT-1 extended).
**Definition of done:** as standard.
**Dependencies:** CE-5, RT-2, EV-2.

---

### Epic: `multimodal-rag`

---

**Title:** [CE-7] Multimodal ingestion: page-image path for vision-worthy PDFs
**Epic:** `multimodal-rag`
**Problem / context:** §7.4: for corpora where meaning lives in charts/tables/figures, text extraction destroys the signal. First half: detect vision-worthy pages at ingest, store page images alongside text chunks, and index them retrievably — at ~5× cost, so only where warranted.
**User story:** As a user with `corpus.ingest.run` permission, I want chart/table-heavy PDF pages preserved as images so that questions about visual content can be answered from what the page actually shows.
**Acceptance criteria:**
- *Given* `MULTIMODAL_ENABLED=true` and a PDF upload, *when* ingestion runs, *then* pages flagged vision-worthy (heuristic: low extractable-text density / embedded raster+vector graphics) render to images stored with (document_id, page, content hash), while normal pages follow the existing text path unchanged.
- *Given* a vision-worthy page, *when* indexed, *then* a cheap-model image caption/description is generated (cached on image hash) and indexed as a retrievable chunk carrying `modality: image` metadata + a pointer to the stored image.
- *Given* the flag off, *when* ingesting any PDF, *then* the pipeline is byte-identical to today.
- *Given* re-ingestion of an unchanged PDF, *when* the job runs, *then* no page re-renders and no captions regenerate (content-hash idempotency, Flow A economics).
- *Given* per-job cost accounting (OB-1), *when* a multimodal ingest completes, *then* vision-path cost is visible separately — the ~5× cost is observable, not folded into totals.
**In scope:** Vision-worthiness heuristic + env-tunable threshold, PDF page → image rendering in the loader layer (IN-3 LoaderSpec route), image storage (documents-adjacent, implementer's call: bytea/object-path column vs disk volume — must survive container restart), caption generation + cache, `modality` metadata on chunks, metrics + cost split, unit tests with fixture PDFs (one text-only, one chart-heavy).
**Out of scope:** Query-time vision answering (CE-8); image embeddings via a dedicated image-embedding model (caption-embedding retrieval is the v1 bridge — revisit only if eval shows recall gaps); non-PDF image formats.
**Technical notes:** Caption chunks flow through the standard IN-2 index path (and CE-1 enrichment when on) — retrieval stays uniform, only the answer stage (CE-8) branches on modality. Metadata filtering (RT §3.6) gets `modality` for free.
**Definition of done:** as standard.
**Dependencies:** CE-0, IN-3. (Independent of the graph track; parallelizable.)

---

**Title:** [CE-8] Vision answer path + page-image citations
**Epic:** `multimodal-rag`
**Problem / context:** §7.4 second half: when retrieval surfaces image-modality chunks, the answer must come from a vision model actually looking at the page image — not from the caption alone — with citations that open the page image.
**User story:** As a user with `chat.query.submit` permission, I want questions about charts and tables answered by a model that sees the page so that numbers and trends come from the figure, not a lossy text rendering of it.
**Acceptance criteria:**
- *Given* a /chat whose budgeted context includes image-modality chunks, *when* generation runs, *then* the corresponding page images attach to a vision-capable model call (routed per GN-5, capped at N images per request, env knob), and the answer grounds on them.
- *Given* only text chunks retrieved, *when* generation runs with the flag on, *then* the standard text path executes — vision cost only when images are actually in play.
- *Given* an answer grounded on an image chunk, *when* citations render in web chat, *then* the citation resolves to the page image (served through an authed endpoint, corpus permissions enforced) — the user can see the chart the answer read.
- *Given* the vision call fails, *when* the failure handler runs, *then* the pipeline retries/fails over per GN-4/GN-5 rules and, exhausted, answers from captions with a degraded-trace marker (never a hard error from the vision branch).
- *Given* the flag off, *when* /chat runs, *then* generation and SSE payloads are byte-identical to today.
- *Given* a golden-set extension of chart/table questions over the fixture corpus, *when* EV runs flag-on vs flag-off, *then* visual-question faithfulness improves, results committed.
**In scope:** Modality branch in GenerationService (vision provider call with image attachments, image-count cap, token/cost accounting for image tokens), authed page-image endpoint + web citation rendering, degradation to caption-grounding, visual golden-set additions + EV comparison, e2e: upload chart PDF → ask about the chart → cited image answer.
**Out of scope:** Vision in the agentic grading loop (graders stay text-only in v1); multimodal streaming previews of images mid-answer.
**Technical notes:** Cache note: CP-1 keys already include config fingerprint; image-bearing requests key on the image chunk hashes too (same query + changed figure must miss). Budgeting: image tokens count against the GN-2 budget with a per-image cost constant per provider docs — verify constants against current provider documentation at implementation time, don't trust memory.
**Definition of done:** as standard.
**Dependencies:** CE-7, GN-5.

---

### Epic: `template-profiles` (assembly)

---

**Title:** [CE-9] Profile eval matrix + template documentation + tradeoff table
**Epic:** `template-profiles`
**Problem / context:** The template's value proposition is "pick a profile, get a proven variant." That claim needs a committed evidence artifact (eval matrix across profiles) and consumer-facing documentation of what each profile costs — otherwise the profiles are just flags with marketing names.
**User story:** As a template consumer, I want a documented comparison of every profile's quality, latency, and cost so that I can choose a variant on evidence instead of vibes.
**Acceptance criteria:**
- *Given* the EV harness and golden set (including CE-6 multi-hop and CE-8 visual extensions), *when* the matrix run executes for `baseline`, `contextual`, `agentic`, and `full`, *then* retrieval metrics, generation metrics, p50/p95 latency, and per-query cost land in stored eval_runs and a committed `docs/PROFILE-MATRIX.md`.
- *Given* the repo README / template docs, *when* a consumer reads them, *then* they find: the profile table, the one-line `.env` switch instruction, per-profile infra requirements (Neo4j profile for `full`), the re-index requirement when enabling contextual (CE-2 runbook link), and the cost/latency tradeoffs with the measured numbers.
- *Given* the CI eval gate (Flow F / EV-4), *when* it runs, *then* it evaluates against the ACTIVE profile's stored baseline — enabling a profile never trips the gate against a different profile's numbers (per-profile baseline identity).
- *Given* a fresh clone with `RAG_PROFILE=baseline`, *when* the documented quickstart runs, *then* it works with no Phase 7 infra started (the template's default path stays lean).
**In scope:** Matrix run automation (script over the EV harness iterating profiles against a fixture corpus), `docs/PROFILE-MATRIX.md` with real numbers, README/template docs section, eval-gate baseline keyed by profile, `.env.example` final pass.
**Out of scope:** DP-1..DP-4 (host deployment remains its own blocked epic); marketing site material.
**Technical notes:** Profile switches that change index shape (contextual, multimodal) require re-index between matrix legs — the script must run them in an order that reuses shadow builds rather than thrashing (baseline → contextual → agentic reuses contextual index → full). Numbers are machine-local until DP-3 exists; label them "dev-environment reference numbers" honestly.
**Definition of done:** as standard.
**Dependencies:** CE-2, CE-4, CE-6, CE-8, EV-4.

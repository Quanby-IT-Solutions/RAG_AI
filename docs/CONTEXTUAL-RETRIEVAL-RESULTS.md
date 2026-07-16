# Contextual retrieval — baseline vs enriched (CE-2 evidence note)

Dev-environment reference numbers (2026-07-16, local single-instance stack —
not the DP-3 load-test environment). Corpus: 85 indexed documents / 175
chunks (dominant real-content document: QHealth EMR Terms of Reference).
Golden set: 11 active items (10 curated over the TOR + 1 handbook item),
`k=5`, hybrid 50/50 RRF, no rerank/multi-query/MMR/compression.

## Method

1. Baseline eval run on the raw index (`CONTEXTUAL_CHUNKS_ENABLED` off at
   index time) — stored eval run `f304fd9b`, promoted as the approved
   baseline.
2. Enrichment rollout per the RUNNING.md runbook: same-model re-embed
   migration with `RAG_PROFILE=contextual` (LLM situating context per chunk,
   cache-first; shadow embeddings over `context + chunk`; `context_text`
   landed on rows for the keyword arm), automatic golden-set verification
   against the shadow, then atomic cutover + response-cache generation bump.
3. Post-cutover eval run on the enriched index — stored eval run `f9eced11`.

## Results

| Metric | Baseline (raw) | Contextual (enriched) | Delta |
|---|---|---|---|
| Hit rate | 1.000 | 1.000 | 0.000 |
| MRR | 1.000 | 1.000 | 0.000 |
| Recall@5 | 1.000 | 1.000 | 0.000 |
| Precision@5 | 0.200 | 0.200 | 0.000 |
| Items evaluated | 11 | 11 | — |

Shadow verification (run inside the migration, before cutover was allowed)
scored the same 1.0 hit rate / MRR against the approved baseline
(`baselineCompared=1.0`, gate passed). End-to-end check after cutover:
"What is break-glass access in the EMR system?" → grounded answer with a
correct `[1]` citation to `upload://QHealth TOR.pdf` page 2.

## Rollout economics

Migration `e324643d` (same-model, `contextEnrichment=true`, prompt `v1`):
175/175 chunks enriched + shadow-embedded in **109 seconds**, 175
`context_cache` rows written (a re-run costs zero enrichment calls —
content-addressed on chunk hash + model + prompt version). Enrichment model:
`gpt-4o-mini` (~100 prompt + ~40 completion tokens per chunk ≈ **under
$0.01 for this corpus**).

## Reading the numbers honestly

The dev corpus is small (175 chunks) and the golden questions retrieve at
the ceiling already (hit rate 1.0 on the raw index), so this environment can
only demonstrate **no regression** for enrichment, not the headline lift the
technique shows on large corpora — the ~50%-class failed-retrieval reduction
in the cited research appears when the corpus is big enough for chunks to be
ambiguous out of context. Re-run this comparison on a production-scale
corpus (and after DP-3 exists, under load) before quoting lift numbers.

Migration record: `e324643d` (`contextEnrichment=true`, prompt `v1`;
`37660c9b` was a first attempt aborted mid-run — its abort correctly left
the serving path untouched). Enrichment economics for the rollout: see the
migration job stats and `rag_context_enrichment_total` metrics.

## Linear vs agentic (CE-4 evidence — CE-9 matrix inputs)

Same three golden questions, fresh compute in both modes (the CE-0 config
fingerprint gives each mode its own cache namespace), dev machine:

| Question (abbrev.) | Linear latency | Agentic latency | Overhead |
|---|---|---|---|
| Pharmacy verification | 12.6 s | 20.1 s | +7.5 s |
| Claim Form 4 extraction | 9.3 s | 12.7 s | +3.4 s |
| HAI tagging | 9.1 s | 12.9 s | +3.8 s |

The overhead is the loop's two extra cheap-model calls on the happy path
(relevance grading ~1.3 s + groundedness verdict ~1.7 s) plus provider
variance; no rewrites fired on these questions (retrieval graded sufficient
first pass — see `rag_agentic_hops`). Off-corpus questions run up to the
2-rewrite cap and end in an honest "I don't know" (live-verified: 3 hops,
2 real LLM rewrites). `rag_chat_mode_latency_seconds{mode}` tracks this
split continuously.

Generation quality on the enriched index (stored eval run `fdfa4a50`,
`generation=true`): faithfulness 1.0, answer relevancy 1.0, hit rate 1.0
(11 items). The EV harness's generation path exercises the shared grounded
prompt directly, so these numbers are mode-independent; the agentic loop's
own quality effect (rewrite recovery + groundedness gate) only shows on
corpora/questions where single-shot retrieval actually fails — dev-corpus
questions retrieve at ceiling, same caveat as above.

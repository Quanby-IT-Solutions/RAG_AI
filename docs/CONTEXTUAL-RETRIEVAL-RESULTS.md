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

Migration record: `37660c9b` (`contextEnrichment=true`, prompt `v1`).
Enrichment economics for the rollout: see the migration job stats and
`rag_context_enrichment_total` metrics (hits/misses/fallbacks).

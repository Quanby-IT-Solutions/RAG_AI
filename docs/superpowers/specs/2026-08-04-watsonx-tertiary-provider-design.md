# Watsonx Tertiary Provider Integration Design

## Objective

Add IBM watsonx.ai as an optional third chat provider without replacing the
existing OpenAI cheap-fast tier, Anthropic strong tier, or OpenAI embeddings.
Watsonx starts as a quality-first tertiary failover and receives no normal
routing traffic until a later evaluation explicitly promotes it.

## Current State

- OpenAI provides the cheap-fast generation tier and all embeddings.
- Anthropic provides the optional strong generation tier.
- Query length and retrieval confidence select between cheap and strong.
- The reliability router wraps exactly two clients and can select the other
  provider when the chosen provider's Redis circuit is already open.
- Provider calls share Redis-backed request budgets, retry/backoff, circuit
  breaking, hard timeouts, usage accounting, tracing, and output validation.
- The Tokyo watsonx.ai project and Runtime association have been validated with
  successful calls to Mistral Small 3.1 and Llama 4 Maverick.
- No IBM credential or Watsonx setting is stored in tracked project files.

## Considered Approaches

### 1. Tertiary failover behind OpenAI and Anthropic — selected

Preserve normal routing and append one Watsonx client to the ordered failover
chain. This provides provider diversity without changing answer quality or cost
for healthy requests before Watsonx has passed the repository's golden set.

### 2. Three-way normal routing

Route normal traffic among OpenAI, Anthropic, and Watsonx. This could reduce
cost, but it changes production answer behavior before provider-specific
faithfulness, citation, latency, and streaming behavior have been evaluated.

### 3. Replace Anthropic with Watsonx

Use Watsonx as the strong tier. This is smaller, but removes the existing Claude
path and does not satisfy the requirement to add Watsonx as another layer.

## Selected Architecture

The existing two-tier decision remains unchanged:

1. Short, high-confidence requests select OpenAI.
2. Long or low-confidence requests select Anthropic when configured.
3. The reliability layer checks the selected provider's circuit.
4. If it is open, the other normal-tier provider is considered first.
5. If both normal-tier providers are unavailable, Watsonx is considered.
6. If every configured provider circuit is unavailable, the existing bounded
   degraded response is returned through the current generation path.

Watsonx is not added to `ModelRouter.route()` as a normal tier. `ModelRouter`
will expose an ordered `fallbacks` collection, and `ReliabilityRouter` will wrap
and consult that collection after the cheap and strong candidates. Existing
decision fields remain stable; a Watsonx selection records `tier=failover`,
`provider=watsonx`, the actual model ID, and a reason naming the circuit that
caused failover.

Failover remains circuit-driven, matching current behavior. A request that
exhausts retries and opens a circuit follows the existing degraded path; the
next request observes the open circuit and selects the next available provider.
Changing this to replay a failed request against another provider in the same
request is outside this integration because it would change spend, streaming,
and audit semantics across every provider.

## Watsonx Client Boundary

Create `apps/rag/app/watsonx.py` with two focused units:

- `WatsonxIamTokenProvider` exchanges an IBM Cloud API key for an IAM bearer
  token, caches the token in memory, refreshes it before expiry, and serializes
  refreshes with an async lock so concurrent requests perform one exchange.
- `WatsonxChatClient` implements the existing `ChatModelClient` protocol using
  the watsonx.ai REST chat endpoints.

The integration will use `httpx.AsyncClient` directly instead of the
`ibm-watsonx-ai` SDK. The service is async end to end, while direct REST keeps
the client small, makes cancellation explicit, and avoids wrapping synchronous
SDK calls in worker threads. `httpx>=0.28` becomes a runtime dependency.

`complete(messages)` calls:

`POST {WATSONX_URL}/ml/v1/text/chat?version={WATSONX_API_VERSION}`

The JSON body contains `project_id`, `model_id`, `messages`, `temperature=0`,
and `max_tokens=GENERATION_MAX_TOKENS`. It extracts the first assistant text and
maps Watsonx prompt/completion/total usage to the service's existing camel-case
usage contract. Empty output raises a provider error.

`stream(messages)` calls:

`POST {WATSONX_URL}/ml/v1/text/chat_stream?version={WATSONX_API_VERSION}`

It parses Watsonx server-sent `data:` events, yields text deltas, retains the
latest usage object, ignores a terminal `[DONE]` marker, and finally yields the
existing `("__usage__", token_usage)` sentinel. Opening the response is bounded
by the existing external-call timeout. Closing or cancelling the application
generator closes the HTTP response immediately.

Both methods use the cached IAM token. A 401 response invalidates the token and
allows one internal refresh/retry because a token can expire between acquisition
and request dispatch. Other HTTP failures raise a typed error exposing only the
status and IBM error code; response bodies, keys, tokens, project IDs, and user
identities are never logged.

## Configuration Contract

Add these settings:

- `WATSONX_ENABLED=false`
- `IBM_CLOUD_API_KEY` as optional `SecretStr`
- `WATSONX_PROJECT_ID` as an optional non-empty string
- `WATSONX_URL=https://jp-tok.ml.cloud.ibm.com`
- `WATSONX_MODEL=meta-llama/llama-4-maverick-17b-128e-instruct-fp8`
- `WATSONX_API_VERSION=2024-03-14`
- `PROVIDER_RPM_WATSONX=60`

When `WATSONX_ENABLED=true`, startup validation requires a non-empty API key,
project ID, HTTPS URL, and model ID. Missing values fail fast with the exact
environment-variable name. When disabled, all fields except safe defaults may
be absent and no Watsonx client or health probe is constructed.

The Llama 4 Maverick default is intentional: tertiary failover is a
quality-preservation path, not a cost-optimization path. Mistral Small 3.1 can
be selected through `WATSONX_MODEL` for evaluation without a code change.

Add the non-secret keys and placeholder secret to both example environment
files and forward them to both the RAG API and worker services in Docker
Compose. No real credential is added to a tracked file. The key disclosed in
chat is used only for a bounded live smoke test and must be rotated before a
production `.env` is configured.

## Provider Construction and Rate Control

`build_model_router()` continues to build OpenAI cheap and the configured
OpenAI/Anthropic strong client exactly as today. When Watsonx is enabled and its
configuration validates, it constructs one `WatsonxChatClient` and appends it
to `ModelRouter.fallbacks`.

The Watsonx client is wrapped by `ThrottledChatClient` when provider buckets are
enabled. `ProviderBucket._rpm()` gains an explicit Watsonx branch using
`PROVIDER_RPM_WATSONX`; unknown providers must not silently inherit OpenAI's
budget. Watsonx 429 responses activate the same shrink, metric, retry, and
circuit behavior used by the existing providers.

`ReliabilityRouter` wraps every fallback in `ReliableChatClient`. Redis breaker
keys and Prometheus labels use the provider name `watsonx`. One Watsonx model is
configured at a time, so provider-level breaker sharing is unambiguous.

## Health and Observability

When enabled, `check_providers()` reports a `watsonx` entry using the same
interval cache as OpenAI and Anthropic. The probe exchanges or reuses an IAM
token and requests the regional foundation-model catalog; it does not run paid
inference. IAM 401/403 responses report `unauthorized`, 5xx/network failures
report `down`, and a successful catalog response reports `up`. Provider health
continues to degrade overall status without failing readiness.

Effective-configuration output places Watsonx settings in the providers group.
`IBM_CLOUD_API_KEY` is masked by `SecretStr` type, and regression tests verify
that neither it nor an IAM token can appear in API responses, logs, or snapshots.
Existing usage, cost, route decision, circuit, retry, failover, and bucket
metrics work through the shared client interface. Model pricing remains
configurable through `MODEL_PRICING_JSON` rather than hard-coded.

## Error Handling

- Disabled or incomplete optional configuration constructs no Watsonx client.
- Enabled but incomplete configuration fails startup with a named setting.
- IAM refresh is concurrency-safe and refreshes before expiration.
- An expired token receives one forced refresh attempt.
- Authentication and authorization errors are non-transient after that refresh.
- Watsonx 429 and 5xx errors participate in existing retry/backoff behavior.
- Network errors and hard timeouts participate in existing retry behavior.
- Open circuits skip Watsonx without making a network call.
- Malformed SSE or an empty completion raises a bounded provider error.
- Exhausting all candidates preserves the current graceful degradation response.

## Test Strategy

All production behavior is implemented test-first.

### Configuration tests

- Disabled Watsonx accepts absent optional fields.
- Enabled Watsonx names each missing required environment variable.
- The configured URL must be HTTPS.
- The API key is represented as `SecretStr` and masked by effective config.

### IAM tests

- The first caller exchanges the key and caches the token.
- Concurrent callers share one refresh.
- A token refreshes before expiry.
- A 401 invalidates the token and causes exactly one refresh.
- Error output never contains the API key, token, or IBM response body.

### Client tests

- Chat messages and model parameters map to the Watsonx request schema.
- Completion text and usage map to the shared client contract.
- Empty output is rejected.
- SSE deltas, usage, `[DONE]`, cancellation, and malformed events follow the
  existing streaming contract.
- Provider errors expose safe status/code metadata for reliability classification.

### Routing and reliability tests

- Normal simple and strong decisions remain OpenAI and Anthropic.
- Watsonx receives no normal routing traffic.
- An open OpenAI circuit prefers Anthropic before Watsonx.
- Open OpenAI and Anthropic circuits select Watsonx.
- An open Watsonx circuit is skipped.
- All circuits open preserves fast degradation.
- Watsonx retries, 429 adaptation, and RPM selection use provider-specific keys.

### Health and deployment tests

- Watsonx health is absent when disabled and cached when enabled.
- IAM/catalog authorization and network outcomes map to health states.
- Docker Compose forwards every Watsonx setting to API and worker.
- Both example environment files document placeholders without real secrets.
- Focused provider/reliability/health/config tests and the full RAG test suite pass.
- A live Mistral/Llama smoke test succeeds with credentials held only in memory.

## Rollout

1. Merge with `WATSONX_ENABLED=false`; existing runtime behavior is identical.
2. Rotate the disclosed IBM key and store the replacement only in the deployment
   secret store or ignored local `.env`.
3. Enable Watsonx in a non-production environment and run the live smoke test.
4. Exercise breaker-driven failover and verify traces, metrics, latency, and cost.
5. Run the existing golden retrieval/generation set against Llama 4 Maverick and
   Mistral Small 3.1.
6. Promote a Watsonx model into normal routing only through a separate reviewed
   design after it meets faithfulness, citation, latency, and cost thresholds.

## Non-Goals

- Replacing OpenAI embeddings or re-embedding the corpus.
- Sending normal traffic to Watsonx in this change.
- Adding multiple simultaneous Watsonx fallback models.
- Changing the cheap/strong routing heuristic or thresholds.
- Replaying a failed streamed request against another provider in the same call.
- Moving vision captioning or image-answer routing away from OpenAI.
- Persisting any credential in tracked files.

## Acceptance Criteria

- OpenAI remains the cheap/default generator and embedding provider.
- Anthropic remains the configured strong generator.
- Enabled Watsonx adds one Llama 4 Maverick tertiary fallback.
- Watsonx is selected only after normal-tier candidates are unavailable by the
  existing circuit-driven failover rules.
- Non-streamed and streamed Watsonx calls satisfy the shared client contract.
- IAM tokens are cached, concurrency-safe, refreshed before expiry, and secret.
- Watsonx participates in rate limiting, retries, breakers, health, tracing,
  metrics, usage accounting, and masked configuration.
- Disabled Watsonx leaves existing behavior byte-for-byte compatible at the
  routing boundary.
- Automated tests and a credential-safe live smoke test pass.
- No IBM key or bearer token is committed, logged, or returned by an API.

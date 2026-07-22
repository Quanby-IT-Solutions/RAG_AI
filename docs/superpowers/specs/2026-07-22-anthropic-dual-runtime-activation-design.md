# Anthropic Dual-Runtime Activation Design

## Objective

Activate the existing Anthropic integration for both Docker Compose and direct local RAG execution, while preserving OpenAI for embeddings and the cheap-fast generation tier. Verify strong-model routing, reliability, failover, health reporting, and secret handling end to end.

## Current State

- The RAG service already implements an Anthropic Messages API client.
- The model router uses OpenAI for the cheap-fast tier and Anthropic for the strong tier when `ANTHROPIC_API_KEY` is available.
- Long queries or low-retrieval-confidence queries select the strong tier.
- The reliability layer already provides bounded retry, Redis-backed circuit breaking, hard timeouts, output validation, and cross-provider failover.
- Provider calls already pass through shared Redis-backed rate control.
- Docker Compose forwards Anthropic and strong-model settings to both the API and worker services.
- The root `.env` contains an Anthropic key. `apps/rag/.env` does not currently contain it.
- Both environment files are ignored by Git.

## Selected Approach

Use the existing provider architecture without changing its routing or reliability semantics.

Configure both supported execution paths:

1. Docker Compose reads `ANTHROPIC_API_KEY` from the root `.env` and passes it into the RAG API and worker containers.
2. Direct local execution reads the same secret from `apps/rag/.env`.
3. Both environments explicitly select `STRONG_MODEL_PROVIDER=anthropic` and `STRONG_MODEL=claude-opus-4-8`, even though these are current defaults, so deployment intent is visible and stable.

This intentionally duplicates the secret across two ignored local files because the current execution paths have separate environment-loading boundaries. A configuration-loader refactor is out of scope.

## Runtime Data Flow

1. `Settings` loads the OpenAI and Anthropic credentials.
2. `build_model_router` creates the OpenAI cheap client and Anthropic strong client.
3. The router selects Anthropic for long queries or low retrieval confidence; simple queries remain on OpenAI.
4. Provider rate control gates each outbound request.
5. The reliability router applies retry, timeout, and circuit-breaker behavior.
6. If the selected provider circuit is open or the provider remains unavailable, the request fails over to the alternate provider.
7. Health checks report Anthropic degradation without failing overall readiness, allowing runtime failover to operate.

## Secret Handling

- Never add either `.env` file to Git.
- Never print, log, snapshot, or include the key in test output.
- Verification reports only present/absent and success/failure status.
- Live validation uses the configured runtime environment and does not place the key in command-line arguments.
- If the credential has been disclosed outside an approved secret channel, rotate it before production use.

## Error Handling

- Missing Anthropic credentials leave the service operational in OpenAI cheap-only mode and record `strong-unavailable` routing decisions.
- An invalid or unauthorized Anthropic credential appears as a degraded provider health result and causes provider calls to follow retry and failover behavior.
- A model-access error must be reported distinctly from an invalid credential where the provider response permits it.
- A failed live check must not expose response headers or bodies that could contain sensitive account details.

## Verification

Verification is complete only when all of the following pass:

1. Redacted configuration checks confirm Anthropic is available to both Docker and direct local execution.
2. Provider unit tests pass, including Anthropic request/response mapping and streaming.
3. Reliability tests pass, including open-circuit failover.
4. Health and generation tests pass.
5. A live, minimal Anthropic request succeeds with `claude-opus-4-8`.
6. Docker Compose renders the Anthropic variables into both RAG service definitions without exposing their values in the handoff.
7. Git status confirms neither environment file is tracked.

## Non-Goals

- Rewriting the provider interface or reliability layer.
- Changing routing thresholds.
- Enabling Anthropic embeddings; OpenAI embeddings remain required.
- Adding adaptive thinking or other model-specific features.
- Committing or distributing secrets.

## Acceptance Criteria

- Docker and direct local RAG execution both construct an Anthropic strong-tier client.
- The configured model identifier is `claude-opus-4-8`.
- Simple requests continue to use the OpenAI cheap-fast tier.
- Strong requests can use Anthropic.
- Provider failure can fall back to the alternate provider.
- Relevant automated tests and the minimal live provider check pass.
- No credential value appears in tracked files, logs, test output, or the final report.

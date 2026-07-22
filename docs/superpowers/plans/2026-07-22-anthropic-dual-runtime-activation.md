# Anthropic Dual-Runtime Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate and verify the existing Anthropic strong-model integration for Docker Compose and direct local RAG execution without exposing or committing the API key.

**Architecture:** Keep OpenAI as the required embedding and cheap-fast generation provider. Supply Anthropic credentials to the existing `Settings` and `ModelRouter` paths so long or low-confidence queries use `claude-opus-4-8`; retain the existing provider bucket, retry, Redis circuit breaker, timeout, validation, and failover layers unchanged.

**Tech Stack:** Python 3.11+, uv, FastAPI, pydantic-settings, Anthropic Python SDK, OpenAI Python SDK, Redis, pytest, Docker Compose, PowerShell.

## Global Constraints

- Never add `.env` or `apps/rag/.env` to Git.
- Never print, log, snapshot, or include `ANTHROPIC_API_KEY` in test output.
- Keep `OPENAI_API_KEY` configured because OpenAI remains required for embeddings and the cheap-fast generation tier.
- Use `STRONG_MODEL_PROVIDER=anthropic` and `STRONG_MODEL=claude-opus-4-8` in both runtime environments.
- Do not change routing thresholds, provider interfaces, reliability semantics, or embedding configuration.
- Live verification must use the configured environment and must not place a credential in command-line arguments.
- Restore `.env.example` to `# ANTHROPIC_API_KEY=sk-ant-your-key` if any real credential is found there, even when commented.

---

## File Map

- Modify locally, never commit: `.env` — Docker Compose environment source.
- Modify locally, never commit: `apps/rag/.env` — direct local RAG environment source.
- Verify unchanged: `.env.example` — tracked root template containing placeholders only.
- Verify unchanged: `apps/rag/.env.example` — tracked RAG template containing placeholders only.
- Exercise without modifying: `apps/rag/app/config.py` — typed provider and routing settings.
- Exercise without modifying: `apps/rag/app/providers.py` — OpenAI/Anthropic clients and model router.
- Exercise without modifying: `apps/rag/app/reliability.py` — retry, circuit breaker, and failover.
- Exercise without modifying: `apps/rag/app/health.py` — provider probes.
- Test: `apps/rag/tests/test_providers.py`.
- Test: `apps/rag/tests/test_reliability.py`.
- Test: `apps/rag/tests/test_health.py`.
- Test: `apps/rag/tests/test_generation.py`.

### Task 1: Enforce Secret Hygiene and Configure Both Runtime Boundaries

**Files:**
- Modify locally: `.env`
- Modify locally: `apps/rag/.env`
- Verify unchanged: `.env.example`
- Verify unchanged: `apps/rag/.env.example`

**Interfaces:**
- Consumes: the non-empty `ANTHROPIC_API_KEY` already stored in root `.env`.
- Produces: both runtime environment files with non-empty `ANTHROPIC_API_KEY`, `STRONG_MODEL_PROVIDER=anthropic`, and `STRONG_MODEL=claude-opus-4-8`.

- [ ] **Step 1: Confirm the tracked templates contain no production-style Anthropic key**

Run from the repository root:

```powershell
$matches = rg -l --hidden --glob '!**/.git/**' --glob '*.example' 'sk-ant-api[0-9]+-' .
if ($matches) { $matches; exit 1 }
Write-Output 'PASS: no production-style Anthropic key in tracked templates'
```

Expected: `PASS: no production-style Anthropic key in tracked templates`.

- [ ] **Step 2: Verify the root credential is present without printing it**

```powershell
$match = Select-String -LiteralPath '.env' -Pattern '^\s*ANTHROPIC_API_KEY\s*=\s*(.+)$' | Select-Object -First 1
if ($null -eq $match -or $match.Matches[0].Groups[1].Value.Trim() -notmatch '^sk-ant-') { throw 'Root .env does not contain a valid-looking Anthropic key' }
Write-Output 'PASS: root Anthropic credential is configured (redacted)'
```

Expected: `PASS: root Anthropic credential is configured (redacted)`.

- [ ] **Step 3: Configure the ignored runtime files**

Using `apply_patch`, make these operational changes without placing the secret in this plan or any tracked file:

```text
.env:
  Keep the existing ANTHROPIC_API_KEY assignment unchanged.
  STRONG_MODEL_PROVIDER=anthropic
  STRONG_MODEL=claude-opus-4-8

apps/rag/.env:
  Add ANTHROPIC_API_KEY with the exact byte-for-byte value from root .env.
  STRONG_MODEL_PROVIDER=anthropic
  STRONG_MODEL=claude-opus-4-8
```

Preserve every unrelated existing setting. Ensure each variable appears exactly once per file.

- [ ] **Step 4: Verify both runtime files with redacted output**

```powershell
$targets = @('.env','apps\rag\.env')
foreach ($target in $targets) {
  foreach ($name in @('ANTHROPIC_API_KEY','STRONG_MODEL_PROVIDER','STRONG_MODEL')) {
    $pattern = '^\s*' + [regex]::Escape($name) + '\s*=\s*(.+)$'
    $match = Select-String -LiteralPath $target -Pattern $pattern | Select-Object -First 1
    if ($null -eq $match) { throw "$target is missing $name" }
    $value = $match.Matches[0].Groups[1].Value.Trim()
    if ($name -eq 'ANTHROPIC_API_KEY') {
      if ($value -notmatch '^sk-ant-') { throw "$target has an invalid-looking Anthropic key" }
      Write-Output "$target | $name | configured (redacted)"
    } else {
      Write-Output "$target | $name | $value"
    }
  }
}
```

Expected: six status lines; credentials are reported only as `configured (redacted)`, provider is `anthropic`, and model is `claude-opus-4-8`.

- [ ] **Step 5: Confirm environment files cannot be committed**

```powershell
git check-ignore -v .env apps/rag/.env
git status --short
```

Expected: both files match `.gitignore`; neither appears in Git status. Do not create a commit for this task because its only changes are ignored secrets.

### Task 2: Verify Typed Configuration and Router Construction

**Files:**
- Exercise: `apps/rag/app/config.py`
- Exercise: `apps/rag/app/providers.py`

**Interfaces:**
- Consumes: the direct-runtime variables configured in `apps/rag/.env`.
- Produces: a `ModelRouter` whose `cheap.provider` is `openai`, whose `strong.provider` is `anthropic`, and whose strong model is `claude-opus-4-8`.

- [ ] **Step 1: Run a redacted configuration and router assertion**

Run from `apps/rag`:

```powershell
@'
from app.config import Settings
from app.providers import build_model_router

settings = Settings()
router = build_model_router(settings)
assert settings.anthropic_api_key is not None
assert settings.strong_model_provider == "anthropic"
assert settings.strong_model == "claude-opus-4-8"
assert router.cheap.provider == "openai"
assert router.strong is not None
assert router.strong.provider == "anthropic"
assert router.strong.model == "claude-opus-4-8"
print("PASS: OpenAI cheap tier and Anthropic strong tier constructed")
'@ | uv run python -
```

Expected: `PASS: OpenAI cheap tier and Anthropic strong tier constructed`.

- [ ] **Step 2: Verify both routing branches**

```powershell
@'
from app.config import Settings
from app.providers import build_model_router

settings = Settings()
router = build_model_router(settings)
simple, simple_decision = router.route(query="short question", chunks=[{"similarity": 0.99}])
strong, strong_decision = router.route(query="x" * settings.routing_query_length_threshold, chunks=[{"similarity": 0.99}])
assert simple.provider == "openai"
assert simple_decision["tier"] == "cheap-fast"
assert strong.provider == "anthropic"
assert strong_decision["tier"] == "strong"
print("PASS: simple routes to OpenAI; long routes to Anthropic")
'@ | uv run python -
```

Expected: `PASS: simple routes to OpenAI; long routes to Anthropic`.

### Task 3: Run Provider and Reliability Regression Tests

**Files:**
- Test: `apps/rag/tests/test_providers.py`
- Test: `apps/rag/tests/test_reliability.py`
- Test: `apps/rag/tests/test_health.py`
- Test: `apps/rag/tests/test_generation.py`

**Interfaces:**
- Consumes: the unchanged provider, reliability, health, and generation implementations.
- Produces: regression evidence for Anthropic mapping, streaming, routing, provider probing, circuit breaking, failover, and generated-answer integration.

- [ ] **Step 1: Run focused provider and reliability tests**

Run from `apps/rag`:

```powershell
uv run pytest tests/test_providers.py tests/test_reliability.py -q
```

Expected: all tests pass with exit code 0.

- [ ] **Step 2: Run health and generation tests**

```powershell
uv run pytest tests/test_health.py tests/test_generation.py -q
```

Expected: all tests pass with exit code 0.

- [ ] **Step 3: Run formatting and static lint checks for the exercised Python service**

From the repository root:

```powershell
pnpm --filter @repo/rag lint
```

Expected: Ruff exits with code 0.

### Task 4: Verify Docker Compose Propagation Without Revealing Secrets

**Files:**
- Exercise: `docker-compose.yml`
- Consume: `.env`

**Interfaces:**
- Consumes: Docker Compose variable interpolation from root `.env`.
- Produces: evidence that both `rag` and `rag-worker` receive the Anthropic credential, provider, and model settings.

- [ ] **Step 1: Render Compose configuration into memory and assert redacted values**

```powershell
$json = docker compose config --format json | ConvertFrom-Json
foreach ($serviceName in @('rag','rag-worker')) {
  $service = $json.services.$serviceName
  if ($null -eq $service) { throw "Missing Compose service: $serviceName" }
  $key = $service.environment.ANTHROPIC_API_KEY
  if ([string]::IsNullOrWhiteSpace($key) -or $key -notmatch '^sk-ant-') { throw "$serviceName lacks Anthropic credential" }
  if ($service.environment.STRONG_MODEL_PROVIDER -ne 'anthropic') { throw "$serviceName has wrong strong provider" }
  if ($service.environment.STRONG_MODEL -ne 'claude-opus-4-8') { throw "$serviceName has wrong strong model" }
  Write-Output "$serviceName | Anthropic configured (redacted) | anthropic | claude-opus-4-8"
}
```

Expected: one redacted success line each for `rag` and `rag-worker`; no credential value is printed.

### Task 5: Perform Minimal Live Anthropic Verification and Final Security Audit

**Files:**
- Exercise: `apps/rag/app/config.py`
- Exercise: `apps/rag/app/providers.py`
- Verify unchanged: `.env.example`
- Verify unchanged: `apps/rag/.env.example`

**Interfaces:**
- Consumes: `Settings()` and `AnthropicChatClient.complete(messages)`.
- Produces: a non-empty completion and token-usage evidence from the live `claude-opus-4-8` API.

- [ ] **Step 1: Make one bounded live Anthropic request**

Run from `apps/rag`. This incurs a very small provider charge:

```powershell
@'
import asyncio
from app.config import Settings
from app.providers import AnthropicChatClient

async def main() -> None:
    settings = Settings(GENERATION_MAX_TOKENS=16)
    client = AnthropicChatClient(settings)
    text, usage = await client.complete([{"role": "user", "content": "Reply with exactly: OK"}])
    assert text.strip()
    assert usage is not None and usage["totalTokens"] > 0
    print(f"PASS: live Anthropic completion via {client.model}; usage recorded")

asyncio.run(main())
'@ | uv run python -
```

Expected: `PASS: live Anthropic completion via claude-opus-4-8; usage recorded`. If the sandbox blocks network access, rerun the same command with approved network escalation. Do not print the provider response on failure.

- [ ] **Step 2: Run the final tracked-secret scan**

```powershell
$matches = rg -l --hidden --glob '!**/.git/**' --glob '!.env' --glob '!apps/rag/.env' 'sk-ant-api[0-9]+-' .
if ($matches) { $matches; exit 1 }
Write-Output 'PASS: no production-style Anthropic key outside ignored runtime env files'
```

Expected: `PASS: no production-style Anthropic key outside ignored runtime env files`.

- [ ] **Step 3: Confirm no unintended tracked changes remain**

```powershell
git diff --check
git status --short
```

Expected: no environment file is listed and no unintended tracked file is modified. If only this plan/spec documentation is present or already committed, report that explicitly. No implementation commit is required when activation changes only ignored secret files.

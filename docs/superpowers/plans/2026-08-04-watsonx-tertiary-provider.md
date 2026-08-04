# Watsonx Tertiary Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add IBM watsonx.ai as an optional, quality-first tertiary chat fallback behind the existing OpenAI cheap tier and Anthropic strong tier.

**Architecture:** A focused async Watsonx module owns IAM token caching and REST chat/SSE mapping. `ModelRouter` keeps its normal two-tier decision but exposes ordered fallback clients; `ReliabilityRouter` wraps and consults those clients only after normal candidates have open circuits. Watsonx remains disabled by default and does not replace OpenAI embeddings or vision.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, httpx 0.28+, pytest/pytest-asyncio, Redis-backed provider buckets and circuit breakers, Docker Compose.

## Global Constraints

- Preserve OpenAI as the cheap/default generator and only embedding provider.
- Preserve Anthropic as the configured strong generator.
- Use `meta-llama/llama-4-maverick-17b-128e-instruct-fp8` as the default Watsonx fallback model.
- Use `https://jp-tok.ml.cloud.ibm.com` and API version `2024-03-14` as safe defaults.
- Watsonx receives no normal routing traffic; it is considered only by circuit-driven failover.
- Preserve the stream contract: text deltas followed by `("__usage__", token_usage)`.
- Preserve wrapper order: provider bucket -> reliable client/breaker -> provider call.
- Never commit, print, log, snapshot, or return an IBM API key, IAM token, project ID, or IBM error response body.
- Do not add or modify a tracked file containing a real credential.
- Implement every production behavior test-first and observe the expected failing test before implementation.
- Do not change OpenAI embeddings, normal vision routing, routing thresholds, or same-request replay semantics.

---

## File Structure

- Create `apps/rag/app/watsonx.py`: IBM IAM token provider, safe error type, non-streamed chat client, streamed chat client.
- Create `apps/rag/tests/test_watsonx.py`: isolated IAM, REST mapping, error-redaction, and SSE contract tests.
- Modify `apps/rag/app/config.py`: optional Watsonx settings and enabled-only validation.
- Modify `apps/rag/tests/test_config.py`: configuration validation tests.
- Modify `apps/rag/app/providers.py`: fallback collection and Watsonx construction.
- Modify `apps/rag/tests/test_providers.py`: construction and no-normal-traffic assertions.
- Modify `apps/rag/app/reliability.py`: wrap and order tertiary fallback candidates.
- Modify `apps/rag/tests/test_reliability.py`: three-provider breaker routing.
- Modify `apps/rag/app/provider_bucket.py`: explicit Watsonx RPM selection.
- Modify `apps/rag/tests/test_provider_bucket.py`: Watsonx bucket and unknown-provider behavior.
- Modify `apps/rag/app/health.py`: cached Watsonx IAM/catalog probe.
- Modify `apps/rag/tests/test_health.py`: disabled, success, unauthorized, down, and cache behavior.
- Modify `apps/rag/app/config_view.py` and `apps/rag/tests/test_observability.py`: provider grouping and secret masking.
- Modify `apps/rag/pyproject.toml` and `apps/rag/uv.lock`: promote `httpx>=0.28`
  to runtime dependencies and refresh the lockfile.
- Modify `.env.example`, `apps/rag/.env.example`, and `docker-compose.yml`: documented and forwarded configuration.
- Modify `README.md`: document optional Watsonx fallback configuration and rollout status.

---

### Task 1: Watsonx configuration contract

**Files:**
- Modify: `apps/rag/app/config.py`
- Modify: `apps/rag/tests/test_config.py`
- Modify: `apps/rag/pyproject.toml`
- Modify: `apps/rag/uv.lock`

**Interfaces:**
- Consumes: existing `Settings` and `pydantic.SecretStr` conventions.
- Produces: `Settings.watsonx_enabled`, `ibm_cloud_api_key`, `watsonx_project_id`, `watsonx_url`, `watsonx_model`, `watsonx_api_version`, and `provider_rpm_watsonx`.

- [ ] **Step 1: Write failing disabled/enabled configuration tests**

Append tests that prove disabled configuration is optional and enabled configuration names every required field:

```python
def test_watsonx_disabled_requires_no_credentials(monkeypatch):
    _clear_env(monkeypatch)
    settings = Settings(_env_file=None, WATSONX_ENABLED="false", **REQUIRED_ENV)
    assert settings.watsonx_enabled is False
    assert settings.ibm_cloud_api_key is None
    assert settings.watsonx_project_id is None


@pytest.mark.parametrize(
    ("override", "missing"),
    [
        ({"WATSONX_PROJECT_ID": "project"}, "IBM_CLOUD_API_KEY"),
        ({"IBM_CLOUD_API_KEY": "secret"}, "WATSONX_PROJECT_ID"),
    ],
)
def test_watsonx_enabled_names_missing_required_setting(monkeypatch, override, missing):
    _clear_env(monkeypatch)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, WATSONX_ENABLED="true", **override, **REQUIRED_ENV)
    assert missing in str(excinfo.value)


def test_watsonx_enabled_uses_quality_first_tokyo_defaults(monkeypatch):
    _clear_env(monkeypatch)
    settings = Settings(
        _env_file=None,
        WATSONX_ENABLED="true",
        IBM_CLOUD_API_KEY="secret",
        WATSONX_PROJECT_ID="project",
        **REQUIRED_ENV,
    )
    assert settings.watsonx_url == "https://jp-tok.ml.cloud.ibm.com"
    assert settings.watsonx_api_version == "2024-03-14"
    assert settings.watsonx_model == (
        "meta-llama/llama-4-maverick-17b-128e-instruct-fp8"
    )
    assert settings.provider_rpm_watsonx == 60


@pytest.mark.parametrize("setting", ["IBM_CLOUD_API_KEY", "WATSONX_PROJECT_ID"])
def test_watsonx_enabled_rejects_blank_credentials(monkeypatch, setting):
    _clear_env(monkeypatch)
    values = {
        "IBM_CLOUD_API_KEY": "secret",
        "WATSONX_PROJECT_ID": "project",
        setting: "   ",
    }
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            WATSONX_ENABLED="true",
            **values,
            **REQUIRED_ENV,
        )
    assert setting in str(excinfo.value)


def test_watsonx_enabled_rejects_non_https_url(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            WATSONX_ENABLED="true",
            IBM_CLOUD_API_KEY="secret",
            WATSONX_PROJECT_ID="project",
            WATSONX_URL="http://jp-tok.ml.cloud.ibm.com",
            **REQUIRED_ENV,
        )
    assert "WATSONX_URL" in str(excinfo.value)
```

- [ ] **Step 2: Run the tests and verify RED**

Run from `apps/rag`:

```powershell
uv run pytest tests/test_config.py -q
```

Expected: failures because the Watsonx fields do not exist.

- [ ] **Step 3: Add the minimal settings and validator**

Add fields next to the existing provider/routing settings:

```python
watsonx_enabled: bool = Field(alias="WATSONX_ENABLED", default=False)
ibm_cloud_api_key: SecretStr | None = Field(alias="IBM_CLOUD_API_KEY", default=None)
watsonx_project_id: SecretStr | None = Field(alias="WATSONX_PROJECT_ID", default=None)
watsonx_url: str = Field(
    alias="WATSONX_URL", default="https://jp-tok.ml.cloud.ibm.com", min_length=1
)
watsonx_model: str = Field(
    alias="WATSONX_MODEL",
    default="meta-llama/llama-4-maverick-17b-128e-instruct-fp8",
    min_length=1,
)
watsonx_api_version: str = Field(alias="WATSONX_API_VERSION", default="2024-03-14")
provider_rpm_watsonx: int = Field(alias="PROVIDER_RPM_WATSONX", default=60, ge=1)
```

Add an enabled-only validator:

```python
@model_validator(mode="after")
def _require_watsonx_configuration_when_enabled(self) -> Self:
    if not self.watsonx_enabled:
        return self
    if self.ibm_cloud_api_key is None or not self.ibm_cloud_api_key.get_secret_value().strip():
        raise ValueError("IBM_CLOUD_API_KEY is required when WATSONX_ENABLED=true")
    if (
        self.watsonx_project_id is None
        or not self.watsonx_project_id.get_secret_value().strip()
    ):
        raise ValueError("WATSONX_PROJECT_ID is required when WATSONX_ENABLED=true")
    if not self.watsonx_url.startswith("https://"):
        raise ValueError("WATSONX_URL must use https:// when WATSONX_ENABLED=true")
    return self
```

Move `httpx>=0.28` from the dev-only assumption into `[project].dependencies`; retaining it in the dev group is unnecessary and must be removed if duplicated.
Run `uv lock` from `apps/rag` after editing the dependency groups and verify
that only the intended dependency classification changes in `uv.lock`.

- [ ] **Step 4: Run configuration tests and verify GREEN**

```powershell
uv run pytest tests/test_config.py -q
```

Expected: all configuration tests pass.

- [ ] **Step 5: Run Ruff on changed Python files**

```powershell
uv run ruff check app/config.py tests/test_config.py
```

Expected: exit 0 with no diagnostics.

- [ ] **Step 6: Commit the configuration contract**

```powershell
git add apps/rag/app/config.py apps/rag/tests/test_config.py apps/rag/pyproject.toml apps/rag/uv.lock
git commit -m "feat(rag): add optional watsonx configuration"
```

---

### Task 2: IAM token cache and non-streamed Watsonx chat

**Files:**
- Create: `apps/rag/app/watsonx.py`
- Create: `apps/rag/tests/test_watsonx.py`

**Interfaces:**
- Consumes: `Settings`, `with_hard_timeout`, `httpx.AsyncClient`.
- Produces: `WatsonxApiError`, `WatsonxIamTokenProvider.get_token(force_refresh: bool = False) -> str`, `invalidate() -> None`, `aclose() -> None`, and `WatsonxChatClient.complete(messages) -> tuple[str, dict[str, int] | None]`.

- [ ] **Step 1: Write failing IAM cache tests**

Define the complete credential-safe fixtures first:

```python
class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeHttpClient:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.post_calls: list[dict] = []
        self.closed = False

    async def post(self, url: str, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        return self.responses.pop(0)

    async def aclose(self):
        self.closed = True


class BlockingFakeHttpClient(FakeHttpClient):
    def __init__(self, response: FakeResponse):
        super().__init__([response])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def post(self, url: str, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        self.started.set()
        await self.release.wait()
        return self.responses.pop(0)


@pytest.fixture
def watsonx_settings() -> Settings:
    return Settings(
        _env_file=None,
        WATSONX_ENABLED="true",
        IBM_CLOUD_API_KEY="ibm-test-key",
        WATSONX_PROJECT_ID="test-project",
        **REQUIRED_ENV,
    )
```

Then add the cache tests; never put a real credential in fixtures.

```python
async def test_iam_token_is_cached_until_refresh_margin(watsonx_settings):
    http = FakeHttpClient([
        FakeResponse(200, {"access_token": "token-1", "expires_in": 3600})
    ])
    tokens = WatsonxIamTokenProvider(watsonx_settings, client=http, clock=lambda: 1000.0)

    assert await tokens.get_token() == "token-1"
    assert await tokens.get_token() == "token-1"
    assert len(http.post_calls) == 1


async def test_concurrent_iam_callers_share_one_exchange(watsonx_settings):
    http = BlockingFakeHttpClient(
        FakeResponse(200, {"access_token": "token-1", "expires_in": 3600})
    )
    tokens = WatsonxIamTokenProvider(watsonx_settings, client=http, clock=lambda: 1000.0)

    first_task = asyncio.create_task(tokens.get_token())
    await http.started.wait()
    second_task = asyncio.create_task(tokens.get_token())
    http.release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert (first, second) == ("token-1", "token-1")
    assert len(http.post_calls) == 1


async def test_iam_token_refreshes_before_expiry(watsonx_settings):
    now = [1000.0]
    http = FakeHttpClient([
        FakeResponse(200, {"access_token": "token-1", "expires_in": 120}),
        FakeResponse(200, {"access_token": "token-2", "expires_in": 120}),
    ])
    tokens = WatsonxIamTokenProvider(
        watsonx_settings, client=http, clock=lambda: now[0]
    )

    assert await tokens.get_token() == "token-1"
    now[0] = 1061.0
    assert await tokens.get_token() == "token-2"
    assert len(http.post_calls) == 2
```

- [ ] **Step 2: Run IAM tests and verify RED**

```powershell
uv run pytest tests/test_watsonx.py -q
```

Expected: collection failure because `app.watsonx` does not exist.

- [ ] **Step 3: Implement the token provider and safe error**

Create these public types:

```python
class WatsonxApiError(RuntimeError):
    def __init__(self, operation: str, status_code: int, code: str = "unknown") -> None:
        super().__init__(f"Watsonx {operation} failed (status={status_code}, code={code})")
        self.status_code = status_code
        self.code = code


def _safe_response_payload(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except (httpx.ResponseNotRead, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_error_code(payload: dict[str, Any]) -> str:
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        nested_code = errors[0].get("code")
        if nested_code:
            return str(nested_code)
    return str(payload.get("errorCode") or payload.get("error") or "unknown")


class WatsonxIamTokenProvider:
    _REFRESH_MARGIN_SECONDS = 60.0

    def __init__(self, settings: Settings, *, client: Any = None, clock=time.time) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._token and self._clock() < self._expires_at:
            return self._token
        async with self._lock:
            if not force_refresh and self._token and self._clock() < self._expires_at:
                return self._token
            response = await with_hard_timeout(
                self._client.post(
                    "https://iam.cloud.ibm.com/identity/token",
                    headers={"accept": "application/json"},
                    data={
                        "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                        "apikey": self._settings.ibm_cloud_api_key.get_secret_value(),
                    },
                ),
                seconds=self._settings.external_call_timeout_seconds,
                operation="watsonx.iam.token",
            )
            payload = _safe_response_payload(response)
            if response.status_code != 200:
                raise WatsonxApiError(
                    "iam_token", response.status_code, _safe_error_code(payload)
                )
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise WatsonxApiError("iam_token", 502, "missing_access_token")
            expires_in = float(payload.get("expires_in") or 0)
            self._token = token
            self._expires_at = self._clock() + max(
                0.0, expires_in - self._REFRESH_MARGIN_SECONDS
            )
            return token

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
```

The response handling must not call `raise_for_status()` because its exception
text can contain response details. It requires status 200 and a non-empty token,
then stores only the token and refresh deadline shown above.

- [ ] **Step 4: Run IAM tests and verify GREEN**

```powershell
uv run pytest tests/test_watsonx.py -q
```

Expected: IAM cache tests pass.

- [ ] **Step 5: Write failing non-streamed chat and 401-refresh tests**

Add the token stub used by client tests:

```python
class StubTokens:
    def __init__(self, values: list[str]):
        self.values = list(values)
        self.current: str | None = None
        self.forced_refreshes = 0

    async def get_token(self, force_refresh: bool = False) -> str:
        if force_refresh:
            self.forced_refreshes += 1
            self.current = None
        if self.current is None:
            self.current = self.values.pop(0)
        return self.current

    def invalidate(self) -> None:
        self.current = None

    async def aclose(self) -> None:
        return None
```

```python
async def test_complete_maps_chat_and_usage(watsonx_settings):
    tokens = StubTokens(["token-1"])
    http = FakeHttpClient([
        FakeResponse(200, {
            "choices": [{"message": {"role": "assistant", "content": "Grounded [1]."}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24},
        })
    ])
    client = WatsonxChatClient(watsonx_settings, client=http, tokens=tokens)

    text, usage = await client.complete([{"role": "user", "content": "Question?"}])

    assert text == "Grounded [1]."
    assert usage == {"promptTokens": 20, "completionTokens": 4, "totalTokens": 24}
    sent = http.post_calls[0]["json"]
    assert sent["project_id"] == "test-project"
    assert sent["model_id"] == watsonx_settings.watsonx_model
    assert sent["messages"] == [{"role": "user", "content": "Question?"}]
    assert sent["temperature"] == 0
    assert sent["max_tokens"] == watsonx_settings.generation_max_tokens


async def test_complete_refreshes_once_on_401(watsonx_settings):
    tokens = StubTokens(["expired", "fresh"])
    http = FakeHttpClient([
        FakeResponse(401, {"errors": [{"code": "authentication_token_not_valid"}]}),
        FakeResponse(200, {
            "choices": [{"message": {"content": "ok"}}],
            "usage": None,
        }),
    ])
    client = WatsonxChatClient(watsonx_settings, client=http, tokens=tokens)

    assert await client.complete([{"role": "user", "content": "q"}]) == ("ok", None)
    assert tokens.forced_refreshes == 1
    assert len(http.post_calls) == 2
```

- [ ] **Step 6: Run the new tests and verify RED**

```powershell
uv run pytest tests/test_watsonx.py -q
```

Expected: failures because `WatsonxChatClient` is absent.

- [ ] **Step 7: Implement non-streamed chat mapping**

Implement:

```python
class WatsonxChatClient:
    provider = "watsonx"

    def __init__(self, settings: Settings, *, client: Any = None, tokens: Any = None) -> None:
        self._settings = settings
        self._http = client or httpx.AsyncClient()
        self._owns_http = client is None
        self._tokens = tokens or WatsonxIamTokenProvider(settings, client=self._http)
        self.model = settings.watsonx_model

    def _url(self, path: str) -> str:
        base = self._settings.watsonx_url.rstrip("/")
        return f"{base}{path}?version={self._settings.watsonx_api_version}"

    def _body(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "project_id": self._settings.watsonx_project_id.get_secret_value(),
            "model_id": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self._settings.generation_max_tokens,
        }
```

Implement `_post_chat(messages, refreshed=False)` to obtain a token with
`self._tokens.get_token(force_refresh=refreshed)`, perform a hard-timeout POST,
and parse the response only through `_safe_response_payload`.
On status 401, call `self._tokens.invalidate()` followed by one recursive call
with `refreshed=True`. When `refreshed=True`, a second 401 raises
`WatsonxApiError("chat", 401, _safe_error_code(payload))`. Other non-200
statuses raise the same safe type without the response body. Extract non-empty
assistant content and map usage fields in `complete()`.

- [ ] **Step 8: Add empty-output and secret-redaction tests**

Assert empty `choices`/content raises
`RuntimeError(f"Watsonx returned an empty completion (model={watsonx_settings.watsonx_model})")`.
Add this complete redaction test:

```python
async def test_provider_error_never_exposes_response_details(watsonx_settings):
    sensitive = ["ibm-test-key", "iam-secret", "test-project", "user@example.test"]
    http = FakeHttpClient([
        FakeResponse(403, {
            "errors": [{"code": "user_authorization_failed", "message": " ".join(sensitive)}]
        })
    ])
    client = WatsonxChatClient(
        watsonx_settings, client=http, tokens=StubTokens(["iam-secret"])
    )

    with pytest.raises(WatsonxApiError) as excinfo:
        await client.complete([{"role": "user", "content": "q"}])

    rendered = str(excinfo.value)
    assert "status=403" in rendered
    assert "code=user_authorization_failed" in rendered
    assert all(value not in rendered for value in sensitive)
```

- [ ] **Step 9: Run all Watsonx tests and Ruff**

```powershell
uv run pytest tests/test_watsonx.py -q
uv run ruff check app/watsonx.py tests/test_watsonx.py
```

Expected: both commands exit 0.

- [ ] **Step 10: Commit IAM and completion support**

```powershell
git add apps/rag/app/watsonx.py apps/rag/tests/test_watsonx.py
git commit -m "feat(rag): add async watsonx chat client"
```

---

### Task 3: Watsonx SSE streaming contract

**Files:**
- Modify: `apps/rag/app/watsonx.py`
- Modify: `apps/rag/tests/test_watsonx.py`

**Interfaces:**
- Consumes: Task 2 `WatsonxChatClient`, token provider, safe error mapping.
- Produces: `WatsonxChatClient.stream(messages)` async generator yielding text deltas and the usage sentinel.

- [ ] **Step 1: Write failing SSE mapping test**

Define streaming fakes with the same context-manager surface as httpx:

```python
class FakeStreamResponse:
    def __init__(self, status_code: int, lines: list[str]):
        self.status_code = status_code
        self._lines = lines
        self.closed = False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    def json(self) -> dict:
        return {}


class FakeStreamManager:
    def __init__(self, response: FakeStreamResponse):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, traceback):
        self.response.closed = True


class FakeStreamingHttpClient(FakeHttpClient):
    def __init__(self, responses: list[FakeStreamResponse]):
        super().__init__([])
        self.stream_responses = list(responses)
        self.stream_calls: list[dict] = []

    def stream(self, method: str, url: str, **kwargs):
        self.stream_calls.append({"method": method, "url": url, **kwargs})
        return FakeStreamManager(self.stream_responses.pop(0))
```

```python
async def test_stream_maps_sse_deltas_and_usage(watsonx_settings):
    response = FakeStreamResponse(200, [
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',
        'data: {"choices":[{"delta":{"content":"Grounded "}}]}',
        'data: {"choices":[{"delta":{"content":"answer [1]."}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":5,"total_tokens":25}}',
        'data: [DONE]',
    ])
    http = FakeStreamingHttpClient([response])
    client = WatsonxChatClient(watsonx_settings, client=http, tokens=StubTokens(["token"]))

    items = [item async for item in client.stream([{"role": "user", "content": "q"}])]

    assert items == [
        "Grounded ",
        "answer [1].",
        ("__usage__", {"promptTokens": 20, "completionTokens": 5, "totalTokens": 25}),
    ]
    assert response.closed is True
```

- [ ] **Step 2: Run the stream test and verify RED**

```powershell
uv run pytest tests/test_watsonx.py::test_stream_maps_sse_deltas_and_usage -q
```

Expected: failure because `stream()` is not implemented.

- [ ] **Step 3: Implement minimal SSE streaming**

Implement `stream()` with `httpx.AsyncClient.stream`, hard-timeout only around
`manager.__aenter__()`, and a `finally` block that always calls
`manager.__aexit__(None, None, None)`. For each `aiter_lines()` line:

```python
if not line.startswith("data:"):
    continue
payload = line[5:].strip()
if not payload or payload == "[DONE]":
    continue
event = json.loads(payload)
usage = event.get("usage")
if usage:
    token_usage = self._usage(usage)
for choice in event.get("choices") or []:
    content = (choice.get("delta") or {}).get("content")
    if content:
        yield content
```

After the response closes, yield `("__usage__", token_usage)`.

- [ ] **Step 4: Run the stream test and verify GREEN**

```powershell
uv run pytest tests/test_watsonx.py::test_stream_maps_sse_deltas_and_usage -q
```

Expected: pass.

- [ ] **Step 5: Add failing cancellation, malformed-event, and 401 tests**

Add these concrete edge tests:

```python
async def test_stream_close_closes_provider_response(watsonx_settings):
    response = FakeStreamResponse(200, [
        'data: {"choices":[{"delta":{"content":"first"}}]}',
        'data: {"choices":[{"delta":{"content":"second"}}]}',
    ])
    client = WatsonxChatClient(
        watsonx_settings,
        client=FakeStreamingHttpClient([response]),
        tokens=StubTokens(["token"]),
    )
    stream = client.stream([{"role": "user", "content": "q"}])

    assert await anext(stream) == "first"
    await stream.aclose()

    assert response.closed is True


async def test_stream_rejects_malformed_sse_without_echoing_payload(watsonx_settings):
    secret_payload = "not-json-iam-secret"
    response = FakeStreamResponse(200, [f"data: {secret_payload}"])
    client = WatsonxChatClient(
        watsonx_settings,
        client=FakeStreamingHttpClient([response]),
        tokens=StubTokens(["token"]),
    )

    with pytest.raises(WatsonxApiError) as excinfo:
        _ = [item async for item in client.stream([{"role": "user", "content": "q"}])]

    assert excinfo.value.code == "malformed_sse"
    assert secret_payload not in str(excinfo.value)


async def test_stream_refreshes_once_on_401_before_deltas(watsonx_settings):
    denied = FakeStreamResponse(401, [])
    success = FakeStreamResponse(200, [
        'data: {"choices":[{"delta":{"content":"ok"}}]}',
    ])
    tokens = StubTokens(["expired", "fresh"])
    client = WatsonxChatClient(
        watsonx_settings,
        client=FakeStreamingHttpClient([denied, success]),
        tokens=tokens,
    )

    items = [item async for item in client.stream([{"role": "user", "content": "q"}])]

    assert items == ["ok", ("__usage__", None)]
    assert tokens.forced_refreshes == 1
```

The implementation must not retry after a delta has been emitted.

- [ ] **Step 6: Implement safe stream retry/error handling and run all Watsonx tests**

Factor the streaming request into `_stream_once(messages, refreshed=False)` and
obtain its token with `self._tokens.get_token(force_refresh=refreshed)`. Enter
the stream manager under `with_hard_timeout`; if the initial response is 401,
close that manager, invalidate the token, and delegate once with
`refreshed=True`. A second 401 raises
`WatsonxApiError("chat_stream", 401, _safe_error_code(payload))`. Any other
non-200 status raises the same safe error type. Track whether a content delta
has been yielded; never retry a request after the first emitted delta. Wrap
`json.loads(payload)` in `try/except (TypeError, ValueError)` and raise
`WatsonxApiError("chat_stream", 502, "malformed_sse")` without retaining or
rendering the payload. Always execute the stream manager's `__aexit__` in a
`finally` block, including generator cancellation via `aclose()`.

```powershell
uv run pytest tests/test_watsonx.py -q
uv run ruff check app/watsonx.py tests/test_watsonx.py
```

Expected: all tests and lint pass.

- [ ] **Step 7: Commit streaming support**

```powershell
git add apps/rag/app/watsonx.py apps/rag/tests/test_watsonx.py
git commit -m "feat(rag): stream watsonx chat completions"
```

---

### Task 4: Ordered tertiary routing, breakers, and provider budget

**Files:**
- Modify: `apps/rag/app/providers.py`
- Modify: `apps/rag/app/reliability.py`
- Modify: `apps/rag/app/provider_bucket.py`
- Modify: `apps/rag/tests/test_providers.py`
- Modify: `apps/rag/tests/test_reliability.py`
- Modify: `apps/rag/tests/test_provider_bucket.py`

**Interfaces:**
- Consumes: `WatsonxChatClient`, `Settings`, existing wrappers.
- Produces: `ModelRouter.fallbacks: list[ChatModelClient]`, `ReliabilityRouter.fallbacks: list[ReliableChatClient]`, ordered candidate selection.

- [ ] **Step 1: Write failing provider-construction tests**

```python
def test_build_router_adds_watsonx_only_as_fallback():
    settings = make_settings(
        ANTHROPIC_API_KEY="sk-ant-test",
        WATSONX_ENABLED="true",
        IBM_CLOUD_API_KEY="ibm-test",
        WATSONX_PROJECT_ID="project",
    )
    router = build_model_router(settings)

    assert isinstance(router.cheap, OpenAIChatClient)
    assert isinstance(router.strong, AnthropicChatClient)
    assert len(router.fallbacks) == 1
    assert isinstance(router.fallbacks[0], WatsonxChatClient)
    assert router.fallbacks[0].provider == "watsonx"


def test_watsonx_never_receives_normal_route():
    settings = make_settings(
        ANTHROPIC_API_KEY="sk-ant-test",
        WATSONX_ENABLED="true",
        IBM_CLOUD_API_KEY="ibm-test",
        WATSONX_PROJECT_ID="project",
    )
    router = build_model_router(settings)

    assert router.route(query="short", chunks=[chunk(0.9)])[0].provider == "openai"
    assert router.route(query="x" * 500, chunks=[chunk(0.9)])[0].provider == "anthropic"
```

- [ ] **Step 2: Run provider tests and verify RED**

```powershell
uv run pytest tests/test_providers.py -q
```

Expected: failure because `fallbacks` and Watsonx construction do not exist.

- [ ] **Step 3: Add the fallback collection and construction**

Change `ModelRouter.__init__` to accept:

```python
fallbacks: list[ChatModelClient] | None = None
```

and assign `self.fallbacks = list(fallbacks or [])`. In
`build_model_router()`, append `WatsonxChatClient(settings)` only when enabled,
wrap it with `ThrottledChatClient` alongside other clients, and pass the list to
`ModelRouter`. Do not change `route()`.

- [ ] **Step 4: Run provider tests and verify GREEN**

```powershell
uv run pytest tests/test_providers.py -q
```

Expected: all provider tests pass.

- [ ] **Step 5: Write failing three-provider reliability tests**

Define this `StubWatsonx` provider and prove exact order:

```python
class StubWatsonx(FlakyClient):
    model = "meta-llama/llama-4-maverick-17b-128e-instruct-fp8"
    provider = "watsonx"


async def test_two_normal_circuits_open_route_to_watsonx():
    settings = make_settings(BREAKER_FAILURE_THRESHOLD="1")
    redis = FakeRedis()
    breaker = CircuitBreaker(settings, redis=redis)
    inner = ModelRouter(
        settings,
        cheap=FlakyClient(),
        strong=StubStrong(),
        fallbacks=[StubWatsonx()],
    )
    router = ReliabilityRouter(inner, breaker=breaker, settings=settings)
    await breaker.record_failure("openai")
    await breaker.record_failure("anthropic")

    client, decision = await router.route(query="short", chunks=[])

    assert client.provider == "watsonx"
    assert decision["tier"] == "failover"
    assert decision["provider"] == "watsonx"


async def test_open_openai_prefers_anthropic_before_watsonx():
    settings = make_settings(BREAKER_FAILURE_THRESHOLD="1")
    redis = FakeRedis()
    breaker = CircuitBreaker(settings, redis=redis)
    inner = ModelRouter(
        settings,
        cheap=FlakyClient(),
        strong=StubStrong(),
        fallbacks=[StubWatsonx()],
    )
    router = ReliabilityRouter(inner, breaker=breaker, settings=settings)
    await breaker.record_failure("openai")

    client, decision = await router.route(query="short", chunks=[])

    assert client.provider == "anthropic"
    assert decision["tier"] == "failover"


async def test_open_watsonx_is_skipped_when_normal_alternate_is_available():
    settings = make_settings(BREAKER_FAILURE_THRESHOLD="1")
    redis = FakeRedis()
    breaker = CircuitBreaker(settings, redis=redis)
    inner = ModelRouter(
        settings,
        cheap=FlakyClient(),
        strong=StubStrong(),
        fallbacks=[StubWatsonx()],
    )
    router = ReliabilityRouter(inner, breaker=breaker, settings=settings)
    await breaker.record_failure("openai")
    await breaker.record_failure("watsonx")

    client, _ = await router.route(query="short", chunks=[])

    assert client.provider == "anthropic"


async def test_all_three_circuits_open_fails_fast():
    settings = make_settings(BREAKER_FAILURE_THRESHOLD="1")
    redis = FakeRedis()
    breaker = CircuitBreaker(settings, redis=redis)
    inner = ModelRouter(
        settings,
        cheap=FlakyClient(),
        strong=StubStrong(),
        fallbacks=[StubWatsonx()],
    )
    router = ReliabilityRouter(inner, breaker=breaker, settings=settings)
    for provider in ("openai", "anthropic", "watsonx"):
        await breaker.record_failure(provider)

    client, decision = await router.route(query="short", chunks=[])

    assert decision["reason"] == "all-breakers-open"
    with pytest.raises(BreakerOpenError):
        await client.complete([])
```

These tests retain the existing two-provider assertions and add only the
tertiary ordering contract.

- [ ] **Step 6: Run reliability tests and verify RED**

```powershell
uv run pytest tests/test_reliability.py -q
```

Expected: tertiary tests fail because `ReliabilityRouter` ignores fallbacks.

- [ ] **Step 7: Wrap and order fallback candidates**

In `ReliabilityRouter.__init__`:

```python
self.fallbacks = [
    ReliableChatClient(client, breaker=breaker, settings=settings)
    for client in inner.fallbacks
]
```

Build alternates in this order:

```python
alternates = [
    client
    for client in (self.cheap, self.strong, *self.fallbacks)
    if client is not None and client.provider != selected.provider
]
```

Keep the first allowed candidate and current decision/logging shape.

- [ ] **Step 8: Run reliability tests and verify GREEN**

```powershell
uv run pytest tests/test_reliability.py -q
```

Expected: all reliability tests pass.

- [ ] **Step 9: Write failing Watsonx RPM and unknown-provider tests**

```python
def test_bucket_uses_watsonx_specific_rpm(settings):
    config = settings(PROVIDER_RPM_WATSONX="17")
    assert ProviderBucket(config, FakeRedis())._rpm("watsonx") == 17


def test_bucket_rejects_unknown_provider(settings):
    with pytest.raises(ValueError, match="unknown provider"):
        ProviderBucket(settings(), FakeRedis())._rpm("mystery")
```

- [ ] **Step 10: Implement explicit provider-to-RPM mapping**

```python
def _rpm(self, provider: str) -> int:
    limits = {
        "openai": self._settings.provider_rpm_openai,
        "anthropic": self._settings.provider_rpm_anthropic,
        "watsonx": self._settings.provider_rpm_watsonx,
    }
    try:
        return limits[provider]
    except KeyError as exc:
        raise ValueError(f"unknown provider for request budget: {provider}") from exc
```

- [ ] **Step 11: Run focused routing/bucket tests and Ruff**

```powershell
uv run pytest tests/test_providers.py tests/test_reliability.py tests/test_provider_bucket.py -q
uv run ruff check app/providers.py app/reliability.py app/provider_bucket.py tests/test_providers.py tests/test_reliability.py tests/test_provider_bucket.py
```

Expected: both commands pass.

- [ ] **Step 12: Commit routing integration**

```powershell
git add apps/rag/app/providers.py apps/rag/app/reliability.py apps/rag/app/provider_bucket.py apps/rag/tests/test_providers.py apps/rag/tests/test_reliability.py apps/rag/tests/test_provider_bucket.py
git commit -m "feat(rag): add watsonx tertiary failover"
```

---

### Task 5: Health, masking, Docker, and operator documentation

**Files:**
- Modify: `apps/rag/app/health.py`
- Modify: `apps/rag/app/config_view.py`
- Modify: `apps/rag/tests/test_health.py`
- Modify: `apps/rag/tests/test_observability.py`
- Modify: `.env.example`
- Modify: `apps/rag/.env.example`
- Modify: `docker-compose.yml`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 2 IAM provider and Task 1 settings.
- Produces: cached `watsonx` health status, masked config output, deploy-time variable forwarding, operator instructions.

- [ ] **Step 1: Write failing Watsonx health tests**

Define tests proving disabled settings omit Watsonx and enabled settings probe it
once per cache interval. Inject a fake `WatsonxIamTokenProvider` and monkeypatch
`_probe_provider` so tests never use the network.

```python
async def test_watsonx_health_is_absent_when_disabled(monkeypatch):
    settings = Settings(_env_file=None, WATSONX_ENABLED="false", **REQUIRED_ENV)
    health._provider_cache.clear()

    results = await health.check_providers(settings)

    assert "watsonx" not in results


async def test_watsonx_health_is_cached_when_enabled(monkeypatch):
    settings = Settings(
        _env_file=None,
        WATSONX_ENABLED="true",
        IBM_CLOUD_API_KEY="ibm-test",
        WATSONX_PROJECT_ID="project",
        **REQUIRED_ENV,
    )
    health._provider_cache.clear()
    calls = {"tokens": 0, "probes": 0}

    class FakeTokens:
        async def get_token(self):
            calls["tokens"] += 1
            return "iam-token"

        async def aclose(self):
            return None

    async def fake_probe(name, url, headers, probe_timeout):
        calls["probes"] += 1
        assert name == "watsonx"
        assert "/ml/v1/foundation_model_specs" in url
        assert headers == {"authorization": "Bearer iam-token"}
        return "up"

    monkeypatch.setattr(health, "WatsonxIamTokenProvider", lambda _settings: FakeTokens())
    monkeypatch.setattr(health, "_probe_provider", fake_probe)
    first = await health.check_providers(settings)
    second = await health.check_providers(settings)
    assert first["watsonx"] == second["watsonx"] == "up"
    assert calls == {"tokens": 1, "probes": 1}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (WatsonxApiError("iam_token", 401, "invalid"), "unauthorized"),
        (WatsonxApiError("iam_token", 403, "forbidden"), "unauthorized"),
        (WatsonxApiError("iam_token", 500, "server_error"), "down"),
        (ConnectionError("offline"), "down"),
    ],
)
async def test_watsonx_health_maps_token_failures(monkeypatch, error, expected):
    settings = Settings(
        _env_file=None,
        WATSONX_ENABLED="true",
        IBM_CLOUD_API_KEY="ibm-test",
        WATSONX_PROJECT_ID="project",
        **REQUIRED_ENV,
    )
    health._provider_cache.clear()

    class FailingTokens:
        async def get_token(self):
            raise error

        async def aclose(self):
            return None

    monkeypatch.setattr(health, "WatsonxIamTokenProvider", lambda _settings: FailingTokens())

    results = await health.check_providers(settings)

    assert results["watsonx"] == expected
```

- [ ] **Step 2: Run health tests and verify RED**

```powershell
uv run pytest tests/test_health.py -q
```

Expected: Watsonx health assertions fail because no probe exists.

- [ ] **Step 3: Implement cached IAM/catalog health probing**

When `settings.watsonx_enabled`, check `_provider_cache["watsonx"]` before
acquiring a token. On cache miss, create a token provider, obtain a token, call
`_probe_provider` against:

```python
f"{settings.watsonx_url.rstrip('/')}/ml/v1/foundation_model_specs?version={settings.watsonx_api_version}&limit=1"
```

with `Authorization: Bearer <token>`, close the token provider in `finally`, and
store only `up`, `unauthorized`, or `down`. Catch `WatsonxApiError` and map
401/403 to `unauthorized`; map all other exceptions to `down`.

- [ ] **Step 4: Run health tests and verify GREEN**

```powershell
uv run pytest tests/test_health.py -q
```

Expected: all health tests pass.

- [ ] **Step 5: Write failing effective-config secret test**

Extend the existing secret fixture with `IBM_CLOUD_API_KEY="ibm-super-secret"`,
`WATSONX_PROJECT_ID="private-project"`, and enabled Watsonx settings. Assert
both raw values are absent from the response; assert both
`providers["ibm_cloud_api_key"]` and `providers["watsonx_project_id"]` have
`secret is True` and the masked value.

- [ ] **Step 6: Group Watsonx configuration and verify masking**

Add prefixes to `_GROUPS` before the fallback group:

```python
("ibm_cloud_api_key", "providers"),
("watsonx_", "providers"),
```

Run:

```powershell
uv run pytest tests/test_observability.py -q
```

Expected: all observability tests pass with no secret in response text.

- [ ] **Step 7: Document and forward exact environment variables**

Add this block to both example files without real values:

```dotenv
# Optional tertiary failover. Disabled means zero IBM calls.
WATSONX_ENABLED=false
# IBM_CLOUD_API_KEY=
# WATSONX_PROJECT_ID=
WATSONX_URL=https://jp-tok.ml.cloud.ibm.com
WATSONX_MODEL=meta-llama/llama-4-maverick-17b-128e-instruct-fp8
WATSONX_API_VERSION=2024-03-14
PROVIDER_RPM_WATSONX=60
```

Forward all seven values, including `IBM_CLOUD_API_KEY`, in both `rag` and
`rag-worker` environment sections. Defaults must keep Watsonx disabled.

- [ ] **Step 8: Update README deployment guidance**

Document that Watsonx is tertiary failover only, OpenAI embeddings remain
required, the Watsonx Runtime must be associated with the project, the service
ID must be a project collaborator, and production keys belong in a secret store.
Include the exact default model and Tokyo URL.

- [ ] **Step 9: Validate deployment files and focused tests**

```powershell
docker compose config --quiet
uv run pytest tests/test_config.py tests/test_health.py tests/test_observability.py -q
uv run ruff check app/config.py app/config_view.py app/health.py tests/test_config.py tests/test_health.py tests/test_observability.py
git diff --check
```

Expected: all commands exit 0. `docker compose config` must not print resolved
environment values because `--quiet` is used.

- [ ] **Step 10: Commit deployment and observability support**

```powershell
git add .env.example apps/rag/.env.example docker-compose.yml README.md apps/rag/app/health.py apps/rag/app/config_view.py apps/rag/tests/test_health.py apps/rag/tests/test_observability.py
git commit -m "feat(rag): operate watsonx fallback safely"
```

---

### Task 6: Full regression and live credential-safe verification

**Files:**
- Modify only if a failing verification exposes a tested defect in files from Tasks 1-5.

**Interfaces:**
- Consumes: complete Watsonx integration.
- Produces: evidence that automated behavior, deployment rendering, and live Tokyo inference work without persisting credentials.

- [ ] **Step 1: Run the complete focused provider suite**

```powershell
uv run pytest tests/test_config.py tests/test_watsonx.py tests/test_providers.py tests/test_reliability.py tests/test_provider_bucket.py tests/test_health.py tests/test_observability.py -q
```

Expected: all focused tests pass with no warnings or secret material.

- [ ] **Step 2: Run the complete RAG test suite**

```powershell
uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: Run full Ruff verification**

```powershell
uv run ruff check app tests
```

Expected: exit 0 with no diagnostics.

- [ ] **Step 4: Validate Docker rendering and diff hygiene**

```powershell
docker compose config --quiet
git diff --check
git status --short
```

Expected: Compose and diff checks pass; status contains only intentional files
if a post-commit correction is pending.

- [ ] **Step 5: Run a live smoke test with memory-only credentials**

Use the approved IBM key supplied in the active session to exchange an IAM
token, then send one bounded chat request through `WatsonxChatClient.complete`
with the following settings construction from `apps/rag`:

```python
settings = Settings(
    _env_file=None,
    **REQUIRED_ENV,
    WATSONX_ENABLED="true",
    IBM_CLOUD_API_KEY=os.environ["IBM_CLOUD_API_KEY"],
    WATSONX_PROJECT_ID=os.environ["WATSONX_PROJECT_ID"],
)
```

Output only HTTP/result status, model ID,
elapsed milliseconds, and token counts. Do not print the key, IAM token,
project ID, response headers, or raw error body. The assertion is:

```python
assert text.strip()
assert usage is None or usage["totalTokens"] > 0
```

Expected: a non-empty response from Llama 4 Maverick through the new production
client and mapped usage values.

- [ ] **Step 6: Confirm no secret persistence**

```powershell
git status --short
git ls-files .env apps/rag/.env
```

Expected: neither local `.env` file is tracked and no credential-bearing file is
staged. Search tracked files for the known environment-variable names, not for
the secret value; examples may contain only empty example assignments.

- [ ] **Step 7: Commit any verified correction**

If verification required a test-first correction, stage only those intentional
files and commit:

```powershell
git commit -m "fix(rag): harden watsonx provider verification"
```

If no correction was required, do not create an empty commit.

- [ ] **Step 8: Prepare handoff**

Report exact test counts, live smoke result, default routing order, configuration
names, commits created, and the requirement to rotate the chat-disclosed key
before setting `WATSONX_ENABLED=true` in production.

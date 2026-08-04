import asyncio

import pytest

from app.config import Settings
from app.watsonx import WatsonxApiError, WatsonxChatClient, WatsonxIamTokenProvider
from tests.conftest import REQUIRED_ENV


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


@pytest.fixture
def watsonx_settings() -> Settings:
    return Settings(
        _env_file=None,
        WATSONX_ENABLED="true",
        IBM_CLOUD_API_KEY="ibm-test-key",
        WATSONX_PROJECT_ID="test-project",
        **REQUIRED_ENV,
    )


async def test_iam_token_is_cached_until_refresh_margin(watsonx_settings):
    http = FakeHttpClient(
        [FakeResponse(200, {"access_token": "token-1", "expires_in": 3600})]
    )
    tokens = WatsonxIamTokenProvider(
        watsonx_settings, client=http, clock=lambda: 1000.0
    )

    assert await tokens.get_token() == "token-1"
    assert await tokens.get_token() == "token-1"
    assert len(http.post_calls) == 1


async def test_concurrent_iam_callers_share_one_exchange(watsonx_settings):
    http = BlockingFakeHttpClient(
        FakeResponse(200, {"access_token": "token-1", "expires_in": 3600})
    )
    tokens = WatsonxIamTokenProvider(
        watsonx_settings, client=http, clock=lambda: 1000.0
    )

    first_task = asyncio.create_task(tokens.get_token())
    await http.started.wait()
    second_task = asyncio.create_task(tokens.get_token())
    http.release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert (first, second) == ("token-1", "token-1")
    assert len(http.post_calls) == 1


async def test_iam_token_refreshes_before_expiry(watsonx_settings):
    now = [1000.0]
    http = FakeHttpClient(
        [
            FakeResponse(200, {"access_token": "token-1", "expires_in": 120}),
            FakeResponse(200, {"access_token": "token-2", "expires_in": 120}),
        ]
    )
    tokens = WatsonxIamTokenProvider(
        watsonx_settings, client=http, clock=lambda: now[0]
    )

    assert await tokens.get_token() == "token-1"
    now[0] = 1061.0
    assert await tokens.get_token() == "token-2"
    assert len(http.post_calls) == 2


async def test_iam_error_uses_safe_code_only(watsonx_settings):
    http = FakeHttpClient(
        [
            FakeResponse(
                400,
                {
                    "errorCode": "BXNIM0415E",
                    "errorMessage": "ibm-test-key must never escape",
                },
            )
        ]
    )
    tokens = WatsonxIamTokenProvider(watsonx_settings, client=http)

    with pytest.raises(WatsonxApiError) as excinfo:
        await tokens.get_token()

    assert "status=400" in str(excinfo.value)
    assert "code=BXNIM0415E" in str(excinfo.value)
    assert "ibm-test-key" not in str(excinfo.value)


async def test_complete_maps_chat_and_usage(watsonx_settings):
    tokens = StubTokens(["token-1"])
    http = FakeHttpClient(
        [
            FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Grounded [1].",
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 4,
                        "total_tokens": 24,
                    },
                },
            )
        ]
    )
    client = WatsonxChatClient(watsonx_settings, client=http, tokens=tokens)

    text, usage = await client.complete(
        [{"role": "user", "content": "Question?"}]
    )

    assert text == "Grounded [1]."
    assert usage == {
        "promptTokens": 20,
        "completionTokens": 4,
        "totalTokens": 24,
    }
    sent = http.post_calls[0]["json"]
    assert sent["project_id"] == "test-project"
    assert sent["model_id"] == watsonx_settings.watsonx_model
    assert sent["messages"] == [{"role": "user", "content": "Question?"}]
    assert sent["temperature"] == 0
    assert sent["max_tokens"] == watsonx_settings.generation_max_tokens


async def test_complete_refreshes_once_on_401(watsonx_settings):
    tokens = StubTokens(["expired", "fresh"])
    http = FakeHttpClient(
        [
            FakeResponse(
                401, {"errors": [{"code": "authentication_token_not_valid"}]}
            ),
            FakeResponse(
                200,
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": None,
                },
            ),
        ]
    )
    client = WatsonxChatClient(watsonx_settings, client=http, tokens=tokens)

    assert await client.complete([{"role": "user", "content": "q"}]) == (
        "ok",
        None,
    )
    assert tokens.forced_refreshes == 1
    assert len(http.post_calls) == 2


async def test_complete_does_not_retry_second_401(watsonx_settings):
    tokens = StubTokens(["expired", "still-expired"])
    http = FakeHttpClient(
        [
            FakeResponse(401, {"errors": [{"code": "first"}]}),
            FakeResponse(401, {"errors": [{"code": "second"}]}),
        ]
    )
    client = WatsonxChatClient(watsonx_settings, client=http, tokens=tokens)

    with pytest.raises(WatsonxApiError) as excinfo:
        await client.complete([{"role": "user", "content": "q"}])

    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "second"
    assert tokens.forced_refreshes == 1
    assert len(http.post_calls) == 2


async def test_complete_rejects_empty_output(watsonx_settings):
    client = WatsonxChatClient(
        watsonx_settings,
        client=FakeHttpClient([FakeResponse(200, {"choices": [], "usage": None})]),
        tokens=StubTokens(["token"]),
    )

    with pytest.raises(RuntimeError, match="Watsonx returned an empty completion"):
        await client.complete([{"role": "user", "content": "q"}])


async def test_provider_error_never_exposes_response_details(watsonx_settings):
    sensitive = [
        "ibm-test-key",
        "iam-secret",
        "test-project",
        "user@example.test",
    ]
    http = FakeHttpClient(
        [
            FakeResponse(
                403,
                {
                    "errors": [
                        {
                            "code": "user_authorization_failed",
                            "message": " ".join(sensitive),
                        }
                    ]
                },
            )
        ]
    )
    client = WatsonxChatClient(
        watsonx_settings, client=http, tokens=StubTokens(["iam-secret"])
    )

    with pytest.raises(WatsonxApiError) as excinfo:
        await client.complete([{"role": "user", "content": "q"}])

    rendered = str(excinfo.value)
    assert "status=403" in rendered
    assert "code=user_authorization_failed" in rendered
    assert all(value not in rendered for value in sensitive)


class FakeStreamResponse:
    def __init__(
        self, status_code: int, lines: list[str], payload: dict | None = None
    ):
        self.status_code = status_code
        self._lines = lines
        self._payload = payload or {}
        self.closed = False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    def json(self) -> dict:
        return self._payload


class FakeStreamManager:
    def __init__(self, response: FakeStreamResponse):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, traceback):
        self.response.closed = True


class FailingAfterDeltaResponse(FakeStreamResponse):
    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"partial"}}]}'
        raise ConnectionError("stream interrupted")


class FakeStreamingHttpClient(FakeHttpClient):
    def __init__(self, responses: list[FakeStreamResponse]):
        super().__init__([])
        self.stream_responses = list(responses)
        self.stream_calls: list[dict] = []

    def stream(self, method: str, url: str, **kwargs):
        self.stream_calls.append({"method": method, "url": url, **kwargs})
        return FakeStreamManager(self.stream_responses.pop(0))


async def test_stream_maps_sse_deltas_and_usage(watsonx_settings):
    response = FakeStreamResponse(
        200,
        [
            'data: {"choices":[{"delta":{"role":"assistant"}}]}',
            'data: {"choices":[{"delta":{"content":"Grounded "}}]}',
            'data: {"choices":[{"delta":{"content":"answer [1]."}}]}',
            (
                'data: {"choices":[],"usage":{"prompt_tokens":20,'
                '"completion_tokens":5,"total_tokens":25}}'
            ),
            "data: [DONE]",
        ],
    )
    http = FakeStreamingHttpClient([response])
    client = WatsonxChatClient(
        watsonx_settings, client=http, tokens=StubTokens(["token"])
    )

    items = [
        item
        async for item in client.stream([{"role": "user", "content": "q"}])
    ]

    assert items == [
        "Grounded ",
        "answer [1].",
        (
            "__usage__",
            {"promptTokens": 20, "completionTokens": 5, "totalTokens": 25},
        ),
    ]
    assert response.closed is True


async def test_stream_close_closes_provider_response(watsonx_settings):
    response = FakeStreamResponse(
        200,
        [
            'data: {"choices":[{"delta":{"content":"first"}}]}',
            'data: {"choices":[{"delta":{"content":"second"}}]}',
        ],
    )
    client = WatsonxChatClient(
        watsonx_settings,
        client=FakeStreamingHttpClient([response]),
        tokens=StubTokens(["token"]),
    )
    stream = client.stream([{"role": "user", "content": "q"}])

    assert await anext(stream) == "first"
    await stream.aclose()

    assert response.closed is True


async def test_stream_rejects_malformed_sse_without_echoing_payload(
    watsonx_settings,
):
    sensitive_payload = "not-json-iam-secret"
    response = FakeStreamResponse(200, [f"data: {sensitive_payload}"])
    client = WatsonxChatClient(
        watsonx_settings,
        client=FakeStreamingHttpClient([response]),
        tokens=StubTokens(["token"]),
    )

    with pytest.raises(WatsonxApiError) as excinfo:
        _ = [
            item
            async for item in client.stream(
                [{"role": "user", "content": "q"}]
            )
        ]

    assert excinfo.value.code == "malformed_sse"
    assert sensitive_payload not in str(excinfo.value)
    assert response.closed is True


async def test_stream_refreshes_once_on_401_before_deltas(watsonx_settings):
    denied = FakeStreamResponse(
        401, [], {"errors": [{"code": "authentication_token_not_valid"}]}
    )
    success = FakeStreamResponse(
        200, ['data: {"choices":[{"delta":{"content":"ok"}}]}']
    )
    tokens = StubTokens(["expired", "fresh"])
    http = FakeStreamingHttpClient([denied, success])
    client = WatsonxChatClient(watsonx_settings, client=http, tokens=tokens)

    items = [
        item
        async for item in client.stream([{"role": "user", "content": "q"}])
    ]

    assert items == ["ok", ("__usage__", None)]
    assert tokens.forced_refreshes == 1
    assert len(http.stream_calls) == 2
    assert denied.closed is True
    assert success.closed is True


async def test_stream_does_not_retry_second_401(watsonx_settings):
    first = FakeStreamResponse(401, [], {"errors": [{"code": "first"}]})
    second = FakeStreamResponse(401, [], {"errors": [{"code": "second"}]})
    tokens = StubTokens(["expired", "still-expired"])
    http = FakeStreamingHttpClient([first, second])
    client = WatsonxChatClient(watsonx_settings, client=http, tokens=tokens)

    with pytest.raises(WatsonxApiError) as excinfo:
        _ = [
            item
            async for item in client.stream(
                [{"role": "user", "content": "q"}]
            )
        ]

    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "second"
    assert tokens.forced_refreshes == 1
    assert len(http.stream_calls) == 2
    assert first.closed is True
    assert second.closed is True


async def test_stream_does_not_retry_after_emitting_delta(watsonx_settings):
    response = FailingAfterDeltaResponse(200, [])
    tokens = StubTokens(["token"])
    http = FakeStreamingHttpClient([response])
    client = WatsonxChatClient(watsonx_settings, client=http, tokens=tokens)
    stream = client.stream([{"role": "user", "content": "q"}])

    assert await anext(stream) == "partial"
    with pytest.raises(ConnectionError, match="stream interrupted"):
        await anext(stream)

    assert len(http.stream_calls) == 1
    assert tokens.forced_refreshes == 0
    assert response.closed is True

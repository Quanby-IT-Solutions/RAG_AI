"""Credential-safe async IBM watsonx.ai chat integration.

The module uses IBM Cloud IAM API-key exchange and the regional watsonx REST
surface directly. Provider response bodies are never rendered in exceptions.
"""

import asyncio
import re
import time
from typing import Any

import httpx

from app.config import Settings
from app.timeouts import with_hard_timeout

_IAM_TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"
_SAFE_ERROR_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")


class WatsonxApiError(RuntimeError):
    """A sanitized IBM failure containing no response body or credentials."""

    def __init__(
        self, operation: str, status_code: int, code: str = "unknown"
    ) -> None:
        super().__init__(
            f"Watsonx {operation} failed (status={status_code}, code={code})"
        )
        self.status_code = status_code
        self.code = code


def _safe_response_payload(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except (httpx.ResponseNotRead, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_code_value(value: Any) -> str:
    rendered = str(value or "unknown")
    return rendered if _SAFE_ERROR_CODE.fullmatch(rendered) else "unknown"


def _safe_error_code(payload: dict[str, Any]) -> str:
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        nested_code = errors[0].get("code")
        if nested_code:
            return _safe_code_value(nested_code)
    return _safe_code_value(payload.get("errorCode") or payload.get("error"))


class WatsonxIamTokenProvider:
    """Concurrency-safe, refresh-margin-aware IBM IAM token cache."""

    _REFRESH_MARGIN_SECONDS = 60.0

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        clock: Any = time.time,
    ) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=None)
        self._owns_client = client is None
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def _api_key(self) -> str:
        api_key = self._settings.ibm_cloud_api_key
        if api_key is None:
            raise RuntimeError("Watsonx IAM requested while Watsonx is disabled")
        return api_key.get_secret_value()

    async def get_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._token and self._clock() < self._expires_at:
            return self._token
        async with self._lock:
            if not force_refresh and self._token and self._clock() < self._expires_at:
                return self._token
            response = await with_hard_timeout(
                self._client.post(
                    _IAM_TOKEN_URL,
                    headers={"accept": "application/json"},
                    data={
                        "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                        "apikey": self._api_key(),
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
            try:
                expires_in = float(payload.get("expires_in") or 0)
            except (TypeError, ValueError):
                expires_in = 0.0
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


class WatsonxChatClient:
    """Watsonx chat client matching the application's provider protocol."""

    provider = "watsonx"

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        tokens: Any = None,
    ) -> None:
        self._settings = settings
        self._http = client or httpx.AsyncClient(timeout=None)
        self._owns_http = client is None
        self._tokens = tokens or WatsonxIamTokenProvider(
            settings, client=self._http
        )
        self.model = settings.watsonx_model

    def _url(self, path: str) -> str:
        base = self._settings.watsonx_url.rstrip("/")
        return f"{base}{path}?version={self._settings.watsonx_api_version}"

    def _body(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        project_id = self._settings.watsonx_project_id
        if project_id is None:
            raise RuntimeError("Watsonx chat requested while Watsonx is disabled")
        return {
            "project_id": project_id.get_secret_value(),
            "model_id": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self._settings.generation_max_tokens,
        }

    @staticmethod
    def _usage(usage: Any) -> dict[str, int] | None:
        if not isinstance(usage, dict):
            return None
        return {
            "promptTokens": int(usage.get("prompt_tokens") or 0),
            "completionTokens": int(usage.get("completion_tokens") or 0),
            "totalTokens": int(usage.get("total_tokens") or 0),
        }

    async def _post_chat(
        self, messages: list[dict[str, str]], *, refreshed: bool = False
    ) -> dict[str, Any]:
        token = await self._tokens.get_token(force_refresh=refreshed)
        response = await with_hard_timeout(
            self._http.post(
                self._url("/ml/v1/text/chat"),
                headers={
                    "accept": "application/json",
                    "authorization": f"Bearer {token}",
                    "content-type": "application/json",
                },
                json=self._body(messages),
            ),
            seconds=self._settings.external_call_timeout_seconds,
            operation="watsonx.chat",
        )
        payload = _safe_response_payload(response)
        if response.status_code == 401 and not refreshed:
            self._tokens.invalidate()
            return await self._post_chat(messages, refreshed=True)
        if response.status_code != 200:
            raise WatsonxApiError(
                "chat", response.status_code, _safe_error_code(payload)
            )
        return payload

    async def complete(
        self, messages: list[dict[str, str]]
    ) -> tuple[str, dict[str, int] | None]:
        payload = await self._post_chat(messages)
        choices = payload.get("choices")
        content: Any = None
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                content = message.get("content")
        text = content.strip() if isinstance(content, str) else ""
        if not text:
            raise RuntimeError(
                f"Watsonx returned an empty completion (model={self.model})"
            )
        return text, self._usage(payload.get("usage"))

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

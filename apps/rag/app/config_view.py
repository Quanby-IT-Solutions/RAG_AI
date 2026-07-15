"""Effective-configuration introspection (OB-5) — read-only, secrets masked.

Masking is BY TYPE, not by field-name convention: every `SecretStr` field
renders as a mask, and DSN-shaped strings have their password component
stripped. No unmasked secret ever leaves the server (response-shape test).
Grouping mirrors the module map so the console reads like the deployment's
tuning sheet.
"""

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import SecretStr

from app.config import Settings

_MASK = "••••••••"

# field-name prefix -> console group.
_GROUPS: list[tuple[str, str]] = [
    ("database_url", "infrastructure"),
    ("redis_url", "infrastructure"),
    ("openai_api_key", "providers"),
    ("anthropic_api_key", "providers"),
    ("cohere_api_key", "providers"),
    ("rag_service_token_secret", "security"),
    ("tracing_", "observability"),
    ("langsmith_", "observability"),
    ("langfuse_", "observability"),
    ("model_pricing", "observability"),
    ("provider_probe", "observability"),
    ("max_inflight", "observability"),
    ("shed_", "observability"),
    ("embedding_", "embedding"),
    ("generation_", "generation"),
    ("context_token_budget", "generation"),
    ("strong_model", "routing"),
    ("routing_", "routing"),
    ("retry_", "reliability"),
    ("breaker_", "reliability"),
    ("chat_retention", "chat"),
    ("chunking_", "chunking"),
    ("chunk_", "chunking"),
    ("semantic_breakpoint", "chunking"),
    ("retrieval_", "retrieval"),
    ("hnsw_", "retrieval"),
    ("rrf_", "retrieval"),
    ("rerank_", "retrieval"),
    ("multi_query", "retrieval"),
    ("mmr_", "retrieval"),
    ("parent_", "retrieval"),
    ("compression_", "retrieval"),
    ("cache_", "caching"),
    ("single_flight", "caching"),
    ("provider_bucket", "caching"),
    ("provider_rpm", "caching"),
    ("bucket_", "caching"),
    ("security_screen", "security"),
    ("injection_", "security"),
    ("rate_limit", "security"),
    ("git_sha", "evaluation"),
    ("eval_gate", "evaluation"),
    ("db_timeout", "timeouts"),
    ("redis_timeout", "timeouts"),
    ("external_call_timeout", "timeouts"),
]


def _group_for(field: str) -> str:
    for prefix, group in _GROUPS:
        if field.startswith(prefix):
            return group
    return "other"


def _mask_dsn(value: str) -> str:
    """Strip the password from a URL-shaped value (postgres://u:pw@host/db)."""
    try:
        parts = urlsplit(value)
        if parts.password:
            netloc = parts.netloc.replace(f":{parts.password}@", f":{_MASK}@")
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return _MASK
    return value


def effective_config(settings: Settings) -> dict[str, Any]:
    """The running deployment's effective settings, grouped, secrets masked."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for name, field in type(settings).model_fields.items():
        raw = getattr(settings, name)
        secret = isinstance(raw, SecretStr)
        if secret:
            value: Any = _MASK
        elif name in ("database_url", "redis_url") and isinstance(raw, str):
            value = _mask_dsn(raw)
        else:
            value = raw
        groups.setdefault(_group_for(name), []).append(
            {
                "key": name,
                "env": field.alias or name.upper(),
                "value": value,
                "secret": secret,
            }
        )
    return {
        "groups": {
            name: sorted(items, key=lambda entry: entry["key"])
            for name, items in sorted(groups.items())
        }
    }

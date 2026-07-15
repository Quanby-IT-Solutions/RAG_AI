"""Service JWT verification for all /internal/* routes (Tech Plan §5, D4).

The NestJS gateway mints a short-lived HS256 JWT signed with the shared
RAG_SERVICE_TOKEN_SECRET, carrying the acting user (`sub`), their effective
permissions, and correlation ids. Every internal route verifies signature +
expiry and extracts the principal; missing/invalid/expired tokens are 401.
"""

from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request


@dataclass(frozen=True)
class ServicePrincipal:
    """The authenticated principal extracted from the gateway service JWT."""

    sub: str
    permissions: tuple[str, ...]
    actor_type: str
    request_id: str | None
    trace_id: str | None


def verify_service_jwt(request: Request) -> ServicePrincipal:
    """FastAPI dependency: verify the bearer service JWT and return its principal."""
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Missing service bearer token")

    settings = request.app.state.settings
    try:
        payload = jwt.decode(
            token,
            settings.rag_service_token_secret.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Service token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid service token") from exc

    return ServicePrincipal(
        sub=str(payload["sub"]),
        permissions=tuple(str(p) for p in payload.get("permissions", [])),
        actor_type=str(payload.get("actorType", "service")),
        request_id=payload.get("requestId"),
        trace_id=payload.get("traceId"),
    )

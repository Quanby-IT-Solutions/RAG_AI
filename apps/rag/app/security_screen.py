"""Security screening ahead of everything (SC-1, Flow B stage 1).

Every inbound /chat query is sanitized, checked for prompt-injection
patterns, and PII-masked BEFORE cache lookup, retrieval, prompting,
persistence, tracing, or logging — the position is contractual: raw PII and
injected instructions must never propagate downstream (CP-1 cache keys are
computed AFTER masking, RA 10173 baseline per PRD).

All checks are local (regex) — no model call, negligible latency (asserted
in tests). Injection handling is configurable per deployment:
- "block" (default): the request is refused, audited, and traced
- "neutralize": the matched instruction is stripped and the query proceeds
"""

import logging
import re
from dataclasses import dataclass, field

from app.config import Settings

logger = logging.getLogger(__name__)

# ── prompt-injection heuristics ─────────────────────────────────────────────
_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "override-instructions",
        re.compile(
            r"(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|your\s+)?"
            r"(?:previous|prior|above|earlier|system)\s+(?:instructions?|prompts?|rules?)",
            re.IGNORECASE,
        ),
    ),
    (
        "reveal-prompt",
        re.compile(
            r"(?:reveal|show|print|repeat|output)\s+(?:me\s+)?(?:your|the)\s+"
            r"(?:system\s+)?(?:prompt|instructions?|rules?)",
            re.IGNORECASE,
        ),
    ),
    (
        "persona-override",
        re.compile(
            r"(?:you\s+are\s+now|act\s+as|pretend\s+(?:to\s+be|you\s+are))\s+"
            r"(?:an?\s+)?(?:unrestricted|unfiltered|jailbroken|developer\s+mode|dan\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "role-injection",
        re.compile(r"^\s*(?:system|assistant)\s*:", re.IGNORECASE | re.MULTILINE),
    ),
]

# ── PII detectors (RA 10173 baseline: emails, phones, PH id formats) ────────
_PII_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("email", "[EMAIL]", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    # PH mobile (+639XXXXXXXXX / 09XXXXXXXXX) and general intl numbers.
    ("phone", "[PHONE]", re.compile(r"(?<!\d)(?:\+?63|0)9\d{2}[\s-]?\d{3}[\s-]?\d{4}(?!\d)")),
    (
        "phone",
        "[PHONE]",
        re.compile(r"(?<!\d)\+\d{1,3}[\s-]?\d{3}[\s-]?\d{3,4}[\s-]?\d{3,4}(?!\d)"),
    ),
    # PhilSys national ID: 4-4-4-4 (16 digits, dashed or spaced).
    ("ph-national-id", "[PH_ID]", re.compile(r"(?<!\d)\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4}(?!\d)")),
    # TIN: 9 or 12 digits grouped in threes.
    ("ph-tin", "[PH_TIN]", re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}(?:-\d{3})?(?!\d)")),
    # SSS: 2-7-1.
    ("ph-sss", "[PH_SSS]", re.compile(r"(?<!\d)\d{2}-\d{7}-\d(?!\d)")),
]

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class ScreenResult:
    """Outcome of the Flow B stage-1 screen."""

    query: str
    blocked: bool = False
    injection: str | None = None
    injection_action: str | None = None
    pii_types: list[str] = field(default_factory=list)

    def trace(self) -> dict[str, object]:
        return {
            "blocked": self.blocked,
            "injection": self.injection,
            "injectionAction": self.injection_action,
            "piiTypes": self.pii_types,
        }


def sanitize(text: str) -> str:
    """Strip control characters and collapse runs of whitespace."""
    cleaned = _CONTROL_CHARS.sub(" ", text)
    return re.sub(r"[ \t\f\v]+", " ", cleaned).strip()


def detect_injection(text: str) -> tuple[str, re.Pattern[str]] | None:
    for name, pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return name, pattern
    return None


def mask_pii(text: str) -> tuple[str, list[str]]:
    """Replace detected PII with typed placeholders; returns found types."""
    found: list[str] = []
    for name, placeholder, pattern in _PII_PATTERNS:
        text, count = pattern.subn(placeholder, text)
        if count and name not in found:
            found.append(name)
    return text, found


def screen_query(text: str, settings: Settings) -> ScreenResult:
    """Sanitize → injection check → PII mask, in that order. The returned
    query is the ONLY form that may reach cache keys, retrieval, prompts,
    persistence, or logs."""
    if not settings.security_screen_enabled:
        return ScreenResult(query=text)

    cleaned = sanitize(text)

    injection = detect_injection(cleaned)
    injection_name: str | None = None
    action: str | None = None
    blocked = False
    if injection is not None:
        injection_name, pattern = injection
        action = settings.injection_action
        if action == "block":
            blocked = True
        else:  # neutralize: strip the matched instruction, keep the rest
            cleaned = sanitize(pattern.sub(" ", cleaned))

    masked, pii_types = mask_pii(cleaned)

    result = ScreenResult(
        query=masked,
        blocked=blocked,
        injection=injection_name,
        injection_action=action,
        pii_types=pii_types,
    )
    if blocked or injection_name or pii_types:
        logger.info("security.screen %s", result.trace())
    return result


class QueryBlockedError(Exception):
    """Raised when the security screen blocks a query (audited by caller)."""

    def __init__(self, injection: str) -> None:
        super().__init__(f"Query blocked by security screening ({injection})")
        self.injection = injection

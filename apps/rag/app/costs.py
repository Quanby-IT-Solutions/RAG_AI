"""Model pricing + per-request cost computation (OB-1).

Cost appears on the model span, the chat payload, and the audit record so a
bad answer is diagnosable WITH its spend. Prices are USD per 1M tokens and
overridable per deployment via MODEL_PRICING_JSON (partial overrides merge).
"""

import json
import logging

from app.config import Settings

logger = logging.getLogger(__name__)

# USD per 1M tokens: (input, output).
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "text-embedding-3-large": (0.13, 0.0),
}


def pricing_table(settings: Settings) -> dict[str, tuple[float, float]]:
    table = dict(DEFAULT_PRICING)
    if settings.model_pricing_json:
        try:
            overrides = json.loads(settings.model_pricing_json)
            for model, pair in overrides.items():
                table[model] = (float(pair[0]), float(pair[1]))
        except Exception:
            logger.warning("costs.invalid_pricing_json", exc_info=True)
    return table


def compute_cost_usd(
    settings: Settings, *, model: str, token_usage: dict[str, int] | None
) -> float | None:
    """USD cost of one model call; None when the model is unpriced or usage
    is unknown (never a silent 0 — unknown must look unknown)."""
    if not token_usage:
        return None
    prices = pricing_table(settings).get(model)
    if prices is None:
        return None
    input_price, output_price = prices
    prompt = token_usage.get("promptTokens", 0)
    completion = token_usage.get("completionTokens", 0)
    return round((prompt * input_price + completion * output_price) / 1_000_000, 8)

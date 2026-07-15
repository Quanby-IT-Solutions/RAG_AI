"""GN-2 budget tests: rank-then-select up to the hard cap (whole chunks only,
never over budget), first-overflow stop, env-override budget resolution (no
restart), and token counting fallbacks."""

import pytest

import app.budget as budget_module
from app.budget import (
    CONTEXT_TOKEN_BUDGET_ENV,
    count_tokens,
    current_context_budget,
    select_within_budget,
)
from app.config import Settings
from tests.conftest import REQUIRED_ENV


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, **REQUIRED_ENV)


@pytest.fixture
def fixed_token_counting(monkeypatch):
    """1 token per character — deterministic budgets regardless of tiktoken."""
    monkeypatch.setattr(budget_module, "count_tokens", lambda text: len(text))
    yield


def chunk(content: str, chunk_id: str) -> dict:
    return {"id": chunk_id, "document_id": "doc", "content": content, "similarity": 0.9}


# ----------------------------------------------------------- rank-then-select
def test_selects_in_rank_order_up_to_the_budget(fixed_token_counting):
    chunks = [chunk("a" * 40, "1"), chunk("b" * 40, "2"), chunk("c" * 40, "3")]

    used, record = select_within_budget(chunks, budget=90)

    assert [c["id"] for c in used] == ["1", "2"]  # rank order, third overflows
    assert record == {"budget": 90, "contextTokens": 80, "retrievedCount": 3, "usedCount": 2}
    assert all(c["context_tokens"] == 40 for c in used)


def test_never_exceeds_budget_and_never_truncates_mid_chunk(fixed_token_counting):
    chunks = [chunk("a" * 100, "1"), chunk("b" * 100, "2")]

    used, record = select_within_budget(chunks, budget=150)

    # Second chunk would exceed: it is dropped WHOLE, not truncated.
    assert [c["id"] for c in used] == ["1"]
    assert record["contextTokens"] == 100 <= 150
    assert used[0]["content"] == "a" * 100  # untouched


def test_selection_stops_at_first_overflow_never_skips_rank_order(fixed_token_counting):
    # Chunk 2 overflows; chunk 3 WOULD fit but must not jump the queue.
    chunks = [chunk("a" * 50, "1"), chunk("b" * 100, "2"), chunk("c" * 10, "3")]

    used, _ = select_within_budget(chunks, budget=70)

    assert [c["id"] for c in used] == ["1"]


def test_first_chunk_larger_than_budget_selects_nothing(fixed_token_counting):
    used, record = select_within_budget([chunk("a" * 500, "1")], budget=100)
    assert used == []
    assert record["usedCount"] == 0
    assert record["contextTokens"] == 0


def test_empty_retrieval_selects_nothing(fixed_token_counting):
    used, record = select_within_budget([], budget=100)
    assert used == []
    assert record == {"budget": 100, "contextTokens": 0, "retrievedCount": 0, "usedCount": 0}


# ------------------------------------------------- budget resolution (hot apply)
def test_env_override_applies_without_restart(settings, monkeypatch):
    assert current_context_budget(settings) == settings.context_token_budget == 2000

    monkeypatch.setenv(CONTEXT_TOKEN_BUDGET_ENV, "750")
    assert current_context_budget(settings) == 750  # next request sees it

    monkeypatch.delenv(CONTEXT_TOKEN_BUDGET_ENV)
    assert current_context_budget(settings) == 2000


def test_invalid_env_budget_is_ignored(settings, monkeypatch):
    monkeypatch.setenv(CONTEXT_TOKEN_BUDGET_ENV, "not-a-number")
    assert current_context_budget(settings) == 2000
    monkeypatch.setenv(CONTEXT_TOKEN_BUDGET_ENV, "0")
    assert current_context_budget(settings) == 2000


# ---------------------------------------------------------------- token counts
def test_count_tokens_falls_back_to_chars_over_four(monkeypatch):
    monkeypatch.setattr(budget_module, "_encoding", lambda: None)
    assert budget_module.count_tokens("x" * 40) == 10
    assert budget_module.count_tokens("") == 1  # floor of 1


def test_count_tokens_uses_tiktoken_when_available():
    class FakeEncoder:
        def encode(self, text: str) -> list[int]:
            return [0] * (len(text.split()))

    original = budget_module._encoder
    budget_module._encoder = FakeEncoder()
    try:
        assert count_tokens("three word phrase") == 3
    finally:
        budget_module._encoder = original

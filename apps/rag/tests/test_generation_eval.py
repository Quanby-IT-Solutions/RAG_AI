"""EV-3 generation-metrics tests."""

import json
from typing import Any

import pytest

from app.generation_eval import (
    EvalSpend,
    evaluate_faithfulness,
    evaluate_relevancy,
    generate_answer,
)
from tests.conftest import REQUIRED_ENV
from tests.test_evaluation import FakeEvalRepository, FakeRetrieval, golden


@pytest.fixture
def settings(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GIT_SHA", "sha-test")
    from app.config import Settings

    return Settings()


class ScriptedClient:
    """Returns queued responses; records every call with usage."""

    model = "gpt-4o-mini"

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages):
        self.calls.append(messages)
        return self.responses.pop(0), {
            "promptTokens": 10,
            "completionTokens": 5,
            "totalTokens": 15,
        }


# ------------------------------------------------------------- faithfulness
async def test_unsupported_claims_drop_faithfulness():
    # Seeded negative case (AC): one claim absent from the context.
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "claims": [
                        {"claim": "Vacation is 20 days.", "supported": True},
                        {"claim": "The CEO approves all leave.", "supported": False},
                    ]
                }
            )
        ]
    )
    spend = EvalSpend()

    score = await evaluate_faithfulness(
        client=client,
        answer="Vacation is 20 days and the CEO approves all leave.",
        contexts=["Vacation is 20 days."],
        spend=spend,
    )

    assert score == 0.5
    assert spend.total_tokens == 15


async def test_fully_supported_answer_scores_one():
    client = ScriptedClient(
        [json.dumps({"claims": [{"claim": "x", "supported": True}]})]
    )
    score = await evaluate_faithfulness(
        client=client, answer="x", contexts=["x"], spend=EvalSpend()
    )
    assert score == 1.0


async def test_evaluator_failure_returns_none_not_a_score():
    class Exploding:
        model = "m"

        async def complete(self, messages):
            raise RuntimeError("down")

    score = await evaluate_faithfulness(
        client=Exploding(), answer="a", contexts=["c"], spend=EvalSpend()
    )
    assert score is None


# ---------------------------------------------------------------- relevancy
async def test_relevancy_parses_and_clamps():
    fenced = '```json\n{"score": 1.7}\n```'
    client = ScriptedClient(['{"score": 0.8}', fenced])
    spend = EvalSpend()
    assert await evaluate_relevancy(
        client=client, question="q", answer="a", spend=spend
    ) == pytest.approx(0.8)
    assert (
        await evaluate_relevancy(client=client, question="q", answer="a", spend=spend)
        == 1.0
    )


# ------------------------------------------------------------- generate_answer
async def test_generate_answer_uses_the_grounded_prompt_and_tallies_spend():
    client = ScriptedClient(["Grounded answer [1]."])
    spend = EvalSpend()

    answer = await generate_answer(
        client=client,
        question="How much vacation?",
        chunks=[{"content": "Vacation is 20 days.", "metadata": {}}],
        spend=spend,
    )

    assert answer == "Grounded answer [1]."
    assert spend.calls == 1
    user_message = client.calls[0][-1]["content"]
    assert "Vacation is 20 days." in user_message
    assert "How much vacation?" in user_message


# --------------------------------------------------------- run integration
async def test_generation_run_aggregates_into_the_same_run_row(settings):
    from app.evaluation import run_retrieval_eval

    repository = FakeEvalRepository([golden("q1", ["h1"]), golden("q2", ["h2"])])
    retrieval = FakeRetrieval({"q1": ["h1"], "q2": ["h2"]})
    # Per item: generate, faithfulness, relevancy -> 6 scripted responses.
    client = ScriptedClient(
        [
            "Answer one [1].",
            json.dumps({"claims": [{"claim": "a", "supported": True}]}),
            '{"score": 1.0}',
            "Answer two [1].",
            json.dumps(
                {
                    "claims": [
                        {"claim": "b", "supported": True},
                        {"claim": "c", "supported": False},
                    ]
                }
            ),
            '{"score": 0.5}',
        ]
    )

    run = await run_retrieval_eval(
        settings=settings,
        retrieval=retrieval,
        repository=repository,
        triggered_by="u",
        generation=True,
        chat_client=client,
    )

    assert run["kind"] == "generation"
    assert run["config"]["generation"] is True
    assert run["config"]["generationModel"] == "gpt-4o-mini"
    # Retrieval + generation metrics live in the SAME row (AC).
    assert run["metrics"]["hitRate"] == 1.0
    assert run["metrics"]["faithfulness"] == pytest.approx(0.75)
    assert run["metrics"]["answerRelevancy"] == pytest.approx(0.75)
    # Eval spend tallied on the run, not on any user conversation.
    assert run["metrics"]["evalPromptTokens"] == 60.0
    assert run["metrics"]["evalCompletionTokens"] == 30.0
    entry: dict[str, Any] = run["perItem"][0]
    assert entry["answer"] == "Answer one [1]."
    assert entry["faithfulness"] == 1.0


async def test_generation_requires_a_chat_client(settings):
    from app.evaluation import EvalRunError, run_retrieval_eval

    with pytest.raises(EvalRunError, match="chat client"):
        await run_retrieval_eval(
            settings=settings,
            retrieval=FakeRetrieval({}),
            repository=FakeEvalRepository([golden("q1", ["h1"])]),
            triggered_by="u",
            generation=True,
        )

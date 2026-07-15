"""EV-2 retrieval metrics harness tests."""

from typing import Any

import pytest

from app.evaluation import (
    EvalRunError,
    aggregate_metrics,
    run_retrieval_eval,
    score_item,
)
from tests.conftest import REQUIRED_ENV


@pytest.fixture
def settings(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GIT_SHA", "sha-test")
    from app.config import Settings

    return Settings()


# ---------------------------------------------------------------- score_item
def test_perfect_retrieval_scores_ones():
    scores = score_item(["h1"], ["h1", "x", "y"], k=3)
    assert scores == {
        "hit": 1.0,
        "reciprocalRank": 1.0,
        "precisionAtK": 1 / 3,
        "recallAtK": 1.0,
    }


def test_miss_scores_zeros():
    scores = score_item(["h1", "h2"], ["a", "b", "c"], k=3)
    assert scores == {
        "hit": 0.0,
        "reciprocalRank": 0.0,
        "precisionAtK": 0.0,
        "recallAtK": 0.0,
    }


def test_reciprocal_rank_uses_first_relevant_position():
    scores = score_item(["h1"], ["a", "b", "h1", "c"], k=4)
    assert scores["reciprocalRank"] == pytest.approx(1 / 3)
    assert scores["hit"] == 1.0


def test_precision_and_recall_count_distinct_matches():
    # 2 of 3 expected found within k=4 -> P@k = 2/4, R@k = 2/3.
    scores = score_item(["h1", "h2", "h3"], ["h1", "x", "h2", "y"], k=4)
    assert scores["precisionAtK"] == pytest.approx(0.5)
    assert scores["recallAtK"] == pytest.approx(2 / 3)


def test_only_top_k_counts():
    # Relevant hash sits below the k cutoff -> miss.
    scores = score_item(["h1"], ["a", "b", "h1"], k=2)
    assert scores["hit"] == 0.0
    assert scores["reciprocalRank"] == 0.0


def test_aggregate_is_the_mean_per_metric():
    aggregate = aggregate_metrics(
        [
            {"hit": 1.0, "reciprocalRank": 1.0, "precisionAtK": 0.5, "recallAtK": 1.0},
            {"hit": 0.0, "reciprocalRank": 0.0, "precisionAtK": 0.0, "recallAtK": 0.0},
        ]
    )
    assert aggregate == {
        "hitRate": 0.5,
        "mrr": 0.5,
        "precisionAtK": 0.25,
        "recallAtK": 0.5,
    }


# ------------------------------------------------------------ run orchestration
class FakeEvalRepository:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.created: list[dict[str, Any]] = []
        self.finished: list[dict[str, Any]] = []

    async def fetch_active_golden_items(self):
        return self.items

    async def create_run(self, *, kind, git_sha, config, triggered_by):
        self.created.append(
            {"kind": kind, "gitSha": git_sha, "config": config, "triggeredBy": triggered_by}
        )
        return "run-1"

    async def finish_run(self, run_id, *, status, metrics, per_item, error):
        self.finished.append(
            {
                "runId": run_id,
                "status": status,
                "metrics": metrics,
                "perItem": per_item,
                "error": error,
            }
        )

    async def fetch_run(self, run_id):
        last = self.finished[-1]
        return {
            "id": run_id,
            "kind": self.created[-1]["kind"],
            "status": last["status"],
            "gitSha": self.created[-1]["gitSha"],
            "config": self.created[-1]["config"],
            "metrics": last["metrics"],
            "perItem": last["perItem"],
            "error": last["error"],
            "triggeredBy": self.created[-1]["triggeredBy"],
            "createdAt": "2026-07-16T00:00:00+00:00",
            "finishedAt": "2026-07-16T00:00:01+00:00",
        }


class FakeRetrieval:
    """Returns chunks whose content_hash comes from a per-question map."""

    def __init__(self, hashes_by_question: dict[str, list[str]]) -> None:
        self.hashes_by_question = hashes_by_question
        self.calls: list[dict[str, Any]] = []

    async def retrieve(
        self, query, *, k=None, vector_weight=None, keyword_weight=None, rerank=None
    ):
        self.calls.append(
            {"query": query, "k": k, "vw": vector_weight, "kw": keyword_weight, "rerank": rerank}
        )
        return [
            {"content_hash": h, "id": f"c-{i}", "document_id": "d", "content": "x"}
            for i, h in enumerate(self.hashes_by_question.get(query, []))
        ]


def golden(question: str, hashes: list[str], stale: list[str] | None = None):
    return {
        "id": f"g-{question}",
        "question": question,
        "idealAnswer": "ideal",
        "expectedChunkHashes": hashes,
        "staleHashes": stale or [],
    }


async def test_run_scores_items_and_persists_completed_run(settings):
    repository = FakeEvalRepository(
        [golden("q1", ["h1"]), golden("q2", ["h2"])]
    )
    retrieval = FakeRetrieval({"q1": ["h1", "x"], "q2": ["a", "b"]})

    run = await run_retrieval_eval(
        settings=settings,
        retrieval=retrieval,
        repository=repository,
        triggered_by="user-1",
        k=2,
    )

    assert run["status"] == "completed"
    assert run["gitSha"] == "sha-test"
    # Config snapshot records exactly what was evaluated (AC).
    assert run["config"]["k"] == 2
    assert run["config"]["rerank"] is False
    assert run["metrics"]["hitRate"] == 0.5
    assert run["metrics"]["mrr"] == 0.5
    assert run["metrics"]["itemCount"] == 2.0
    assert run["metrics"]["skippedStaleCount"] == 0.0
    assert retrieval.calls[0]["k"] == 2


async def test_weight_overrides_reach_retrieval_and_the_snapshot(settings):
    repository = FakeEvalRepository([golden("q1", ["h1"])])
    retrieval = FakeRetrieval({"q1": ["h1"]})

    run = await run_retrieval_eval(
        settings=settings,
        retrieval=retrieval,
        repository=repository,
        triggered_by="user-1",
        vector_weight=1.0,
        keyword_weight=0.0,
    )

    assert run["config"]["vectorWeight"] == 1.0
    assert run["config"]["keywordWeight"] == 0.0
    assert retrieval.calls[0]["vw"] == 1.0
    assert retrieval.calls[0]["kw"] == 0.0


async def test_stale_items_are_skipped_and_counted(settings):
    repository = FakeEvalRepository(
        [golden("q1", ["h1"]), golden("q2", ["gone"], stale=["gone"])]
    )
    retrieval = FakeRetrieval({"q1": ["h1"]})

    run = await run_retrieval_eval(
        settings=settings, retrieval=retrieval, repository=repository, triggered_by="u"
    )

    assert run["metrics"]["itemCount"] == 1.0
    assert run["metrics"]["skippedStaleCount"] == 1.0
    skipped = [entry for entry in run["perItem"] if entry["skipped"]]
    assert skipped[0]["staleHashes"] == ["gone"]
    # The stale item's question is never sent to retrieval.
    assert [call["query"] for call in retrieval.calls] == ["q1"]


async def test_empty_golden_set_fails_loud_without_creating_a_run(settings):
    repository = FakeEvalRepository([])
    with pytest.raises(EvalRunError, match="empty"):
        await run_retrieval_eval(
            settings=settings,
            retrieval=FakeRetrieval({}),
            repository=repository,
            triggered_by="u",
        )
    assert repository.created == []


async def test_fully_stale_set_records_a_failed_run_and_raises(settings):
    repository = FakeEvalRepository([golden("q1", ["gone"], stale=["gone"])])
    with pytest.raises(EvalRunError, match="stale"):
        await run_retrieval_eval(
            settings=settings,
            retrieval=FakeRetrieval({}),
            repository=repository,
            triggered_by="u",
        )
    assert repository.finished[-1]["status"] == "failed"


async def test_retrieval_exception_marks_the_run_failed_and_reraises(settings):
    class ExplodingRetrieval:
        async def retrieve(
            self, query, *, k=None, vector_weight=None, keyword_weight=None, rerank=None
        ):
            raise RuntimeError("boom")

    repository = FakeEvalRepository([golden("q1", ["h1"])])
    with pytest.raises(RuntimeError, match="boom"):
        await run_retrieval_eval(
            settings=settings,
            retrieval=ExplodingRetrieval(),
            repository=repository,
            triggered_by="u",
        )
    assert repository.finished[-1]["status"] == "failed"
    assert "boom" in repository.finished[-1]["error"]


# ------------------------------------------------------------- HTTP endpoint
@pytest.fixture
def eval_app(settings):
    from app.evaluation import get_eval_repository
    from app.main import create_app
    from app.retrieval import get_retrieval_service

    application = create_app(settings)
    repository = FakeEvalRepository([golden("q1", ["h1"])])
    retrieval = FakeRetrieval({"q1": ["h1"]})
    application.dependency_overrides[get_eval_repository] = lambda: repository
    application.dependency_overrides[get_retrieval_service] = lambda: retrieval
    return application, repository


async def _post_eval(app, *, token, json):
    import httpx

    headers = {"authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://rag") as client:
        return await client.post("/internal/eval/retrieval", headers=headers, json=json)


async def test_eval_endpoint_requires_service_jwt(eval_app):
    app, _ = eval_app
    response = await _post_eval(app, token=None, json={})
    assert response.status_code == 401


async def test_eval_endpoint_runs_and_returns_the_run(eval_app):
    from tests.test_ingest import make_token

    app, _ = eval_app
    response = await _post_eval(app, token=make_token(), json={"k": 3})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["metrics"]["hitRate"] == 1.0
    assert body["triggeredBy"] == "user-1"


async def test_eval_endpoint_maps_fail_loud_to_409(settings):
    from app.evaluation import get_eval_repository
    from app.main import create_app
    from app.retrieval import get_retrieval_service
    from tests.test_ingest import make_token

    application = create_app(settings)
    application.dependency_overrides[get_eval_repository] = lambda: FakeEvalRepository([])
    application.dependency_overrides[get_retrieval_service] = lambda: FakeRetrieval({})

    response = await _post_eval(application, token=make_token(), json={})
    assert response.status_code == 409
    assert "empty" in response.json()["detail"]

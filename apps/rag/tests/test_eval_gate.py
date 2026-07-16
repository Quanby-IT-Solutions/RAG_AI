"""EV-4 gate-comparison tests (Flow F)."""

from app.eval_gate import evaluate_gate, profile_identity, select_baseline_row


# ----------------------------------------------- CE-9 per-profile baselines
def _row(run_id: str, flags):
    return {
        "run_id": run_id,
        "config": {"profileFlags": flags} if flags is not None else {},
        "metrics": {"hitRate": 1.0},
        "git_sha": "x",
        "promoted_by": "u",
    }


BASELINE_FLAGS = {"contextual": False, "mode": "linear", "graph": False, "multimodal": False}
AGENTIC_FLAGS = {"contextual": True, "mode": "agentic", "graph": False, "multimodal": False}


def test_baseline_selection_matches_the_active_profile_identity():
    rows = [
        _row("newest-agentic", AGENTIC_FLAGS),
        _row("older-baseline", BASELINE_FLAGS),
    ]

    # Enabling agentic gates against the agentic baseline, never baseline's.
    assert select_baseline_row(rows, AGENTIC_FLAGS)["run_id"] == "newest-agentic"
    assert select_baseline_row(rows, BASELINE_FLAGS)["run_id"] == "older-baseline"


def test_no_matching_profile_baseline_returns_none():
    rows = [_row("agentic-only", AGENTIC_FLAGS)]
    assert select_baseline_row(rows, BASELINE_FLAGS) is None


def test_legacy_baseline_is_a_warned_fallback_only():
    rows = [
        _row("legacy", None),
        _row("agentic", AGENTIC_FLAGS),
    ]
    # A profile-matched row wins over a NEWER legacy row...
    assert select_baseline_row(rows, AGENTIC_FLAGS)["run_id"] == "agentic"
    # ...but a legacy row still gates profiles with no baseline of their own.
    assert select_baseline_row(rows, BASELINE_FLAGS)["run_id"] == "legacy"


def test_profile_identity_uses_resolved_flags(monkeypatch):
    from tests.conftest import REQUIRED_ENV

    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RAG_PROFILE", "agentic")
    from app.config import Settings

    identity = profile_identity(Settings())
    assert identity == AGENTIC_FLAGS


def test_gate_passes_when_all_metrics_hold_within_tolerance():
    result = evaluate_gate(
        candidate_metrics={"hitRate": 0.79, "mrr": 0.60, "itemCount": 5},
        baseline_metrics={"hitRate": 0.80, "mrr": 0.55},
        tolerance=0.02,
    )

    assert result["passed"] is True
    statuses = {entry["metric"]: entry["status"] for entry in result["comparisons"]}
    assert statuses == {"hitRate": "pass", "mrr": "pass"}


def test_gate_fails_when_any_metric_drops_below_baseline_minus_tolerance():
    result = evaluate_gate(
        candidate_metrics={"hitRate": 0.70, "mrr": 0.60},
        baseline_metrics={"hitRate": 0.80, "mrr": 0.55},
        tolerance=0.02,
    )

    assert result["passed"] is False
    report = {entry["metric"]: entry for entry in result["comparisons"]}
    assert report["hitRate"]["status"] == "FAIL"
    assert report["hitRate"]["delta"] == -0.10000000000000004 or report["hitRate"][
        "delta"
    ] < -0.09
    assert report["hitRate"]["floor"] == 0.78
    assert report["mrr"]["status"] == "pass"


def test_gate_only_compares_metrics_present_in_both_runs():
    # Baseline predates EV-3 -> faithfulness exists only on the candidate.
    result = evaluate_gate(
        candidate_metrics={"hitRate": 0.8, "faithfulness": 0.2},
        baseline_metrics={"hitRate": 0.8},
        tolerance=0.02,
    )

    assert result["passed"] is True
    assert [entry["metric"] for entry in result["comparisons"]] == ["hitRate"]


def test_gate_with_no_shared_metrics_is_not_a_pass():
    result = evaluate_gate(candidate_metrics={}, baseline_metrics={}, tolerance=0.02)
    assert result["passed"] is False
    assert result["comparisons"] == []


def test_generation_metrics_are_gated_when_shared():
    result = evaluate_gate(
        candidate_metrics={"faithfulness": 0.5, "answerRelevancy": 0.9},
        baseline_metrics={"faithfulness": 0.9, "answerRelevancy": 0.9},
        tolerance=0.02,
    )

    assert result["passed"] is False
    report = {entry["metric"]: entry for entry in result["comparisons"]}
    assert report["faithfulness"]["status"] == "FAIL"
    assert report["answerRelevancy"]["status"] == "pass"

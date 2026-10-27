"""Tests for the ReviewPilot benchmark framework (pr_agent.agent_exec.benchmark).

Uses a stub runner so no live LLM is required and the scoring is deterministic.
"""

import json
from pathlib import Path

import pytest

from pr_agent.agent_exec.benchmark import (
    BenchmarkRunner,
    ScoringWeights,
    _finding_matches,
    _title_overlap,
    load_dataset,
)
from pr_agent.agent_exec.models import Finding, RiskLevel


class _StubState:
    def __init__(self, findings, final_answer="done", tool_results=None, trajectory=None):
        self.findings = findings
        self.final_answer = final_answer
        self.tool_results = tool_results or []
        self.trajectory = trajectory or []


class _StubRunner:
    """Returns a scripted state per call so the scorer is deterministic."""

    def __init__(self, states_per_item):
        self._states = list(states_per_item)
        self.calls = 0

    async def run(self, diff_files):
        state = self._states[self.calls]
        self.calls += 1
        return state


def _f(file, line, title, risk="high"):
    return Finding(
        id=f"{file}:{line}",
        file_path=file,
        line_number=line,
        title=title,
        risk_level=RiskLevel(risk),
        confidence=0.8,
    )


def test_title_overlap_token_based():
    assert _title_overlap("Command injection", "command injection") == 1.0
    # Shared token "eval" -> overlap > 0.
    assert _title_overlap("use of eval", "eval in code") > 0.0
    assert _title_overlap("totally different", "completely unrelated") == 0.0


def test_finding_matches_requires_file_line_and_title_similarity():
    truth = _f("a.py", 3, "command injection")
    assert _finding_matches(_f("a.py", 3, "command injection"), truth)
    # Wrong file.
    assert not _finding_matches(_f("b.py", 3, "command injection"), truth)
    # Wrong line.
    assert not _finding_matches(_f("a.py", 4, "command injection"), truth)
    # Dissimilar title.
    assert not _finding_matches(_f("a.py", 3, "naming issue"), truth)


@pytest.mark.asyncio
async def test_benchmark_scores_perfect_precision_recall_when_matches():
    # One item, one ground-truth finding, agent reports exactly that finding.
    item_state = _StubState(findings=[_f("a.py", 1, "command injection")])
    runner = _StubRunner([item_state])

    from pr_agent.agent_exec.benchmark import BenchmarkItem

    items = [
        BenchmarkItem(
            id="pr-001",
            diff_files=[{"filename": "a.py", "patch": "+os.system('x')\n"}],
            ground_truth=[_f("a.py", 1, "command injection")],
        )
    ]

    report = await BenchmarkRunner(runner_factory=lambda: runner).run(items)

    assert report.n_items == 1
    assert report.precision == 1.0
    assert report.recall == 1.0
    assert report.completion_rate == 1.0
    # Weighted = 1.0 * 0.4 + 1.0 * 0.4 + 1.0 * 0.2 = 1.0
    assert report.weighted_score == 1.0


@pytest.mark.asyncio
async def test_benchmark_scores_zero_recall_when_agent_finds_nothing():
    # Agent reports no findings on a PR that has a ground-truth finding.
    # The stub still produces a final_answer, so completion=1.0; with the
    # default weights the weighted score = 0*0.4 + 0*0.4 + 1*0.2 = 0.2.
    runner = _StubRunner([_StubState(findings=[])])

    from pr_agent.agent_exec.benchmark import BenchmarkItem

    items = [
        BenchmarkItem(
            id="pr-002",
            diff_files=[{"filename": "a.py", "patch": "+os.system('x')\n"}],
            ground_truth=[_f("a.py", 1, "command injection")],
        )
    ]

    report = await BenchmarkRunner(runner_factory=lambda: runner).run(items)

    assert report.precision == 0.0
    assert report.recall == 0.0
    assert report.completion_rate == 1.0
    assert abs(report.weighted_score - 0.2) < 1e-9


@pytest.mark.asyncio
async def test_benchmark_aggregates_over_multiple_items_and_averages():
    # Two items: perfect on the first, zero on the second.
    runner = _StubRunner(
        [
            _StubState(findings=[_f("a.py", 1, "command injection")]),
            _StubState(findings=[]),
        ]
    )

    from pr_agent.agent_exec.benchmark import BenchmarkItem

    items = [
        BenchmarkItem(
            id="pr-001",
            diff_files=[{"filename": "a.py", "patch": "+os.system('x')\n"}],
            ground_truth=[_f("a.py", 1, "command injection")],
        ),
        BenchmarkItem(
            id="pr-002",
            diff_files=[{"filename": "b.py", "patch": "+eval(y)\n"}],
            ground_truth=[_f("b.py", 1, "use of eval")],
        ),
    ]

    report = await BenchmarkRunner(runner_factory=lambda: runner).run(items)

    assert report.n_items == 2
    # 1 correct out of 1 predicted -> precision 1.0; 1 correct out of 2 truth -> recall 0.5
    assert report.precision == 1.0
    assert report.recall == 0.5
    assert report.completion_rate == 1.0
    # weighted = 1.0*0.4 + 0.5*0.4 + 1.0*0.2 = 0.4 + 0.2 + 0.2 = 0.8
    assert abs(report.weighted_score - 0.8) < 1e-9


@pytest.mark.asyncio
async def test_benchmark_weights_are_configurable():
    runner = _StubRunner(
        [
            _StubState(findings=[_f("a.py", 1, "command injection")]),
            _StubState(findings=[]),
        ]
    )

    from pr_agent.agent_exec.benchmark import BenchmarkItem

    items = [
        BenchmarkItem(
            id="pr-001",
            diff_files=[],
            ground_truth=[_f("a.py", 1, "command injection")],
        ),
        BenchmarkItem(
            id="pr-002",
            diff_files=[],
            ground_truth=[_f("b.py", 1, "use of eval")],
        ),
    ]

    # Recall-only weighting: weighted score should equal recall (0.5).
    weights = ScoringWeights(precision=0.0, recall=1.0, completion=0.0)
    report = await BenchmarkRunner(runner_factory=lambda: runner, weights=weights).run(items)
    assert abs(report.weighted_score - report.recall) < 1e-9


def test_load_dataset_parses_json_file(tmp_path: Path):
    data = [
        {
            "id": "pr-001",
            "diff_files": [{"filename": "a.py", "patch": "+x = 1\n"}],
            "ground_truth": [
                {"file_path": "a.py", "line_number": 1, "title": "issue", "risk_level": "medium"}
            ],
        }
    ]
    p = tmp_path / "dataset.json"
    p.write_text(json.dumps(data), encoding="utf-8")

    items = load_dataset(p)

    assert len(items) == 1
    assert items[0].id == "pr-001"
    assert items[0].diff_files[0]["filename"] == "a.py"
    assert items[0].ground_truth[0].file_path == "a.py"
    assert items[0].ground_truth[0].risk_level == RiskLevel.MEDIUM

"""Tests for multi-subagent fan-out and merge (pr_agent.agent_exec.federation)."""

import pytest

from pr_agent.agent_exec.federation import (
    ReviewerFederation,
    merge_findings,
    partition_files,
    review_with_audit,
)
from pr_agent.agent_exec.models import Finding, RiskLevel, ToolResult, ToolStatus
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo


def _py_file(name: str, patch: str) -> FilePatchInfo:
    return FilePatchInfo(
        base_file=name,
        head_file=name,
        patch=patch,
        filename=name,
        edit_type=EDIT_TYPE.MODIFIED,
        language="python",
    )


def _finding(id, file, line, title, risk="medium", confidence=0.8, verified=None, note=None):
    return Finding(
        id=id,
        file_path=file,
        line_number=line,
        title=title,
        risk_level=RiskLevel(risk),
        confidence=confidence,
        verified=verified,
        verification_note=note,
    )


def test_partition_files_one_per_batch_by_default():
    files = [_py_file(f"f{i}.py", "+x\n") for i in range(3)]
    batches = partition_files(files)
    assert [len(b) for b in batches] == [1, 1, 1]


def test_partition_files_respects_max_files_per_batch():
    files = [_py_file(f"f{i}.py", "+x\n") for i in range(5)]
    batches = partition_files(files, max_files_per_batch=2)
    assert [len(b) for b in batches] == [2, 2, 1]


def test_partition_files_empty_input():
    assert partition_files([]) == []


def test_merge_findings_dedups_same_location_and_title():
    a = _finding("f1", "a.py", 3, "use of eval", risk="medium", confidence=0.5)
    b = _finding("f2", "a.py", 3, "use of eval", risk="medium", confidence=0.5)
    merged = merge_findings([[a], [b]])
    # Same file+title+line+risk: the duplicate is dropped, keeping the first.
    assert len(merged) == 1
    assert merged[0].id == "f1"


def test_merge_findings_keeps_different_locations_separately():
    a = _finding("f1", "a.py", 3, "issue A")
    b = _finding("f2", "b.py", 5, "issue B")
    merged = merge_findings([[a], [b]])
    assert {f.id for f in merged} == {"f1", "f2"}


def test_merge_findings_carries_verification_verdict_from_later_batch():
    a = _finding("f1", "a.py", 3, "command injection", risk="high", confidence=0.3, verified=None)
    b = _finding("f2", "a.py", 3, "command injection", risk="high", confidence=0.3, verified=True, note="confirmed")
    merged = merge_findings([[a], [b]])
    # The first is kept but the verification verdict from the second is carried.
    assert len(merged) == 1
    assert merged[0].verified is True
    assert merged[0].verification_note == "confirmed"


def test_merge_findings_is_deterministic_order():
    a = _finding("f1", "a.py", 1, "x")
    b = _finding("f2", "a.py", 2, "y")
    out1 = merge_findings([[a], [b]])
    out2 = merge_findings([[a], [b]])
    assert [f.id for f in out1] == [f.id for f in out2] == ["f1", "f2"]


class _StubState:
    """Minimal stand-in for AgentState exposing the fields the merger reads."""

    def __init__(self, findings):
        self.findings = findings
        self.audit_events = []
        self.trajectory = []
        self.tool_results = [
            ToolResult(tool_name="read_file_context", success=True, status=ToolStatus.COMPLETED, data={})
        ]


class _StubRunner:
    """Returns a scripted state per batch; records the batches it received."""

    def __init__(self, findings_by_file):
        self._findings_by_file = findings_by_file
        self.batches = []

    async def run(self, batch):
        self.batches.append(batch)
        # Return findings keyed by the first file name in the batch.
        key = batch[0].filename if batch else ""
        return _StubState(list(self._findings_by_file.get(key, [])))


@pytest.mark.asyncio
async def test_federation_runs_one_subagent_per_file_and_merges():
    files = [_py_file("a.py", "+x\n"), _py_file("b.py", "+y\n")]
    findings_by_file = {
        "a.py": [_finding("a1", "a.py", 1, "issue A")],
        "b.py": [_finding("b1", "b.py", 2, "issue B")],
    }
    stub_runner = _StubRunner(findings_by_file)
    fed = ReviewerFederation(runner_factory=lambda: stub_runner)

    findings = await fed.review(files)

    # Two sub-agents ran, one per file, and the merger combined their findings.
    assert len(stub_runner.batches) == 2
    assert {f.id for f in findings} == {"a1", "b1"}


@pytest.mark.asyncio
async def test_federation_dedups_across_subagents():
    files = [_py_file("a.py", "+x\n"), _py_file("b.py", "+y\n")]
    # Both sub-agents report the exact same location+title -> merged to one.
    findings_by_file = {
        "a.py": [_finding("a1", "shared.py", 3, "dup issue")],
        "b.py": [_finding("b1", "shared.py", 3, "dup issue")],
    }
    stub_runner = _StubRunner(findings_by_file)
    fed = ReviewerFederation(runner_factory=lambda: stub_runner)

    findings = await fed.review(files)

    assert len(findings) == 1


@pytest.mark.asyncio
async def test_federation_empty_diff_returns_empty():
    fed = ReviewerFederation(runner_factory=lambda: _StubRunner({}))
    assert await fed.review([]) == []


@pytest.mark.asyncio
async def test_review_with_audit_aggregates_across_subagents():
    files = [_py_file("a.py", "+x\n"), _py_file("b.py", "+y\n")]
    findings_by_file = {
        "a.py": [_finding("a1", "a.py", 1, "issue A")],
        "b.py": [_finding("b1", "b.py", 2, "issue B")],
    }
    stub_runner = _StubRunner(findings_by_file)

    findings, audits, trajectories, tool_results = await review_with_audit(
        files, runner_factory=lambda: stub_runner
    )

    assert {f.id for f in findings} == {"a1", "b1"}
    # One audit per sub-agent (empty in stub) + one tool result per sub-agent.
    assert len(tool_results) == 2

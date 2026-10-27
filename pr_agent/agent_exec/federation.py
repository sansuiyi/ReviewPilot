"""Multi-subagent fan-out and merge for the ReviewPilot agent loop.

A ``ReviewerFederation`` partitions the PR diff files into per-file batches and
runs one bounded :class:`~pr_agent.agent_exec.runner.ReviewAgentRunner` per batch
concurrently. The per-file findings are then merged with deterministic
deduplication so the same issue reported by two sub-agents is published once.

The federation reuses the existing agent loop, so every sub-agent still honors
the harness budgets (step/tool), the whitelist, loop detection and the Jev
verification rule. The merger is intentionally simple and deterministic so the
aggregate result is replayable and unit-testable.
"""

import asyncio
from typing import Any, List

from pr_agent.agent_exec.models import Finding, ToolResult
from pr_agent.agent_exec.runner import ReviewAgentRunner


# A stable key for "this finding points at the same location+title as another".
# file_path may be None; line_number may be None; both are normalized to "".
def _finding_key(f: Finding) -> tuple[str, str, str, str]:
    return (
        (f.file_path or "").strip(),
        (f.title or "").strip().lower(),
        str(f.line_number) if f.line_number is not None else "",
        f.risk_level.value,
    )


def merge_findings(per_batch: List[List[Finding]]) -> List[Finding]:
    """Merge per-subagent findings with deterministic dedup.

    Dedup keeps the first occurrence of each ``(file_path, title, line, risk)``
    key but preserves a verified verdict from any sub-agent. The order follows
    the batch order then the in-batch order, so the merged list is stable.
    """
    seen: dict[tuple[str, str, str, str], Finding] = {}
    for batch in per_batch:
        for f in batch:
            key = _finding_key(f)
            if key not in seen:
                seen[key] = f
                continue
            # If a later sub-agent verified (or refuted) the same finding, carry
            # that verdict onto the kept record so verification work is not lost.
            kept = seen[key]
            if f.verified is not None and kept.verified is None:
                seen[key] = kept.model_copy(
                    update={
                        "verified": f.verified,
                        "verification_note": f.verification_note or kept.verification_note,
                    }
                )
    return list(seen.values())


def partition_files(diff_files: List[Any], max_files_per_batch: int = 1) -> List[List[Any]]:
    """Partition diff files into batches. Defaults to one batch per file."""
    if max_files_per_batch < 1:
        max_files_per_batch = 1
    return [diff_files[i : i + max_files_per_batch] for i in range(0, len(diff_files), max_files_per_batch)]


class ReviewerFederation:
    """Fan out one agent per file batch and merge the findings."""

    def __init__(self, runner_factory=None) -> None:
        # Default to the production ReviewAgentRunner; tests inject a stub.
        self._runner_factory = runner_factory or ReviewAgentRunner

    async def review(self, diff_files: List[Any], max_files_per_batch: int = 1) -> List[Finding]:
        """Run sub-agents concurrently and return the merged findings."""
        if not diff_files:
            return []
        batches = partition_files(diff_files, max_files_per_batch=max_files_per_batch)

        async def _run_batch(batch: List[Any]) -> List[Finding]:
            runner = self._runner_factory()
            state = await runner.run(batch)
            return list(getattr(state, "findings", []) or [])

        per_batch_findings = await asyncio.gather(*[_run_batch(b) for b in batches])
        return merge_findings(per_batch_findings)


# Aggregate audit/trajectory across sub-agents, for callers that want them.
async def review_with_audit(diff_files: List[Any], runner_factory=None, max_files_per_batch: int = 1):
    """Run the federation and return (findings, audit_events, trajectories)."""
    if not diff_files:
        return [], [], []
    batches = partition_files(diff_files, max_files_per_batch=max_files_per_batch)
    factory = runner_factory or ReviewAgentRunner

    async def _run_batch(batch: List[Any]):
        runner = factory()
        state = await runner.run(batch)
        return state

    states = await asyncio.gather(*[_run_batch(b) for b in batches])
    per_batch_findings = [list(getattr(s, "findings", []) or []) for s in states]
    findings = merge_findings(per_batch_findings)
    audit_events: List[Any] = []
    trajectories: List[Any] = []
    tool_results: List[ToolResult] = []
    for s in states:
        audit_events.extend(getattr(s, "audit_events", []) or [])
        trajectories.extend(getattr(s, "trajectory", []) or [])
        tool_results.extend(getattr(s, "tool_results", []) or [])
    return findings, audit_events, trajectories, tool_results

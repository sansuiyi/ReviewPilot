"""Reproducible benchmark harness for the ReviewPilot agent loop.

A small, dependency-light benchmark framework that scores the agent over a
fixed PR dataset against a ground-truth label set. The scorer computes, for a
given run over the dataset:

  - Precision: |correct findings reported| / |findings reported|
  - Recall:    |correct findings reported| / |ground-truth findings|
  - Task completion rate: |PRs that produced a non-empty final answer| / |PRs|
  - Average tool calls per PR
  - Average steps per PR
  - Average tokens (estimate) per PR
  - Weighted score: a single number combining precision, recall and completion
    with explicit weights (defaults: 0.4 / 0.4 / 0.2).

The dataset format is intentionally simple so it can be checked in as JSON and
loaded without any external service. Each item is::

    {
      "id": "pr-001",
      "diff_files": [{"filename": "a.py", "patch": "+os.system('rm -rf x')\n"}],
      "ground_truth": [
        {"file_path": "a.py", "line_number": 1, "title": "command injection",
         "risk_level": "high"}
      ]
    }

A finding counts as a "correct match" when file_path, line_number and a
normalized title match (case-insensitive, alphanumeric token overlap >= 0.5).
This keeps the scorer deterministic and unit-testable without a live LLM: the
harness accepts a ``runner_factory`` so tests inject a stub runner instead.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

from pr_agent.agent_exec.models import Finding, RiskLevel


@dataclass(frozen=True)
class BenchmarkItem:
    id: str
    diff_files: List[dict]
    ground_truth: List[Finding]


@dataclass
class ItemResult:
    item_id: str
    findings: List[Finding] = field(default_factory=list)
    tool_calls: int = 0
    steps: int = 0
    tokens_estimate: int = 0
    completed: bool = False


@dataclass
class BenchmarkReport:
    n_items: int
    precision: float
    recall: float
    completion_rate: float
    avg_tool_calls: float
    avg_steps: float
    avg_tokens: float
    weighted_score: float
    per_item: List[ItemResult] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "n_items": self.n_items,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "completion_rate": round(self.completion_rate, 4),
            "avg_tool_calls": round(self.avg_tool_calls, 4),
            "avg_steps": round(self.avg_steps, 4),
            "avg_tokens": round(self.avg_tokens, 4),
            "weighted_score": round(self.weighted_score, 4),
        }


@dataclass(frozen=True)
class ScoringWeights:
    precision: float = 0.4
    recall: float = 0.4
    completion: float = 0.2


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _title_tokens(title: str) -> set[str]:
    return set(_TOKEN_RE.findall((title or "").lower()))


def _title_overlap(a: str, b: str) -> float:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _finding_matches(predicted: Finding, truth: Finding) -> bool:
    if (predicted.file_path or "") != (truth.file_path or ""):
        return False
    if predicted.line_number != truth.line_number:
        return False
    if _title_overlap(predicted.title, truth.title) < 0.5:
        return False
    return True


def load_dataset(path: str | Path) -> List[BenchmarkItem]:
    """Load a benchmark dataset from a JSON file.

    The file is a list of items with the shape documented in the module
    docstring. Unknown keys are ignored so the dataset can evolve.
    """
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    items: List[BenchmarkItem] = []
    for entry in data:
        truths = []
        for gt in entry.get("ground_truth", []):
            truths.append(
                Finding(
                    id=gt.get("id", f"{entry['id']}-gt-{len(truths)+1}"),
                    file_path=gt.get("file_path"),
                    line_number=gt.get("line_number"),
                    title=gt.get("title", ""),
                    risk_level=RiskLevel(gt.get("risk_level", "medium")),
                )
            )
        items.append(
            BenchmarkItem(
                id=entry["id"],
                diff_files=entry.get("diff_files", []),
                ground_truth=truths,
            )
        )
    return items


class BenchmarkRunner:
    """Score an agent over a fixed dataset.

    ``runner_factory`` builds the ReviewPilot runner per item; tests inject a
    stub so no live LLM is needed. ``diff_files_builder`` turns a dataset
    ``diff_files`` list into the objects the runner expects; the default builds
    lightweight FilePatchInfo objects, matching the agent's tool contract.
    """

    def __init__(
        self,
        runner_factory: Callable[[], Any],
        diff_files_builder: Optional[Callable[[List[dict]], List[Any]]] = None,
        weights: Optional[ScoringWeights] = None,
    ) -> None:
        self._runner_factory = runner_factory
        self._build_diff_files = diff_files_builder or _default_diff_files_builder
        self._weights = weights or ScoringWeights()

    async def run(self, items: List[BenchmarkItem]) -> BenchmarkReport:
        per_item: List[ItemResult] = []
        total_tp = 0
        total_predicted = 0
        total_truth = 0

        for item in items:
            diff_files = self._build_diff_files(item.diff_files)
            runner = self._runner_factory()
            state = await runner.run(diff_files)
            findings = list(getattr(state, "findings", []) or [])
            final_answer = getattr(state, "final_answer", None)
            tool_results = list(getattr(state, "tool_results", []) or [])
            trajectory = list(getattr(state, "trajectory", []) or [])
            tokens = sum(int(getattr(t, "tokens_estimate", 0) or 0) for t in trajectory)
            steps = len({int(getattr(t, "step", 0)) for t in trajectory})
            completed = bool(final_answer)

            tp = 0
            for truth in item.ground_truth:
                if any(_finding_matches(p, truth) for p in findings):
                    tp += 1
            total_tp += tp
            total_predicted += len(findings)
            total_truth += len(item.ground_truth)

            per_item.append(
                ItemResult(
                    item_id=item.id,
                    findings=findings,
                    tool_calls=len(tool_results),
                    steps=steps,
                    tokens_estimate=tokens,
                    completed=completed,
                )
            )

        n = len(items)
        precision = total_tp / total_predicted if total_predicted else 0.0
        recall = total_tp / total_truth if total_truth else 0.0
        completion = sum(1 for r in per_item if r.completed) / n if n else 0.0
        avg_tools = sum(r.tool_calls for r in per_item) / n if n else 0.0
        avg_steps = sum(r.steps for r in per_item) / n if n else 0.0
        avg_tokens = sum(r.tokens_estimate for r in per_item) / n if n else 0.0
        w = self._weights
        weight_sum = w.precision + w.recall + w.completion
        weighted = (
            (precision * w.precision + recall * w.recall + completion * w.completion)
            / weight_sum
            if weight_sum > 0
            else 0.0
        )

        return BenchmarkReport(
            n_items=n,
            precision=precision,
            recall=recall,
            completion_rate=completion,
            avg_tool_calls=avg_tools,
            avg_steps=avg_steps,
            avg_tokens=avg_tokens,
            weighted_score=weighted,
            per_item=per_item,
        )


def _default_diff_files_builder(specs: List[dict]) -> List[Any]:
    """Build FilePatchInfo objects matching the agent's tool contract."""
    from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo

    out: List[Any] = []
    for spec in specs:
        out.append(
            FilePatchInfo(
                base_file=spec.get("filename", "file"),
                head_file=spec.get("filename", "file"),
                patch=spec.get("patch", ""),
                filename=spec.get("filename", "file"),
                edit_type=EDIT_TYPE.MODIFIED,
                language=spec.get("language", "python"),
            )
        )
    return out

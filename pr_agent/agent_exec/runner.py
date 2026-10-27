"""Independent entry point for the ReviewPilot agent loop.

The runner builds the tool registry from already-loaded PR diff files, assembles
the LangGraph, and invokes it. It stays separate from ``PRReviewer`` so the
existing ``/review`` command is untouched; enabling the agent is gated by the
``[agent_exec]`` ``enabled`` setting and by the caller choosing to use this
runner.
"""

from typing import Any, List, Optional

from pr_agent.agent_exec.graph import build_graph
from pr_agent.agent_exec.llm import AgentLLM
from pr_agent.agent_exec.models import AgentState
from pr_agent.agent_exec.policy import load_policy
from pr_agent.agent_exec.tools import (
    FindReferencesTool,
    ReadFileContextTool,
    RunStaticAnalysisTool,
    default_registry,
)
from pr_agent.config_loader import get_settings


def is_enabled() -> bool:
    """Return True when the ReviewPilot agent is explicitly enabled."""
    agent_exec = get_settings().get("agent_exec", {})
    return bool(agent_exec.get("enabled", False))


class ReviewAgentRunner:
    """Runs the bounded tool-use review loop over a PR diff."""

    def __init__(self, ai_handler_factory=None) -> None:
        self._llm = AgentLLM(ai_handler_factory)
        self._policy = load_policy()

    async def run(self, diff_files: List[Any], findings: Optional[List[Any]] = None) -> AgentState:
        """Execute the loop and return the final state (findings + final_answer)."""
        registry = default_registry(
            ReadFileContextTool(diff_files),
            FindReferencesTool(diff_files),
            RunStaticAnalysisTool(diff_files),
        )
        graph = build_graph(self._llm, registry, self._policy)

        initial: dict[str, Any] = {
            "messages": [],
            "findings": list(findings or []),
            "tool_results": [],
            "decision": None,
            "step": 0,
            "final_answer": None,
            "orientation": None,
            "decision_history": [],
            "audit_events": [],
            "trajectory": [],
        }

        result = await graph.ainvoke(
            initial,
            # Each agent step maps to several graph super-steps (orient + review
            # + tools/verify), so allow ~3x the step budget plus a small margin.
            config={"recursion_limit": self._policy.max_steps * 3 + 4},
        )
        return AgentState(**result)

"""LangGraph state graph for the ReviewPilot agent loop.

Builds the ``orient -> review -> {tools | verify | finalize} -> orient`` cycle
with conditional routing driven by the ``decision`` produced by the review node.
Each non-finalize branch returns to ``orient`` so the OODA cycle completes on
every iteration. The loop is bounded in server code by the step/tool-call budget
and loop detection enforced in the review node; the recursion limit is passed at
invoke time via ``config``.

``messages`` uses ``operator.add`` so nodes return only their *incremental*
message list and the graph concatenates them, keeping the trajectory auditable
without relying on LangGraph's tool-call pairing semantics. All other fields are
plain (overwritten) fields, so nodes return the full updated list.
"""

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from pr_agent.agent_exec.models import AuditEvent, Decision, Finding, ToolResult, TrajectoryStep
from pr_agent.agent_exec.nodes import finalize_node, orient_node, review_node, tools_node, verify_node
from pr_agent.agent_exec.policy import AgentPolicy
from pr_agent.agent_exec.tools.base import ToolRegistry


class AgentGraphState(TypedDict, total=False):
    messages: Annotated[List[Any], operator.add]
    findings: List[Finding]
    tool_results: List[ToolResult]
    decision: Optional[Decision]
    step: int
    final_answer: Optional[str]
    orientation: Optional[str]
    decision_history: List[str]
    audit_events: List[AuditEvent]
    trajectory: List[TrajectoryStep]


def _route(state: AgentGraphState) -> str:
    decision = state.get("decision")
    if decision is None:
        return "finalize"
    if decision.action == "tools":
        return "tools"
    if decision.action == "verify":
        return "verify"
    return "finalize"


def build_graph(llm: Any, registry: ToolRegistry, policy: AgentPolicy):
    """Assemble the compiled state graph.

    ``llm`` is an ``AgentLLM`` (or a mock exposing ``complete``); ``registry`` is
    the tool whitelist; ``policy`` supplies the server-enforced budgets.
    """

    async def _orient(state: AgentGraphState) -> Dict[str, Any]:
        from pr_agent.agent_exec.models import AgentState

        return await orient_node(AgentState(**state), policy)

    async def _review(state: AgentGraphState) -> Dict[str, Any]:
        from pr_agent.agent_exec.models import AgentState

        return await review_node(AgentState(**state), llm, policy)

    async def _tools(state: AgentGraphState) -> Dict[str, Any]:
        from pr_agent.agent_exec.models import AgentState

        return await tools_node(AgentState(**state), registry, policy)

    async def _verify(state: AgentGraphState) -> Dict[str, Any]:
        from pr_agent.agent_exec.models import AgentState

        return await verify_node(AgentState(**state), llm)

    async def _finalize(state: AgentGraphState) -> Dict[str, Any]:
        from pr_agent.agent_exec.models import AgentState

        return await finalize_node(AgentState(**state))

    graph = StateGraph(AgentGraphState)

    graph.add_node("orient", _orient)
    graph.add_node("review", _review)
    graph.add_node("tools", _tools)
    graph.add_node("verify", _verify)
    graph.add_node("finalize", _finalize)

    graph.add_edge(START, "orient")
    graph.add_edge("orient", "review")
    graph.add_conditional_edges("review", _route, {"tools": "tools", "verify": "verify", "finalize": "finalize"})
    graph.add_edge("tools", "orient")
    graph.add_edge("verify", "orient")
    graph.add_edge("finalize", END)

    return graph.compile()

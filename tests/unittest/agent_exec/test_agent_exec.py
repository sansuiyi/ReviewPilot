"""Unit tests for the ReviewPilot agent loop (pr_agent/agent_exec).

These tests use a mock AI handler and mock ``FilePatchInfo`` objects, so no real
API key or network is required. They verify the core property of the upgrade:
the model decides the next step based on tool results, and the loop stops under
bounded budgets while distinguishing tool failure from "found nothing".
"""

import json

import pytest

from pr_agent.agent_exec.models import ToolStatus
from pr_agent.agent_exec.policy import AgentPolicy
from pr_agent.agent_exec.tools import (
    FindReferencesTool,
    ReadFileContextTool,
    RunStaticAnalysisTool,
)
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo


def _py_file(patch: str) -> FilePatchInfo:
    return FilePatchInfo(
        base_file="a.py",
        head_file="a.py",
        patch=patch,
        filename="a.py",
        edit_type=EDIT_TYPE.MODIFIED,
        language="python",
    )


class MockHandler:
    """A mock AI handler exposing the ``chat_completion`` contract.

    ``chat_completion`` returns ``(content, finish_reason)`` like the real
    handlers, sourced from a scripted queue of content strings.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
        self.calls.append((model, system, user))
        return self._responses.pop(0), "stop"


class MockHandlerFactory:
    def __init__(self, responses):
        self._responses = list(responses)
        self.handlers = []

    def __call__(self):
        handler = MockHandler(self._responses)
        self.handlers.append(handler)
        return handler


def _decision_json(action, **kwargs):
    payload = {"action": action, "reasoning": "test", **kwargs}
    return json.dumps(payload)


def test_finding_accepts_critical_and_mixed_case_risk_level():
    from pr_agent.agent_exec.models import Finding, RiskLevel

    # Real models emit "critical" (and arbitrary casing); they must survive
    # parsing instead of being silently dropped.
    f1 = Finding.model_validate({"id": "f1", "title": "t", "risk_level": "critical"})
    assert f1.risk_level is RiskLevel.CRITICAL
    f2 = Finding.model_validate({"id": "f2", "title": "t", "risk_level": "HIGH"})
    assert f2.risk_level is RiskLevel.HIGH
    f3 = Finding.model_validate({"id": "f3", "title": "t", "risk_level": " Critical "})
    assert f3.risk_level is RiskLevel.CRITICAL


@pytest.mark.asyncio
async def test_read_file_context_does_not_treat_deleted_lines_as_content():
    tool = ReadFileContextTool([_py_file("@@ -1,2 +1,2 @@\n-old\n+new\n")])
    result = await tool.execute({})
    assert result.success is True
    assert result.status is ToolStatus.COMPLETED
    assert result.data["files"][0]["patch"].startswith("@@")
    # Deleted lines are preserved as '-' lines, never presented as current content.
    assert "-old" in result.data["files"][0]["patch"]


@pytest.mark.asyncio
async def test_find_references_returns_explicit_empty_result():
    tool = FindReferencesTool([_py_file("+def foo():\n+    return 1\n")])
    result = await tool.execute({"symbol": "nonexistent_symbol"})
    assert result.success is True
    assert result.status is ToolStatus.COMPLETED
    assert result.data["references"] == []
    assert result.data["count"] == 0


@pytest.mark.asyncio
async def test_run_static_analysis_detects_dangerous_pattern():
    tool = RunStaticAnalysisTool([_py_file("+os.system('ls')\n")])
    result = await tool.execute({})
    assert result.status is ToolStatus.COMPLETED
    assert any(i["rule_id"] == "RP001" for i in result.data["issues"])


@pytest.mark.asyncio
async def test_tool_missing_argument_is_unsupported_not_clean():
    tool = FindReferencesTool([_py_file("+x = 1\n")])
    result = await tool.execute({})
    assert result.success is False
    assert result.status is ToolStatus.UNSUPPORTED
    assert "symbol" in result.error


@pytest.mark.asyncio
async def test_agent_loop_decides_based_on_tool_result():
    from pr_agent.agent_exec.runner import ReviewAgentRunner

    responses = [
        _decision_json("tools", tool_name="find_references", tool_arguments={"symbol": "foo"}),
        _decision_json("finalize"),
    ]
    factory = MockHandlerFactory(responses)
    runner = ReviewAgentRunner(ai_handler_factory=factory)

    state = await runner.run([_py_file("+def foo():\n+    return 1\n")])

    # The loop produced a final answer and recorded the tool call.
    assert state.final_answer is not None
    assert len(state.tool_results) == 1
    assert state.tool_results[0].tool_name == "find_references"
    # The mock handler was called twice: once to decide tools, once to finalize.
    assert len(factory.handlers) == 1
    assert len(factory.handlers[0].calls) == 2
    # Incremental-message fix: the trajectory must not duplicate earlier messages.
    assert len(state.messages) == 3  # assistant(tools) + tool + assistant(finalize)
    assert len({m["content"] for m in state.messages}) == len(state.messages)


@pytest.mark.asyncio
async def test_agent_loop_stops_on_tool_budget():
    from pr_agent.agent_exec.runner import ReviewAgentRunner

    responses = [_decision_json("tools", tool_name="read_file_context")]
    factory = MockHandlerFactory(responses)
    runner = ReviewAgentRunner(ai_handler_factory=factory)
    # A zero tool budget forces the decision to finalize without executing tools.
    runner._policy = AgentPolicy(max_steps=6, max_tool_calls=0)

    state = await runner.run([_py_file("+x = 1\n")])
    assert state.tool_results == []
    assert state.final_answer is not None


@pytest.mark.asyncio
async def test_review_node_extracts_findings_on_finalize():
    from pr_agent.agent_exec.runner import ReviewAgentRunner

    finding = {
        "id": "f1",
        "file_path": "a.py",
        "line_number": 3,
        "title": "use of eval",
        "risk_level": "medium",
        "confidence": 0.9,
    }
    responses = [_decision_json("finalize", findings=[finding])]
    factory = MockHandlerFactory(responses)
    runner = ReviewAgentRunner(ai_handler_factory=factory)

    state = await runner.run([_py_file("+x = eval(y)\n")])

    assert len(state.findings) == 1
    assert state.findings[0].title == "use of eval"
    assert "use of eval" in state.final_answer


@pytest.mark.asyncio
async def test_jev_forces_verify_for_high_risk_low_confidence():
    from pr_agent.agent_exec.runner import ReviewAgentRunner

    high_risk = {
        "id": "f1",
        "file_path": "a.py",
        "line_number": 1,
        "title": "command injection",
        "risk_level": "high",
        "confidence": 0.3,
    }
    responses = [
        _decision_json("finalize", findings=[high_risk]),
        '{"verified": true, "note": "confirmed"}',
        _decision_json("finalize"),
    ]
    factory = MockHandlerFactory(responses)
    runner = ReviewAgentRunner(ai_handler_factory=factory)

    state = await runner.run([_py_file("+os.system(cmd)\n")])

    # The Jev rule forced an independent verification pass before finalizing.
    assert state.findings[0].verified is True
    assert state.findings[0].verification_note == "confirmed"


@pytest.mark.asyncio
async def test_jev_forces_verify_for_critical_risk_low_confidence():
    from pr_agent.agent_exec.runner import ReviewAgentRunner

    critical_risk = {
        "id": "f1",
        "file_path": "a.py",
        "line_number": 1,
        "title": "remote code execution",
        "risk_level": "critical",
        "confidence": 0.4,
    }
    responses = [
        _decision_json("finalize", findings=[critical_risk]),
        '{"verified": true, "note": "confirmed"}',
        _decision_json("finalize"),
    ]
    factory = MockHandlerFactory(responses)
    runner = ReviewAgentRunner(ai_handler_factory=factory)

    state = await runner.run([_py_file("+eval(user_input)\n")])

    # Critical findings trigger Jev just like high-risk ones.
    assert state.findings[0].risk_level.value == "critical"
    assert state.findings[0].verified is True


@pytest.mark.asyncio
async def test_loop_detection_forces_finalize():
    from pr_agent.agent_exec.runner import ReviewAgentRunner

    repeated = _decision_json("tools", tool_name="find_references", tool_arguments={"symbol": "foo"})
    factory = MockHandlerFactory([repeated, repeated, repeated])
    runner = ReviewAgentRunner(ai_handler_factory=factory)
    runner._policy = AgentPolicy(max_steps=6, max_tool_calls=10, loop_detection_window=3)

    state = await runner.run([_py_file("+def foo():\n+    return 1\n")])

    # The loop executed the repeated tool twice, then detected the loop on the
    # third identical decision and forced finalize.
    assert len(state.tool_results) == 2
    assert state.final_answer is not None


@pytest.mark.asyncio
async def test_harness_refuses_unknown_tool_and_lets_model_retry():
    from pr_agent.agent_exec.runner import ReviewAgentRunner

    factory = MockHandlerFactory(
        [
            _decision_json("tools", tool_name="delete_all_files"),
            _decision_json("finalize"),
        ]
    )
    runner = ReviewAgentRunner(ai_handler_factory=factory)

    state = await runner.run([_py_file("+x = 1\n")])

    # The unknown tool is refused before execution; the refusal lists the
    # available names and is fed back so the model can self-correct.
    assert len(state.tool_results) == 1
    refusal = state.tool_results[0]
    assert refusal.status is ToolStatus.UNSUPPORTED
    assert "read_file_context" in refusal.error
    assert state.final_answer is not None


@pytest.mark.asyncio
async def test_trajectory_and_audit_recorded():
    from pr_agent.agent_exec.runner import ReviewAgentRunner

    responses = [
        _decision_json("tools", tool_name="read_file_context"),
        _decision_json("finalize"),
    ]
    factory = MockHandlerFactory(responses)
    runner = ReviewAgentRunner(ai_handler_factory=factory)

    state = await runner.run([_py_file("+x = 1\n")])

    node_names = {t.node for t in state.trajectory}
    assert {"orient", "review", "tools", "finalize"} <= node_names
    assert len(state.audit_events) == 1
    assert state.audit_events[0].tool_name == "read_file_context"
    assert state.audit_events[0].outcome == "completed"

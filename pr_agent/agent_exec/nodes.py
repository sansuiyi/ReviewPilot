"""LangGraph node implementations for the ReviewPilot agent loop.

OODA mapping:
  orient   -> Orient (deterministic environment summary, no LLM call)
  review   -> Observe + Decide (produces the next Decision and, on finalize, the findings)
  tools    -> Act (executes a whitelisted tool under harness budgets)
  verify   -> Act (independent LLM check on a low-confidence high-risk finding)
  finalize -> assemble the final review text

The harness (server code, not the model) enforces: the tool whitelist, the
per-tool timeout, the tool-call and step budgets, loop detection, and the Jev
rule that a high-risk low-confidence finding must be independently verified
before the loop is allowed to finalize.
"""

import asyncio
import json
import time
from typing import Any, Dict, List

from loguru import logger

from pr_agent.agent_exec.models import (
    AgentState,
    AuditEvent,
    Decision,
    Finding,
    RiskLevel,
    ToolResult,
    ToolStatus,
    TrajectoryStep,
)
from pr_agent.agent_exec.policy import AgentPolicy
from pr_agent.agent_exec.tools.base import ToolRegistry

_SYSTEM_PROMPT = (
    "You are a code-review agent inspecting a pull request. You may call tools to "
    "gather context before finalizing. Reply with a single JSON object using one "
    "of these shapes:\n"
    '{"action":"tools","tool_name":"<name>","tool_arguments":{...},"confidence":0.8,"reasoning":"..."}\n'
    '{"action":"verify","finding_id":"<id>","reasoning":"..."}\n'
    '{"action":"finalize","confidence":0.9,"reasoning":"...",'
    '"findings":[{"id":"f1","file_path":"a.py","line_number":3,"title":"...","description":"...",'
    '"risk_level":"high","confidence":0.5,"evidence":["..."]}]}\n'
    "On finalize, always include the complete list of findings you discovered. "
    "Choose verify only for a finding that is high risk AND low confidence. "
    "Choose finalize only when no further tool call would change your judgment. "
    "The pull-request diff itself is NOT included in the conversation: call the "
    "read_file_context tool first (path may be empty) to inspect the changed files."
)


def _extract_json(content: str) -> str:
    """Best-effort extraction of the first JSON object from model output."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _decision_signature(decision: Decision) -> str:
    """A compact, comparable signature used for loop detection."""
    args = json.dumps(decision.tool_arguments or {}, sort_keys=True)
    return f"{decision.action}|{decision.tool_name or ''}|{args}"


def _parse_findings(raw: Any) -> List[Finding]:
    """Parse model-provided findings, generating ids when the model omits them."""
    findings: List[Finding] = []
    for index, item in enumerate(raw or [], start=1):
        if isinstance(item, dict):
            if not item.get("id"):
                item = {**item, "id": f"finding-{index}"}
            try:
                findings.append(Finding.model_validate(item))
            except Exception as exc:
                # Skip a malformed finding rather than failing the whole step,
                # but leave a trace so the loss is diagnosable.
                logger.warning("dropped malformed finding #{}: {} ({})", index, item.get("title"), exc)
                continue
    return findings


async def orient_node(state: AgentState, policy: AgentPolicy) -> Dict[str, Any]:
    """Build a deterministic environment summary (OODA Orient).

    This is intentionally non-LLM: it distills the current state into a stable
    text block so the Decide step has a grounded view of files, findings, and
    remaining budget.
    """
    files = sorted({f.file_path for f in state.findings if f.file_path})
    budget_left = max(0, policy.max_tool_calls - len(state.tool_results))
    steps_left = max(0, policy.max_steps - state.step)

    parts = [
        "ENVIRONMENT SUMMARY (Orient)",
        f"step {state.step}/{policy.max_steps} (steps left: {steps_left})",
        f"tool calls used: {len(state.tool_results)}/{policy.max_tool_calls} (budget left: {budget_left})",
        f"findings so far: {len(state.findings)}",
        f"decision history: {state.decision_history[-policy.loop_detection_window:]}",
    ]
    if files:
        parts.append(f"files mentioned: {', '.join(files)}")
    orientation = "\n".join(parts)

    trajectory = list(state.trajectory) + [
        TrajectoryStep(step=state.step, node="orient", summary=orientation[:200])
    ]
    return {"orientation": orientation, "trajectory": trajectory}


async def review_node(state: AgentState, llm: Any, registry: ToolRegistry, policy: AgentPolicy) -> Dict[str, Any]:
    """Observe the environment and produce the next routing decision (Decide).

    Also extracts findings when the model finalizes, and applies server-side
    guardrails: step/tool budgets, loop detection, and the Jev rule that forces
    verification for high-risk low-confidence findings. An unknown tool name is
    not fatal: the tools node refuses it and the refusal is fed back so the
    model can self-correct on the next iteration (still budget-bounded).
    """
    orientation = state.orientation or "(no orientation)"
    system = (
        f"{_SYSTEM_PROMPT}\n\n"
        f"Available tools: {', '.join(sorted(registry.names()))}. Any other tool name is refused."
    )
    user = (
        f"{orientation}\n\n"
        f"Findings so far:\n{json.dumps([f.model_dump() for f in state.findings], ensure_ascii=False)}\n"
        f"Tool results so far:\n{json.dumps([r.model_dump() for r in state.tool_results], ensure_ascii=False)}\n"
        "Decide the next action."
    )
    content, _ = await llm.complete(system=system, user=user)

    decision = Decision(action="finalize", reasoning="unable to parse decision, finalizing")
    new_findings: List[Finding] = []
    try:
        payload = json.loads(_extract_json(content))
        if not isinstance(payload, dict):
            payload = {}
        raw_findings = payload.pop("findings", [])
        decision = Decision.model_validate(payload)
        if decision.action == "finalize":
            new_findings = _parse_findings(raw_findings)
    except Exception:
        decision = Decision(action="finalize", reasoning="unable to parse decision, finalizing")

    # --- Harness guardrails (server-side, not prompt-dependent) ---
    if decision.action == "tools":
        if state.step >= policy.max_steps:
            decision = Decision(action="finalize", reasoning="step budget exhausted")
        elif len(state.tool_results) >= policy.max_tool_calls:
            decision = Decision(action="finalize", reasoning="tool-call budget exhausted")
        # An unregistered tool is NOT finalized here: the tools node refuses it
        # with the available names and the loop gives the model a chance to
        # self-correct, bounded by the budgets above and loop detection.

    # Loop detection: force finalize when the last N decisions repeat.
    if decision.action != "finalize":
        signature = _decision_signature(decision)
        window = state.decision_history[-(policy.loop_detection_window - 1):] + [signature]
        if len(window) >= policy.loop_detection_window and len(set(window)) == 1:
            decision = Decision(action="finalize", reasoning="loop detected, forcing finalize")

    # Jev: a high-risk (or critical) low-confidence unverified finding must be
    # verified first.
    merged_findings = list(state.findings) + new_findings
    if decision.action == "finalize":
        for finding in merged_findings:
            high_risk = finding.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
            low_confidence = finding.confidence < policy.verify_confidence_threshold
            if high_risk and low_confidence and finding.verified is None:
                decision = Decision(
                    action="verify",
                    finding_id=finding.id,
                    reasoning="high-risk low-confidence finding requires verification",
                )
                break

    decision_history = list(state.decision_history) + [_decision_signature(decision)]
    trajectory = list(state.trajectory) + [
        TrajectoryStep(
            step=state.step,
            node="review",
            summary=f"decision={decision.action}",
            tokens_estimate=len(system) + len(user),
        )
    ]

    result: Dict[str, Any] = {
        "messages": [{"role": "assistant", "content": content}],
        "decision": decision,
        "step": state.step + 1,
        "decision_history": decision_history,
        "trajectory": trajectory,
    }
    if new_findings:
        result["findings"] = merged_findings
    return result


async def tools_node(state: AgentState, registry: ToolRegistry, policy: AgentPolicy) -> Dict[str, Any]:
    """Execute the selected tool under harness control (OODA Act).

    Enforces the whitelist, a per-tool timeout, and records an audit event so the
    execution history is trustworthy regardless of what the model claims.
    """
    decision = state.decision
    if decision is None:
        return {}

    tool_name = decision.tool_name or "unknown"
    start = time.perf_counter()

    # Whitelist enforcement happens before any execution.
    tool = registry.get(decision.tool_name) if decision.tool_name else None
    if tool is None:
        result = ToolResult(
            tool_name=tool_name,
            success=False,
            status=ToolStatus.UNSUPPORTED,
            error=(
                "tool not registered; "
                f"available tools: {', '.join(sorted(registry.names()))}"
            ),
        )
        outcome = "refused"
    else:
        try:
            result = await asyncio.wait_for(
                tool.execute(decision.tool_arguments or {}),
                timeout=policy.tool_timeout_seconds,
            )
            outcome = result.status.value
        except asyncio.TimeoutError:
            result = ToolResult(
                tool_name=tool_name,
                success=False,
                status=ToolStatus.FAILED,
                error=f"tool timed out after {policy.tool_timeout_seconds}s",
            )
            outcome = "failed"
        except Exception as exc:  # noqa: BLE001 - tools may raise arbitrary errors
            result = ToolResult(
                tool_name=tool_name,
                success=False,
                status=ToolStatus.FAILED,
                error=str(exc),
            )
            outcome = "failed"

    duration = time.perf_counter() - start
    audit = AuditEvent(
        step=state.step,
        tool_name=tool_name,
        arguments=decision.tool_arguments or {},
        outcome=outcome,
        duration_seconds=duration,
    )

    trajectory = list(state.trajectory) + [
        TrajectoryStep(step=state.step, node="tools", summary=f"tool={tool_name} outcome={outcome}")
    ]
    return {
        "messages": [{"role": "tool", "content": json.dumps(result.model_dump(), ensure_ascii=False)}],
        "tool_results": list(state.tool_results) + [result],
        "audit_events": list(state.audit_events) + [audit],
        "trajectory": trajectory,
    }


async def verify_node(state: AgentState, llm: Any) -> Dict[str, Any]:
    """Run an independent verification pass on a targeted finding."""
    decision = state.decision
    finding = next((f for f in state.findings if decision and f.id == decision.finding_id), None)
    if finding is None:
        return {"decision": Decision(action="finalize", reasoning="verification target not found")}

    user = (
        "Independently verify this finding and reply with JSON: "
        '{"verified": true|false, "note": "..."}\n\n'
        f"{json.dumps(finding.model_dump(), ensure_ascii=False)}"
    )
    content, _ = await llm.complete(system="You are an independent reviewer.", user=user)
    verified = None
    note = ""
    try:
        payload = json.loads(_extract_json(content))
        verified = bool(payload.get("verified"))
        note = payload.get("note", "")
    except Exception:
        note = "verification could not be parsed"

    updated = [
        f.model_copy(update={"verified": verified, "verification_note": note})
        if f.id == finding.id
        else f
        for f in state.findings
    ]
    trajectory = list(state.trajectory) + [
        TrajectoryStep(step=state.step, node="verify", summary=f"finding={finding.id} verified={verified}")
    ]
    return {
        "messages": [{"role": "assistant", "content": content}],
        "findings": updated,
        "decision": Decision(action="finalize", reasoning="verification complete"),
        "trajectory": trajectory,
    }


async def finalize_node(state: AgentState) -> Dict[str, Any]:
    """Assemble the final review text."""
    trajectory = list(state.trajectory) + [
        TrajectoryStep(step=state.step, node="finalize", summary="review assembled")
    ]
    return {"final_answer": _finalize_text_from_findings(state.findings), "trajectory": trajectory}


def _finalize_text_from_findings(findings: List[Finding]) -> str:
    if not findings:
        return "No findings."
    lines = []
    for f in findings:
        status = "unverified"
        if f.verified is True:
            status = "verified"
        elif f.verified is False:
            status = "refuted"
        lines.append(
            f"- [{f.risk_level.value}] {f.title} ({f.file_path}:{f.line_number}) [{status}]"
        )
    return "\n".join(lines)

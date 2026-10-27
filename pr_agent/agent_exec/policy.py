"""Execution policy for the ReviewPilot agent loop.

The harness reads these bounds from ``[agent_exec]`` settings (falling back to
conservative defaults) and enforces them in server code, independent of the
model's prompt. The model may *suggest* continuing, but the policy decides when
the loop must stop or when a tool call is refused.
"""

from dataclasses import dataclass

from pr_agent.config_loader import get_settings


@dataclass(frozen=True)
class AgentPolicy:
    max_steps: int = 6
    max_tool_calls: int = 4
    max_tokens: int = 20000
    tool_timeout_seconds: float = 10.0
    # High-risk findings below this confidence threshold are flagged for
    # verification by the decision layer (Jev).
    verify_confidence_threshold: float = 0.6
    # Loop detection: if the last N decision signatures repeat, force finalize.
    loop_detection_window: int = 3
    # Harness: refuse tools that are not in the whitelist.
    enforce_tool_whitelist: bool = True


def load_policy() -> AgentPolicy:
    """Build a policy from ``[agent_exec]`` settings with safe defaults."""
    settings = get_settings()
    agent_exec = settings.get("agent_exec", {})

    def _int(key: str, default: int) -> int:
        try:
            return int(agent_exec.get(key, default))
        except (TypeError, ValueError):
            return default

    def _float(key: str, default: float) -> float:
        try:
            return float(agent_exec.get(key, default))
        except (TypeError, ValueError):
            return default

    return AgentPolicy(
        max_steps=_int("max_steps", AgentPolicy.max_steps),
        max_tool_calls=_int("max_tool_calls", AgentPolicy.max_tool_calls),
        max_tokens=_int("max_tokens", AgentPolicy.max_tokens),
        tool_timeout_seconds=_float("tool_timeout_seconds", AgentPolicy.tool_timeout_seconds),
        verify_confidence_threshold=_float(
            "verify_confidence_threshold", AgentPolicy.verify_confidence_threshold
        ),
        loop_detection_window=_int("loop_detection_window", AgentPolicy.loop_detection_window),
        enforce_tool_whitelist=bool(
            agent_exec.get("enforce_tool_whitelist", AgentPolicy.enforce_tool_whitelist)
        ),
    )

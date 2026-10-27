"""Data models for the ReviewPilot agent execution loop.

All models are Pydantic v2 (``ConfigDict(extra="forbid")``) to stay consistent
with the rest of the repository. ``ToolResult`` is the single contract returned
by every tool so the harness can enforce budgets and audit uniformly, and the
model only ever observes structured results rather than raw provider objects.
"""

import time
from enum import Enum
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ToolStatus(str, Enum):
    """Execution status of a single tool call.

    ``unsupported`` is distinct from ``failed`` so that "did not run" is never
    reported as "ran and found nothing".
    """

    COMPLETED = "completed"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    success: bool
    status: ToolStatus = ToolStatus.COMPLETED
    data: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    title: str
    description: str = ""
    risk_level: RiskLevel = RiskLevel.MEDIUM
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: List[str] = Field(default_factory=list)
    # True once a separate verification pass has confirmed or refuted the finding.
    verified: Optional[bool] = None
    verification_note: Optional[str] = None


class Decision(BaseModel):
    """Decision layer output for one review step.

    ``action`` tells the loop whether to keep investigating (via tools), to
    verify a low-confidence high-risk finding, or to finalize.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["tools", "verify", "finalize"]
    # Tool requests when action == "tools".
    tool_name: Optional[str] = None
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    # Free-form reasoning kept in the trajectory for auditability.
    reasoning: str = ""
    # Finding id targeted when action == "verify".
    finding_id: Optional[str] = None
    # Jev: the model's confidence in this decision (0..1). The harness uses it
    # to decide whether an automated verification pass is warranted.
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class AuditEvent(BaseModel):
    """Harness-level audit record for one executed tool call.

    Recorded in server code (the harness), not by the model, so the audit log is
    trustworthy regardless of what the LLM claims.
    """

    model_config = ConfigDict(extra="forbid")

    step: int
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    outcome: Literal["completed", "unsupported", "failed", "refused"]
    duration_seconds: Optional[float] = None
    timestamp: float = Field(default_factory=time.time)


class TrajectoryStep(BaseModel):
    """One step of the structured, replayable execution trajectory."""

    model_config = ConfigDict(extra="forbid")

    step: int
    node: Literal["orient", "review", "tools", "verify", "finalize"]
    summary: str
    # Coarse token estimate of the model input for this step.
    tokens_estimate: int = 0
    timestamp: float = Field(default_factory=time.time)


class AgentState(BaseModel):
    """LangGraph state carried across nodes.

    ``messages`` uses ``add_messages`` semantics so each node appends to the
    shared conversation instead of overwriting it.
    """

    model_config = ConfigDict(extra="forbid")

    messages: List[Any] = Field(default_factory=list)
    findings: List[Finding] = Field(default_factory=list)
    tool_results: List[ToolResult] = Field(default_factory=list)
    # The most recent routing decision, consumed by conditional edges.
    decision: Optional[Decision] = None
    step: int = 0
    # Filled by the finalize node; None means the run did not complete normally.
    final_answer: Optional[str] = None
    # OODA Orient: structured summary of the current environment (changed files,
    # open risks, budget usage) produced before each Decide step.
    orientation: Optional[str] = None
    # Loop detection: signatures of past decisions ("action|tool|args").
    decision_history: List[str] = Field(default_factory=list)
    # Harness audit trail: one event per executed tool call.
    audit_events: List[AuditEvent] = Field(default_factory=list)
    # Structured, replayable trajectory of every node visit.
    trajectory: List[TrajectoryStep] = Field(default_factory=list)

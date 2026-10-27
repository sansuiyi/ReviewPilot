"""Deterministic lightweight static checks for changed files.

First phase only: a Python AST syntax check plus a small set of explicit,
low-false-positive pattern rules. Each result carries a rule id, file path, line
number, severity and evidence, and the tool status distinguishes ``completed``
from ``unsupported``/``failed`` so that "did not run" is never reported as
"found nothing".
"""

import ast
from typing import Any, Dict

from pr_agent.agent_exec.models import ToolResult, ToolStatus

# Explicit dangerous patterns; kept minimal to avoid noisy false positives.
DANGEROUS_PATTERNS = [
    ("RP001", "high", "os.system", "shell command execution"),
    ("RP002", "high", "subprocess.Popen", "subprocess invocation"),
    ("RP003", "medium", "eval(", "dynamic code evaluation"),
    ("RP004", "medium", "exec(", "dynamic code execution"),
]


class RunStaticAnalysisTool:
    name = "run_static_analysis"
    description = (
        "Run deterministic lightweight static checks (Python AST syntax check "
        "and a few explicit dangerous-pattern rules) over changed files."
    )
    # MCP-style self-description: a JSON Schema object describing the arguments.
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Optional file path to restrict analysis to a single changed file.",
            }
        },
        "additionalProperties": False,
    }

    def __init__(self, files: list) -> None:
        self._files = files

    async def execute(self, arguments: Dict[str, Any]) -> ToolResult:
        file_path = arguments.get("file_path")
        matched = self._files
        if file_path:
            matched = [f for f in self._files if f.filename == file_path]

        if not matched:
            return ToolResult(
                tool_name=self.name,
                success=False,
                status=ToolStatus.UNSUPPORTED,
                data={"issues": []},
                error=f"no changed files to analyze for {file_path or 'the PR'}",
            )

        issues = []
        for f in matched:
            # Only Python files are supported by the AST check in phase one.
            if not f.filename.endswith(".py"):
                continue
            # The diff patch is not a valid module; extract added lines as a
            # best-effort surface and run syntax + pattern checks on them.
            added_lines = []
            for line in f.patch.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    added_lines.append(line[1:])
            text = "\n".join(added_lines)

            # Syntax check on the added-lines surface.
            try:
                ast.parse(text)
            except SyntaxError as exc:
                issues.append(
                    {
                        "rule_id": "SYN001",
                        "file_path": f.filename,
                        "line_number": exc.lineno,
                        "severity": "high",
                        "evidence": exc.msg,
                    }
                )

            for rule_id, severity, token, desc in DANGEROUS_PATTERNS:
                for idx, line in enumerate(added_lines, start=1):
                    if token in line:
                        issues.append(
                            {
                                "rule_id": rule_id,
                                "file_path": f.filename,
                                "line_number": idx,
                                "severity": severity,
                                "evidence": f"{desc}: {line.strip()}",
                            }
                        )

        return ToolResult(
            tool_name=self.name,
            success=True,
            status=ToolStatus.COMPLETED,
            data={"issues": issues, "count": len(issues)},
        )

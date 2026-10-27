"""Local text-level reference search over the PR's loaded files.

First phase only: exact text search for identifiers, function or class names
across the files already loaded into the run. It deliberately does not claim
whole-repository coverage, and returns an explicit empty result when nothing is
found within the searched scope.
"""

from typing import Any, Dict

from pr_agent.agent_exec.models import ToolResult, ToolStatus


class FindReferencesTool:
    name = "find_references"
    description = (
        "Search for exact occurrences of a symbol in the PR's loaded file "
        "content and return file path, line number and the matching line."
    )
    # MCP-style self-description: a JSON Schema object describing the arguments.
    input_schema = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "The symbol (identifier/function/class name) to search for.",
            }
        },
        "required": ["symbol"],
        "additionalProperties": False,
    }

    def __init__(self, files: list) -> None:
        self._files = files

    async def execute(self, arguments: Dict[str, Any]) -> ToolResult:
        symbol = arguments.get("symbol")
        if not symbol:
            return ToolResult(
                tool_name=self.name,
                success=False,
                status=ToolStatus.UNSUPPORTED,
                data={"references": []},
                error="missing required argument 'symbol'",
            )

        matches = []
        for f in self._files:
            if not getattr(f, "patch", ""):
                continue
            for line_no, line in enumerate(f.patch.splitlines(), start=1):
                if symbol in line:
                    matches.append(
                        {
                            "filename": f.filename,
                            "line_number": line_no,
                            "line": line.strip(),
                        }
                    )

        return ToolResult(
            tool_name=self.name,
            success=True,
            status=ToolStatus.COMPLETED,
            data={"references": matches, "count": len(matches)},
        )

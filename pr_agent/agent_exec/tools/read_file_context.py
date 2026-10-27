"""Read the changed-file context of a PR diff.

Reuses the existing GitProvider ``get_diff_files`` result rather than inventing
a second data path. It returns the changed fragments with line numbers and, when
available, the surrounding file content; it never treats a deleted line as the
current version of the file.
"""

from typing import Any, Dict

from pr_agent.agent_exec.models import ToolResult, ToolStatus


class ReadFileContextTool:
    name = "read_file_context"
    description = (
        "Return the changed fragments (with line numbers) of files in the PR "
        "diff, optionally restricted to a single file path."
    )
    # MCP-style self-description: a JSON Schema object describing the arguments.
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Optional file path to restrict the returned diff fragments.",
            }
        },
        "additionalProperties": False,
    }

    def __init__(self, files: list) -> None:
        # ``files`` is the list of FilePatchInfo already loaded by the caller.
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
                data={"files": []},
                error=f"no diff data available for {file_path or 'the PR'}",
            )

        entries = []
        for f in matched:
            entries.append(
                {
                    "filename": f.filename,
                    "edit_type": getattr(f.edit_type, "name", None),
                    "num_plus_lines": f.num_plus_lines,
                    "num_minus_lines": f.num_minus_lines,
                    # Raw unified diff; the consumer must not mistake '-' lines
                    # for the current file content.
                    "patch": f.patch,
                    "content_fetch_failed": getattr(f, "content_fetch_failed", False),
                }
            )

        return ToolResult(
            tool_name=self.name,
            success=True,
            status=ToolStatus.COMPLETED,
            data={"files": entries},
        )

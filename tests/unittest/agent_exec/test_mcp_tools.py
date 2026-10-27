"""Tests for the MCP-standardized tool registration layer (pr_agent.agent_exec.tools).

Verifies that:
  - the built-in tools self-describe their arguments via ``input_schema``;
  - the registry exposes them in the MCP ``tools/list`` descriptor shape;
  - external MCP-style tools are adapted through ``MCPToolAdapter`` and run
    under the same ``ToolResult`` contract as the built-ins.
"""

import pytest

from pr_agent.agent_exec.models import ToolResult, ToolStatus
from pr_agent.agent_exec.tools import (
    FindReferencesTool,
    MCPToolAdapter,
    MCPToolDescriptor,
    ReadFileContextTool,
    RunStaticAnalysisTool,
    ToolRegistry,
    to_mcp_descriptor,
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


def test_builtin_tools_expose_input_schema():
    # Each built-in tool self-describes its arguments with a JSON Schema object.
    for tool in (ReadFileContextTool, FindReferencesTool, RunStaticAnalysisTool):
        instance = tool([_py_file("+x = 1\n")])
        schema = getattr(instance, "input_schema", None)
        assert isinstance(schema, dict)
        assert schema["type"] == "object"
        assert isinstance(schema.get("properties"), dict)
        assert schema.get("additionalProperties") is False


def test_registry_mcp_descriptors_match_builtin_tools():
    registry = ToolRegistry()
    registry.register(ReadFileContextTool([_py_file("+x = 1\n")]))
    registry.register(FindReferencesTool([_py_file("+x = 1\n")]))
    registry.register(RunStaticAnalysisTool([_py_file("+x = 1\n")]))

    descriptors = registry.mcp_descriptors()

    # The registry exposes every tool in the MCP tools/list shape.
    assert isinstance(descriptors, list)
    assert {d.name for d in descriptors} == {
        "read_file_context",
        "find_references",
        "run_static_analysis",
    }
    for d in descriptors:
        assert isinstance(d, MCPToolDescriptor)
        assert isinstance(d.name, str) and d.name
        assert isinstance(d.description, str) and d.description
        assert isinstance(d.inputSchema, dict)
        assert d.inputSchema["type"] == "object"
        assert isinstance(d.inputSchema.get("properties"), dict)


@pytest.mark.asyncio
async def test_mcp_adapter_runs_external_tool_under_toolresult_contract():
    # An external MCP-style tool is adapted to the Tool protocol and runs under
    # the same ToolResult contract as the built-ins.
    async def _external_handler(args):
        return ToolResult(
            tool_name="external_count",
            success=True,
            status=ToolStatus.COMPLETED,
            data={"count": len(args.get("items", []))},
        )

    external = MCPToolAdapter(
        name="external_count",
        description="Count items passed to the tool.",
        input_schema={
            "type": "object",
            "properties": {"items": {"type": "array"}},
            "required": ["items"],
            "additionalProperties": False,
        },
        handler=_external_handler,
    )

    registry = ToolRegistry()
    registry.register(external)

    # The adapter's descriptor matches the MCP tools/list shape.
    descriptor = to_mcp_descriptor(external)
    assert descriptor.name == "external_count"
    assert descriptor.inputSchema["type"] == "object"

    # The handler runs through the harness-facing Tool.execute contract.
    result = await registry.get("external_count").execute({"items": [1, 2, 3]})
    assert result.status is ToolStatus.COMPLETED
    assert result.data == {"count": 3}

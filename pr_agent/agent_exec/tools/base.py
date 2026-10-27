"""Tool protocol and registry for the ReviewPilot agent.

Every tool implements ``Tool`` and returns a ``ToolResult`` so the harness can
enforce a whitelist, timeouts and per-run budgets uniformly. Tools never execute
arbitrary model-generated code; they only expose a fixed, schema-described set
of read-only operations in the first phase.

Tools self-describe their arguments with a JSON Schema ``input_schema`` so the
registry can expose them in the MCP ``tools/list`` shape (name, description,
inputSchema). External MCP-style tools are ingested via ``MCPToolAdapter`` and
run under the same harness budgets, whitelist and audit as the built-in tools.
"""

from typing import Any, Awaitable, Callable, Dict, Protocol

from pydantic import BaseModel, ConfigDict

from pr_agent.agent_exec.models import ToolResult


class Tool(Protocol):
    name: str
    description: str
    input_schema: Dict[str, Any]

    async def execute(self, arguments: Dict[str, Any]) -> ToolResult:
        """Run the tool and return a structured result."""
        ...


class ToolRegistry:
    """A whitelist of tools available to the agent."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def mcp_descriptors(self) -> "list[MCPToolDescriptor]":
        """Return all tools in the MCP ``tools/list`` descriptor shape."""
        return [to_mcp_descriptor(t) for t in self._tools.values()]


def default_registry(*tools: Tool) -> ToolRegistry:
    """Build a registry pre-populated with the first-phase read-only tools."""
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


class MCPToolDescriptor(BaseModel):
    """MCP ``tools/list`` response item.

    ``inputSchema`` is a JSON Schema object describing the tool's arguments,
    matching the Model Context Protocol tool descriptor.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    inputSchema: Dict[str, Any]


def to_mcp_descriptor(tool: Tool) -> MCPToolDescriptor:
    """Project a registered tool into the MCP descriptor shape."""
    return MCPToolDescriptor(
        name=tool.name,
        description=tool.description,
        inputSchema=getattr(tool, "input_schema", {"type": "object"}),
    )


class MCPToolAdapter:
    """Adapt an external MCP-style tool to the ``Tool`` protocol.

    External tools provide (a) an MCP descriptor (name, description, inputSchema)
    and (b) an async ``handler(arguments) -> ToolResult``. The adapter exposes
    them as a ``Tool`` so the harness can register, whitelist and execute them
    under the same budgets and audit as the built-in tools.
    """

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: Callable[[Dict[str, Any]], Awaitable[ToolResult]],
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self._handler = handler

    async def execute(self, arguments: Dict[str, Any]) -> ToolResult:
        return await self._handler(arguments)

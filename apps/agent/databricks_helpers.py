"""Plumbing to reach Databricks from the OpenAI Agents SDK. You normally don't need to edit this file.

  ai_gateway_model(name)     the LLM, called through an AI Gateway service
  managed_mcp_server(path)   a Databricks managed MCP server (UC functions, Vector Search, Genie, external)
  label_mcp_trace_spans()    name the MCP tool-listing spans in MLflow traces (otherwise "Unknown")

Both authenticate as whoever runs the code: the app's service principal in Databricks Apps, your CLI
profile locally (DATABRICKS_CONFIG_PROFILE).
"""

from __future__ import annotations

import httpx2
import openai
from agents import OpenAIChatCompletionsModel
from agents.mcp import MCPServerStreamableHttp
from databricks.sdk import WorkspaceClient

workspace = WorkspaceClient()
HOST = workspace.config.host.rstrip("/")


class DatabricksAuth(httpx2.Auth):
    """Adds a fresh Databricks OAuth header to every HTTP request (the SDK caches and refreshes it)."""

    def auth_flow(self, request):
        request.headers.update(workspace.config.authenticate())
        yield request


def ai_gateway_model(service_name: str, timeout_s: float = 30) -> OpenAIChatCompletionsModel:
    """An OpenAI-compatible model served by an AI Gateway service (guardrails, rate limits, usage)."""
    client = openai.AsyncOpenAI(
        base_url=f"{HOST}/ai-gateway/mlflow/v1",
        api_key="unused",  # DatabricksAuth sets the Authorization header
        http_client=httpx2.AsyncClient(auth=DatabricksAuth(), timeout=timeout_s),
        max_retries=1,
    )
    return OpenAIChatCompletionsModel(model=service_name, openai_client=client)


class _ManagedMCPServer(MCPServerStreamableHttp):
    """Managed MCP tools are named `<catalog>__<schema>__<name>`; LLM APIs cap tool names at 64
    characters, so the agent sees `<name>` only (and optionally just the `only` subset)."""

    def __init__(self, *args, only: list[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._only = only
        self._full_names: dict[str, str] = {}

    async def list_tools(self, run_context=None, agent=None):
        tools = []
        for tool in await super().list_tools(run_context, agent):
            short = tool.name.rsplit("__", 1)[-1]
            if self._only is None or short in self._only:
                self._full_names[short] = tool.name
                tools.append(tool.model_copy(update={"name": short}))
        return tools

    async def call_tool(self, tool_name, arguments, meta=None):
        return await super().call_tool(self._full_names.get(tool_name, tool_name), arguments, meta)


def managed_mcp_server(path: str, only: list[str] | None = None, timeout_s: float = 30) -> MCPServerStreamableHttp:
    """A Databricks managed MCP server at {host}/api/2.0/mcp/<path>:

      functions/<catalog>/<schema>       every UC function in the schema
      vector-search/<catalog>/<schema>   every Vector Search index in the schema
      genie/<space_id>                   a Genie space
      external/<connection>              an external MCP server registered as a UC connection

    `only` restricts the tools the agent sees.
    """
    return _ManagedMCPServer(
        {"url": f"{HOST}/api/2.0/mcp/{path}", "auth": DatabricksAuth(), "timeout": timeout_s},
        name=path,
        only=only,
        cache_tools_list=True,
        client_session_timeout_seconds=timeout_s,
    )


def label_mcp_trace_spans() -> None:
    """MLflow's Agents SDK tracer names span types it doesn't know "Unknown", including the span where
    the agent lists an MCP server's tools. Name it `MCP list_tools · <server>` and show the tools found.
    Uses a private MLflow module: if that ever moves, this is a no-op and the spans read "Unknown" again."""
    try:
        from mlflow.openai import _agent_tracer as tracer
        get_name, parse = tracer._get_span_name, tracer._parse_span_data
    except (ImportError, AttributeError):
        return

    def span_name(data):
        return f"MCP list_tools · {data.server}" if data.type == "mcp_tools" else get_name(data)

    def span_io(data):  # (inputs, outputs, attributes)
        return ({"server": data.server}, {"tools": data.result}, {}) if data.type == "mcp_tools" else parse(data)

    tracer._get_span_name, tracer._parse_span_data = span_name, span_io

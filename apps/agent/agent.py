"""Sofia — ENERGY's customer-service voice agent, built with the OpenAI Agents SDK on Databricks.

An agent is three things: a MODEL, INSTRUCTIONS and TOOLS. This file puts them together and
serves the agent through mlflow.genai.agent_server (see start_server.py).

    ┌─ model ──────── an AI Gateway service (guardrails, rate limits, usage tracking)
    ├─ instructions ─ prompt.py
    └─ tools ──────── Python functions (tools.py) + Databricks managed MCP servers (below)

HOW TO ADD A TOOL — two options:

  1. A Python function. Write it in tools.py with @function_tool and add it to PYTHON_TOOLS.
     Use this for custom logic (here: the Lakebase reads and writes, on-call alerts, Genie).

  2. A Databricks managed MCP server. Add one line to mcp_servers(). Every UC function in a schema,
     every Vector Search index in a schema, a Genie space or an external MCP connection becomes a
     tool, with no code to write. Its description comes from the function's or index's COMMENT.

  Then (a) say in prompt.py when the model should use the tool, and (b) grant the app access to what
  the tool touches, in databricks.yml under the app's `resources` (e.g. EXECUTE on a UC function).

Every run is traced to MLflow by mlflow.openai.autolog(): the agent, each LLM call, each tool call.
"""

from __future__ import annotations

import json
import os
import uuid

import mlflow
import openai
from agents import Agent, ModelSettings, RunConfig, Runner
from agents.exceptions import MaxTurnsExceeded
from agents.mcp import MCPServerManager
from mlflow.genai.agent_server import invoke, stream
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentResponse, ResponsesAgentStreamEvent

import tools
from databricks_helpers import ai_gateway_model, label_mcp_trace_spans, managed_mcp_server
from prompt import SYSTEM_PROMPT

mlflow.openai.autolog()
label_mcp_trace_spans()
# The SDK's per-run "task" and per-turn "turn" spans only add nesting (MLflow shows turns as "Unknown"):
# without them, each LLM call and tool call sits directly under the agent span.
RUN_CONFIG = RunConfig(tracing={"include_task_and_turn_spans": False})

CATALOG = os.environ["ENERGY_CATALOG"]
SCHEMA = os.environ.get("ENERGY_SCHEMA", "energy_voicebot")
LLM_ENDPOINT = os.environ["ENERGY_LLM_ENDPOINT"]  # AI Gateway service, e.g. <catalog>.<schema>.<name>


# ---------------------------------------------------------------------------
# 1. Model
# ---------------------------------------------------------------------------

MODEL = ai_gateway_model(LLM_ENDPOINT)
MODEL_SETTINGS = ModelSettings(max_tokens=320)  # a spoken reply is 2-4 sentences
MAX_TURNS = 4                                    # LLM rounds per customer turn (tool calls happen in between)


# ---------------------------------------------------------------------------
# 2. Tools
# ---------------------------------------------------------------------------

PYTHON_TOOLS = [
    tools.get_customer_summary,     # Lakebase read
    tools.create_installment_plan,  # Lakebase write
    tools.submit_meter_reading,     # Lakebase write
    tools.get_account_activity,     # Lakebase read
    tools.notify_oncall,            # Lakebase write, shown in the supervisor panel
    tools.ask_genie,                # Genie space
    # tools.web_search,             # example: external MCP via a UC connection (see tools.py)
]


def mcp_servers():
    """Databricks managed MCP servers, opened for each request."""
    return [
        # UC functions in the schema → get_consumption_history
        managed_mcp_server(f"functions/{CATALOG}/{SCHEMA}", only=["get_consumption_history"]),
        # Vector Search indexes in the schema → knowledgebase_index
        managed_mcp_server(f"vector-search/{CATALOG}/{SCHEMA}"),
    ]


# ---------------------------------------------------------------------------
# 3. Agent
# ---------------------------------------------------------------------------

def build_agent(connected_mcp_servers) -> Agent:
    return Agent(
        name="Sofia",
        instructions=SYSTEM_PROMPT,
        model=MODEL,
        model_settings=MODEL_SETTINGS,
        tools=PYTHON_TOOLS,
        mcp_servers=connected_mcp_servers,
    )


# ---------------------------------------------------------------------------
# 4. Serving: run the agent and stream Responses-API events
#    (text deltas, plus one function_call and one function_call_output item per tool call)
# ---------------------------------------------------------------------------

GUARDRAIL_REPLY = ("I can't help with that request. I can assist with billing, meter readings, and account "
                   "questions — or connect you with a member of our customer-service team for anything else.")
TECHNICAL_ISSUE_REPLY = ("I'm having a brief technical issue on my end. Let me connect you with a member of our "
                         "customer-service team who can help you right away.")
HANDOFF_REPLY = "Let me connect you to a human customer-service agent who can help from here."


def _message(text: str, item_id: str | None = None) -> ResponsesAgentStreamEvent:
    return ResponsesAgentStreamEvent(type="response.output_item.done", item={
        "type": "message", "role": "assistant", "id": item_id or f"msg-{uuid.uuid4().hex[:12]}",
        "content": [{"type": "output_text", "text": text}]})


def _as_text(output) -> str:
    """A tool result as text (MCP tools return {'type': 'text', 'text': ...})."""
    if isinstance(output, str):
        return output
    if isinstance(output, dict) and output.get("type") == "text":
        return output["text"]
    return json.dumps(output, default=str)


@stream()
async def streaming(request: ResponsesAgentRequest):
    history = [{k: v for k, v in item.model_dump().items() if v is not None} for item in request.input]
    item_id = f"msg-{uuid.uuid4().hex[:12]}"
    text = ""
    try:
        async with MCPServerManager(mcp_servers(), connect_in_parallel=True) as mcp:
            result = Runner.run_streamed(build_agent(mcp.active_servers), input=history, max_turns=MAX_TURNS,
                                         run_config=RUN_CONFIG)
            async for event in result.stream_events():
                if event.type == "raw_response_event" and event.data.type == "response.output_text.delta":
                    text += event.data.delta
                    yield ResponsesAgentStreamEvent(type="response.output_text.delta", item_id=item_id,
                                                    output_index=0, content_index=0, delta=event.data.delta)
                elif event.type == "run_item_stream_event" and event.name == "tool_called":
                    call = event.item.raw_item
                    yield ResponsesAgentStreamEvent(type="response.output_item.done", item={
                        "type": "function_call", "id": f"fc-{uuid.uuid4().hex[:12]}",
                        "call_id": call.call_id, "name": call.name, "arguments": call.arguments})
                elif event.type == "run_item_stream_event" and event.name == "tool_output":
                    yield ResponsesAgentStreamEvent(type="response.output_item.done", item={
                        "type": "function_call_output", "call_id": event.item.raw_item["call_id"],
                        "output": _as_text(event.item.output)})
        yield _message(text, item_id)

    except MaxTurnsExceeded:
        yield _message(f"{text} {HANDOFF_REPLY}".strip(), item_id)
    except (openai.BadRequestError, openai.PermissionDeniedError) as e:
        # Guardrails live in the AI Gateway: when it blocks a request, we only narrate a refusal and tag the trace.
        mlflow.update_current_trace(tags={"guardrail_block": str(e)[:200]})
        yield _message(GUARDRAIL_REPLY)
    except Exception as e:  # noqa: BLE001 — rate limit, timeout, network: hand off to a human
        mlflow.update_current_trace(tags={"agent_error": f"{type(e).__name__}: {str(e)[:200]}"})
        yield _message(TECHNICAL_ISSUE_REPLY)


@invoke()
async def non_streaming(request: ResponsesAgentRequest) -> ResponsesAgentResponse:
    items = [event.item async for event in streaming(request) if event.type == "response.output_item.done"]
    return ResponsesAgentResponse(output=items)

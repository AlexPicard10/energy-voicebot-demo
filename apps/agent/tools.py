"""Sofia's Python tools.

HOW TO WRITE A TOOL
  Decorate a function with @function_tool. The SDK builds the tool schema from it:
    • the function name        → the tool name
    • the type hints           → the argument types (Literal[...] becomes an enum)
    • the docstring            → the description the LLM reads, including each `Args:` line
  Return anything JSON-serializable. If the function raises, the error is sent back to the LLM
  instead of crashing the turn. Plain `def` functions run in a worker thread; use `async def` for
  async I/O. Then add the function to PYTHON_TOOLS in agent.py.

The tools here use:
  • Lakebase (serverless Postgres) — governed OLTP reads and writes, as the app's own Postgres role
    (tickets and on-call alerts)
  • Genie — natural-language questions over the customer tables
  • (commented example) web search through an external MCP server registered as a UC connection
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from typing import Literal

import psycopg
from agents import function_tool
from psycopg_pool import ConnectionPool

from databricks_helpers import workspace

PG_SCHEMA = os.environ.get("ENERGY_SCHEMA", "energy_voicebot")
LAKEBASE_ENDPOINT = os.environ.get("ENERGY_LAKEBASE_ENDPOINT", "")
GENIE_SPACE_ID = os.environ.get("ENERGY_GENIE_SPACE_ID", "")


# ---------------------------------------------------------------------------
# Lakebase
# ---------------------------------------------------------------------------

class _OAuthConnection(psycopg.Connection):
    """Each new Lakebase connection uses a fresh OAuth token (valid 60 min) as its password."""

    @classmethod
    def connect(cls, conninfo="", **kwargs):
        kwargs["password"] = workspace.postgres.generate_database_credential(endpoint=LAKEBASE_ENDPOINT).token
        return super().connect(conninfo, **kwargs)


# PGHOST / PGUSER / PGDATABASE are injected by the app's `postgres` resource (databricks.yml).
_pool = ConnectionPool(
    kwargs={"host": os.environ.get("PGHOST", ""), "port": os.environ.get("PGPORT", "5432"),
            "dbname": os.environ.get("PGDATABASE", "databricks_postgres"),
            "user": os.environ.get("PGUSER") or workspace.current_user.me().user_name,
            "sslmode": "require"},
    connection_class=_OAuthConnection, min_size=1, max_size=4, max_lifetime=2700, open=False,
)


def _sql(query: str, params: tuple = ()) -> list[dict]:
    """Run parameterized SQL on Lakebase and return the rows as dicts."""
    _pool.open()  # no-op once open
    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        if cur.description is None:
            return []
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _new_ticket(action_type: str, customer_id: str, payload: dict, status: str) -> str:
    ticket_id = "TKT-" + uuid.uuid4().hex[:8].upper()
    _sql(f"INSERT INTO {PG_SCHEMA}.tickets (ticket_id, action_type, customer_id, payload_json, status) "
         "VALUES (%s, %s, %s, %s::jsonb, %s)",
         (ticket_id, action_type, customer_id, json.dumps(payload), status))
    return ticket_id


@function_tool
def get_customer_summary(customer_id: str) -> dict:
    """Look up a customer's profile and their open (overdue or pending) invoices.
    Call whenever a customer_id is provided.

    Args:
        customer_id: The caller's customer id, e.g. CUST-000142.
    """
    # customer_sync / invoice_sync are Lakebase synced tables: Delta copies kept in Postgres.
    rows = _sql(
        "SELECT c.customer_id, c.first_name, c.last_name, c.region, c.city, c.segment, c.sdd_enabled, "
        "c.overdue_amount_eur, c.overdue_invoice_id, c.has_active_installment_plan, "
        "coalesce((SELECT json_agg(json_build_object('invoice_id', i.invoice_id, 'amount_eur', i.amount_eur, "
        "  'due_date', i.due_date, 'status', i.status, 'installment_plan_id', i.installment_plan_id)) "
        f" FROM {PG_SCHEMA}.invoice_sync i "
        "  WHERE i.customer_id = c.customer_id AND i.status IN ('overdue', 'pending')), '[]'::json) AS open_invoices "
        f"FROM {PG_SCHEMA}.customer_sync c WHERE c.customer_id = %s",
        (customer_id,),
    )
    return rows[0] if rows else {"error": f"no customer {customer_id}"}


@function_tool
def create_installment_plan(customer_id: str, invoice_id: str, months: Literal[3, 6, 12]) -> dict:
    """Open an installment plan on the caller's overdue/pending invoice and return the new ticket_id.
    Call ONLY after the customer has chosen the term and confirmed.

    Args:
        customer_id: The caller's customer id.
        invoice_id: The invoice to split (from get_customer_summary).
        months: Number of monthly installments chosen by the customer: 3, 6, or 12.
    """
    admin_fee_pct = {3: 0.0, 6: 0.015, 12: 0.03}[months]
    ticket_id = _new_ticket("installment_plan", customer_id,
                            {"invoice_id": invoice_id, "months": months, "admin_fee_pct": admin_fee_pct}, "created")
    return {"ticket_id": ticket_id, "months": months, "admin_fee_pct": admin_fee_pct}


@function_tool
def submit_meter_reading(customer_id: str, reading_value: float, unit: Literal["kWh", "SMC"],
                         supply_point_id: str = "UNKNOWN") -> dict:
    """Record a customer self-reported meter reading and return the new ticket_id.
    Call ONLY after the customer confirms the value.

    Args:
        customer_id: The caller's customer id.
        reading_value: The reading as a plain number (strip thousands separators: '48,620' -> 48620).
        unit: 'kWh' for electricity, 'SMC' for gas.
        supply_point_id: Supply point id if known; 'UNKNOWN' otherwise — do not stall to ask.
    """
    ticket_id = _new_ticket("meter_reading", customer_id,
                            {"supply_point_id": supply_point_id, "reading_value": reading_value, "unit": unit},
                            "recorded")
    return {"ticket_id": ticket_id}


@function_tool
def get_account_activity(customer_id: str, limit: int = 5) -> list[dict]:
    """The caller's most recent tickets (installment plans, meter readings), newest first.
    Use it to confirm a write or answer 'what have I requested recently?'. Caller's own id only.

    Args:
        customer_id: The caller's customer id.
        limit: Max tickets to return.
    """
    return _sql(f"SELECT ticket_id, ts::text AS ts, action_type, status, payload_json FROM {PG_SCHEMA}.tickets "
                "WHERE customer_id = %s ORDER BY ts DESC LIMIT %s", (customer_id, min(limit, 20)))


@function_tool
def notify_oncall(session_id: str, customer_id: str | None,
                  intent: Literal["installment_plan", "meter_reading", "gas_disconnection", "supplier_switch",
                                  "direct_debit", "consumption", "out_of_scope"],
                  sentiment: float, transcript_excerpt: str) -> dict:
    """Alert the on-call supervisor team. Call IN ADDITION to your normal reply (never instead of it)
    whenever the customer shows distress, anger, a safety or wellbeing issue, a service cut-off, or
    repeated contact. The alert appears live in the supervisor panel.

    Args:
        session_id: A short identifier for this call, e.g. SPOT-LIVE-1234.
        customer_id: The caller's customer id if known, else null.
        intent: What the call is about.
        sentiment: Sentiment in [-1.0, 1.0]; use <= -0.6 when the customer is angry.
        transcript_excerpt: One short verbatim sentence from the customer.
    """
    alert_id = "ALT-" + uuid.uuid4().hex[:8].upper()
    _sql(f"INSERT INTO {PG_SCHEMA}.supervisor_alerts "
         "(alert_id, session_id, customer_id, intent, sentiment, excerpt) VALUES (%s, %s, %s, %s, %s, %s)",
         (alert_id, session_id, customer_id, intent, sentiment, transcript_excerpt))
    return {"alert_id": alert_id, "status": "alert recorded"}


# ---------------------------------------------------------------------------
# Genie
# ---------------------------------------------------------------------------

@function_tool
def ask_genie(question: str) -> dict:
    """Ask the Genie space (natural language → SQL over the customer and invoice tables) a question
    about the caller's own account, e.g. totals or breakdowns across their invoices. The question
    must name the caller's customer_id. Takes ~15 s.

    Args:
        question: A natural-language question that names the caller's customer_id.
    """
    message = workspace.genie.start_conversation_and_wait(GENIE_SPACE_ID, question,
                                                         timeout=dt.timedelta(seconds=60))
    result = {"answer": " ".join(a.text.content for a in message.attachments or [] if a.text and a.text.content)}
    for a in message.attachments or []:
        if a.query:  # the SQL Genie wrote, and its rows
            data = workspace.genie.get_message_attachment_query_result(
                GENIE_SPACE_ID, message.conversation_id, message.message_id, a.attachment_id).statement_response
            result["sql"] = a.query.query
            result["columns"] = [c.name for c in data.manifest.schema.columns]
            result["rows"] = (data.result.data_array or [])[:20]
    return result


# ---------------------------------------------------------------------------
# EXAMPLE (disabled): web search through an external MCP server
#
# An external MCP server (here You.com) registered in AI Gateway → MCP servers becomes a Unity
# Catalog connection that the agent reaches through Databricks' managed MCP proxy. To enable it:
#   1. register the MCP server in AI Gateway (this creates a UC connection, e.g. mcp-you-web-api);
#   2. uncomment the code below and add `tools.web_search` to PYTHON_TOOLS in agent.py;
#   3. uncomment `web_search_connection` (variable, env and resource) in databricks.yml;
#   4. add this paragraph to the TOOLS section of prompt.py:
#      - web_search: GENERAL public-knowledge energy questions that are NOT about the caller's account
#        and that the knowledge base doesn't answer (market or regulatory facts, definitions, current
#        external information). Never use it for the caller's account or ENERGY policy. Ground your
#        reply in the returned snippets.
# ---------------------------------------------------------------------------

# from databricks_helpers import managed_mcp_server
#
# WEB_SEARCH_CONNECTION = os.environ.get("ENERGY_WEB_SEARCH_CONNECTION", "")
#
#
# @function_tool
# async def web_search(query: str, count: int = 3) -> list[dict]:
#     """Search the public web for GENERAL, current energy facts that the ENERGY knowledge base and
#     account tools do not cover (market or regulatory facts, definitions). Never for the caller's
#     account or ENERGY policy.
#
#     Args:
#         query: A concise keyword query (3-6 words), no search operators.
#         count: Max results (keep small for a voice reply).
#     """
#     # Called through a wrapper (rather than giving the agent the MCP server directly) because
#     # you-search returns ~40 KB of JSON; three short snippets are enough for a spoken answer.
#     async with managed_mcp_server(f"external/{WEB_SEARCH_CONNECTION}") as server:
#         response = await server.call_tool("you-search", {"query": query, "count": count})
#     results = json.loads(response.content[0].text).get("results", {})
#     hits = [*results.get("web", []), *results.get("news", [])][:count]
#     return [{"title": h.get("title"), "url": h.get("url"),
#              "snippet": (h.get("snippets") or [h.get("description", "")])[0]} for h in hits]

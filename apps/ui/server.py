"""ENERGY voicebot — voice demo UI (Databricks App).

Serves the single-page voice UI (static/index.html) and:
  • POST /stream   relays the conversation to the agent app's /invocations (stream: true) and pipes
                   its Server-Sent Events back to the browser.
  • POST /suggest  asks a Foundation Model for the customer's likely next replies (suggestion chips).
  • GET  /alerts, PATCH /alerts/{id}/resolve   the supervisor panel: the alerts the agent's
                   notify_oncall tool writes to Lakebase.

The browser can't call the agent app directly (it sits behind the Apps OAuth proxy), so this server
calls it on behalf of the signed-in user (X-Forwarded-Access-Token), falling back to its own
service principal.

Local dev:  AGENT_URL=http://localhost:8000 uvicorn server:app --port 8001
"""

import json
import os
import time
from contextlib import asynccontextmanager, contextmanager

import httpx
import psycopg
from databricks.sdk import WorkspaceClient
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
SUGGEST_MODEL = os.environ.get("SUGGEST_MODEL", "databricks-gpt-5-4-mini")
PG_SCHEMA = os.environ.get("ENERGY_SCHEMA", "energy_voicebot")
LAKEBASE_ENDPOINT = os.environ.get("ENERGY_LAKEBASE_ENDPOINT", "")
LAKEBASE_ENABLED = bool(os.environ.get("PGHOST"))

_ws = WorkspaceClient()
_host = _ws.config.host.rstrip("/")
AGENT_URL = (os.environ.get("AGENT_URL") or _ws.apps.get(os.environ["AGENT_APP_NAME"]).url).rstrip("/")

_http: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _http
    _http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))  # shared keep-alive pool
    yield
    await _http.aclose()


app = FastAPI(title="energy-voicebot-ui", lifespan=lifespan)


def _sp_authorization() -> str:
    return _ws.config.authenticate()["Authorization"]


# ---------------------------------------------------------------------------
# Lakebase (supervisor alerts)
# ---------------------------------------------------------------------------

_pg_token = ("", 0.0)  # (token, expiry)


@contextmanager
def _pg():
    """A short-lived Lakebase connection as this app's service-principal role (OAuth token as password)."""
    global _pg_token
    token, expiry = _pg_token
    if time.monotonic() > expiry:
        token = _ws.postgres.generate_database_credential(endpoint=LAKEBASE_ENDPOINT).token
        _pg_token = (token, time.monotonic() + 50 * 60)  # tokens are valid for 60 min
    conn = psycopg.connect(
        host=os.environ["PGHOST"], port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "databricks_postgres"),
        user=os.environ.get("PGUSER") or _ws.current_user.me().user_name,
        password=token, sslmode="require", autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class Conversation(BaseModel):
    # Role messages plus replayed function_call / function_call_output items (the agent's `input`).
    messages: list[dict]


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/health")
def health():
    return {"status": "ok", "agent_url": AGENT_URL}


def _sse_error(message: str) -> bytes:
    return f"data: {json.dumps({'type': 'error', 'message': message})}\n\n".encode()


@app.post("/stream")
async def stream(conv: Conversation, request: Request):
    user_token = request.headers.get("x-forwarded-access-token")
    authorization = f"Bearer {user_token}" if user_token else _sp_authorization()
    body = {"input": conv.messages[-50:], "stream": True}

    async def relay():
        try:
            async with _http.stream("POST", f"{AGENT_URL}/invocations", json=body,
                                    headers={"Authorization": authorization}) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode(errors="replace")[:300]
                    yield _sse_error(f"agent returned {r.status_code}: {detail}")
                    return
                async for chunk in r.aiter_bytes():
                    yield chunk
        except Exception as e:  # noqa: BLE001
            yield _sse_error(f"proxy error: {type(e).__name__}: {e}")

    if not conv.messages:
        return StreamingResponse(iter([_sse_error("empty request: no messages")]), media_type="text/event-stream")
    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


SUGGEST_SYSTEM = (
    "You are the CUSTOMER in a real phone call with ENERGY's AI agent Sofia. "
    "Propose exactly 3 short follow-ups the customer would naturally say next — "
    "be specific: reference what was just offered or asked (e.g. pick a plan term, confirm a value, ask about a detail). "
    "First person. Max 8 words each. Reply with ONLY a JSON array of 3 strings, nothing else."
)


def _parse_suggestions(content: str) -> list[str]:
    s = (content or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        arr = json.loads(s)
    except json.JSONDecodeError:
        return []
    return [str(x).strip() for x in arr if str(x).strip()][:3] if isinstance(arr, list) else []


@app.post("/suggest")
async def suggest(conv: Conversation):
    """Up to 3 likely next customer replies. Best effort: any failure returns an empty list."""
    turns = [m for m in conv.messages if m.get("role") in ("user", "assistant") and m.get("content")][-8:]
    if not turns:
        return {"suggestions": []}
    transcript = "\n".join(f"{'Customer' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in turns)
    payload = {
        "messages": [
            {"role": "system", "content": SUGGEST_SYSTEM},
            {"role": "user", "content": f"Conversation so far:\n{transcript}\n\nThe CUSTOMER speaks next."},
        ],
        "max_tokens": 120,
        "temperature": 0.7,
    }
    try:
        # Model serving needs the app service principal: the forwarded user token lacks that scope.
        r = await _http.post(f"{_host}/serving-endpoints/{SUGGEST_MODEL}/invocations", json=payload,
                             headers={"Authorization": _sp_authorization()}, timeout=30.0)
        r.raise_for_status()
        content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        return {"suggestions": _parse_suggestions(content)}
    except Exception:  # noqa: BLE001
        return {"suggestions": []}


@app.get("/alerts")
def get_alerts():
    """Latest supervisor alerts, newest first."""
    if not LAKEBASE_ENABLED:
        return {"alerts": []}
    try:
        with _pg() as cur:
            cur.execute(
                "SELECT alert_id, ts, session_id, customer_id, intent, sentiment, excerpt, status "
                f"FROM {PG_SCHEMA}.supervisor_alerts ORDER BY ts DESC LIMIT 20")
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        return {"alerts": [], "error": str(e)}
    for r in rows:
        r["ts"] = r["ts"].isoformat() if r.get("ts") else None
    return {"alerts": rows}


@app.patch("/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: str):
    if not LAKEBASE_ENABLED:
        return {"status": "ok"}
    try:
        with _pg() as cur:
            cur.execute(f"UPDATE {PG_SCHEMA}.supervisor_alerts SET status = 'resolved' WHERE alert_id = %s",
                        (alert_id,))
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": str(e)}
    return {"status": "ok"}
